from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from threading import Lock
from typing import Any

import httpx
from openai import AzureOpenAI

from agent_cv.config import settings
from agent_cv.db.connection import get_connection
from agent_cv.services.query_service import QueryAnalysis, normalize_text

logger = logging.getLogger(__name__)

MAX_AGENT_ITERATIONS = 5

# ------------------------------------------------------------------ #
# Tool schema                                                          #
# ------------------------------------------------------------------ #

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_certifications",
            "description": (
                "Search the employee certifications database by technology, vendor, or keyword. "
                "Returns employees with matching certifications including name, vendor, status, and dates. "
                "Use this whenever the user asks who has certifications in a specific area. "
                "Supports pagination: use 'offset' to fetch the next page of results (page size is 15)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Technology, vendor, or certification name (e.g. 'Azure', 'Red Hat', 'CCNA', 'AZ-900')",
                    },
                    "employee_name": {
                        "type": "string",
                        "description": "Optional: restrict to a specific employee",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["active", "expired", "any"],
                        "description": "Filter by certificate status. Default: 'any'.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of results to skip for pagination. Default: 0 (first page). Use 15 for the second page, 30 for the third, etc.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_experience",
            "description": (
                "Search employee CV documents for work experience, skills, and professional background. "
                "Returns relevant excerpts. Use this for experience/skill queries, "
                "or to complement a certification search with evidence from CVs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Skill, technology, or domain (e.g. 'cybersecurity', 'cloud infrastructure', 'project management')",
                    },
                    "employee_name": {
                        "type": "string",
                        "description": "Optional: restrict to a specific employee",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_employee_profile",
            "description": (
                "Get the full profile for a specific employee, including all certifications and CV summary. "
                "Use when the user asks about a specific person by name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_name": {
                        "type": "string",
                        "description": "Full or partial name of the employee",
                    }
                },
                "required": ["employee_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_employees",
            "description": "Return a list of all employees in the system. Use when the user wants to know who is available.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_employee_cv_link",
            "description": (
                "Get the SharePoint link to an employee's CV document. "
                "Use when the user asks to see, open, or share a specific employee's CV."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_name": {
                        "type": "string",
                        "description": "Full or partial name of the employee",
                    }
                },
                "required": ["employee_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Search the internet for information about certifications, technologies, or vendors. "
                "Useful to verify whether a technology relates to a domain, or to describe what a certification covers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Web search query",
                    }
                },
                "required": ["query"],
            },
        },
    },
]

# ------------------------------------------------------------------ #
# System prompts                                                       #
# ------------------------------------------------------------------ #

_SYSTEM_PROMPT_EN = """\
You are Agent CV, an intelligent HR assistant for a technology company.
You have access to a database of employee certifications and CV documents.
Your job is to help HR managers and team leads find employees with specific skills, certifications, or experience.

TOOLS AVAILABLE:
• search_certifications — find employee certifications by technology, vendor, or keyword
• search_experience — search employee CVs for work experience and skills
• get_employee_profile — retrieve the full profile for a specific employee
• list_employees — list all employees in the system
• get_employee_cv_link — get the SharePoint link to an employee's CV document
• search_web — look up external information about certifications or technologies

HOW TO RESPOND:
1. For any data query, use tools FIRST — never answer from memory or make assumptions
2. If a search returns no results, try alternate keywords (e.g. "AZ-900" for Azure, "RHCE" for Red Hat, "SC-200" for security)
3. For broad topics like "cybersecurity" or "cloud", search BOTH certifications AND experience
4. Respond in the SAME LANGUAGE as the user's message
5. Write in a conversational, helpful tone — like a knowledgeable colleague
6. FORMATTING — structure your replies clearly:
   - Group results by employee: use the employee name as a bold header (e.g. "**Name**")
   - Under each employee, list each certification as a top-level bullet: "- Certification Name"
   - Under each certification bullet, add indented sub-bullets (2 spaces + dash) for its details:
       "  - Vendor: Microsoft"
       "  - Issued: 2020-06-23"
       "  - Expires: 2025-06-23 (estimated)"
       "  - Status: expired"
   - Separate each employee block with a blank line
7. Be specific: list names and certification titles when found; avoid unnecessary hedging
8. End your reply with 1-2 relevant follow-up suggestions the user might find useful — keep them concise
9. If tools return no data, say clearly what you searched for and suggest alternatives
10. ALWAYS write exclusively in Latin script — never output characters from Georgian, Arabic, Cyrillic, Greek, or any other non-Latin alphabet, even as abbreviations or parenthetical notes
11. When the user asks to see or share an employee's CV, call get_employee_cv_link and include the returned URL as a plain hyperlink in your reply
12. When answering questions about expired or expiring certifications, ALWAYS call search_certifications with status="any" — never pre-filter to "expired" or "expiring" in the tool call. Filter and categorise in your answer instead, to avoid incomplete results.
13. Certification results include an "inferred_expiry_date" field for records where the expiry date was not registered. If this field is a date (not null or "unknown"), use it as an estimated expiry and clearly label it as "estimated" or "inferred" in your answer. If it is null, the cert does not expire. If it is "unknown", you cannot infer an expiry date for that record.
14. PAGINATION — search_certifications returns 15 results per page. When has_more=true in the result, tell the user how many results remain and offer to show more. When the user asks to see more results (e.g. "show more", "next", "ver mais"), call search_certifications again with the same query/status and offset=next_offset from the previous result.
"""

