"""LLM-based vendor discovery for certification documents.

When the filename parser cannot match a known vendor from the hardcoded
VENDOR_HINTS list, this service queries Azure OpenAI to infer the issuing
organisation from the certificate title. The result is stored in the DB
``vendors`` table so subsequent ingestions of the same vendor are resolved
locally without another LLM call.
"""
from __future__ import annotations

import logging

from openai import AzureOpenAI

from agent_cv.config import settings

logger = logging.getLogger(__name__)

# Responses that indicate the model could not identify a vendor.
_UNKNOWN_MARKERS = {"unknown", "n/a", "none", "not found", "not applicable", "unclear"}

# Upper bound on a plausible vendor name length. Longer replies are almost
# certainly explanatory prose rather than a clean organisation name.
_MAX_VENDOR_NAME_LEN = 60


def _get_client() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
    )


def discover_vendor(cert_title: str) -> str | None:
    """Return the vendor/organisation that issues *cert_title*, or ``None``.

    Makes a single text-only chat completion request to Azure OpenAI.
    Returns ``None`` when:
    - the model replies "Unknown" or a known uncertainty marker;
    - the reply exceeds ``_MAX_VENDOR_NAME_LEN`` characters (likely prose);
    - any API or network error occurs.

    The caller is responsible for persisting the returned vendor name in the
    database.
    """
    if not cert_title or not cert_title.strip():
        return None

    if not settings.azure_openai_endpoint or not settings.azure_openai_chat_deployment:
        logger.debug("vendor_discovery: Azure OpenAI not configured — skipping LLM lookup")
        return None

    try:
        client = _get_client()
        response = client.chat.completions.create(
            model=settings.azure_openai_chat_deployment,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a certification database expert. "
                        "When given a certification or credential title, reply with ONLY "
                        "the name of the vendor or organisation that issues it — nothing else. "
                        "If the certification is unknown or you are not confident, "
                        "reply with exactly: Unknown"
                    ),
                },
                {
                    "role": "user",
                    "content": f"What vendor or organisation issues the following certification?\n\n{cert_title.strip()}",
                },
            ],
            max_tokens=30,
            temperature=0,
        )

        raw = (response.choices[0].message.content or "").strip()
        logger.debug("vendor_discovery: LLM returned %r for cert %r", raw, cert_title)

        if not raw:
            return None

        # Reject replies that are longer than a plausible vendor name.
        if len(raw) > _MAX_VENDOR_NAME_LEN:
            logger.debug(
                "vendor_discovery: reply too long (%d chars) — treating as uncertain",
                len(raw),
            )
            return None

        # Reject well-known uncertainty phrases.
        if raw.lower().rstrip(".") in _UNKNOWN_MARKERS:
            return None

        return raw

    except Exception as exc:
        logger.warning("vendor_discovery: LLM call failed for %r: %s", cert_title, exc)
        return None
