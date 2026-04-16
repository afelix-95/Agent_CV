from __future__ import annotations

from collections.abc import Sequence


def build_summary(query: str, rows: Sequence[dict], language: str | None) -> tuple[str, str]:
    lang = (language or _guess_lang(query)).lower()
    total = len(rows)
    if lang == "pt":
        summary = f"Encontrei {total} certificacoes para a sua pesquisa."
    else:
        summary = f"I found {total} certifications for your query."
    return summary, lang


def _guess_lang(text: str) -> str:
    lowered = text.lower()
    if any(token in lowered for token in ["quem", "mostrar", "certificacoes", "expirad"]):
        return "pt"
    return "en"