_SYSTEM_PROMPT_PT = """\
És o Agent CV, um assistente de RH inteligente para uma empresa de tecnologia.
Tens acesso a uma base de dados de certificações e documentos de CV dos colaboradores.
O teu trabalho é ajudar gestores de RH e líderes de equipa a encontrar colaboradores com competências, certificações ou experiência específicas.

FERRAMENTAS DISPONÍVEIS:
• search_certifications — encontrar certificações por tecnologia, fornecedor ou palavra-chave
• search_experience — pesquisar CVs por experiência profissional e competências
• get_employee_profile — obter o perfil completo de um colaborador específico
• list_employees — listar todos os colaboradores no sistema
• get_employee_cv_link — obter o link do SharePoint para o CV de um colaborador
• search_web — pesquisar informação externa sobre certificações ou tecnologias

COMO RESPONDER:
1. Para qualquer pergunta sobre dados, usa as ferramentas PRIMEIRO — nunca adivinhes
2. Se uma pesquisa não tiver resultados, tenta palavras-chave alternativas (ex: "AZ-900" para Azure, "RHCE" para Red Hat, "SC-200" para segurança)
3. Para tópicos amplos como "cibersegurança" ou "cloud", pesquisa TANTO em certificações COMO em experiência
4. Responde sempre no MESMO IDIOMA da mensagem do utilizador
5. Escreve num tom conversacional e útil — como um colega experiente
6. FORMATAÇÃO — estrutura as respostas de forma clara:
   - Agrupa os resultados por colaborador: usa o nome como cabeçalho em negrito (ex: "**Nome**")
   - Para cada colaborador, lista cada certificação como um marcador de primeiro nível: "- Nome da Certificação"
   - Sob cada marcador de certificação, adiciona sub-marcadores indentados (2 espaços + traço) com os detalhes:
       "  - Fornecedor: Microsoft"
       "  - Emissão: 2020-06-23"
       "  - Validade: 2025-06-23 (estimada)"
       "  - Estado: expired"
   - Separa cada bloco de colaborador com uma linha em branco
7. Sê específico: lista nomes e títulos de certificações quando encontrados; não sejas desnecessariamente cauteloso
8. Termina a resposta com 1-2 sugestões de perguntas de seguimento relevantes — mantém-nas concisas
9. Se as ferramentas não retornarem dados, diz claramente o que pesquisaste e sugere alternativas
10. Escreve SEMPRE exclusivamente em alfabeto latino — nunca uses caracteres do alfabeto georgiano, árabe, cirílico, grego ou qualquer outro alfabeto não-latino, mesmo em abreviaturas ou notas
11. Quando o utilizador pedir para ver ou partilhar o CV de um colaborador, usa get_employee_cv_link e inclui o URL retornado como hiperligação na tua resposta
12. Quando responderes a perguntas sobre certificações vencidas ou a vencer, usa SEMPRE search_certifications com status="any" — nunca pré-filtres para "expired" ou "expiring" na chamada da ferramenta. Filtra e categoriza na tua resposta, para evitar resultados incompletos.
13. Os resultados de certificações incluem um campo "inferred_expiry_date" para registos em que a data de validade não foi registada. Se este campo contiver uma data (não nulo nem "unknown"), usa-a como validade estimada e indica claramente que é uma estimativa na tua resposta. Se for nulo, a certificação não expira. Se for "unknown", não é possível inferir a data de validade desse registo.
14. PAGINAÇÃO — search_certifications devolve 15 resultados por página. Quando has_more=true no resultado, informa o utilizador de quantos resultados restam e oferece mostrar mais. Quando o utilizador pedir para ver mais resultados (ex: "mostrar mais", "ver mais", "próximos"), chama search_certifications novamente com o mesmo query/status e offset=next_offset do resultado anterior.
"""


