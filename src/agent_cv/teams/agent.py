"""
Teams integration via Microsoft Graph Change Notifications (webhooks).

The bot authenticates as the service account (GRAPH_USER_EMAIL) through the
registered Entra app using the ROPC flow (see services/graph_service.py).
At startup it creates a Graph subscription on /me/chats/getAllMessages, which
causes Microsoft Graph to POST a notification to WEBHOOK_BASE_URL/webhooks/teams
whenever a new message arrives in any chat the service account is part of.
The bot then fetches the specific message and processes it through the Agent CV
pipeline.

The subscription expires after 60 minutes (Graph delegated-auth maximum for chat
messages) and is renewed automatically by a background task every 50 minutes.
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx

from agent_cv.config import settings
from agent_cv.services.agent_service import handle_user_query
from agent_cv.services.graph_service import get_access_token, get_graph_client
from agent_cv.services.query_service import audit_query

if TYPE_CHECKING:
    from msgraph import GraphServiceClient

logger = logging.getLogger(__name__)

_STRIP_HTML = re.compile(r"<[^>]+>")
# Subscription lifetime in seconds — 3600 is the delegated-auth maximum for chat messages
_SUBSCRIPTION_LIFETIME_S: int = 3600
# Renew this many seconds before expiry (leaves a 10-minute safety margin)
_SUBSCRIPTION_RENEW_BEFORE_S: int = 600
# Resource URL pattern sent by Graph in change notifications (two formats possible)
_RESOURCE_RE = re.compile(
    r"chats\('([^']+)'\)/messages\('([^']+)'\)"
    r"|chats/([^/]+)/messages/([^/]+)"
)
# Upper bound on the in-memory dedup set to prevent unbounded memory growth
_MAX_PROCESSED_MSGS: int = 10_000


class TeamsWebhookBot:
    """Manages a Graph change-notification subscription and processes incoming
    Teams chat messages via the Agent CV query pipeline.

    Lifecycle (managed by the FastAPI lifespan in main.py):
      await bot.start()  — resolves bot identity, creates subscription, starts renewal task
      await bot.stop()   — cancels renewal task, deletes subscription
      await bot.handle_notification(resource, client_state)  — called from the webhook route
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._bot_user_id: str | None = None
        self._subscription_id: str | None = None
        # chat IDs for which acceptance has succeeded
        self._accepted_chats: set[str] = set()
        # Dedup set to prevent double-processing the same message (Graph can re-deliver)
        self._processed_messages: set[str] = set()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        # Resolve the bot's own user ID so we never reply to ourselves
        client = get_graph_client()
        me = await client.me.get()
        self._bot_user_id = me.id
        logger.info(
            "Teams webhook bot: resolved user ID %s (account=%s)",
            self._bot_user_id,
            settings.graph_user_email,
        )

        # Schedule subscription creation as a background task so it runs AFTER
        # uvicorn has started accepting connections.  Graph validates the notification
        # URL immediately when the subscription is created, so the server must already
        # be serving requests — which it isn't yet during lifespan startup (before yield).
        renew_interval = _SUBSCRIPTION_LIFETIME_S - _SUBSCRIPTION_RENEW_BEFORE_S
        self._task = asyncio.create_task(
            self._startup_then_renewal_loop(renew_interval),
            name="graph-subscription-renewal",
        )
        logger.info(
            "Teams webhook bot initialised (subscription will be created once server is ready, account=%s)",
            settings.graph_user_email,
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self._subscription_id:
            try:
                token = await asyncio.to_thread(get_access_token)
                async with httpx.AsyncClient(timeout=10.0) as http:
                    await http.delete(
                        f"https://graph.microsoft.com/v1.0/subscriptions/{self._subscription_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                logger.info("Teams webhook bot: deleted subscription %s", self._subscription_id)
            except Exception:
                logger.exception("Teams webhook bot: failed to delete subscription on shutdown")

        logger.info("Teams webhook bot stopped")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------ #
    # Subscription management                                              #
    # ------------------------------------------------------------------ #

    async def _create_subscription(self) -> None:
        expiry = datetime.now(timezone.utc) + timedelta(seconds=_SUBSCRIPTION_LIFETIME_S)
        expiry_str = expiry.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        token = await asyncio.to_thread(get_access_token)

        async with httpx.AsyncClient(timeout=15.0) as http:
            if self._subscription_id:
                # Attempt to renew the existing subscription via PATCH
                resp = await http.patch(
                    f"https://graph.microsoft.com/v1.0/subscriptions/{self._subscription_id}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={"expirationDateTime": expiry_str},
                )
                if resp.status_code == 200:
                    logger.info(
                        "Teams webhook bot: subscription %s renewed until %s",
                        self._subscription_id,
                        expiry_str,
                    )
                    return
                logger.warning(
                    "Teams webhook bot: subscription renew returned HTTP %s — will recreate",
                    resp.status_code,
                )
                self._subscription_id = None

            # Create a new subscription
            resp = await http.post(
                "https://graph.microsoft.com/v1.0/subscriptions",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "changeType": "created",
                    "notificationUrl": f"{settings.webhook_base_url}/graph-notifications/teams",
                    "resource": "/me/chats/getAllMessages",
                    "expirationDateTime": expiry_str,
                    "clientState": settings.webhook_secret,
                },
            )

        if resp.status_code == 201:
            self._subscription_id = resp.json()["id"]
            logger.info(
                "Teams webhook bot: created subscription %s (expires %s)",
                self._subscription_id,
                expiry_str,
            )
        else:
            raise RuntimeError(
                f"Failed to create Graph subscription: HTTP {resp.status_code}: {resp.text[:300]}"
            )

    async def _startup_then_renewal_loop(self, renew_interval: int) -> None:
        """Create the initial subscription, then keep renewing it.

        Running this as a background task (via asyncio.create_task) ensures it
        executes only after uvicorn has started accepting connections, which is
        required because Graph validates the notificationUrl immediately.

        A short initial delay plus a retry loop handles the race window between
        the task being scheduled and uvicorn actually serving HTTP requests.
        """
        # Give uvicorn a moment to finish starting before Graph validates the URL
        await asyncio.sleep(5)

        # Retry up to 5 times with increasing back-off in case the server is
        # still not ready or Traefik hasn't finished routing yet
        for attempt in range(1, 6):
            try:
                await self._create_subscription()
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt == 5:
                    logger.exception(
                        "Teams webhook bot: failed to create initial subscription after %d attempts",
                        attempt,
                    )
                    return
                wait = attempt * 10
                logger.warning(
                    "Teams webhook bot: subscription creation attempt %d failed, retrying in %ds",
                    attempt,
                    wait,
                )
                await asyncio.sleep(wait)

        await self._renewal_loop(renew_interval)

    async def _renewal_loop(self, interval: int) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self._create_subscription()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Teams webhook bot: subscription renewal failed")

    # ------------------------------------------------------------------ #
    # Notification handling (called from the webhook route)               #
    # ------------------------------------------------------------------ #

    async def handle_notification(self, resource: str, client_state: str) -> None:
        """Fetch and process the message identified by a Graph change notification."""
        if client_state != settings.webhook_secret:
            logger.warning(
                "Teams webhook bot: notification with unexpected clientState, ignoring"
            )
            return

        m = _RESOURCE_RE.search(resource)
        if not m:
            logger.warning("Teams webhook bot: could not parse resource URL: %s", resource)
            return

        chat_id = m.group(1) or m.group(3)
        message_id = m.group(2) or m.group(4)

        if message_id in self._processed_messages:
            logger.debug(
                "Teams webhook bot: duplicate notification for message %s, skipping", message_id
            )
            return

        self._processed_messages.add(message_id)
        # Prevent unbounded memory growth — a full clear is acceptable here because
        # duplicate delivery of old messages is rare and the set is only used for dedup.
        if len(self._processed_messages) > _MAX_PROCESSED_MSGS:
            self._processed_messages.clear()

        # Accept federated/external chats on first contact
        if chat_id not in self._accepted_chats:
            accepted = await self._accept_chat(chat_id)
            if accepted:
                self._accepted_chats.add(chat_id)
                await self._send_greeting(chat_id)

        client = get_graph_client()
        try:
            msg = await (
                client.chats
                .by_chat_id(chat_id)
                .messages
                .by_chat_message_id(message_id)
                .get()
            )
        except Exception:
            logger.exception(
                "Teams webhook bot: failed to fetch message %s from chat %s",
                message_id,
                chat_id,
            )
            return

        # Skip messages sent by the bot itself
        from_field = getattr(msg, "from_property", None) or getattr(msg, "from_", None)
        sender_user = getattr(from_field, "user", None) if from_field else None
        sender_id = getattr(sender_user, "id", None) if sender_user else None
        if sender_id and sender_id == self._bot_user_id:
            return

        # Only process regular chat messages (skip system events, typing, etc.)
        msg_type = getattr(msg, "message_type", None)
        msg_type_val = getattr(msg_type, "value", str(msg_type)) if msg_type else ""
        if msg_type_val != "message":
            return

        body = getattr(msg, "body", None)
        raw_content = getattr(body, "content", "") or ""
        content_type = getattr(body, "content_type", None)
        content_type_val = (
            getattr(content_type, "value", str(content_type)) if content_type else ""
        )
        text = (
            _STRIP_HTML.sub("", raw_content).strip()
            if content_type_val == "html"
            else raw_content.strip()
        )

        if not text:
            return

        logger.info(
            "Teams webhook bot: new message %s in chat %s — %.80s", message_id, chat_id, text
        )
        await self._handle_message(client, chat_id, message_id, text, sender_id)

    # ------------------------------------------------------------------ #
    # Chat acceptance (federated / external chats)                        #
    # ------------------------------------------------------------------ #

    async def _accept_chat(self, chat_id: str) -> bool:
        """Accept a pending external/federated chat via POST /chats/{id}/unhideForUser."""
        if not self._bot_user_id:
            logger.warning("Teams webhook bot: _accept_chat skipped — bot user ID not resolved")
            return False

        logger.info("Teams webhook bot: calling unhideForUser on %s", chat_id)
        try:
            token = await asyncio.to_thread(get_access_token)
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.post(
                    f"https://graph.microsoft.com/v1.0/chats/{chat_id}/unhideForUser",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "user": {
                            "@odata.type": "#microsoft.graph.teamworkUserIdentity",
                            "id": self._bot_user_id,
                            "tenantId": settings.teams_bot_tenant_id,
                        }
                    },
                )
            if resp.status_code in (200, 204):
                logger.info("Teams webhook bot: accepted (unhid) chat %s", chat_id)
                return True
            logger.warning(
                "Teams webhook bot: unhideForUser returned HTTP %s for %s: %s",
                resp.status_code,
                chat_id,
                resp.text[:300],
            )
            return False
        except Exception:
            logger.exception("Teams webhook bot: failed to accept chat %s", chat_id)
            return False

    async def _send_greeting(self, chat_id: str) -> None:
        """Send an initial greeting to complete the federated-chat acceptance handshake."""
        greeting = (
            "Hello! I'm the CV Finder assistant. "
            "Ask me to search for employees by certification, skill, or technology."
        )
        try:
            token = await asyncio.to_thread(get_access_token)
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.post(
                    f"https://graph.microsoft.com/v1.0/chats/{chat_id}/messages",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={"body": {"contentType": "text", "content": greeting}},
                )
            if resp.status_code in (200, 201):
                logger.info("Teams webhook bot: sent greeting to %s", chat_id)
            else:
                logger.warning(
                    "Teams webhook bot: greeting failed HTTP %s for %s: %s",
                    resp.status_code,
                    chat_id,
                    resp.text[:200],
                )
        except Exception:
            logger.exception("Teams webhook bot: failed to send greeting to %s", chat_id)

    # ------------------------------------------------------------------ #
    # Message handling                                                     #
    # ------------------------------------------------------------------ #

    async def _handle_message(
        self,
        client: "GraphServiceClient",
        chat_id: str,
        msg_id: str,
        text: str,
        sender_id: str | None,
    ) -> None:
        from msgraph.generated.models.body_type import BodyType
        from msgraph.generated.models.chat_message import ChatMessage
        from msgraph.generated.models.item_body import ItemBody

        started = time.perf_counter()
        try:
            result = await asyncio.to_thread(handle_user_query, text, None, chat_id)
        except Exception:
            logger.exception(
                "Teams webhook bot: query pipeline error for message %s", msg_id
            )
            reply_text = "An unexpected error occurred while processing your query."
            _safe_audit(
                query_text=text,
                query_language=None,
                response_language="en",
                result_count=0,
                latency_ms=int((time.perf_counter() - started) * 1000),
                sender_id=sender_id,
                chat_id=chat_id,
                tool_calls_log=[],
            )
        else:
            reply_text = _build_reply(result)
            _safe_audit(
                query_text=text,
                query_language=None,
                response_language=result.language,
                result_count=result.total_results,
                latency_ms=int((time.perf_counter() - started) * 1000),
                sender_id=sender_id,
                chat_id=chat_id,
                tool_calls_log=result.tool_calls_log,
            )

        try:
            reply = ChatMessage(body=ItemBody(
                content=_text_to_html(reply_text),
                content_type=BodyType.Html,
            ))
            await client.chats.by_chat_id(chat_id).messages.post(reply)
        except Exception:
            logger.exception(
                "Teams webhook bot: failed to post reply for message %s", msg_id
            )


