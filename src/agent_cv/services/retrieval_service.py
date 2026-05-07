from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
import re

from openai import AzureOpenAI

from agent_cv.config import settings
from agent_cv.db.connection import get_connection

MAX_CONTEXT_SNIPPETS = 20
MAX_CONTEXT_TEXT_CHARS = 1800


@dataclass(frozen=True)
class RetrievedSnippet:
    employee_name: str
    source: str
    text: str
    score: float


def retrieve_semantic_context(
    query: str,
    intent: str,
    rows: Sequence[dict],
    employee_names: Sequence[str],
) -> list[RetrievedSnippet]:
    query_vector = _embed_query(query)
    if query_vector:
        semantic_rows = _search_semantic_chunks(query_vector, intent, list(employee_names)[:12])
        if semantic_rows:
            return semantic_rows
    return _context_from_rows(rows, intent)


def format_context_for_prompt(snippets: list[RetrievedSnippet], language: str) -> str:
    if not snippets:
        return "- Nenhum contexto recuperado." if language == "pt" else "- No context retrieved."

    lines: list[str] = []
    for idx, snippet in enumerate(snippets, start=1):
        score_label = f"{snippet.score:.3f}" if snippet.score else "n/a"
        lines.append(
            f"[{idx}] Employee: {snippet.employee_name} | Source: {snippet.source} | Score: {score_label}\n"
            f"{snippet.text}"
        )
    return "\n\n".join(lines)


def build_lightweight_citations(snippets: list[RetrievedSnippet], language: str, max_items: int = 3) -> str:
    if not snippets:
        return ""

    unique_items: list[tuple[str, str]] = []
    for snippet in snippets:
        item = (snippet.source, snippet.employee_name)
        if item not in unique_items:
            unique_items.append(item)
        if len(unique_items) >= max_items:
            break

    if not unique_items:
        return ""

    header = "Fontes" if language == "pt" else "Sources"
    lines = [f"\n\n{header}:"]
    for idx, (source, employee_name) in enumerate(unique_items, start=1):
        lines.append(f"[{idx}] {source} ({employee_name})")
    return "\n".join(lines)


def _embed_query(query: str) -> list[float]:
    client = _get_openai_client()
    embedding_model = settings.azure_openai_embedding_deployment
    if client is None or not embedding_model:
        return []

    try:
        response = client.embeddings.create(model=embedding_model, input=[query])
        return response.data[0].embedding
    except Exception:
        return []


def _search_semantic_chunks(query_vector: list[float], intent: str, scoped_names: list[str]) -> list[RetrievedSnippet]:
    vector_literal = str(query_vector)
    scoped_names_param = scoped_names or None

    if intent == "certifications":
        sql = """
            select
                e.full_name as employee_name,
                coalesce(sd.original_filename, c.cert_name, 'certification') as source,
                left(coalesce(cc.chunk_text, c.cert_name, ''), %s) as chunk_text,
                1 - (cc.embedding <=> %s::vector) as score
            from certification_chunks cc
            join certifications c on c.certification_id = cc.certification_id
            join employees e on e.employee_id = c.employee_id
            left join document_versions dv on dv.document_version_id = c.document_version_id
            left join source_documents sd on sd.document_id = dv.document_id
            where (%s::text[] is null or e.full_name = any(%s))
            order by cc.embedding <=> %s::vector
            limit %s
        """
    else:
        sql = """
            select
                e.full_name as employee_name,
                coalesce(sd.original_filename, 'cv') as source,
                left(cc.chunk_text, %s) as chunk_text,
                1 - (cc.embedding <=> %s::vector) as score
            from cv_chunks cc
            join employees e on e.employee_id = cc.employee_id
            left join document_versions dv on dv.document_version_id = cc.document_version_id
            left join source_documents sd on sd.document_id = dv.document_id
            where (%s::text[] is null or e.full_name = any(%s))
            order by cc.embedding <=> %s::vector
            limit %s
        """

    params = [
        MAX_CONTEXT_TEXT_CHARS,
        vector_literal,
        scoped_names_param,
        scoped_names_param,
        vector_literal,
        MAX_CONTEXT_SNIPPETS,
    ]

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
    except Exception:
        return []

    snippets: list[RetrievedSnippet] = []
    for row in rows:
        text = (row.get("chunk_text") or "").strip()
        if not text:
            continue
        snippets.append(
            RetrievedSnippet(
                employee_name=row.get("employee_name") or "Unknown",
                source=row.get("source") or "document",
                text=_clean_context_text(text)[:MAX_CONTEXT_TEXT_CHARS],
                score=float(row.get("score") or 0.0),
            )
        )
    return snippets


def _context_from_rows(rows: Sequence[dict], intent: str) -> list[RetrievedSnippet]:
    snippets: list[RetrievedSnippet] = []
    for row in rows[:MAX_CONTEXT_SNIPPETS]:
        if intent == "certifications":
            text = (
                f"Certification: {row.get('certification_name', 'Unknown')}. "
                f"Vendor: {row.get('vendor', 'Unknown')}. "
                f"Status: {row.get('status', 'unknown')}."
            )
            source = row.get("certification_name") or "certification"
        else:
            text = row.get("snippet") or ""
            source = row.get("source_document") or "cv"

        cleaned = _clean_context_text(text)
        if not cleaned:
            continue

        snippets.append(
            RetrievedSnippet(
                employee_name=row.get("employee_name") or "Unknown",
                source=source,
                text=cleaned[:MAX_CONTEXT_TEXT_CHARS],
                score=0.0,
            )
        )
    return snippets


def _clean_context_text(text: str) -> str:
    raw = (text or "").replace("\n", " ")
    raw = raw.replace("â¢", " ").replace("•", " ")
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = re.sub(r"\b(email|e-mail|telefone|phone|contacto|contact)\s*:\s*[^.]+", "", raw, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", raw).strip()


@lru_cache(maxsize=1)
def _get_openai_client() -> AzureOpenAI | None:
    if not settings.azure_openai_endpoint or not settings.azure_openai_api_key:
        return None
    try:
        return AzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            api_version=settings.azure_openai_api_version,
        )
    except Exception:
        return None
