from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import re
import unicodedata

from psycopg.types.json import Jsonb

from agent_cv.db.connection import get_connection


STORAGE_TERMS = ["dell", "emc", "netapp", "datadomain", "storage", "unity", "vnx"]
STORAGE_EXPERIENCE_TERMS = [
    "storage",
    "emc",
    "netapp",
    "datadomain",
    "unity",
    "vnx",
    "san",
    "nas",
    "iscsi",
    "fibre channel",
    "backup",
]
DATACENTER_EXPERIENCE_TERMS = [
    "datacenter",
    "data center",
    "centro de dados",
    "virtualization",
    "vmware",
    "hyper-v",
    "infrastructure",
    "infraestrutura",
    "network",
    "rede",
]

VENDOR_ALIASES: dict[str, tuple[str, ...]] = {
    "redhat": ("red hat", "redhat"),
    "dell": ("dell", "emc"),
    "microsoft": ("microsoft", "ms"),
    "aws": ("aws", "amazon"),
    "google": ("google", "gcp"),
    "cisco": ("cisco",),
    "oracle": ("oracle",),
    "vmware": ("vmware",),
    "ibm": ("ibm",),
}

STOP_TERMS = {
    "who",
    "has",
    "show",
    "list",
    "find",
    "with",
    "about",
    "certification",
    "certifications",
    "certificate",
    "certificates",
    "quem",
    "tem",
    "mostrar",
    "mostre",
    "lista",
    "listar",
    "com",
    "sobre",
    "certificacao",
    "certificacoes",
    "certificado",
    "certificados",
    "para",
    "that",
    "this",
    "those",
    "these",
    "what",
    "does",
    "have",
    "and",
    "the",
    "tell",
    "me",
    "qual",
    "quais",
    "que",
    "dos",
    "das",
    "de",
    "del",
    "do",
    "da",
    "em",
    "how",
    "about",
    "experience",
    "experiences",
    "experiencia",
    "background",
    "resume",
    "curriculum",
    "perfil",
    "role",
    "summary",
}

CERTIFICATION_TERMS = {
    "certification",
    "certifications",
    "certificate",
    "certificates",
    "certificacao",
    "certificacoes",
    "certificado",
    "certificados",
    "expired",
    "expirad",
    "vencid",
    "vendor",
    "issuer",
}

EXPERIENCE_TERMS = {
    "experience",
    "background",
    "resume",
    "cv",
    "curriculum",
    "worked",
    "project",
    "projects",
    "skills",
    "skill",
    "role",
    "summary",
    "experiencia",
    "historico",
    "perfil",
    "carreira",
    "trabalhou",
    "projeto",
    "projetos",
    "competencias",
    "habilidades",
    "funcao",
    "resumo",
}


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


def analyze_query(query: str, preferred_language: str | None = None) -> QueryAnalysis:
    normalized = normalize_text(query)
    tokens = _extract_search_tokens(normalized)
    vendor_terms = _extract_vendor_terms(normalized)
    employee_terms = _extract_employee_terms(tokens, vendor_terms)
    certification_score = _term_score(normalized, CERTIFICATION_TERMS) + (1 if vendor_terms else 0)
    experience_score = _term_score(normalized, EXPERIENCE_TERMS) + (1 if employee_terms else 0)

    if certification_score > experience_score:
        query_type = "certifications"
    elif experience_score > certification_score:
        query_type = "experience"
    elif any(term in normalized for term in ("expired", "expirad", "vencid", "storage", "armazen")):
        query_type = "certifications"
    else:
        query_type = "experience"

    return QueryAnalysis(
        query_type=query_type,
        language=_resolve_query_language(query, preferred_language),
        normalized_query=normalized,
        tokens=tokens,
        vendor_terms=vendor_terms,
        employee_terms=employee_terms,
        expired_only=("expired" in normalized or "expirad" in normalized or "vencid" in normalized),
        active_only=any(term in normalized for term in ("active", "valid", "ativas", "validas", "vigent")),
        storage_only=("storage" in normalized or "armazen" in normalized),
        wants_certification_details=_wants_certification_details(normalized),
        wants_employee_names_only=_wants_employee_names_only(normalized),
        wants_experience_summary=_wants_experience_summary(normalized),
    )


def run_query(query: str, preferred_language: str | None = None) -> tuple[QueryAnalysis, Sequence[dict]]:
    analysis = analyze_query(query, preferred_language)
    if analysis.query_type == "experience":
        return analysis, _run_experience_query(analysis)
    return analysis, _run_certification_query(analysis)