# ------------------------------------------------------------------ #
# Public types                                                         #
# ------------------------------------------------------------------ #


@dataclass
class ConversationState:
    language: str
    history: list[tuple[str, str]]


@dataclass(frozen=True)
class AgentQueryResult:
    language: str
    summary: str
    answer: str
    total_results: int
    shown_results: int
    has_more: bool
    analysis: QueryAnalysis
    rows_page: list[dict] = field(default_factory=list)
    show_certification_details: bool = False
    tool_calls_log: list[dict] = field(default_factory=list)


# ------------------------------------------------------------------ #
# In-memory conversation store                                         #
# ------------------------------------------------------------------ #

_STATE: dict[str, ConversationState] = {}
_STATE_LOCK = Lock()


# ------------------------------------------------------------------ #
# Entry point                                                          #
# ------------------------------------------------------------------ #


def handle_user_query(
    query_text: str,
    preferred_language: str | None,
    conversation_id: str | None,
) -> AgentQueryResult:
    state_key = (conversation_id or "default").strip() or "default"
    with _STATE_LOCK:
        prior = _STATE.get(state_key)

    language = preferred_language or _detect_language(query_text, prior)
    logger.info(
        "Agent: handling query (lang=%s, conv=%s) — %.80s", language, state_key, query_text
    )
    client = _get_chat_client()
    if client is None or not settings.azure_openai_chat_deployment:
        logger.warning(
            "Agent: no LLM client (endpoint=%s, deployment=%s) — using fallback",
            bool(settings.azure_openai_endpoint),
            settings.azure_openai_chat_deployment or "<unset>",
        )
        answer = _no_llm_fallback(language)
        _save_state(state_key, language, query_text, answer, prior)
        return AgentQueryResult(
            language=language,
            summary="",
            answer=answer,
            total_results=0,
            shown_results=0,
            has_more=False,
            analysis=_stub_analysis(language),
            tool_calls_log=[],
        )

    system_prompt = _SYSTEM_PROMPT_PT if language == "pt" else _SYSTEM_PROMPT_EN
    messages: list[dict] = [{"role": "system", "content": system_prompt}]

    if prior:
        for past_user, past_assistant in prior.history[-6:]:
            if past_user:
                messages.append({"role": "user", "content": past_user})
            if past_assistant:
                messages.append({"role": "assistant", "content": past_assistant})

    messages.append({"role": "user", "content": query_text})

    tool_call_count = 0
    tool_calls_log: list[dict] = []
    answer = ""
    for iteration in range(MAX_AGENT_ITERATIONS):
        logger.debug("Agent: LLM call iteration %d (messages=%d)", iteration, len(messages))
        try:
            response = client.chat.completions.create(
                model=settings.azure_openai_chat_deployment,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.3,
                max_completion_tokens=2048,
            )
        except Exception:
            logger.exception("Agent loop: LLM call failed on iteration %d", iteration)
            break

        msg = response.choices[0].message
        finish_reason = getattr(response.choices[0], "finish_reason", None)
        logger.debug(
            "Agent: iteration %d — finish_reason=%s, tool_calls=%s",
            iteration,
            finish_reason,
            len(msg.tool_calls) if msg.tool_calls else 0,
        )
        if not msg.tool_calls:
            answer = (msg.content or "").strip()
            logger.info(
                "Agent: final answer produced on iteration %d (%d chars)", iteration, len(answer)
            )
            break

        # Append assistant turn with tool calls
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ],
        })

        # Execute each requested tool
        for tool_call in msg.tool_calls:
            tool_call_count += 1
            try:
                args = json.loads(tool_call.function.arguments)
            except Exception:
                args = {}
            logger.debug("Agent: calling tool %s with args %s", tool_call.function.name, args)
            result = _dispatch_tool(tool_call.function.name, args)
            result_count = (
                result.get("total_found", 0)
                if isinstance(result, dict)
                else 0
            )
            logger.debug(
                "Agent: tool %s returned %d results", tool_call.function.name, result_count
            )
            tool_calls_log.append({
                "tool": tool_call.function.name,
                "args": args,
                "result_count": result_count,
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })

    if not answer:
        logger.warning(
            "Agent: loop ended with no answer after %d tool call(s) — returning fallback",
            tool_call_count,
        )
        answer = (
            "Não foi possível processar a sua pergunta. Por favor tente novamente."
            if language == "pt"
            else "Unable to process your query. Please try again."
        )

    _save_state(state_key, language, query_text, answer, prior)

    return AgentQueryResult(
        language=language,
        summary="",
        answer=answer,
        total_results=tool_call_count,
        shown_results=0,
        has_more=False,
        analysis=_stub_analysis(language),
        tool_calls_log=tool_calls_log,
    )


