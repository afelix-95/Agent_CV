import asyncio
import hashlib
import hmac
import time

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse

from agent_cv.api.models import (
    AuditLogsResponse,
    CertificationHit,
    ExperienceHit,
    IngestRequest,
    QueryRequest,
    QueryResponse,
)
from agent_cv.db.schema import apply_schema
from agent_cv.ingestion.ingest_service import ingest_documents
from agent_cv.db.connection import get_connection
from agent_cv.services.query_service import audit_query
from agent_cv.services.agent_service import handle_user_query
from agent_cv.config import settings
from agent_cv.services.graph_service import graph_setup_issue
from agent_cv.teams.agent import get_teams_bot

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/admin/init-db")
def init_db() -> dict[str, str]:
    apply_schema()
    return {"status": "schema-applied"}


@router.post("/admin/ingest")
def ingest(request: IngestRequest) -> dict[str, int]:
    return ingest_documents(
        max_files=request.max_files,
        force_reingest=request.force_reingest,
        filename_contains=request.filename_contains,
    )


@router.get("/admin/query-audit/recent", response_model=AuditLogsResponse)
def audit_logs_recent(limit: int = Query(50, ge=1, le=500)) -> AuditLogsResponse:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("select count(*) as total from query_audit")
            total_row = cur.fetchone()
            total = total_row["total"] if total_row else 0

            cur.execute(
                """
                select
                    aad_object_id,
                    chat_id,
                    query_text,
                    query_language,
                    response_language,
                    result_count,
                    latency_ms,
                    agent_tool_calls,
                    created_at
                from query_audit
                order by created_at desc
                limit %s
                """,
                (limit,),
            )
            rows = cur.fetchall()

    return AuditLogsResponse(
        total=total,
        entries=[dict(row) for row in rows],
    )


@router.post("/query", response_model=QueryResponse)
def query(request: QueryRequest) -> QueryResponse:
    started = time.perf_counter()
    agent_result = handle_user_query(
        request.query,
        request.language,
        request.conversation_id,
    )
    _safe_audit(
        query_text=request.query,
        query_language=request.language,
        response_language=agent_result.language,
        result_count=agent_result.total_results,
        latency_ms=int((time.perf_counter() - started) * 1000),
        tool_calls_log=agent_result.tool_calls_log,
    )

    certifications = []
    experiences = []
    if agent_result.analysis.query_type == "certifications":
        certifications = [CertificationHit(**row) for row in agent_result.rows_page]
    elif agent_result.analysis.query_type == "experience":
        experiences = [ExperienceHit(**row) for row in agent_result.rows_page]

    return QueryResponse(
        intent=agent_result.analysis.query_type,
        language=agent_result.language,
        answer=agent_result.answer,
        summary=agent_result.summary,
        total_results=agent_result.total_results,
        shown_results=agent_result.shown_results,
        has_more=agent_result.has_more,
        show_certification_details=agent_result.show_certification_details,
        certifications=certifications,
        experiences=experiences,
    )


@router.get("/teams/status")
def teams_status() -> dict[str, str]:
    issue = graph_setup_issue()
    if issue:
        return {"status": "not-configured", "detail": issue}
    bot = get_teams_bot()
    return {
        "status": "running" if bot.running else "stopped",
        "account": settings.graph_user_email,
    }