def _run_certification_query(analysis: QueryAnalysis) -> Sequence[dict]:
    q = analysis.normalized_query
    tokens = analysis.tokens

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

    if analysis.expired_only:
        where_parts.append("c.status = 'expired'")

    if analysis.active_only:
        where_parts.append("coalesce(c.status, '') <> 'expired'")

    matched_vendor_terms = analysis.vendor_terms
    if matched_vendor_terms:
        vendor_sql = " or ".join(["lower(coalesce(v.vendor_name, '')) like %s" for _ in matched_vendor_terms])
        where_parts.append(f"({vendor_sql})")
        params.extend([f"%{term}%" for term in matched_vendor_terms])

    if analysis.storage_only:
        like_terms = " or ".join(["lower(c.cert_name) like %s" for _ in STORAGE_TERMS])
        vendor_terms = " or ".join(["lower(coalesce(v.vendor_name, '')) like %s" for _ in STORAGE_TERMS])
        where_parts.append(f"({like_terms} or {vendor_terms})")
        params.extend([f"%{x}%" for x in STORAGE_TERMS])
        params.extend([f"%{x}%" for x in STORAGE_TERMS])

    # If no direct intent matched, fall back to token search across employee/cert/vendor text.
    if not where_parts and tokens:
        token_clauses: list[str] = []
        for token in tokens[:4]:
            token_clauses.append(
                "(" 
                "lower(e.full_name) like %s or "
                "lower(c.cert_name) like %s or "
                "lower(coalesce(v.vendor_name, '')) like %s"
                ")"
            )
            params.extend([f"%{token}%", f"%{token}%", f"%{token}%"])
        where_parts.append(" and ".join(token_clauses))

    if where_parts:
        base_sql += " where " + " and ".join(where_parts)

    base_sql += " order by e.full_name asc, c.expiry_date nulls last limit 100"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(base_sql, params)
            rows = cur.fetchall()

    return rows


def _run_experience_query(analysis: QueryAnalysis) -> Sequence[dict]:
    base_sql = """
        select
            e.full_name as employee_name,
            coalesce(el.localized_headline, cs.section_type, 'summary') as headline,
            left(coalesce(el.localized_summary, cs.section_text, dv.text_snapshot, ''), 700) as snippet,
            sd.original_filename as source_document,
            coalesce(cs.language_code, sd.detected_language, e.primary_language, 'en') as language
        from employees e
        join source_documents sd on sd.employee_id = e.employee_id
        join document_versions dv on dv.document_id = sd.document_id and dv.is_current = true
        left join cv_sections cs on cs.document_version_id = dv.document_version_id
        left join certifications c on c.employee_id = e.employee_id
        left join vendors v on v.vendor_id = c.vendor_id
        left join employee_localizations el
            on el.employee_id = e.employee_id and el.language_code = %s
    """

    params: list[str] = [analysis.language]
    filters: list[str] = [
        "(cs.cv_section_id is not null or lower(sd.original_filename) like %s or lower(sd.original_filename) like %s)"
    ]
    params.extend(["%cv%", "%curriculum%"])

    searchable_tokens = list(analysis.employee_terms or analysis.tokens)
    if analysis.storage_only:
        searchable_tokens.extend(STORAGE_EXPERIENCE_TERMS)
    if any(marker in analysis.normalized_query for marker in ("data center", "data centers", "datacenter", "centro de dados")):
        searchable_tokens.extend(DATACENTER_EXPERIENCE_TERMS)

    dedup_tokens: list[str] = []
    for token in searchable_tokens:
        if token not in dedup_tokens:
            dedup_tokens.append(token)

    order_by = "e.full_name asc"
    combined_text_expr = (
        "lower(" 
        "coalesce(el.localized_summary, cs.section_text, dv.text_snapshot, '') || ' ' || "
        "coalesce(c.cert_name, '') || ' ' || coalesce(v.vendor_name, '')"
        ")"
    )

    if analysis.vendor_terms:
        vendor_clauses: list[str] = []
        for vendor_term in analysis.vendor_terms:
            vendor_clauses.append(f"{combined_text_expr} like %s")
            params.append(f"%{vendor_term}%")
        filters.append("(" + " or ".join(vendor_clauses) + ")")

    if dedup_tokens:
        token_clauses: list[str] = []
        score_parts: list[str] = []
        for token in dedup_tokens[:8]:
            token_clauses.append(
                "(" 
                "lower(e.full_name) like %s or "
                f"{combined_text_expr} like %s or "
                "lower(coalesce(el.localized_headline, cs.section_type, '')) like %s"
                ")"
            )
            params.extend([f"%{token}%", f"%{token}%", f"%{token}%"])
            score_parts.append(
                "case when lower(e.full_name) like %s then 6 else 0 end + "
                "case when lower(coalesce(el.localized_headline, cs.section_type, '')) like %s then 3 else 0 end + "
                f"case when {combined_text_expr} like %s then 1 else 0 end"
            )
            params.extend([f"%{token}%", f"%{token}%", f"%{token}%"])
        filters.append("(" + " or ".join(token_clauses) + ")")
        order_by = "(" + " + ".join(score_parts) + ") desc, e.full_name asc"

    base_sql += " where " + " and ".join(filters)
    base_sql += f" order by {order_by} limit 25"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(base_sql, params)
            rows = cur.fetchall()

    return rows