# ------------------------------------------------------------------ #
# Module-level singleton                                              #
# ------------------------------------------------------------------ #

_bot: TeamsWebhookBot | None = None


def get_teams_bot() -> TeamsWebhookBot:
    global _bot
    if _bot is None:
        _bot = TeamsWebhookBot()
    return _bot


# Backwards-compatible alias for any code still calling get_graph_bot()
get_graph_bot = get_teams_bot





# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and s.count("|") >= 2


def _is_separator_row(line: str) -> bool:
    """Matches markdown table separator rows like |---|---|"""
    import re
    return bool(re.fullmatch(r"[\|\s\-:]+", line.strip()))


def _parse_table(lines: list[str]) -> str:
    """Convert a list of markdown pipe-table lines into an HTML table."""
    html_rows: list[str] = []
    header_done = False
    for raw in lines:
        stripped = raw.strip()
        if _is_separator_row(stripped) and not stripped.replace("|", "").replace("-", "").replace(":", "").replace(" ", ""):
            # This is the separator row — marks end of header
            header_done = True
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not header_done:
            tag = "th"
        else:
            tag = "td"
        row_html = "".join(f"<{tag}>{_html.escape(c)}</{tag}>" for c in cells)
        html_rows.append(f"<tr>{row_html}</tr>")
    return (
        '<table style="border-collapse:collapse;width:100%;">'
        + "".join(html_rows)
        + "</table>"
    )


