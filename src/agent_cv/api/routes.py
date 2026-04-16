from fastapi import APIRouter

from agent_cv.api.models import IngestRequest, QueryRequest, QueryResponse, CertificationHit
from agent_cv.db.schema import apply_schema
from agent_cv.ingestion.ingest_service import ingest_documents
from agent_cv.services.query_service import run_query
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
    rows = run_query(request.query)
    summary, language = build_summary(request.query, rows, request.language)
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

    rows = run_query(text)
    summary, language = build_summary(text, rows, None)

    if language == "pt":
        message = summary
    else:
        message = summary

    return {
        "type": "message",
        "text": message,
        "results": rows[:10],
    }
