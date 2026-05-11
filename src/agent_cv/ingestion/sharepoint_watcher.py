"""OneDrive/SharePoint document library watcher.

Polls the bot's own OneDrive / SharePoint document library for new or modified
files using the Microsoft Graph delta API and ingests them automatically into
the Agent CV database.

Two complementary mechanisms are used:
  1. Graph change notifications (push) — a subscription on the drive/folder
     delivers a notification within seconds of a file being uploaded or changed.
     The notification handler calls _tick() immediately so ingestion is near-
     instant.  Subscriptions expire after 72 hours (drives, delegated auth)
     and are renewed automatically every 70 hours.
  2. Periodic delta poll (fallback) — runs every SHAREPOINT_POLL_INTERVAL
     seconds (default 3600) to catch any files that slipped through missed
     notifications (e.g. while the app was offline).

The delta link (a resumption token) is persisted in the ``sync_state`` table
so restarts continue from where the previous run left off.

Configuration (all in .env / Settings):
    SHAREPOINT_DRIVE_ID       — Drive ID of the SharePoint library.
    SHAREPOINT_FOLDER_PATH    — Subfolder path within the drive root (optional).
    SHAREPOINT_FOLDER_ITEM_ID — Specific folder item ID (optional, faster).
    SHAREPOINT_POLL_INTERVAL  — Seconds between fallback delta polls (default 3600).
    WEBHOOK_BASE_URL          — Public base URL; notifications go to
                                {WEBHOOK_BASE_URL}/graph-notifications/sharepoint
    WEBHOOK_SECRET            — Shared secret for clientState validation.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

import httpx

from agent_cv.config import settings
from agent_cv.db.connection import get_connection
from agent_cv.ingestion.ingest_service import ingest_sharepoint_file
from agent_cv.services.graph_service import get_access_token

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_SYNC_KEY = "sharepoint_delta_link"
_SUPPORTED = {".pdf", ".txt", ".docx"}

# Graph drive subscriptions (delegated auth) can be up to 4320 minutes = 72 hours.
_SUBSCRIPTION_LIFETIME_S: int = 72 * 3600
_SUBSCRIPTION_RENEW_BEFORE_S: int = 2 * 3600


def _encode_sharing_url(url: str) -> str:
    """Encode a sharing URL to the base64url token expected by the Graph Shares API."""
    parsed = urlparse(url)
    if parsed.query:
        from urllib.parse import parse_qs, urlencode
        params = parse_qs(parsed.query, keep_blank_values=True)
        params.pop("email", None)
        clean_query = urlencode({k: v[0] for k, v in params.items()})
        parsed = parsed._replace(query=clean_query)
    clean_url = urlunparse(parsed._replace(fragment=""))
    encoded = base64.urlsafe_b64encode(clean_url.encode()).rstrip(b"=").decode()
    return f"u!{encoded}"


def sharepoint_configured() -> bool:
    """Return True when the minimum OneDrive/SharePoint configuration is present."""
    return bool(
        settings.sharepoint_folder_path
        or settings.sharepoint_url
        or settings.sharepoint_drive_id
    )


class SharePointWatcher:
    """Async background task that watches a SharePoint document library for new
    files and ingests them via the existing pipeline.

    Uses Graph change notifications for near-instant detection, with a periodic
    delta poll as a fallback.  Lifecycle is managed by the FastAPI lifespan.
    """

    def __init__(self, poll_interval: int = 3600) -> None:
        self._poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._subscription_task: asyncio.Task | None = None
        self._subscription_id: str | None = None
        # Set by notify() when Graph pushes a change notification
        self._notify_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._poll_loop(), name="sharepoint-watcher"
            )
        if self._subscription_task is None or self._subscription_task.done():
            self._subscription_task = asyncio.create_task(
                self._subscription_lifecycle(), name="sharepoint-subscription"
            )
        folder = settings.sharepoint_folder_path or "/"
        logger.info(
            "SharePoint watcher started (interval=%ds, folder=%r)",
            self._poll_interval,
            folder,
        )

    async def stop(self) -> None:
        for task in (self._task, self._subscription_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self._delete_subscription()
        logger.info("SharePoint watcher stopped")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def notify(self) -> None:
        """Signal the watcher that a Graph change notification has arrived."""
        self._notify_event.set()

    # ------------------------------------------------------------------ #
    # Polling loop                                                         #
    # ------------------------------------------------------------------ #

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("SharePoint watcher: unhandled error in tick")
            # Wake on push notification OR fall back to the poll interval
            try:
                await asyncio.wait_for(
                    self._notify_event.wait(),
                    timeout=self._poll_interval,
                )
                logger.debug("SharePoint watcher: woken by Graph change notification")
            except asyncio.TimeoutError:
                pass
            finally:
                self._notify_event.clear()

    # ------------------------------------------------------------------ #
    # Graph change notification subscription                              #
    # ------------------------------------------------------------------ #

    async def _subscription_lifecycle(self) -> None:
        """Create the subscription after server is ready, then renew periodically."""
        await asyncio.sleep(5)  # wait for uvicorn to start serving
        await self._cleanup_stale_subscriptions()

        renew_interval = _SUBSCRIPTION_LIFETIME_S - _SUBSCRIPTION_RENEW_BEFORE_S
        for attempt in range(1, 6):
            try:
                await self._create_or_renew_subscription()
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt == 5:
                    logger.exception(
                        "SharePoint watcher: failed to create subscription after %d attempts — "
                        "falling back to poll-only mode", attempt,
                    )
                    return
                wait = attempt * 10
                logger.warning(
                    "SharePoint watcher: subscription attempt %d failed, retrying in %ds",
                    attempt, wait,
                )
                await asyncio.sleep(wait)

        while True:
            await asyncio.sleep(renew_interval)
            for attempt in range(1, 6):
                try:
                    await self._create_or_renew_subscription()
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if attempt == 5:
                        logger.exception(
                            "SharePoint watcher: subscription renewal failed after %d attempts", attempt,
                        )
                    else:
                        wait = attempt * 15
                        logger.warning(
                            "SharePoint watcher: renewal attempt %d failed, retrying in %ds",
                            attempt, wait,
                        )
                        await asyncio.sleep(wait)

    def _notification_url(self) -> str:
        base = (settings.webhook_base_url or "").rstrip("/")
        return f"{base}/graph-notifications/sharepoint"

    def _subscription_resource(self) -> str:
        drive_id = settings.sharepoint_drive_id.strip()
        drive_prefix = f"drives/{drive_id}" if drive_id else "me/drive"
        folder_item_id = settings.sharepoint_folder_item_id.strip()
        if folder_item_id and drive_id:
            return f"/drives/{drive_id}/items/{folder_item_id}"
        return f"/{drive_prefix}/root"

    async def _create_or_renew_subscription(self) -> None:
        if not settings.webhook_base_url:
            logger.debug(
                "SharePoint watcher: WEBHOOK_BASE_URL not set — skipping subscription creation"
            )
            return

        expiry = datetime.now(timezone.utc) + timedelta(seconds=_SUBSCRIPTION_LIFETIME_S)
        expiry_str = expiry.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        token = await asyncio.to_thread(get_access_token)

        async with httpx.AsyncClient(timeout=15.0) as http:
            if self._subscription_id:
                resp = await http.patch(
                    f"https://graph.microsoft.com/v1.0/subscriptions/{self._subscription_id}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={"expirationDateTime": expiry_str},
                )
                if resp.status_code == 200:
                    logger.info(
                        "SharePoint watcher: subscription %s renewed until %s",
                        self._subscription_id, expiry_str,
                    )
                    return
                logger.warning(
                    "SharePoint watcher: subscription renew returned HTTP %s — will recreate",
                    resp.status_code,
                )
                self._subscription_id = None

            resp = await http.post(
                "https://graph.microsoft.com/v1.0/subscriptions",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "changeType": "updated",
                    "notificationUrl": self._notification_url(),
                    "resource": self._subscription_resource(),
                    "expirationDateTime": expiry_str,
                    "clientState": settings.webhook_secret,
                },
            )

        if resp.status_code == 201:
            self._subscription_id = resp.json()["id"]
            logger.info(
                "SharePoint watcher: created subscription %s (resource=%s, expires %s)",
                self._subscription_id, self._subscription_resource(), expiry_str,
            )
        else:
            raise RuntimeError(
                f"SharePoint watcher: failed to create subscription — "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )

    async def _delete_subscription(self) -> None:
        if not self._subscription_id:
            return
        try:
            token = await asyncio.to_thread(get_access_token)
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.delete(
                    f"https://graph.microsoft.com/v1.0/subscriptions/{self._subscription_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code in (200, 204):
                logger.info(
                    "SharePoint watcher: deleted subscription %s", self._subscription_id
                )
            self._subscription_id = None
        except Exception:
            logger.exception("SharePoint watcher: error deleting subscription on shutdown")

    async def _cleanup_stale_subscriptions(self) -> None:
        """Delete any existing subscriptions pointing at our notification URL."""
        if not settings.webhook_base_url:
            return
        try:
            token = await asyncio.to_thread(get_access_token)
            async with httpx.AsyncClient(timeout=15.0) as http:
                resp = await http.get(
                    "https://graph.microsoft.com/v1.0/subscriptions",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code != 200:
                    return
                our_url = self._notification_url().rstrip("/")
                stale = [
                    s["id"]
                    for s in resp.json().get("value", [])
                    if s.get("notificationUrl", "").rstrip("/") == our_url
                ]
                for sub_id in stale:
                    del_resp = await http.delete(
                        f"https://graph.microsoft.com/v1.0/subscriptions/{sub_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    if del_resp.status_code in (200, 204):
                        logger.info(
                            "SharePoint watcher: deleted stale subscription %s", sub_id
                        )
        except Exception:
            logger.exception("SharePoint watcher: error during stale subscription cleanup")

    async def _tick(self) -> None:
        token = await asyncio.to_thread(get_access_token)
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
        select = "$select=id,name,file,webUrl,lastModifiedDateTime,deleted"

        stored_delta = await asyncio.to_thread(self._load_delta_link)
        if stored_delta:
            start_url = stored_delta
        else:
            drive_id = settings.sharepoint_drive_id.strip()
            drive_prefix = (
                f"https://graph.microsoft.com/v1.0/drives/{drive_id}"
                if drive_id
                else "https://graph.microsoft.com/v1.0/me/drive"
            )
            folder_item_id = settings.sharepoint_folder_item_id.strip()
            folder_path = settings.sharepoint_folder_path.strip("/")
            if folder_item_id:
                start_url = f"{drive_prefix}/items/{folder_item_id}/delta?{select}"
            elif folder_path:
                folder_id = await self._resolve_folder_id(headers, folder_path, drive_prefix)
                if not folder_id:
                    return
                start_url = f"{drive_prefix}/items/{folder_id}/delta?{select}"
            else:
                start_url = f"{drive_prefix}/root/delta?{select}"

        new_items: list[dict] = []
        next_url: str | None = start_url

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
            while next_url:
                resp = await http.get(next_url, headers=headers)
                if resp.status_code != 200:
                    logger.error(
                        "SharePoint watcher: delta request failed HTTP %s — %s",
                        resp.status_code,
                        resp.text[:300],
                    )
                    return

                data = resp.json()
                for item in data.get("value", []):
                    if "deleted" in item:
                        continue
                    if "file" not in item:
                        continue
                    if Path(item.get("name", "")).suffix.lower() in _SUPPORTED:
                        new_items.append(item)

                delta_link: str | None = data.get("@odata.deltaLink")
                next_url = data.get("@odata.nextLink")
                if delta_link:
                    await asyncio.to_thread(self._save_delta_link, delta_link)
                    break

        if not new_items:
            logger.debug("SharePoint watcher: no new or changed files")
            return

        logger.info("SharePoint watcher: %d new/changed file(s) to process", len(new_items))
        token = await asyncio.to_thread(get_access_token)
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as http:
            for item in new_items:
                await self._process_item(http, token, item)

    async def _resolve_folder_id(
        self, headers: dict[str, str], folder: str,
        drive_prefix: str = "https://graph.microsoft.com/v1.0/me/drive",
    ) -> str | None:
        """Resolve a folder name/path to its drive item ID.

        Tries three strategies in order:
        1. Path-based lookup: {drive_prefix}/root:/{folder}
        2. Enumerate root children and match by name (case-insensitive)
        3. Enumerate root children recursively for nested paths
        Logs all root folder names when resolution fails.
        """
        # Strategy 1: direct path lookup
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as _http:
            resp = await _http.get(
                f"{drive_prefix}/root:/{folder}",
                headers=headers,
                params={"$select": "id,name"},
            )
        if resp.status_code == 200:
            return resp.json()["id"]

        # Strategy 2: enumerate root children and match by name (handles casing, etc.)
        target_name = folder.split("/")[0]  # top-level folder name

        # First check if the drive itself is accessible
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as _http:
            drive_resp = await _http.get(
                drive_prefix,
                headers=headers,
                params={"$select": "id,driveType,owner"},
            )
        if drive_resp.status_code != 200:
            logger.error(
                "SharePoint watcher: drive not accessible (%s) — HTTP %s %s. "
                "Ensure Files.Read (or Files.ReadWrite) is granted for the bot account.",
                drive_prefix,
                drive_resp.status_code,
                drive_resp.text[:300],
            )
            return None

        logger.debug("SharePoint watcher: drive info — %s", drive_resp.text[:200])

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as _http:
            children_resp = await _http.get(
                f"{drive_prefix}/root/children",
                headers=headers,
                params={"$select": "id,name,folder"},
            )
        if children_resp.status_code == 200:
            all_folders = [
                i for i in children_resp.json().get("value", []) if "folder" in i
            ]
            folder_names = [i["name"] for i in all_folders]
            matched = next(
                (i for i in all_folders if i["name"].lower() == target_name.lower()),
                None,
            )
            if matched:
                item_id = matched["id"]
                # If the config path has sub-segments, resolve them recursively
                remaining = "/".join(folder.split("/")[1:])
                if remaining:
                    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as _http:
                        sub_resp = await _http.get(
                            f"{drive_prefix}/items/{item_id}:/{remaining}",
                            headers=headers,
                            params={"$select": "id,name"},
                        )
                    if sub_resp.status_code == 200:
                        return sub_resp.json()["id"]
                    logger.error(
                        "SharePoint watcher: found top-level folder %r but could not "
                        "resolve sub-path %r — HTTP %s",
                        matched["name"], remaining, sub_resp.status_code,
                    )
                    return None
                return item_id
            logger.error(
                "SharePoint watcher: folder %r not found. "
                "Root folders on the drive: %s",
                folder,
                folder_names,
            )
        else:
            logger.error(
                "SharePoint watcher: could not list root children — HTTP %s %s",
                children_resp.status_code,
                children_resp.text[:300],
            )
        return None

    async def _process_item(
        self, http: httpx.AsyncClient, token: str, item: dict
    ) -> None:
        item_id: str = item["id"]
        filename: str = item.get("name", "unknown")
        web_url: str = item.get("webUrl", "")
        modified_at: str = item.get("lastModifiedDateTime", "")

        logger.info("SharePoint watcher: downloading %s (id=%s)", filename, item_id)
        try:
            # Download from the bot's own drive — no cross-tenant issues.
            download_url: str | None = item.get("@microsoft.graph.downloadUrl")
            if download_url:
                resp = await http.get(download_url)
            else:
                drive_id = settings.sharepoint_drive_id.strip()
                drive_prefix = (
                    f"https://graph.microsoft.com/v1.0/drives/{drive_id}"
                    if drive_id
                    else "https://graph.microsoft.com/v1.0/me/drive"
                )
                resp = await http.get(
                    f"{drive_prefix}/items/{item_id}/content",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code != 200:
                logger.error(
                    "SharePoint watcher: failed to download %s — HTTP %s",
                    filename,
                    resp.status_code,
                )
                return

            result = await asyncio.to_thread(
                ingest_sharepoint_file,
                filename,
                resp.content,
                item_id,
                web_url,
                modified_at,
            )
            logger.info("SharePoint watcher: ingested %s — %s", filename, result)

        except Exception:
            logger.exception(
                "SharePoint watcher: error processing item %s (%s)", filename, item_id
            )

    # ------------------------------------------------------------------ #
    # Sync state helpers (run in thread via asyncio.to_thread)            #
    # ------------------------------------------------------------------ #

    def _load_delta_link(self) -> str | None:
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT value FROM sync_state WHERE key = %s",
                        (_SYNC_KEY,),
                    )
                    row = cur.fetchone()
                    return row["value"] if row else None
        except Exception:
            logger.warning("SharePoint watcher: could not load delta link from DB")
            return None

    def _save_delta_link(self, delta_link: str) -> None:
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO sync_state (key, value, updated_at)
                        VALUES (%s, %s, now())
                        ON CONFLICT (key) DO UPDATE
                            SET value = EXCLUDED.value,
                                updated_at = now()
                        """,
                        (_SYNC_KEY, delta_link),
                    )
                conn.commit()
        except Exception:
            logger.warning("SharePoint watcher: could not persist delta link")


# ------------------------------------------------------------------ #
# Module-level singleton                                              #
# ------------------------------------------------------------------ #

_watcher: SharePointWatcher | None = None


def get_sharepoint_watcher() -> SharePointWatcher:
    global _watcher
    if _watcher is None:
        _watcher = SharePointWatcher(
            poll_interval=settings.sharepoint_poll_interval
        )
    return _watcher
