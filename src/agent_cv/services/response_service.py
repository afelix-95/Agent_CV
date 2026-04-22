from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
import re

from openai import AzureOpenAI

from agent_cv.config import settings
from agent_cv.services.query_service import normalize_text
from agent_cv.services.retrieval_service import (
    RetrievedSnippet,
    build_lightweight_citations,
    format_context_for_prompt,
    retrieve_semantic_context,
)

MAX_INLINE_CITATIONS = 3


def build_summary(
    query: str,
    rows: Sequence[dict],
    language: str | None,
    intent: str,
    show_certification_details: bool,
    wants_experience_summary: bool,
) -> tuple[str, str, str]:
    lang = (language or _guess_lang(query)).lower()
    if intent == "experience":
        total = len(_unique_names(rows))
    elif intent == "certifications" and not show_certification_details:
        total = len(_unique_names(rows))
    else:
        total = len(rows)

    if total == 0:
        return "", _build_no_results_answer(intent, lang), lang

    snippets = retrieve_semantic_context(
        query=query,
        intent=intent,
        rows=rows,
        employee_names=_unique_names(rows),
    )

    answer = _build_grounded_answer(
        query=query,
        intent=intent,
        language=lang,
        show_certification_details=show_certification_details,
        snippets=snippets,
    )
    if not answer:
        answer = _build_fallback_answer(
            intent=intent,
            rows=rows,
            total=total,
            language=lang,
            show_certification_details=show_certification_details,
            wants_experience_summary=wants_experience_summary,
        )

    answer = _append_citations(answer, snippets, lang)
    return "", answer, lang


def _build_grounded_answer(
    query: str,
    intent: str,
    language: str,
    show_certification_details: bool,
    snippets: list[RetrievedSnippet],
) -> str:
    client = _get_chat_client()
    deployment = settings.azure_openai_chat_deployment
    if client is None or not deployment:
        return ""

    context_block = format_context_for_prompt(snippets, language)

    try:
        response = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": _build_system_prompt(language)},
                {
                    "role": "user",
                    "content": _build_user_prompt(
                        query=query,
                        intent=intent,
                        language=language,
                        show_certification_details=show_certification_details,
                        context_block=context_block,
                    ),
                },
            ],
            temperature=0.2,
            max_completion_tokens=520,
        )
    except Exception:
        return ""

    return (response.choices[0].message.content or "").strip()


def _append_citations(answer: str, snippets: list[RetrievedSnippet], language: str) -> str:
    if not answer.strip():
        return answer
    citations = build_lightweight_citations(snippets, language, max_items=MAX_INLINE_CITATIONS)
    if not citations:
        return answer
    return answer.rstrip() + citations


def _build_system_prompt(language: str) -> str:
    if language == "pt":
        return (
            "Você é um assistente de CV corporativo. "
            "Responda apenas com base no contexto fornecido por pesquisa semântica. "
            "Se a evidência for insuficiente, diga claramente que não encontrou informação suficiente. "
            "Não invente certificações, cargos, datas, empresas ou tecnologias. "
            "Use um tom objetivo, útil e natural."
        )
    return (
        "You are a corporate CV assistant. "
        "Answer only using semantic-retrieval context provided to you. "
        "If evidence is insufficient, clearly say there is not enough information. "
        "Do not invent certifications, roles, dates, companies, or technologies. "
        "Use a concise, helpful, natural tone."
    )


