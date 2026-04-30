"""OneDrive/SharePoint document library watcher.

Polls the bot's own OneDrive folder for new or modified files using the
Microsoft Graph delta API and ingests them automatically into the Agent CV
database.

Because the bot owns the drive, no cross-tenant permissions are required —
only the basic ``Files.Read`` delegated scope (self-consentable) is needed.

The delta link (a resumption token) is persisted in the ``sync_state`` table
so restarts continue from where the previous run left off rather than
re-scanning the entire library.

Configuration (all in .env / Settings):
    SHAREPOINT_FOLDER_PATH    — Subfolder path inside the bot's OneDrive root
                                (e.g. "REPOCV").  Leave blank to watch the
                                entire OneDrive root.
    SHAREPOINT_POLL_INTERVAL  — Seconds between delta polls (default 3600).

Cross-tenant sharing link support (fallback):
    SHAREPOINT_URL            — If set, children are listed via the Graph
                                Shares API instead of delta.  Requires an
                                "Anyone with the link" sharing URL and
                                ``Files.Read.All`` admin-consented.
"""
from __future__ import annotations

import asyncio
import base64
import logging
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
    """Async background task that watches the bot's OneDrive for new files
    and ingests them via the existing pipeline.

    Lifecycle is managed by the FastAPI lifespan in main.py.
    """

    def __init__(self, poll_interval: int = 3600) -> None:
        self._poll_interval = poll_interval
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._poll_loop(), name="sharepoint-watcher"
            )
            folder = settings.sharepoint_folder_path or "/"
            logger.info(
                "SharePoint watcher started (interval=%ds, folder=%r)",
                self._poll_interval,
                folder,
            )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("SharePoint watcher stopped")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

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
            await asyncio.sleep(self._poll_interval)

    async def _tick(self) -> None:
        token = await asyncio.to_thread(get_access_token)
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
        select = "$select=id,name,file,webUrl,lastModifiedDateTime,deleted"

        stored_delta = await asyncio.to_thread(self._load_delta_link)
        if stored_delta:
            start_url = stored_delta
        else:
            folder = settings.sharepoint_folder_path.strip("/")
            if folder:
                folder_id = await self._resolve_folder_id(headers, folder)
                if not folder_id:
                    return
                start_url = (
                    f"https://graph.microsoft.com/v1.0"
                    f"/me/drive/items/{folder_id}/delta?{select}"
                )
            else:
                start_url = (
                    f"https://graph.microsoft.com/v1.0"
                    f"/me/drive/root/delta?{select}"
                )

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
        self, headers: dict[str, str], folder: str
    ) -> str | None:
        """Resolve a folder name/path to its OneDrive item ID.

        Tries three strategies in order:
        1. Path-based lookup: /me/drive/root:/{folder}
        2. Enumerate root children and match by name (case-insensitive)
        3. Enumerate root children recursively for nested paths
        Logs all root folder names when resolution fails.
        """
        # Strategy 1: direct path lookup
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as _http:
            resp = await _http.get(
                f"https://graph.microsoft.com/v1.0/me/drive/root:/{folder}",
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
                "https://graph.microsoft.com/v1.0/me/drive",
                headers=headers,
                params={"$select": "id,driveType,owner"},
            )
        if drive_resp.status_code != 200:
            logger.error(
                "SharePoint watcher: /me/drive not accessible — HTTP %s %s. "
                "Ensure Files.Read (or Files.ReadWrite) is granted for the bot account.",
                drive_resp.status_code,
                drive_resp.text[:300],
            )
            return None

        logger.debug("SharePoint watcher: drive info — %s", drive_resp.text[:200])

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as _http:
            children_resp = await _http.get(
                "https://graph.microsoft.com/v1.0/me/drive/root/children",
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
                            f"https://graph.microsoft.com/v1.0/me/drive/items/{item_id}:/{remaining}",
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
                resp = await http.get(
                    f"https://graph.microsoft.com/v1.0/me/drive/items/{item_id}/content",
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
