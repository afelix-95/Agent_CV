import asyncio
import base64
import json
import logging
import os
import re
import time

import httpx
from microsoft_teams.api import MessageActivity, TypingActivityInput
from microsoft_teams.apps import ActivityContext, App
from microsoft_teams.devtools import DevToolsPlugin

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

_client_id = os.getenv("BOT_APP_ID")
_client_secret = os.getenv("BOT_APP_PASSWORD")
# skip_auth=True bypasses inbound Bot Framework token validation.
# For DevTools local testing we also skip outbound token acquisition (MSAL)
# by supplying a dummy token callable instead of the client_secret — this avoids
# unauthorized_client errors when no Azure Bot resource backs the app registration.
# Set BOT_SKIP_AUTH=false in production once an Azure Bot resource is configured.
_skip_auth = os.getenv("BOT_SKIP_AUTH", "true").lower() not in ("false", "0", "no")


def _make_fake_jwt() -> str:
    """Return a syntactically-valid (but unsigned) JWT for DevTools-only mode.
    Signature verification is disabled by the SDK when skip_auth=True, so only
    the structure needs to be correct to satisfy JsonWebToken construction."""
    def _b64url(data: dict) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(data, separators=(",", ":")).encode()
        ).rstrip(b"=").decode()

    header = _b64url({"alg": "RS256", "typ": "JWT"})
    payload = _b64url({"exp": int(time.time()) + 3600})
    return f"{header}.{payload}.AAAA"


async def _devtools_token(scopes, tenant_id=None) -> str:
    """Dummy token provider for DevTools-only mode (no Azure Bot resource required)."""
    return _make_fake_jwt()


app = App(
    client_id=_client_id,
    tenant_id=os.getenv("BOT_TENANT_ID"),
    # In skip-auth mode: use dummy token callable to bypass MSAL outbound auth.
    # In production: use client_secret so MSAL acquires a real Bot Framework token.
    **({"token": _devtools_token} if _skip_auth else {"client_secret": _client_secret}),
    skip_auth=_skip_auth,
    plugins=[DevToolsPlugin()],
)

BACKEND_QUERY_URL = os.getenv("BACKEND_QUERY_URL", "http://localhost:8000/query")
BACKEND_TIMEOUT_SECONDS = float(os.getenv("BACKEND_TIMEOUT_SECONDS", "20"))
# Set BACKEND_SSL_VERIFY=false to disable TLS verification (corporate proxy with self-signed CA).
# Set BACKEND_SSL_VERIFY to a path to trust a specific CA bundle file.
_ssl_verify_env = os.getenv("BACKEND_SSL_VERIFY", "true")
BACKEND_SSL_VERIFY: bool | str = (
    False if _ssl_verify_env.lower() in ("false", "0", "no")
    else True if _ssl_verify_env.lower() in ("true", "1", "yes")
    else _ssl_verify_env  # treat as CA bundle path
)


_bot_token_cache: dict = {"token": None, "expires_at": 0.0}


async def _get_bot_service_token() -> str | None:
    """Acquire a Bot Framework bearer token for downloading Teams attachment images."""
    if _skip_auth or not (_client_id and _client_secret):
        return None
    now = time.time()
    if _bot_token_cache["token"] and _bot_token_cache["expires_at"] > now + 60:
        return _bot_token_cache["token"]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": _client_id,
                    "client_secret": _client_secret,
                    "scope": "https://api.botframework.com/.default",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            _bot_token_cache["token"] = data["access_token"]
            _bot_token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
            return _bot_token_cache["token"]
    except Exception:
        logger.debug("Could not acquire bot service token for image download", exc_info=True)
        return None


async def _download_attachment_image(content_url: str, content_type: str) -> str | None:
    """Download a Teams attachment and return a base64 data-URL string, or None on failure."""
    _MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB encoded limit
    try:
        token = await _get_bot_service_token()
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, verify=BACKEND_SSL_VERIFY) as client:
            resp = await client.get(content_url, headers=headers)
            resp.raise_for_status()
            raw = resp.content
        mime = content_type if content_type.startswith("image/") else "image/png"
        b64 = base64.b64encode(raw).decode("utf-8")
        if len(b64) > _MAX_IMAGE_BYTES:
            logger.warning("Attachment image too large (%d bytes encoded) — skipping", len(b64))
            return None
        return f"data:{mime};base64,{b64}"
    except Exception:
        logger.debug("Failed to download Teams attachment from %s", content_url, exc_info=True)
        return None