def _apply_inline_markdown(text: str) -> str:
    """Convert inline markdown (bold, italic, code, citations) to HTML spans."""
    # Bold+italic ***text*** or ___text___
    text = re.sub(r"\*{3}(.+?)\*{3}", r"<strong><em>\1</em></strong>", text)
    text = re.sub(r"_{3}(.+?)_{3}", r"<strong><em>\1</em></strong>", text)
    # Bold **text** or __text__
    text = re.sub(r"\*{2}(.+?)\*{2}", r"<strong>\1</strong>", text)
    text = re.sub(r"_{2}(.+?)_{2}", r"<strong>\1</strong>", text)
    # Italic *text* or _text_
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"_(.+?)_", r"<em>\1</em>", text)
    # Inline code `code`
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return text


def _text_to_html(text: str) -> str:
    """Convert LLM markdown output (bullets, tables, headings, bold/italic) to Teams-compatible HTML."""
    lines = text.split("\n")
    parts: list[str] = []
    in_list = False
    table_buf: list[str] = []

    def flush_table() -> None:
        if table_buf:
            parts.append(_parse_table(table_buf))
            table_buf.clear()

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            parts.append("</ul>")
            in_list = False

    for line in lines:
        stripped = line.strip()

        # Accumulate markdown pipe-table rows
        if _is_table_row(stripped):
            close_list()
            table_buf.append(stripped)
            continue

        # Non-table line — flush any buffered table first
        flush_table()

        # ATX headings: # H1, ## H2, ### H3
        heading_match = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading_match:
            close_list()
            level = len(heading_match.group(1))
            tag = f"h{level}"
            parts.append(f"<{tag}>{_html.escape(heading_match.group(2))}</{tag}>")
            continue

        # Horizontal rule --- or ***
        if re.fullmatch(r"[-*_]{3,}", stripped):
            close_list()
            parts.append("<hr/>")
            continue

        # Bullet points: •, -, *, +
        bullet_match = re.match(r"^[•\-\*\+]\s+(.*)", stripped)
        if bullet_match:
            if not in_list:
                parts.append("<ul>")
                in_list = True
            content = _apply_inline_markdown(_html.escape(bullet_match.group(1)))
            parts.append(f"<li>{content}</li>")
            continue

        # Numbered list: 1. item
        numbered_match = re.match(r"^\d+\.\s+(.*)", stripped)
        if numbered_match:
            if not in_list:
                parts.append("<ul>")
                in_list = True
            content = _apply_inline_markdown(_html.escape(numbered_match.group(1)))
            parts.append(f"<li>{content}</li>")
            continue

        close_list()
        if stripped:
            content = _apply_inline_markdown(_html.escape(stripped))
            parts.append(f"<p>{content}</p>")

    flush_table()
    close_list()

    return "".join(parts)


