"""SharePoint document library watcher.

Polls a SharePoint folder for new or modified files using the Microsoft Graph
children listing API and ingests them automatically into the Agent CV database.

Cross-tenant shared folders are supported by providing the sharing link URL
(SHAREPOINT_URL), which causes the watcher to list children via the Graph
Shares API (``/shares/{encodedUrl}/driveItem/children``).  This avoids the
delta API which does not support cross-tenant access.

To avoid re-downloading unchanged files, the ``lastModifiedDateTime`` of each
item is stored in the ``sharepoint_modified_at`` column of ``source_documents``
and compared on every scan.  Only files whose remote timestamp is newer than
the stored value are downloaded and re-ingested.

Configuration (all in .env / Settings):
    SHAREPOINT_URL            — Sharing link URL for the folder (from Share →
                                Copy link in OneDrive/SharePoint).  Enables
                                cross-tenant access via the Shares API.
    SHAREPOINT_DRIVE_ID       — Drive ID of the SharePoint library.  Used as a
                                fallback when SHAREPOINT_URL is not set.
    SHAREPOINT_FOLDER_ITEM_ID — Item ID of a specific subfolder (takes
                                precedence over SHAREPOINT_FOLDER_PATH).
    SHAREPOINT_FOLDER_PATH    — Subfolder path within the drive root.
    SHAREPOINT_POLL_INTERVAL  — Seconds between scans (default 3600).

The Entra app registration must have the delegated permission
``Files.Read.All`` consented by the tenant admin.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime
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

_SUPPORTED = {".pdf", ".txt", ".docx"}


def _encode_sharing_url(url: str) -> str:
    """Encode a sharing URL to the base64url token expected by the Graph Shares API.

    Only the ``email=`` query parameter (an invitation-tracking extra added when
    sharing with a specific person) is stripped.  All other parameters — in
    particular ``e=``, which is the link token for "Anyone with the link" URLs —
    are preserved because they are required for the Shares API to resolve the link.
    """
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
    """Return True when the minimum SharePoint configuration is present."""
    return bool(settings.sharepoint_url or settings.sharepoint_folder_item_id or settings.sharepoint_drive_id)


class SharePointWatcher:
    """Async background task that watches a SharePoint drive for new files
    and ingests them via the existing pipeline.

    Lifecycle is managed by the FastAPI lifespan in main.py.
    """

    def __init__(self, poll_interval: int = 300) -> None:
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
            source = (
                f"shares={settings.sharepoint_url[:60]}…"
                if settings.sharepoint_url
                else (
                    f"item={settings.sharepoint_folder_item_id}"
                    if settings.sharepoint_folder_item_id
                    else f"drive={settings.sharepoint_drive_id}, folder={settings.sharepoint_folder_path or '/'}"
                )
            )
            logger.info(
                "SharePoint watcher started (interval=%ds, %s)",
                self._poll_interval,
                source,
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
        drive = settings.sharepoint_drive_id
        item_id = settings.sharepoint_folder_item_id.strip()
        folder = settings.sharepoint_folder_path.strip("/")
        sharing_url = settings.sharepoint_url.strip()
        select = "$select=id,name,file,webUrl,lastModifiedDateTime"

        # Build auth headers; include the sharing link password when set.
        token = await asyncio.to_thread(get_access_token)
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
        if settings.sharepoint_password:
            headers["X-Sharing-Link-Password"] = settings.sharepoint_password

        if sharing_url:
            # Shares API: works cross-tenant for "anyone with the link" links,
            # including password-protected ones.
            encoded = _encode_sharing_url(sharing_url)
            list_url = (
                f"https://graph.microsoft.com/v1.0"
                f"/shares/{encoded}/driveItem/children?{select}"
            )
        elif item_id:
            # Shared folder: access via the owner's drive ID + item ID.
            # The bot must have Files.Read.All consented to traverse another user's drive.
            list_url = (
                f"https://graph.microsoft.com/v1.0"
                f"/drives/{drive}/items/{item_id}/children?{select}"
            )
        elif folder:
            list_url = (
                f"https://graph.microsoft.com/v1.0"
                f"/drives/{drive}/root:/{folder}:/children?{select}"
            )
        else:
            list_url = (
                f"https://graph.microsoft.com/v1.0"
                f"/drives/{drive}/root/children?{select}"
            )

        known_mtimes = await asyncio.to_thread(self._load_item_mtimes)
        to_process: list[dict] = []
        next_url: str | None = list_url

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
            while next_url:
                resp = await http.get(
                    next_url,
                    headers=headers,
                )
                if resp.status_code != 200:
                    logger.error(
                        "SharePoint watcher: listing request failed HTTP %s — %s",
                        resp.status_code,
                        resp.text[:300],
                    )
                    return

                data = resp.json()
                for item in data.get("value", []):
                    if "file" not in item:
                        continue  # skip folders
                    if Path(item.get("name", "")).suffix.lower() not in _SUPPORTED:
                        continue

                    remote_mtime_str: str = item.get("lastModifiedDateTime", "")
                    stored_mtime: datetime | None = known_mtimes.get(item["id"])
                    if stored_mtime and remote_mtime_str:
                        try:
                            remote_mtime = datetime.fromisoformat(
                                remote_mtime_str.replace("Z", "+00:00")
                            )
                            if remote_mtime <= stored_mtime:
                                continue  # unchanged since last ingest
                        except ValueError:
                            pass  # malformed timestamp → process anyway

                    to_process.append(item)

                next_url = data.get("@odata.nextLink")

        if not to_process:
            logger.debug("SharePoint watcher: no new or changed files")
            return

        logger.info("SharePoint watcher: %d new/changed file(s) to process", len(to_process))
        token = await asyncio.to_thread(get_access_token)
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as http:
            for item in to_process:
                await self._process_item(http, token, item)

    async def _process_item(
        self, http: httpx.AsyncClient, token: str, item: dict
    ) -> None:
        item_id: str = item["id"]
        filename: str = item.get("name", "unknown")
        web_url: str = item.get("webUrl", "")
        modified_at: str = item.get("lastModifiedDateTime", "")
        # Pre-authenticated download URL returned by Graph for file items.
        # Present when listing via the Shares API (cross-tenant) and avoids
        # a separate auth-gated request to /drives/{id}/items/{id}/content.
        download_url: str | None = item.get("@microsoft.graph.downloadUrl")

        logger.info("SharePoint watcher: downloading %s (id=%s)", filename, item_id)
        try:
            if download_url:
                resp = await http.get(download_url)
            else:
                dl_headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
                if settings.sharepoint_password:
                    dl_headers["X-Sharing-Link-Password"] = settings.sharepoint_password
                resp = await http.get(
                    f"https://graph.microsoft.com/v1.0"
                    f"/drives/{settings.sharepoint_drive_id}/items/{item_id}/content",
                    headers=dl_headers,
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
    # DB helpers (run in thread via asyncio.to_thread)                    #
    # ------------------------------------------------------------------ #

    def _load_item_mtimes(self) -> dict[str, datetime]:
        """Return {sharepoint_item_id: sharepoint_modified_at} for all known items."""
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT sharepoint_item_id, sharepoint_modified_at
                          FROM source_documents
                         WHERE sharepoint_item_id IS NOT NULL
                           AND sharepoint_modified_at IS NOT NULL
                        """
                    )
                    return {
                        row["sharepoint_item_id"]: row["sharepoint_modified_at"]
                        for row in cur.fetchall()
                    }
        except Exception:
            logger.warning("SharePoint watcher: could not load item modification times")
            return {}


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