def _build_user_prompt(
    query: str,
    intent: str,
    language: str,
    show_certification_details: bool,
    context_block: str,
) -> str:
    if language == "pt":
        return (
            f"Pergunta: {query}\n"
            f"Intenção classificada: {intent}\n"
            f"Mostrar detalhes de certificações: {show_certification_details}\n\n"
            "Contexto semântico recuperado:\n"
            f"{context_block}\n\n"
            "Instruções:\n"
            "- Responder diretamente à pergunta.\n"
            "- Em experiência, resumir responsabilidades e competências técnicas principais.\n"
            "- Em certificações, incluir fornecedor e estado quando existir no contexto.\n"
            "- Não incluir dados pessoais sensíveis.\n"
            "- Quando possível, incluir marcadores de citação inline como [1], [2]."
        )
    return (
        f"Question: {query}\n"
        f"Classified intent: {intent}\n"
        f"Show certification details: {show_certification_details}\n\n"
        "Retrieved semantic context:\n"
        f"{context_block}\n\n"
        "Instructions:\n"
        "- Answer the question directly.\n"
        "- For experience, summarize key responsibilities and technical strengths.\n"
        "- For certifications, include vendor and status where present in context.\n"
        "- Do not include sensitive personal data.\n"
        "- When possible, add inline citation markers like [1], [2]."
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


def _build_fallback_answer(
    intent: str,
    rows: Sequence[dict],
    total: int,
    language: str,
    show_certification_details: bool,
    wants_experience_summary: bool,
) -> str:
    if total == 0:
        return _build_no_results_answer(intent, language)

    if intent == "experience":
        if wants_experience_summary:
            return _build_experience_summary(rows, language)
        names = [row.get("employee_name", "Unknown") for row in _first_rows_per_employee(rows, 10)]
        return "- " + "\n- ".join(names)

    if not show_certification_details:
        return "- " + "\n- ".join(_unique_names(rows)[:10])

    detail_rows = rows[:10]
    lines = [
        f"{row.get('employee_name', 'Employee')} | {row.get('certification_name', 'Certification')} | {row.get('vendor', 'Unknown')} | {row.get('status', 'unknown')}"
        for row in detail_rows
    ]
    return "- " + "\n- ".join(lines)


def _build_no_results_answer(intent: str, language: str) -> str:
    if language == "pt":
        if intent == "experience":
            return "Não encontrei experiência ou conteúdo de CV que corresponda a esse pedido."
        return "Não encontrei certificações que correspondam a esse pedido."
    if intent == "experience":
        return "I couldn't find any CV or experience content matching that request."
    return "I couldn't find any certifications matching that request."


def _guess_lang(text: str) -> str:
    lowered = normalize_text(text)
    if any(token in lowered for token in ["quem", "mostrar", "certificacoes", "expirad", "experiencia", "curriculo"]):
        return "pt"
    return "en"


def _unique_names(rows: Sequence[dict]) -> list[str]:
    names: list[str] = []
    for row in rows:
        name = row.get("employee_name") or "Unknown"
        if name not in names:
            names.append(name)
    return names or ["Unknown"]


def _first_rows_per_employee(rows: Sequence[dict], limit: int) -> list[dict]:
    selected: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        name = row.get("employee_name") or "Unknown"
        if name in seen:
            continue
        selected.append(row)
        seen.add(name)
        if len(selected) >= limit:
            break
    return selected


def _build_experience_summary(rows: Sequence[dict], language: str) -> str:
    unique_rows = _first_rows_per_employee(rows, 3)
    if not unique_rows:
        if language == "pt":
            return "Não encontrei dados suficientes para gerar um resumo de experiência."
        return "I couldn't find enough data to build an experience summary."

    if len(unique_rows) == 1:
        row = unique_rows[0]
        name = row.get("employee_name", "Colaborador")
        snippet = _clean_experience_text(row.get("snippet", ""))
        if language == "pt":
            return f"Resumo da experiência de {name}: {snippet[:420]}"
        return f"Experience summary for {name}: {snippet[:420]}"

    lines: list[str] = []
    for row in unique_rows:
        name = row.get("employee_name", "Colaborador")
        snippet = _clean_experience_text(row.get("snippet", ""))
        points = _extract_summary_points(snippet, max_points=1)
        lines.append(f"- {name}: {(points[0] if points else snippet[:220])}")

    if language == "pt":
        return "Resumo de experiência dos colaboradores encontrados:\n" + "\n".join(lines)
    return "Experience summary for the matched employees:\n" + "\n".join(lines)


def _clean_experience_text(text: str) -> str:
    raw = (text or "").replace("\n", " ")
    raw = raw.replace("â", " ").replace("", " ")
    raw = re.sub(r"\s+", " ", raw).strip()

    pii_patterns = [
        r"\b(nome|name)\s*:\s*[^.]+",
        r"\b(nacionalidade|nationality)\s*:\s*[^.]+",
        r"\b(data nascimento|date of birth|born)\s*:\s*[^.]+",
        r"\b(email|e-mail|telefone|phone|contacto|contact)\s*:\s*[^.]+",
    ]
    for pattern in pii_patterns:
        raw = re.sub(pattern, "", raw, flags=re.IGNORECASE)

    raw = re.sub(r"\b\d{1,2}/\d{4}\s*[\-]\s*(atual|current|\d{1,2}/\d{4})\b", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\b(lisboa|portugal|brasil|coimbra)\b", "", raw, flags=re.IGNORECASE)

    return re.sub(r"\s+", " ", raw).strip()


def _extract_summary_points(text: str, max_points: int = 3) -> list[str]:
    if not text:
        return []

    parts = re.split(r"[.;:]", text)
    cleaned = [re.sub(r"\s+", " ", p).strip(" -") for p in parts]
    cleaned = [p for p in cleaned if len(p) >= 35]

    priority_terms = [
        "respons",
        "gest",
        "implement",
        "design",
        "admin",
        "network",
        "cloud",
        "security",
        "infrastructure",
        "servi",
        "project",
    ]

    scored = sorted(
        cleaned,
        key=lambda p: sum(1 for term in priority_terms if term in normalize_text(p)),
        reverse=True,
    )

    points: list[str] = []
    for item in scored:
        if item not in points:
            points.append(item[:180])
        if len(points) >= max_points:
            break
    return points