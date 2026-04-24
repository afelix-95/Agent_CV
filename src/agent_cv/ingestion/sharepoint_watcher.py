"""SharePoint document library watcher.

Polls a SharePoint drive for new or modified files using the Microsoft Graph
delta API and ingests them automatically into the Agent CV database.

The delta link (a resumption token) is persisted in the ``sync_state`` table
so restarts continue from where the previous run left off rather than
re-scanning the entire library.

Configuration (all in .env / Settings):
    SHAREPOINT_DRIVE_ID       — Drive ID for the SharePoint document library.
                                Find it via Graph Explorer:
                                GET /me/drives  or  GET /sites/{site-id}/drives
    SHAREPOINT_FOLDER_PATH    — Optional subfolder (e.g. "CV Repository").
                                Leave blank to watch the entire drive root.
    SHAREPOINT_POLL_INTERVAL  — Seconds between delta polls (default 300).

The Entra app registration must have the delegated permission
``Files.Read.All`` (or ``Sites.Read.All``) consented by the tenant admin.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

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


def sharepoint_configured() -> bool:
    """Return True when the minimum SharePoint configuration is present."""
    return bool(settings.sharepoint_drive_id)


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
            logger.info(
                "SharePoint watcher started (interval=%ds, drive=%s, folder=%r)",
                self._poll_interval,
                settings.sharepoint_drive_id,
                settings.sharepoint_folder_path or "/",
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
        stored_delta = await asyncio.to_thread(self._load_delta_link)

        if stored_delta:
            start_url = stored_delta
        else:
            drive = settings.sharepoint_drive_id
            select = "$select=id,name,file,webUrl,deleted,parentReference"
            item_id = settings.sharepoint_folder_item_id.strip()
            folder = settings.sharepoint_folder_path.strip("/")
            if item_id:
                # Shared folder referenced by item ID (e.g. from /me/drive/sharedWithMe)
                start_url = (
                    f"https://graph.microsoft.com/v1.0"
                    f"/drives/{drive}/items/{item_id}/delta?{select}"
                )
            elif folder:
                start_url = (
                    f"https://graph.microsoft.com/v1.0"
                    f"/drives/{drive}/root:/{folder}:/delta?{select}"
                )
            else:
                start_url = (
                    f"https://graph.microsoft.com/v1.0"
                    f"/drives/{drive}/root/delta?{select}"
                )

        token = await asyncio.to_thread(get_access_token)
        new_items: list[dict] = []
        next_url: str | None = start_url

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
            while next_url:
                resp = await http.get(
                    next_url,
                    headers={"Authorization": f"Bearer {token}"},
                )
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
                        continue  # ignore deletions
                    if "file" not in item:
                        continue  # skip folders / drive root
                    if Path(item.get("name", "")).suffix.lower() in _SUPPORTED:
                        new_items.append(item)

                delta_link: str | None = data.get("@odata.deltaLink")
                next_url = data.get("@odata.nextLink")

                if delta_link:
                    await asyncio.to_thread(self._save_delta_link, delta_link)
                    break  # delta link marks end of this change-set

        if not new_items:
            return

        logger.info("SharePoint watcher: %d new/changed file(s) to process", len(new_items))
        token = await asyncio.to_thread(get_access_token)
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as http:
            for item in new_items:
                await self._process_item(http, token, item)

    async def _process_item(
        self, http: httpx.AsyncClient, token: str, item: dict
    ) -> None:
        item_id: str = item["id"]
        filename: str = item.get("name", "unknown")
        web_url: str = item.get("webUrl", "")

        logger.info("SharePoint watcher: downloading %s (id=%s)", filename, item_id)
        try:
            resp = await http.get(
                f"https://graph.microsoft.com/v1.0"
                f"/drives/{settings.sharepoint_drive_id}/items/{item_id}/content",
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
