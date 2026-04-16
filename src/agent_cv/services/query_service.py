from __future__ import annotations

from collections.abc import Sequence

from agent_cv.db.connection import get_connection


STORAGE_TERMS = ["dell", "emc", "netapp", "datadomain", "storage", "unity", "vnx"]


def run_query(query: str) -> Sequence[dict]:
    q = query.lower().strip()

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