# ------------------------------------------------------------------ #
# Tool dispatcher                                                      #
# ------------------------------------------------------------------ #


def _dispatch_tool(name: str, args: dict) -> Any:
    try:
        if name == "search_certifications":
            return _tool_search_certifications(
                **{k: v for k, v in args.items() if k in ("query", "employee_name", "status", "offset")}
            )
        if name == "search_experience":
            return _tool_search_experience(
                **{k: v for k, v in args.items() if k in ("query", "employee_name")}
            )
        if name == "get_employee_profile":
            return _tool_get_employee_profile(args.get("employee_name", ""))
        if name == "list_employees":
            return _tool_list_employees()
        if name == "get_employee_cv_link":
            return _tool_get_employee_cv_link(args.get("employee_name", ""))
        if name == "search_web":
            return _tool_search_web(args.get("query", ""))
    except Exception:
        logger.exception("Agent: tool %s raised an exception with args %s", name, args)
        return {"error": f"Tool '{name}' failed unexpectedly"}
    logger.warning("Agent: unknown tool requested: %s", name)
    return {"error": f"Unknown tool: {name}"}


# ------------------------------------------------------------------ #
# Tool implementations                                                 #
# ------------------------------------------------------------------ #

# Certification validity periods by vendor/cert keyword (months).
# None = cert does not expire; omitted entries = cannot infer.
# Ordered from most-specific to least-specific so the first match wins.
_CERT_VALIDITY: list[tuple[str, int | None]] = [
    # Certs that do not expire
    ("itil foundation", None),
    ("itil 4 foundation", None),
    ("prince2 foundation", None),
    # Microsoft – 1 year (all role-based and specialty certs since 2020)
    # Fundamentals (az-900, ms-900, sc-900, dp-900) also expire after 1 yr
    ("microsoft", 12),
    ("azure", 12),
    # AWS – 3 years
    ("aws certified", 36),
    ("aws", 36),
    ("amazon web services", 36),
    # Red Hat – 3 years
    ("red hat", 36),
    ("rhce", 36),
    ("rhcsa", 36),
    ("rhcva", 36),
    # Cisco – 3 years
    ("cisco", 36),
    ("ccna", 36),
    ("ccnp", 36),
    ("ccie", 36),
    ("cct", 36),
    # CompTIA – 3 years (Continuing Education)
    ("comptia", 36),
    ("security+", 36),
    ("network+", 36),
    ("cysa+", 36),
    ("pentest+", 36),
    ("casp+", 36),
    # Google Cloud – 2 years
    ("google cloud", 24),
    ("gcp", 24),
    # Kubernetes / CNCF – 2 years
    ("cka", 24),
    ("ckad", 24),
    ("cks", 24),
    # VMware – 2 years
    ("vmware", 24),
    ("vcp", 24),
    # Fortinet – 2 years
    ("fortinet", 24),
    ("nse ", 24),  # trailing space avoids matching "nsec" etc.
    # Palo Alto Networks – 2 years
    ("palo alto", 24),
    ("pcnsa", 24),
    ("pcnse", 24),
    # HashiCorp – 2 years
    ("hashicorp", 24),
    ("terraform associate", 24),
    # PMI – 3 years
    ("pmp", 36),
    ("pmi", 36),
    # ISACA – 3 years
    ("cism", 36),
    ("cisa", 36),
    ("crisc", 36),
    # ISC2 – 3 years
    ("cissp", 36),
    ("ccsp", 36),
    ("sscp", 36),
]


