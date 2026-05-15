"""OWV employee roster sync service.

Fetches the full employee list from the OWV API on startup and once per day
afterwards, keeping the ``owv_employees`` table up to date:

* New employees are inserted.
* Existing employees are updated (name, team, manager, dates, etc.).
* Employees no longer returned by the API are soft-deleted:
  ``active`` is set to ``false`` and ``date_end`` is set to the current date.

Configuration (all in .env / Settings):
    OWV_API_URL               — Endpoint URL (default: owv-qua instance).
    OWV_USERNAME              — Service account username for Basic Auth.
    OWV_PAT                   — Personal access token / password for Basic Auth.
    OWV_SYNC_INTERVAL_SECONDS — Seconds between syncs (default: 86400 / 1 day).
"""
from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from agent_cv.config import settings
from agent_cv.db.connection import get_connection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public result type (returned by _tick for the manual trigger endpoint)
# ---------------------------------------------------------------------------

@dataclass
class SyncResult:
    upserted: int
    deactivated: int


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class OWVSyncService:
    """Async background task that syncs the OWV employee roster once per day."""

    def __init__(self, sync_interval: int | None = None) -> None:
        self._sync_interval = sync_interval or settings.owv_sync_interval_seconds
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._sync_loop(), name="owv-sync")
        logger.info(
            "OWV sync service started (interval=%ds, url=%s)",
            self._sync_interval,
            settings.owv_api_url,
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("OWV sync service stopped")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def _sync_loop(self) -> None:
        while True:
            try:
                result = await self.tick()
                logger.info(
                    "OWV sync: upserted=%d deactivated=%d",
                    result.upserted,
                    result.deactivated,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("OWV sync: unhandled error in tick")
            await asyncio.sleep(self._sync_interval)

    # ------------------------------------------------------------------
    # Sync logic (also called directly by the manual trigger endpoint)
    # ------------------------------------------------------------------

    async def tick(self) -> SyncResult:
        """Fetch OWV roster and apply upserts + soft-deletes. Returns counts."""
        people = await self._fetch_people()
        if not people:
            logger.warning("OWV sync: API returned empty list — skipping sync to avoid wiping data")
            return SyncResult(upserted=0, deactivated=0)

        # ------------------------------------------------------------------
        # Deduplicate before upserting.
        # OWV sometimes contains multiple entries for the same person
        # (e.g. integration artefacts, test accounts with malformed names).
        # Keeping duplicates causes false "missing CV" results.
        #
        # Primary key: email (normalised lowercase) — reliable across entries
        #   even when the name field has different capitalisation/order.
        # Fallback key: display_name — used when email is absent.
        #
        # Within each group keep the most complete entry (most non-null
        # meaningful fields). Break ties by highest owv_id.
        # ------------------------------------------------------------------

        _COMPLETENESS_FIELDS = ("name", "fullName", "email", "team", "manager", "doExecutiveManager", "dateStarted")

        def _completeness(p: dict) -> int:
            return sum(1 for f in _COMPLETENESS_FIELDS if p.get(f))

        canonical: dict[str, dict] = {}  # dedup_key -> best person dict
        for person in people:
            # Skip ex-employees — OWV returns them with active=False.
            # Departed employees that disappear from the API entirely are
            # handled by the soft-delete step below, so we don't need to
            # ingest inactive rows at all.
            if not person.get("active", True):
                continue
            owv_id = person.get("id")
            if owv_id is None:
                continue
            name = (person.get("name") or "").strip()
            display_name = _compute_display_name(name)
            if not display_name:
                continue

            # Use email as the dedup key when available; fall back to
            # display_name. This ensures entries like owv_id=210/320/338
            # for the same person (same email, but name stored in different
            # order) are treated as duplicates even though their computed
            # display_name differs.
            email = (person.get("email") or "").strip().lower()
            dedup_key = email if email else display_name.lower()

            existing = canonical.get(dedup_key)
            if existing is None:
                canonical[dedup_key] = person
            else:
                # Both entries are active (OWV data quality issue).
                # Keep the most complete one; break ties by highest owv_id.
                curr_score = _completeness(person)
                prev_score = _completeness(existing)
                curr_id = int(owv_id)
                prev_id = int(existing.get("id", 0))
                if curr_score > prev_score or (curr_score == prev_score and curr_id > prev_id):
                    canonical[dedup_key] = person
                    logger.warning(
                        "OWV sync: duplicate active person (key=%r) — keeping owv_id=%d (score %d), discarding owv_id=%d (score %d)",
                        dedup_key, curr_id, curr_score, prev_id, prev_score,
                    )
                else:
                    logger.warning(
                        "OWV sync: duplicate active person (key=%r) — keeping owv_id=%d (score %d), discarding owv_id=%d (score %d)",
                        dedup_key, prev_id, prev_score, curr_id, curr_score,
                    )

        upserted = 0
        deactivated = 0
        received_ids: list[int] = []

        with get_connection() as conn:
            with conn.cursor() as cur:
                for person in canonical.values():
                    owv_id = person.get("id")
                    if owv_id is None:
                        continue
                    received_ids.append(owv_id)

                    full_name = (person.get("fullName") or "").strip()
                    name = (person.get("name") or "").strip()
                    # display_name already computed during dedup; recompute for clarity
                    display_name = _compute_display_name(name)
                    email = person.get("email") or None
                    team = person.get("team") or None
                    manager_name = person.get("manager") or None
                    do_exec = person.get("doExecutiveManager") or None
                    date_started = _parse_date(person.get("dateStarted"))
                    date_end = _parse_date(person.get("dateEnd"))
                    active = bool(person.get("active", True))

                    cur.execute(
                        """
                        insert into owv_employees (
                            owv_id, name, full_name, display_name, email, team,
                            manager_name, do_executive_manager_name,
                            date_started, date_end, active, last_synced_at
                        ) values (
                            %(owv_id)s, %(name)s, %(full_name)s, %(display_name)s,
                            %(email)s, %(team)s, %(manager_name)s, %(do_exec)s,
                            %(date_started)s, %(date_end)s, %(active)s, now()
                        )
                        on conflict (owv_id) do update set
                            name                      = excluded.name,
                            full_name                 = excluded.full_name,
                            display_name              = excluded.display_name,
                            email                     = excluded.email,
                            team                      = excluded.team,
                            manager_name              = excluded.manager_name,
                            do_executive_manager_name = excluded.do_executive_manager_name,
                            date_started              = excluded.date_started,
                            date_end                  = excluded.date_end,
                            active                    = excluded.active,
                            last_synced_at            = now()
                        """,
                        {
                            "owv_id": owv_id,
                            "name": name,
                            "full_name": full_name,
                            "display_name": display_name,
                            "email": email,
                            "team": team,
                            "manager_name": manager_name,
                            "do_exec": do_exec,
                            "date_started": date_started,
                            "date_end": date_end,
                            "active": active,
                        },
                    )
                    upserted += 1

                # Soft-delete employees no longer returned by the API.
                # Use != ALL(%s) instead of NOT IN (%s) — psycopg3 handles
                # list parameters correctly with the ANY/ALL array operators.
                if received_ids:
                    cur.execute(
                        """
                        update owv_employees
                        set active   = false,
                            date_end = current_date
                        where owv_id != ALL(%(ids)s)
                          and active  = true
                        """,
                        {"ids": received_ids},
                    )
                    deactivated = cur.rowcount if cur.rowcount >= 0 else 0

            conn.commit()

        return SyncResult(upserted=upserted, deactivated=deactivated)

    # ------------------------------------------------------------------
    # HTTP helper
    # ------------------------------------------------------------------

    async def _fetch_people(self) -> list[dict[str, Any]]:
        """Call the OWV API and return the raw list of person dicts."""
        credentials = base64.b64encode(
            f"{settings.owv_username}:{settings.owv_pat}".encode()
        ).decode()
        headers = {"Authorization": f"Basic {credentials}"}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(settings.owv_api_url, headers=headers)
            response.raise_for_status()
            data = response.json()
        if isinstance(data, list):
            return data
        # Some API versions wrap the list in a "value" key
        if isinstance(data, dict) and "value" in data:
            return data["value"]
        logger.warning("OWV sync: unexpected response shape: %s", type(data))
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_display_name(owv_short_name: str) -> str:
    """Compute 'Firstname Lastname' from OWV's 'LASTNAME Firstname' name field.

    The `name` field from OWV is always in 'LASTNAME Firstname' format and is
    reliable. We reverse the two parts and normalise the lastname to title case
    so it can be matched (case-insensitively) against employees.full_name, which
    is derived from filenames and is typically 'Firstname Lastname'.

    Examples:
        "ABEL Rui"              → "Rui Abel"
        "ANTUNES Mauro"         → "Mauro Antunes"
        "JORGE-OLIVEIRA Fernando" → "Fernando Jorge-Oliveira"
        "GUILHOTO Vitorino"     → "Vitorino Guilhoto"
    """
    parts = owv_short_name.split(' ', 1)  # split at first space only
    if len(parts) != 2:
        return owv_short_name
    lastname_raw, firstname = parts
    # Normalise lastname to title case, preserving hyphens
    lastname = '-'.join(segment.capitalize() for segment in lastname_raw.split('-'))
    return f"{firstname} {lastname}"


def _parse_date(value: object) -> object:
    """Parse an ISO-8601 datetime string from the OWV API into a date, or None."""
    if value is None:
        return None
    import datetime
    s = str(value)
    try:
        # Handles "2025-10-22T00:00:00" and plain "2025-10-22"
        return datetime.date.fromisoformat(s[:10])
    except ValueError:
        logger.debug("OWV sync: could not parse date value %r", value)
        return None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_owv_sync_service: OWVSyncService | None = None


def get_owv_sync_service() -> OWVSyncService:
    global _owv_sync_service
    if _owv_sync_service is None:
        _owv_sync_service = OWVSyncService()
    return _owv_sync_service
