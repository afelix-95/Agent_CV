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
    # Graph subscription validation handshake: echo the token back as text/plain
    if validationToken:
        return PlainTextResponse(validationToken, status_code=200)

    body = await request.json()
    notifications = body.get("value", [])
    bot = get_teams_bot()
    for notification in notifications:
        # Only process new-message events; ignore lifecycle/missed notifications
        if notification.get("changeType") != "created":
            continue
        resource = notification.get("resource", "")
        client_state = notification.get("clientState", "")
        asyncio.create_task(bot.handle_notification(resource, client_state))

    # Return 202 immediately — processing happens in background tasks
    return Response(status_code=202)


# ------------------------------------------------------------------ #
# Signed CV download endpoint                                         #
# ------------------------------------------------------------------ #

_TOKEN_TTL = 86400  # 24 hours


def generate_cv_download_url(document_id: int) -> str:
    """Return a time-limited signed URL for downloading a CV file."""
    base = (settings.webhook_base_url or "").rstrip("/")
    expires = int(time.time()) + _TOKEN_TTL
    secret = settings.file_download_secret.encode()
    msg = f"{document_id}:{expires}".encode()
    sig = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    return f"{base}/files/{document_id}?expires={expires}&token={sig}"


@router.get("/files/{document_id}")
def download_cv_file(
    document_id: int,
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
    pdf_root = settings.pdf_root
    # source_path may be absolute or relative to pdf_root
    candidate = source_path if os.path.isabs(source_path) else os.path.join(pdf_root, source_path)
    if not os.path.isfile(candidate):
        raise HTTPException(status_code=404, detail="File not found on disk")

    return FileResponse(
        path=candidate,
        filename=original_filename,
        media_type="application/octet-stream",
    )


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