def _infer_expiry_date(cert_name: str, vendor: str, issue_date: object) -> object:
    """Return an inferred expiry date (datetime.date) or None (cert does not expire).

    Returns the sentinel string ``"unknown"`` when no rule matches and inference
    is not possible — callers should propagate this distinction to the LLM.
    """
    import datetime

    if issue_date is None:
        return "unknown"

    haystack = f"{cert_name} {vendor}".lower()

    for keyword, months in _CERT_VALIDITY:
        if keyword in haystack:
            if months is None:
                return None  # does not expire
            # dateutil not guaranteed; use manual month arithmetic
            if isinstance(issue_date, (datetime.date, datetime.datetime)):
                base = issue_date if isinstance(issue_date, datetime.date) else issue_date.date()
            else:
                try:
                    base = datetime.date.fromisoformat(str(issue_date)[:10])
                except ValueError:
                    return "unknown"
            # Add months manually to avoid dateutil dependency
            month = base.month + months
            year = base.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            try:
                return datetime.date(year, month, base.day)
            except ValueError:
                # Handle e.g. Feb 29 → Feb 28
                return datetime.date(year, month, 28)

    return "unknown"  # no rule matched


_PAGE_SIZE = 15


def _tool_search_certifications(
    query: str,
    employee_name: str | None = None,
    status: str = "any",
    offset: int = 0,
) -> dict:
    from agent_cv.services.retrieval_service import _embed_query, _search_semantic_chunks

    structured = _sql_search_certifications(query, employee_name, status)
    total = len(structured)
    page = structured[offset: offset + _PAGE_SIZE]

    query_vector = _embed_query(query)
    semantic_excerpts: list[dict] = []
    if query_vector:
        scoped = [employee_name] if employee_name else []
        for s in _search_semantic_chunks(query_vector, "certifications", scoped):
            semantic_excerpts.append({
                "employee_name": s.employee_name,
                "source": s.source,
                "relevance_score": round(s.score, 3),
                "excerpt": s.text[:300],
            })

    result: dict = {
        "certifications": page,
        "semantic_excerpts": semantic_excerpts[:5],
        "total_found": total,
        "offset": offset,
        "page_size": _PAGE_SIZE,
    }
    if offset + _PAGE_SIZE < total:
        result["has_more"] = True
        result["next_offset"] = offset + _PAGE_SIZE
        result["note"] = (
            f"Showing results {offset + 1}–{offset + len(page)} of {total}. "
            f"Call again with offset={offset + _PAGE_SIZE} to get the next page."
        )
    else:
        result["has_more"] = False
        result["note"] = f"Showing results {offset + 1}–{offset + len(page)} of {total}. All results shown."
    return result


