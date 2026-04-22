from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from threading import Lock
import re

import httpx
from openai import AzureOpenAI

from agent_cv.config import settings
from agent_cv.services.query_service import QueryAnalysis, run_query
from agent_cv.services.response_service import build_summary


FOLLOW_UP_MORE_MARKERS = {
    "show more",
    "more results",
    "next results",
    "next page",
    "remaining results",
    "demais resultados",
    "mais resultados",
    "proximos resultados",
    "mostrar mais",
    "continuar",
    "continue",
}

PAGE_SIZE = 10
NAMES_PAGE_SIZE = 10
WEB_SEARCH_MARKERS = {
    "search the web",
    "web search",
    "look on the web",
    "internet search",
    "pesquise na web",
    "pesquise na internet",
    "procure na web",
    "buscar na internet",
}

GREETING_PATTERNS = [
    r"^\s*(oi|ola|olá|hello|hi|hey|bom dia|boa tarde|boa noite)\s*!*\s*$",
]

CHAT_MARKERS = {
    "help",
    "ajuda",
    "obrigado",
    "obrigada",
    "thanks",
    "thank you",
    "who are you",
    "quem es",
    "quem és",
    "o que consegues",
    "o que voce consegue",
    "o que você consegue",
    "o que voce pode fazer",
    "o que você pode fazer",
    "o que podes fazer",
    "what do you do",
    "what can you do",
}

DATA_QUERY_HINTS = {
    "cert",
    "certific",
    "expir",
    "experience",
    "experiencia",
    "cv",
    "curriculum",
    "colaborador",
    "employee",
    "vendor",
    "fornecedor",
    "microsoft",
    "cisco",
    "red hat",
    "dell",
    "aws",
    "vmware",
    "oracle",
}


@dataclass
class ConversationState:
    intent: str
    language: str
    show_certification_details: bool
    rows: list[dict]
    cursor: int
    names: list[str]
    names_cursor: int
    history: list[tuple[str, str]]


@dataclass(frozen=True)
class AgentQueryResult:
    analysis: QueryAnalysis
    language: str
    summary: str
    answer: str
    rows_page: list[dict]
    total_results: int
    shown_results: int
    has_more: bool
    show_certification_details: bool


_STATE: dict[str, ConversationState] = {}
_STATE_LOCK = Lock()


def handle_user_query(
    query_text: str,
    preferred_language: str | None,
    conversation_id: str | None,
) -> AgentQueryResult:
    normalized_query = (query_text or "").strip().lower()
    state_key = (conversation_id or "default").strip() or "default"

    with _STATE_LOCK:
        prior = _STATE.get(state_key)

    if prior and _is_follow_up_more(normalized_query):
        result = _continue_from_history(prior)
        _append_history(state_key, query_text, result.answer)
        return result

    if _is_conversational_turn(normalized_query):
        language = preferred_language or (prior.language if prior else _detect_language(normalized_query))
        answer = _tool_chat_completion(state_key, query_text, language, prior)
        result = AgentQueryResult(
            analysis=_chat_analysis(language),
            language=language,
            summary="",
            answer=answer,
            rows_page=[],
            total_results=0,
            shown_results=0,
            has_more=False,
            show_certification_details=False,
        )
        _append_history(state_key, query_text, answer, prior)
        return result

    analysis, rows = _tool_query_database(query_text, preferred_language)

    if not rows and _should_use_web_search(normalized_query):
        web_hits = _tool_web_search(query_text)
        if web_hits:
            language = analysis.language
            summary = "Sem resultados locais; encontrei referências na web." if language == "pt" else "No local matches found; I found web references."
            answer = _build_web_answer(web_hits, language)
            return AgentQueryResult(
                analysis=analysis,
                language=language,
                summary=summary,
                answer=answer,
                rows_page=[],
                total_results=len(web_hits),
                shown_results=min(len(web_hits), 5),
                has_more=False,
                show_certification_details=False,
            )

    summary, answer, language = build_summary(
        query_text,
        rows,
        analysis.language,
        analysis.query_type,
        analysis.wants_certification_details,
        analysis.wants_experience_summary,
    )

    names = _unique_employee_names(rows)
    if analysis.query_type == "certifications" and not analysis.wants_certification_details:
        shown = min(len(names), NAMES_PAGE_SIZE)
        has_more = len(names) > shown
        rows_page = []
        names_cursor = shown
        total_results = len(names)
    elif analysis.query_type == "experience":
        shown = min(len(names), NAMES_PAGE_SIZE)
        has_more = len(names) > shown
        rows_page = rows[:shown]
        names_cursor = shown
        total_results = len(names)
    else:
        shown = min(len(rows), PAGE_SIZE)
        has_more = len(rows) > shown
        rows_page = rows[:shown]
        names_cursor = min(len(names), NAMES_PAGE_SIZE)
        total_results = len(rows)

    with _STATE_LOCK:
        _STATE[state_key] = ConversationState(
            intent=analysis.query_type,
            language=language,
            show_certification_details=analysis.wants_certification_details,
            rows=list(rows),
            cursor=shown,
            names=names,
            names_cursor=names_cursor,
            history=_next_history(prior.history if prior else [], query_text, answer),
        )

    return AgentQueryResult(
        analysis=analysis,
        language=language,
        summary=summary,
        answer=answer,
        rows_page=rows_page,
        total_results=total_results,
        shown_results=shown,
        has_more=has_more,
        show_certification_details=analysis.wants_certification_details,
    )


