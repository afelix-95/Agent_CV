from __future__ import annotations

from collections.abc import Sequence
import re

from agent_cv.services.query_service import normalize_text


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
    summary = _build_summary_text(intent, total, lang, show_certification_details)
    answer = _build_answer_text(
        intent,
        rows,
        total,
        lang,
        show_certification_details,
        wants_experience_summary,
    )
    return summary, answer, lang


def _guess_lang(text: str) -> str:
    lowered = normalize_text(text)
    if any(token in lowered for token in ["quem", "mostrar", "certificacoes", "expirad", "experiencia", "curriculo"]):
        return "pt"
    return "en"


def _build_summary_text(intent: str, total: int, language: str, show_certification_details: bool) -> str:
    if language == "pt":
        if intent == "experience":
            return f"Encontrei {total} perfis de colaboradores relacionados com a sua pergunta."
        if not show_certification_details:
            return f"Encontrei {total} certificações correspondentes e agrupei por colaborador."
        return f"Encontrei {total} certificações relacionadas com a sua pergunta."

    if intent == "experience":
        return f"I found {total} employee profile matches related to your question."
    if not show_certification_details:
        return f"I found {total} matching certifications and grouped them by employee."
    return f"I found {total} certification matches related to your question."


def _build_answer_text(
    intent: str,
    rows: Sequence[dict],
    total: int,
    language: str,
    show_certification_details: bool,
    wants_experience_summary: bool,
) -> str:
    if total == 0:
        if language == "pt":
            if intent == "experience":
                return "Não encontrei experiência ou conteúdo de CV que corresponda a esse pedido."
            return "Não encontrei certificações que correspondam a esse pedido."
        if intent == "experience":
            return "I couldn't find any CV or experience content matching that request."
        return "I couldn't find any certifications matching that request."

    top_rows = rows[:3]
    if intent == "experience":
        if wants_experience_summary:
            return _build_experience_summary(rows, language)
        top_names = [
            row.get("employee_name", "Unknown")
            for row in _first_rows_per_employee(rows, 3)
        ]
        if language == "pt":
            return "Os colaboradores mais relevantes para este tema são:\n- " + "\n- ".join(top_names)
        return "The most relevant employees for this topic are:\n- " + "\n- ".join(top_names)

    if not show_certification_details:
        names = _unique_names(rows)
        if language == "pt":
            return "Os colaboradores com as certificações pedidas são:\n- " + "\n- ".join(names)
        return "The employees with the requested certifications are:\n- " + "\n- ".join(names)

    if language == "pt":
        intros = [
            f"{row.get('employee_name', 'Colaborador')} tem {row.get('certification_name', 'uma certificação')} ({row.get('vendor', 'Fornecedor desconhecido')}, estado: {row.get('status', 'desconhecido')})"
            for row in top_rows
        ]
        return "As correspondências mais relevantes são:\n- " + "\n- ".join(intros)

    intros = [
        f"{row.get('employee_name', 'Employee')} has {row.get('certification_name', 'a certification')} ({row.get('vendor', 'Unknown vendor')}, status: {row.get('status', 'unknown')})"
        for row in top_rows
    ]
    return "The most relevant matches are:\n- " + "\n- ".join(intros)


def _unique_names(rows: Sequence[dict]) -> list[str]:
    names: list[str] = []
    for row in rows:
        name = row.get("employee_name") or "Unknown"
        if name not in names:
            names.append(name)
    return names or ["Unknown"]


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
        lines.append(f"- {name}: {snippet[:220]}")

    if language == "pt":
        return "Resumo de experiência dos colaboradores encontrados:\n" + "\n".join(lines)
    return "Experience summary for the matched employees:\n" + "\n".join(lines)


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


def _clean_experience_text(text: str) -> str:
    raw = (text or "").replace("\n", " ")
    raw = re.sub(r"\s+", " ", raw).strip()

    pii_patterns = [
        r"\b(nome|name)\s*:\s*[^.]+",
        r"\b(nacionalidade|nationality)\s*:\s*[^.]+",
        r"\b(data nascimento|date of birth|born)\s*:\s*[^.]+",
        r"\b(email|e-mail|telefone|phone|contacto|contact)\s*:\s*[^.]+",
    ]
    for pattern in pii_patterns:
        raw = re.sub(pattern, "", raw, flags=re.IGNORECASE)

    return re.sub(r"\s+", " ", raw).strip()