def _sql_search_certifications(
    query: str,
    employee_name: str | None,
    status: str,
) -> list[dict]:
    # Words that describe intent but don't identify a specific cert or vendor.
    # When the query is made up entirely of these, skip keyword filtering so
    # the tool returns ALL certs matching the status/employee filters — not a
    # random subset that happens to contain those words in a cert name.
    _GENERIC_TOKENS = {
        "all", "any", "expired", "expiring", "active", "certification",
        "certifications", "certificacao", "certificacoes", "certificate",
        "certificates", "list", "show", "give", "get", "find", "search",
        "employee", "employees", "colaborador", "colaboradores", "todas",
        "todos", "vencidas", "vencidos", "validade", "status", "with", "has",
        "that", "are", "the", "and", "for", "por", "com", "que", "dos", "das",
    }

    norm = normalize_text(query)
    raw_tokens = [t for t in norm.split() if len(t) >= 3][:6]
    # Only use tokens that are likely cert/vendor/tech names, not generic words
    tokens = [t for t in raw_tokens if t not in _GENERIC_TOKENS]

    where_parts: list[str] = []
    params: list[Any] = []

    if employee_name:
        where_parts.append("lower(e.full_name) like %s")
        params.append(f"%{normalize_text(employee_name)}%")

    if status == "active":
        where_parts.append("c.status <> 'expired'")
    elif status == "expired":
        where_parts.append("c.status = 'expired'")

    if tokens:
        token_clauses: list[str] = []
        for token in tokens:
            token_clauses.append(
                "(lower(c.cert_name) like %s "
                "or lower(coalesce(v.vendor_name, '')) like %s "
                "or lower(e.full_name) like %s)"
            )
            params.extend([f"%{token}%", f"%{token}%", f"%{token}%"])
        where_parts.append("(" + " or ".join(token_clauses) + ")")

    sql = """
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
    if where_parts:
        sql += " where " + " and ".join(where_parts)
    sql += " order by e.full_name, c.expiry_date nulls last limit 200"

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = [dict(row) for row in cur.fetchall()]
    except Exception:
        logger.exception("Agent: _sql_search_certifications failed")
        return []

    # Enrich rows that have no expiry_date with an inferred estimate
    for row in rows:
        if row.get("expiry_date") is None and row.get("issue_date") is not None:
            inferred = _infer_expiry_date(
                row.get("certification_name", ""),
                row.get("vendor", ""),
                row["issue_date"],
            )
            row["inferred_expiry_date"] = inferred  # None means "does not expire"
            row["expiry_date_is_inferred"] = inferred is not None
        else:
            row["inferred_expiry_date"] = None
            row["expiry_date_is_inferred"] = False

    return rows


def _tool_search_experience(
    query: str,
    employee_name: str | None = None,
) -> dict:
    from agent_cv.services.retrieval_service import _embed_query, _search_semantic_chunks

    query_vector = _embed_query(query)
    if not query_vector:
        return {"experience_snippets": [], "total_found": 0, "note": "Embedding unavailable"}

    scoped = [employee_name] if employee_name else []
    snippets = _search_semantic_chunks(query_vector, "experience", scoped)

    return {
        "experience_snippets": [
            {
                "employee_name": s.employee_name,
                "source": s.source,
                "relevance_score": round(s.score, 3),
                "excerpt": s.text[:400],
            }
            for s in snippets
        ],
        "total_found": len(snippets),
    }


def _tool_get_employee_profile(employee_name: str) -> dict:
    norm = normalize_text(employee_name)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select employee_id, full_name, primary_language, department "
                    "from employees where lower(full_name) like %s limit 3",
                    (f"%{norm}%",),
                )
                employees = cur.fetchall()
                if not employees:
                    return {"error": f"No employee found matching '{employee_name}'"}

                emp = employees[0]
                emp_id = emp["employee_id"]

                cur.execute(
                    """
                    select c.cert_name, coalesce(v.vendor_name, 'Unknown') as vendor,
                           c.status, c.issue_date, c.expiry_date
                    from certifications c
                    left join vendors v on v.vendor_id = c.vendor_id
                    where c.employee_id = %s
                    order by c.expiry_date nulls last
                    """,
                    (emp_id,),
                )
                certs = [dict(r) for r in cur.fetchall()]

                cur.execute(
                    """
                    select cs.section_type, left(cs.section_text, 500) as section_text
                    from cv_sections cs
                    join document_versions dv on dv.document_version_id = cs.document_version_id
                    join source_documents sd on sd.document_id = dv.document_id
                    where sd.employee_id = %s and dv.is_current = true
                    order by cs.section_type
                    limit 10
                    """,
                    (emp_id,),
                )
                sections = [dict(r) for r in cur.fetchall()]

        return {
            "employee": {
                "name": emp["full_name"],
                "department": emp.get("department"),
                "language": emp.get("primary_language"),
            },
            "certifications": certs,
            "cv_sections": sections,
            "other_matches": [e["full_name"] for e in employees[1:]],
        }
    except Exception:
        logger.exception("Agent: get_employee_profile failed for '%s'", employee_name)
        return {"error": "Failed to retrieve employee profile"}


def _tool_list_employees() -> dict:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select full_name, department, primary_language from employees order by full_name"
                )
                rows = cur.fetchall()
        return {"employees": [dict(r) for r in rows], "total": len(rows)}
    except Exception:
        logger.exception("Agent: list_employees failed")
        return {"employees": [], "total": 0}


def _tool_get_employee_cv_link(employee_name: str) -> dict:
    """Return SharePoint URLs for a specific employee's CV documents."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.full_name, sd.original_filename, sd.sharepoint_web_url
                FROM source_documents sd
                JOIN employees e ON sd.employee_id = e.employee_id
                WHERE e.full_name ILIKE %s
                  AND sd.sharepoint_web_url IS NOT NULL
                ORDER BY sd.created_at DESC
                """,
                (f"%{employee_name}%",),
            )
            rows = cur.fetchall()

    if rows:
        return {
            "found": True,
            "documents": [
                {
                    "employee": r["full_name"],
                    "filename": r["original_filename"],
                    "url": r["sharepoint_web_url"],
                }
                for r in rows
            ],
        }
    return {
        "found": False,
        "message": (
            f"No SharePoint CV link found for '{employee_name}'. "
            "The document may not have been ingested from SharePoint yet."
        ),
    }


def _tool_search_web(query: str) -> dict:
    url = "https://api.duckduckgo.com/"
    params = {"q": query, "format": "json", "no_redirect": 1, "skip_disambig": 1}
    try:
        with httpx.Client(timeout=6.0) as http:
            response = http.get(url, params=params)
            response.raise_for_status()
            body = response.json()
    except Exception:
        return {"results": [], "error": "Web search unavailable"}

    hits: list[str] = []
    abstract = (body.get("AbstractText") or "").strip()
    heading = (body.get("Heading") or "").strip()
    if abstract:
        hits.append(f"{heading}: {abstract}" if heading else abstract)
    for item in body.get("RelatedTopics") or []:
        if isinstance(item, dict):
            text = (item.get("Text") or "").strip()
            if text:
                hits.append(text)
        if len(hits) >= 5:
            break
    return {"results": hits[:5]}


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _detect_language(query: str, prior: ConversationState | None) -> str:
    norm = normalize_text(query)
    pt_score = sum(
        1 for m in {"quem", "tem", "qual", "quais", "certificacoes", "experiencia",
                    "colaborador", "ola", "obrigado", "inclua", "incluir", "pessoas",
                    "vencidas", "vencer", "perto", "mostra", "mostrar", "lista", "quero"}
        if m in norm
    )
    en_score = sum(
        1 for m in {"who", "has", "which", "what", "certifications", "experience",
                    "employee", "hello", "thanks", "include", "show", "list", "expired",
                    "expiring", "soon", "find", "search"}
        if m in norm
    )
    if pt_score > en_score:
        return "pt"
    if en_score > pt_score:
        return "en"
    # Ambiguous — keep prior language if available, else default to "en"
    return prior.language if prior else "en"


def _no_llm_fallback(language: str) -> str:
    if language == "pt":
        return "O serviço de linguagem não está configurado. Verifique as variáveis AZURE_OPENAI_* no ficheiro .env."
    return "Language service is not configured. Please check AZURE_OPENAI_* environment variables."


def _stub_analysis(language: str) -> QueryAnalysis:
    return QueryAnalysis(
        query_type="chat",
        language=language,
        normalized_query="",
        tokens=[],
        vendor_terms=[],
        employee_terms=[],
        expired_only=False,
        active_only=False,
        storage_only=False,
        wants_certification_details=False,
        wants_employee_names_only=False,
        wants_experience_summary=False,
    )


def _save_state(
    state_key: str,
    language: str,
    user_msg: str,
    assistant_msg: str,
    prior: ConversationState | None,
) -> None:
    history = list(prior.history if prior else [])
    history.append((user_msg.strip(), assistant_msg.strip()))
    with _STATE_LOCK:
        _STATE[state_key] = ConversationState(
            language=language,
            history=history[-8:],
        )


@lru_cache(maxsize=1)
def _get_chat_client() -> AzureOpenAI | None:
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