def _build_reply(result: object) -> str:
    lines: list[str] = []
    summary = getattr(result, "summary", "")
    answer = getattr(result, "answer", "")
    language = getattr(result, "language", "en")
    total_results = getattr(result, "total_results", 0)
    shown_results = getattr(result, "shown_results", 0)
    has_more = getattr(result, "has_more", False)

    if summary:
        lines.append(summary)
    if answer:
        if lines:
            lines.append("")
        lines.append(answer)
    if total_results > 0 and shown_results > 0:
        lines.append("")
        lines.append(
            f"A mostrar {shown_results} de {total_results} resultados."
            if language == "pt"
            else f"Showing {shown_results} of {total_results} results."
        )
    if has_more:
        lines.append("")
        lines.append(
            "Peça 'mostrar mais resultados' para continuar."
            if language == "pt"
            else "Ask 'show more results' to continue."
        )
    return "\n".join(lines)


def _safe_audit(
    query_text: str,
    query_language: str | None,
    response_language: str,
    result_count: int,
    latency_ms: int,
    sender_id: str | None,
    chat_id: str | None = None,
    tool_calls_log: list | None = None,
) -> None:
    try:
        audit_query(
            query_text=query_text,
            query_language=query_language,
            response_language=response_language,
            result_count=result_count,
            latency_ms=latency_ms,
            agent_tool_calls=tool_calls_log or [],
            aad_object_id=sender_id,
            chat_id=chat_id,
        )
    except Exception:
        logger.exception("Graph bot: failed to write query audit log")