def infer_intent(query: str) -> dict:
    analysis = analyze_query(query)
    return {
        "query_type": analysis.query_type,
        "language": analysis.language,
        "expired": analysis.expired_only,
        "active": analysis.active_only,
        "vendors": analysis.vendor_terms,
        "storage": analysis.storage_only,
        "employee_terms": analysis.employee_terms,
        "wants_certification_details": analysis.wants_certification_details,
        "wants_employee_names_only": analysis.wants_employee_names_only,
        "wants_experience_summary": analysis.wants_experience_summary,
    }


def audit_query(
    query_text: str,
    query_language: str | None,
    response_language: str,
    result_count: int,
    latency_ms: int,
    normalized_intent: dict | None = None,
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
                    normalized_intent_json,
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


def _extract_vendor_terms(query: str) -> list[str]:
    terms: list[str] = []
    for _, aliases in VENDOR_ALIASES.items():
        for alias in aliases:
            if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", query) and alias not in terms:
                terms.append(alias)
    return terms


def _extract_search_tokens(query: str) -> list[str]:
    parts = [x for x in query.replace("?", " ").replace("!", " ").replace(",", " ").split() if x]
    return [x for x in parts if len(x) >= 3 and x not in STOP_TERMS]


def _extract_employee_terms(tokens: list[str], vendor_terms: list[str]) -> list[str]:
    vendor_parts = {part for term in vendor_terms for part in term.split()}
    return [token for token in tokens if token not in vendor_parts and token not in STORAGE_TERMS]


def _term_score(query: str, candidates: set[str]) -> int:
    return sum(1 for term in candidates if term in query)


def _resolve_query_language(query: str, preferred_language: str | None = None) -> str:
    if preferred_language in {"pt", "en"}:
        return preferred_language

    normalized = normalize_text(query)
    portuguese_markers = {
        "quem",
        "tem",
        "mostrar",
        "mostre",
        "experiencia",
        "certificacoes",
        "projetos",
        "habilidades",
        "curriculo",
        "perfil",
        "resumo",
    }
    english_markers = {
        "who",
        "show",
        "experience",
        "background",
        "certifications",
        "projects",
        "skills",
        "resume",
        "profile",
        "summary",
    }
    pt_score = sum(1 for term in portuguese_markers if term in normalized)
    en_score = sum(1 for term in english_markers if term in normalized)
    return "pt" if pt_score > en_score else "en"


def _wants_certification_details(normalized_query: str) -> bool:
    detail_markers = {
        "which certifications",
        "what certifications",
        "certification names",
        "list certifications",
        "show certifications",
        "certification details",
        "which certs",
        "list certs",
        "quais certificacoes",
        "listar certificacoes",
        "mostrar certificacoes",
        "detalhes das certificacoes",
        "nomes das certificacoes",
        "quais certificados",
    }
    return any(marker in normalized_query for marker in detail_markers)


def _wants_employee_names_only(normalized_query: str) -> bool:
    name_markers = {
        "who has",
        "who have",
        "which employees",
        "which people",
        "quais colaboradores",
        "quem tem",
        "quem possui",
        "quais pessoas",
    }
    return any(marker in normalized_query for marker in name_markers)


def _wants_experience_summary(normalized_query: str) -> bool:
    summary_markers = {
        "resumo",
        "resuma",
        "sumario",
        "sumarize",
        "summarize",
        "summary",
        "resumir",
    }
    return any(marker in normalized_query for marker in summary_markers)