def _continue_from_history(state: ConversationState) -> AgentQueryResult:
    analysis = QueryAnalysis(
        query_type=state.intent,
        language=state.language,
        normalized_query="",
        tokens=[],
        vendor_terms=[],
        employee_terms=[],
        expired_only=False,
        active_only=False,
        storage_only=False,
        wants_certification_details=state.show_certification_details,
        wants_employee_names_only=False,
        wants_experience_summary=False,
    )

    if state.intent == "certifications" and state.show_certification_details:
        start = state.cursor
        end = min(start + PAGE_SIZE, len(state.rows))
        rows_page = state.rows[start:end]
        state.cursor = end
        has_more = state.cursor < len(state.rows)
        answer = _build_more_details_answer(rows_page, state.language)
        shown = end
        total = len(state.rows)
    else:
        start = state.names_cursor
        end = min(start + NAMES_PAGE_SIZE, len(state.names))
        next_names = state.names[start:end]
        state.names_cursor = end
        has_more = state.names_cursor < len(state.names)
        rows_page = [
            {"employee_name": name, "headline": "profile", "snippet": "", "source_document": "", "language": state.language}
            for name in next_names
        ]
        answer = _build_more_names_answer(next_names, state.intent, state.language)
        shown = end
        total = len(state.names)

    summary = _more_summary(state.language, shown, total)
    return AgentQueryResult(
        analysis=analysis,
        language=state.language,
        summary=summary,
        answer=answer,
        rows_page=rows_page,
        total_results=total,
        shown_results=shown,
        has_more=has_more,
        show_certification_details=state.show_certification_details,
    )


def _tool_query_database(query_text: str, preferred_language: str | None) -> tuple[QueryAnalysis, list[dict]]:
    analysis, rows = run_query(query_text, preferred_language)
    return analysis, list(rows)


def _tool_web_search(query_text: str) -> list[str]:
    url = "https://api.duckduckgo.com/"
    params = {
        "q": query_text,
        "format": "json",
        "no_redirect": 1,
        "skip_disambig": 1,
    }
    try:
        with httpx.Client(timeout=6.0) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            body = response.json()
    except Exception:
        return []

    hits: list[str] = []
    abstract = (body.get("AbstractText") or "").strip()
    heading = (body.get("Heading") or "").strip()
    if abstract:
        hits.append(f"{heading}: {abstract}" if heading else abstract)

    related = body.get("RelatedTopics") or []
    for item in related:
        if isinstance(item, dict):
            text = (item.get("Text") or "").strip()
            if text:
                hits.append(text)
        if len(hits) >= 5:
            break
    return hits[:5]


def _is_follow_up_more(normalized_query: str) -> bool:
    return any(marker in normalized_query for marker in FOLLOW_UP_MORE_MARKERS)


def _should_use_web_search(normalized_query: str) -> bool:
    return any(marker in normalized_query for marker in WEB_SEARCH_MARKERS)


def _unique_employee_names(rows: list[dict]) -> list[str]:
    names: list[str] = []
    for row in rows:
        name = row.get("employee_name") or "Unknown"
        if name not in names:
            names.append(name)
    return names


def _build_more_details_answer(rows: list[dict], language: str) -> str:
    if not rows:
        return "Não há mais certificações para mostrar." if language == "pt" else "There are no more certifications to show."

    lines: list[str] = []
    for row in rows:
        lines.append(
            f"- {row.get('employee_name', 'Employee')} | {row.get('certification_name', 'Certification')} | {row.get('vendor', 'Vendor')} | {row.get('status', 'unknown')}"
        )
    return "\n".join(lines)


def _build_more_names_answer(names: list[str], intent: str, language: str) -> str:
    if not names:
        return "Não há mais resultados para mostrar." if language == "pt" else "There are no more results to show."

    return "- " + "\n- ".join(names)