@router.post("/graph-notifications/teams")
async def teams_webhook(
    request: Request,
    validationToken: str | None = Query(default=None),
) -> Response:
    import logging as _logging
    _wh_log = _logging.getLogger("agent_cv.webhook")

    # Graph subscription validation handshake: echo the token back as text/plain
    if validationToken:
        _wh_log.info("Teams webhook: Graph validation handshake received")
        return PlainTextResponse(validationToken, status_code=200)

    raw_body = await request.body()
    _wh_log.debug("Teams webhook: raw payload (%d bytes): %s", len(raw_body), raw_body[:2000])

    try:
        body = await request.json()
    except Exception:
        _wh_log.warning("Teams webhook: failed to parse JSON body: %s", raw_body[:500])
        return Response(status_code=400)

    notifications = body.get("value", [])
    _wh_log.info(
        "Teams webhook: received %d notification(s) from Graph", len(notifications)
    )

    bot = get_teams_bot()
    for idx, notification in enumerate(notifications):
        change_type = notification.get("changeType", "")
        resource = notification.get("resource", "")
        lifecycle = notification.get("lifecycleEvent", "")
        _wh_log.info(
            "Teams webhook: notification[%d] changeType=%r resource=%r lifecycleEvent=%r",
            idx,
            change_type,
            resource,
            lifecycle,
        )
        # Only process new-message events; ignore lifecycle/missed notifications
        if change_type != "created":
            _wh_log.debug(
                "Teams webhook: skipping notification[%d] — changeType is %r, not 'created'",
                idx,
                change_type,
            )
            continue
        client_state = notification.get("clientState", "")
        asyncio.create_task(bot.handle_notification(resource, client_state))

    # Return 202 immediately — processing happens in background tasks
    return Response(status_code=202)


@router.post("/graph-notifications/teams-chats")
async def teams_chats_webhook(
    request: Request,
    validationToken: str | None = Query(default=None),
) -> Response:
    """Webhook for /me/chats subscription — fires when a new chat is created.

    This gives near-instant acceptance of external/pending chats without
    waiting for the periodic poller.
    """
    import logging as _logging
    _wh_log = _logging.getLogger("agent_cv.webhook")

    if validationToken:
        _wh_log.info("Teams chats webhook: Graph validation handshake received")
        return PlainTextResponse(validationToken, status_code=200)

    raw_body = await request.body()
    _wh_log.debug(
        "Teams chats webhook: raw payload (%d bytes): %s", len(raw_body), raw_body[:2000]
    )

    try:
        body = await request.json()
    except Exception:
        _wh_log.warning("Teams chats webhook: failed to parse JSON body: %s", raw_body[:500])
        return Response(status_code=400)

    notifications = body.get("value", [])
    _wh_log.info(
        "Teams chats webhook: received %d notification(s) from Graph", len(notifications)
    )

    bot = get_teams_bot()
    for idx, notification in enumerate(notifications):
        change_type = notification.get("changeType", "")
        resource = notification.get("resource", "")
        _wh_log.info(
            "Teams chats webhook: notification[%d] changeType=%r resource=%r",
            idx,
            change_type,
            resource,
        )
        if change_type != "created":
            continue
        client_state = notification.get("clientState", "")
        asyncio.create_task(bot.handle_chat_notification(resource, client_state))

    return Response(status_code=202)


# ------------------------------------------------------------------ #
# Signed CV download endpoint                                         #
# ------------------------------------------------------------------ #

_TOKEN_TTL = 86400  # 24 hours


def generate_cv_download_url(document_id: str) -> str:
    """Return a time-limited signed URL for downloading a CV file."""
    base = (settings.webhook_base_url or "").rstrip("/")
    expires = int(time.time()) + _TOKEN_TTL
    secret = settings.file_download_secret.encode()
    msg = f"{document_id}:{expires}".encode()
    sig = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    return f"{base}/files/{document_id}?expires={expires}&token={sig}"


