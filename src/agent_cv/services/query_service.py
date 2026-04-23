from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from agent_cv.db.connection import get_connection


# ------------------------------------------------------------------ #
# QueryAnalysis dataclass — kept for backward-compat with routes and  #
# the stub analysis returned by AgentQueryResult.                     #
# ------------------------------------------------------------------ #

@dataclass(frozen=True)
class QueryAnalysis:
    query_type: str
    language: str
    normalized_query: str
    tokens: list[str]
    vendor_terms: list[str]
    employee_terms: list[str]
    expired_only: bool
    active_only: bool
    storage_only: bool
    wants_certification_details: bool
    wants_employee_names_only: bool
    wants_experience_summary: bool


# ------------------------------------------------------------------ #
# Utilities                                                            #
# ------------------------------------------------------------------ #


def normalize_text(text: str) -> str:
    base = unicodedata.normalize("NFKD", text or "")
    no_accents = "".join(ch for ch in base if not unicodedata.combining(ch))
    return no_accents.lower().strip()


def infer_intent(query: str) -> dict:
    """Minimal fallback used when no agent tool-call data is available (e.g. REST /query endpoint)."""
    norm = normalize_text(query)
    pt_markers = {"quem", "tem", "qual", "quais", "certificacoes", "experiencia", "colaborador"}
    en_markers = {"who", "has", "which", "what", "certifications", "experience", "employee"}
    pt_score = sum(1 for m in pt_markers if m in norm)
    en_score = sum(1 for m in en_markers if m in norm)
    language = "pt" if pt_score > en_score else "en"
    return {"language": language, "normalized_query": norm}


# ------------------------------------------------------------------ #
# Audit logging                                                        #
# ------------------------------------------------------------------ #


def audit_query(
    query_text: str,
    query_language: str | None,
    response_language: str,
    result_count: int,
    latency_ms: int,
    agent_tool_calls: list | None = None,
    aad_object_id: str | None = None,
    chat_id: str | None = None,
) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into query_audit (
                    aad_object_id,
                    chat_id,
                    query_text,
                    query_language,
                    agent_tool_calls,
                    response_language,
                    result_count,
                    latency_ms
                ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    aad_object_id,
                    chat_id,
                    query_text,
                    query_language,
                    Jsonb(agent_tool_calls or []),
                    response_language,
                    result_count,
                    latency_ms,
                ),
            )
        conn.commit()