def _more_summary(language: str, shown: int, total: int) -> str:
    return ""


def _build_web_answer(hits: list[str], language: str) -> str:
    if language == "pt":
        return "Referências externas:\n- " + "\n- ".join(hits)
    return "External references:\n- " + "\n- ".join(hits)


def _is_conversational_turn(normalized_query: str) -> bool:
    if not normalized_query:
        return True
    if any(re.search(pattern, normalized_query, re.IGNORECASE) for pattern in GREETING_PATTERNS):
        return True
    if any(marker in normalized_query for marker in CHAT_MARKERS):
        return True
    if not _looks_like_data_query(normalized_query) and any(
        token in normalized_query for token in {"voce", "você", "you", "assistant", "agente", "bot"}
    ):
        return True
    if len(normalized_query.split()) <= 2 and not _looks_like_data_query(normalized_query):
        return True
    return False


def _looks_like_data_query(normalized_query: str) -> bool:
    return any(hint in normalized_query for hint in DATA_QUERY_HINTS)


def _detect_language(normalized_query: str) -> str:
    pt_markers = {"ola", "olá", "obrigado", "obrigada", "ajuda", "quais", "quem", "colaborador"}
    en_markers = {"hello", "thanks", "help", "which", "who", "employee"}
    pt_score = sum(1 for marker in pt_markers if marker in normalized_query)
    en_score = sum(1 for marker in en_markers if marker in normalized_query)
    return "pt" if pt_score >= en_score else "en"


def _chat_analysis(language: str) -> QueryAnalysis:
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


def _append_history(
    state_key: str,
    user_message: str,
    assistant_message: str,
    prior: ConversationState | None = None,
) -> None:
    with _STATE_LOCK:
        state = _STATE.get(state_key) or prior
        if state is None:
            _STATE[state_key] = ConversationState(
                intent="chat",
                language=_detect_language((user_message or "").lower()),
                show_certification_details=False,
                rows=[],
                cursor=0,
                names=[],
                names_cursor=0,
                history=_next_history([], user_message, assistant_message),
            )
            return
        state.history = _next_history(state.history, user_message, assistant_message)


def _next_history(
    history: list[tuple[str, str]],
    user_message: str,
    assistant_message: str,
    limit: int = 8,
) -> list[tuple[str, str]]:
    updated = list(history)
    updated.append((user_message.strip(), assistant_message.strip()))
    return updated[-limit:]


def _tool_chat_completion(
    state_key: str,
    user_message: str,
    language: str,
    prior: ConversationState | None,
) -> str:
    # Fast-path deterministic replies for common greetings and thanks.
    norm = (user_message or "").strip().lower()
    if any(re.search(pattern, norm, re.IGNORECASE) for pattern in GREETING_PATTERNS):
        if language == "pt":
            return (
                "Olá. Posso ajudar com perguntas sobre certificações e experiência profissional. "
                "Também consigo continuar resultados anteriores se pedir 'mostrar mais resultados'."
            )
        return (
            "Hello. I can help with certification and professional experience questions. "
            "I can also continue previous result lists if you ask 'show more results'."
        )

    if any(token in norm for token in {"obrigado", "obrigada", "thanks", "thank you"}):
        return "De nada. Quando quiser, faça a próxima pergunta." if language == "pt" else "You're welcome. Ask me anything when you're ready."

    client = _get_chat_client()
    deployment = settings.azure_openai_chat_deployment
    if client is None or not deployment:
        return (
            "Posso continuar a conversa e ajudar com perguntas sobre certificações e experiência." if language == "pt"
            else "I can continue the conversation and help with certification and experience questions."
        )

    history = prior.history if prior else []
    messages: list[dict] = [
        {
            "role": "system",
            "content": (
                "You are Agent CV assistant. Be concise, friendly, and conversational. "
                "Do not reveal personal information beyond employee names. "
                "If the user asks generic chat/help questions, answer directly without querying data."
            ),
        }
    ]

    for past_user, past_assistant in history[-4:]:
        if past_user:
            messages.append({"role": "user", "content": past_user})
        if past_assistant:
            messages.append({"role": "assistant", "content": past_assistant})

    messages.append({"role": "user", "content": user_message})
    try:
        response = client.chat.completions.create(
            model=deployment,
            messages=messages,
            temperature=0.3,
            max_completion_tokens=300,
        )
        content = (response.choices[0].message.content or "").strip()
        if content:
            return content
    except Exception:
        pass

    return "Posso ajudar com isso." if language == "pt" else "I can help with that."


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