async def _query_backend(query_text: str, conversation_id: str | None, images: list[str] | None = None) -> tuple[str | None, str | None]:
    payload: dict = {
        "query": query_text,
        "conversation_id": conversation_id,
    }
    if images:
        payload["images"] = images
    try:
        async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT_SECONDS, follow_redirects=True, verify=BACKEND_SSL_VERIFY) as client:
            response = await client.post(BACKEND_QUERY_URL, json=payload)
            response.raise_for_status()
            body = response.json()
    except httpx.TimeoutException:
        return None, "Backend timeout. Confirm the Agent CV API is running and reachable."
    except httpx.HTTPStatusError as exc:
        return None, f"Backend returned HTTP {exc.response.status_code}."
    except httpx.HTTPError as exc:
        return None, f"Failed to reach backend: {exc}"

    language = body.get("language", "en")
    summary = body.get("summary")
    answer = body.get("answer")
    shown_results = int(body.get("shown_results", 0) or 0)
    total_results = int(body.get("total_results", 0) or 0)
    has_more = bool(body.get("has_more", False))

    if not summary and not answer:
        return None, "Backend response did not include an answer."

    lines: list[str] = []
    if summary:
        lines.append(summary)
    if answer:
        if lines:
            lines.append("")
        lines.append(answer)

    if total_results > 0 and shown_results > 0:
        lines.append("")
        if language == "pt":
            lines.append(f"A mostrar {shown_results} de {total_results} resultados.")
        else:
            lines.append(f"Showing {shown_results} of {total_results} results.")

    if has_more:
        lines.append("")
        lines.append("Peça 'mostrar mais resultados' para continuar." if language == "pt" else "Ask 'show more results' to continue.")

    return "\n".join(lines), None


@app.on_install_add
async def handle_install(ctx: ActivityContext) -> None:
    """Acknowledge bot installation — required for Teams to complete the install flow."""
    pass


@app.event("error")
async def handle_error(event) -> None:
    logger.exception("Unhandled error processing activity: %s", event.error, exc_info=event.error)


@app.on_message_pattern(re.compile(r"^\s*(hello|hi|greetings|ola|oi)\b", re.IGNORECASE))
async def handle_greeting(ctx: ActivityContext[MessageActivity]) -> None:
    """Handle greeting messages."""
    await ctx.send(
        "Hello! Ask me about certifications, for example: "
        "'Who has Red Hat certifications?' or 'Show expired Dell certifications'."
    )


@app.on_message
async def handle_message(ctx: ActivityContext[MessageActivity]):
    """Forward user queries to the Agent CV backend and return real results."""
    try:
        await ctx.reply(TypingActivityInput())
    except Exception:
        logger.debug("Could not send typing indicator", exc_info=True)

    user_text = (ctx.activity.text or "").strip()

    # Download any image attachments (inline screenshots, pasted images)
    images: list[str] = []
    attachments = getattr(ctx.activity, "attachments", None) or []
    for att in attachments[:4]:  # Limit to 4 images
        content_type = getattr(att, "content_type", "") or ""
        content_url = getattr(att, "content_url", "") or ""
        if content_type.startswith("image/") and content_url:
            data_url = await _download_attachment_image(content_url, content_type)
            if data_url:
                images.append(data_url)
                logger.debug("Attached image downloaded: %s (%d chars)", content_type, len(data_url))

    if not user_text and not images:
        await ctx.send("Please enter a query.")
        return

    # When a user sends only an image with no text, use a neutral prompt
    if not user_text and images:
        user_text = "Analisa a imagem em anexo."

    conversation = getattr(ctx.activity, "conversation", None)
    conversation_id = getattr(conversation, "id", None)
    result_text, error_text = await _query_backend(user_text, conversation_id, images or None)
    if error_text:
        await ctx.send(error_text)
        return

    await ctx.send(result_text)


def main():
    asyncio.run(app.start())


if __name__ == "__main__":
    main()
