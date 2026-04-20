from __future__ import annotations

from collections.abc import Sequence
import unicodedata

from psycopg.types.json import Jsonb

from agent_cv.db.connection import get_connection


STORAGE_TERMS = ["dell", "emc", "netapp", "datadomain", "storage", "unity", "vnx"]


def run_query(query: str) -> Sequence[dict]:
    q = normalize_text(query)

    base_sql = """
        select
            e.full_name as employee_name,
            c.cert_name as certification_name,
            coalesce(v.vendor_name, 'Unknown') as vendor,
            c.status,
            c.issue_date,
            c.expiry_date
        from certifications c
        join employees e on e.employee_id = c.employee_id
        left join vendors v on v.vendor_id = c.vendor_id
    """

    where_parts: list[str] = []
    params: list = []

    if "expired" in q or "expirad" in q:
        where_parts.append("c.status = 'expired'")

    if "dell" in q:
        where_parts.append("lower(coalesce(v.vendor_name, '')) like %s")
        params.append("%dell%")

    if "storage" in q or "armazen" in q:
        like_terms = " or ".join(["lower(c.cert_name) like %s" for _ in STORAGE_TERMS])
        vendor_terms = " or ".join(["lower(coalesce(v.vendor_name, '')) like %s" for _ in STORAGE_TERMS])
        where_parts.append(f"({like_terms} or {vendor_terms})")
        params.extend([f"%{x}%" for x in STORAGE_TERMS])
        params.extend([f"%{x}%" for x in STORAGE_TERMS])

    if where_parts:
        base_sql += " where " + " and ".join(where_parts)

    base_sql += " order by e.full_name asc, c.expiry_date nulls last limit 100"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(base_sql, params)
            rows = cur.fetchall()

    return rows


def infer_intent(query: str) -> dict:
    q = normalize_text(query)
    return {
        "expired": "expired" in q or "expirad" in q or "vencid" in q,
        "vendor_dell": "dell" in q,
        "storage": "storage" in q or "armazen" in q,
    }


def audit_query(
    query_text: str,
    query_language: str | None,
    response_language: str,
    result_count: int,
    latency_ms: int,
    normalized_intent: dict | None = None,
    teams_user_id: str | None = None,
) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into query_audit (
                    teams_user_id,
                    query_text,
                    query_language,
                    normalized_intent_json,
                    response_language,
                    result_count,
                    latency_ms
                ) values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    teams_user_id,
                    query_text,
                    query_language,
                    Jsonb(normalized_intent or {}),
                    response_language,
                    result_count,
                    latency_ms,
                ),
            )
        conn.commit()


def normalize_text(text: str) -> str:
    base = unicodedata.normalize("NFKD", text or "")
    no_accents = "".join(ch for ch in base if not unicodedata.combining(ch))
    return no_accents.lower().strip()
