import time

from fastapi import APIRouter, Query

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
from agent_cv.teams.agent import get_graph_bot

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
    bot = get_graph_bot()
    return {
        "status": "running" if bot.running else "stopped",
        "account": settings.graph_user_email,
    }


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