@router.get("/files/{document_id}")
def download_cv_file(
    document_id: str,
    expires: int = Query(...),
    token: str = Query(...),
) -> FileResponse:
    """Serve a CV file via a time-limited HMAC-signed URL.

    The token is validated before any DB or filesystem access to prevent
    enumeration attacks.
    """
    secret = settings.file_download_secret
    if not secret:
        raise HTTPException(status_code=503, detail="File downloads not configured")

    # Validate token first (constant-time compare prevents timing attacks)
    msg = f"{document_id}:{expires}".encode()
    expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, token):
        raise HTTPException(status_code=403, detail="Invalid or expired token")

    if time.time() > expires:
        raise HTTPException(status_code=403, detail="Invalid or expired token")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT original_filename, source_path, sharepoint_item_id
                  FROM source_documents
                 WHERE document_id = %s
                """,
                (document_id,),
            )
            row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Document not found")

    sharepoint_item_id = row["sharepoint_item_id"]
    original_filename: str = row["original_filename"] or f"cv_{document_id}.pdf"

    if sharepoint_item_id:
        # File lives on the bot's OneDrive — fetch on demand and stream back.
        import httpx as _httpx
        from agent_cv.services.graph_service import get_access_token

        tok = get_access_token()
        r = _httpx.get(
            f"https://graph.microsoft.com/v1.0/me/drive/items/{sharepoint_item_id}/content",
            headers={"Authorization": f"Bearer {tok}"},
            follow_redirects=True,
            timeout=30.0,
        )
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail="Could not retrieve file from OneDrive")

        from fastapi.responses import Response as _Response
        return _Response(  # type: ignore[return-value]
            content=r.content,
            media_type=r.headers.get("content-type", "application/octet-stream"),
            headers={"Content-Disposition": f'attachment; filename="{original_filename}"'},
        )

    # Locally ingested file — serve from disk
    source_path = row["source_path"] or ""
    import os
    # source_path is stored as the full relative path (e.g. "PDFs/REPOCV/file.pdf")
    # Use it directly; it's relative to the app working directory (/app in Docker).
    if os.path.isabs(source_path):
        candidate = source_path
    else:
        candidate = os.path.join("/app", source_path) if os.path.sep == "/" else source_path
    if not os.path.isfile(candidate):
        # Fallback: filename only, look inside pdf_root
        candidate = os.path.join(settings.pdf_root, os.path.basename(source_path))
    if not os.path.isfile(candidate):
        raise HTTPException(status_code=404, detail="File not found on disk")

    return FileResponse(
        path=candidate,
        filename=original_filename,
        media_type="application/octet-stream",
    )


# ------------------------------------------------------------------ #
# Signed CSV export download endpoint                                 #
# ------------------------------------------------------------------ #

_EXPORT_TOKEN_TTL = 3600  # 1 hour


def generate_export_url(export_id: str) -> str:
    """Return a time-limited signed URL for downloading a generated CSV export."""
    base = (settings.webhook_base_url or "").rstrip("/")
    expires = int(time.time()) + _EXPORT_TOKEN_TTL
    secret = settings.file_download_secret.encode()
    msg = f"export:{export_id}:{expires}".encode()
    sig = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    return f"{base}/exports/{export_id}?expires={expires}&token={sig}"


@router.get("/exports/{export_id}")
def download_export_file(
    export_id: str,
    expires: int = Query(...),
    token: str = Query(...),
) -> FileResponse:
    """Serve a generated CSV export via a time-limited HMAC-signed URL."""
    import os
    import re

    secret = settings.file_download_secret
    if not secret:
        raise HTTPException(status_code=503, detail="File downloads not configured")

    msg = f"export:{export_id}:{expires}".encode()
    expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, token):
        raise HTTPException(status_code=403, detail="Invalid or expired token")

    if time.time() > expires:
        raise HTTPException(status_code=403, detail="Invalid or expired token")

    # Validate export_id to prevent path traversal
    if not re.fullmatch(r"[0-9a-f\-]{36}", export_id):
        raise HTTPException(status_code=400, detail="Invalid export ID")

    exports_dir = "/tmp/agent_cv_exports"
    csv_path = f"{exports_dir}/{export_id}.csv"
    pdf_path = f"{exports_dir}/{export_id}.pdf"

    if os.path.isfile(csv_path):
        return FileResponse(
            path=csv_path,
            filename=f"{export_id}.csv",
            media_type="text/csv; charset=utf-8",
        )
    if os.path.isfile(pdf_path):
        return FileResponse(
            path=pdf_path,
            filename=f"{export_id}.pdf",
            media_type="application/pdf",
        )
    raise HTTPException(status_code=404, detail="Export file not found or has expired")


def _safe_audit(
    query_text: str,
    query_language: str | None,
    response_language: str,
    result_count: int,
    latency_ms: int,
    tool_calls_log: list | None = None,
) -> None:
    try:
        audit_query(
            query_text=query_text,
            query_language=query_language,
            response_language=response_language,
            result_count=result_count,
            latency_ms=latency_ms,
            agent_tool_calls=tool_calls_log or [],
        )
    except Exception:
        # Auditing should not block user responses.
        pass
