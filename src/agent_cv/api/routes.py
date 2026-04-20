import time

from fastapi import APIRouter

from agent_cv.api.models import IngestRequest, QueryRequest, QueryResponse, CertificationHit
from agent_cv.db.schema import apply_schema
from agent_cv.ingestion.ingest_service import ingest_documents
from agent_cv.services.query_service import audit_query, infer_intent, run_query
from agent_cv.services.response_service import build_summary

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
    return ingest_documents(max_files=request.max_files)


@router.post("/query", response_model=QueryResponse)
def query(request: QueryRequest) -> QueryResponse:
    started = time.perf_counter()
    rows = run_query(request.query)
    summary, language = build_summary(request.query, rows, request.language)
    _safe_audit(
        query_text=request.query,
        query_language=request.language,
        response_language=language,
        result_count=len(rows),
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return QueryResponse(
        language=language,
        summary=summary,
        certifications=[CertificationHit(**row) for row in rows],
    )


@router.post("/teams/messages")
def teams_message(payload: dict) -> dict:
    text = (payload.get("text") or "").strip()
    if not text:
        return {"type": "message", "text": "Please provide a query."}

    started = time.perf_counter()
    rows = run_query(text)
    summary, language = build_summary(text, rows, None)
    _safe_audit(
        query_text=text,
        query_language=None,
        response_language=language,
        result_count=len(rows),
        latency_ms=int((time.perf_counter() - started) * 1000),
    )

    if language == "pt":
        message = summary
    else:
        message = summary

    return {
        "type": "message",
        "text": message,
        "results": rows[:10],
    }


def _safe_audit(
    query_text: str,
    query_language: str | None,
    response_language: str,
    result_count: int,
    latency_ms: int,
) -> None:
    try:
        audit_query(
            query_text=query_text,
            query_language=query_language,
            response_language=response_language,
            result_count=result_count,
            latency_ms=latency_ms,
            normalized_intent=infer_intent(query_text),
        )
    except Exception:
        # Auditing should not block user responses.
        pass
