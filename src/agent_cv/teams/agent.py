"""
Teams integration via Microsoft Graph delegated polling.

The bot authenticates as the service account (GRAPH_USER_EMAIL) through the
registered Entra app using the ROPC flow (see services/graph_service.py).
It polls every GRAPH_POLL_INTERVAL seconds for new messages in Teams chats
where that account is a participant, processes queries through the Agent CV
pipeline, and replies in the same chat.

Previous approach (Microsoft 365 Agents SDK / webhook-based) has been removed
to keep a single integration path.  The microsoft-agents-* packages are kept
in requirements.txt (commented out) in case a webhook approach is needed later.
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from agent_cv.config import settings
from agent_cv.services.agent_service import handle_user_query
from agent_cv.services.graph_service import get_access_token, get_graph_client
from agent_cv.services.query_service import audit_query

if TYPE_CHECKING:
    from msgraph import GraphServiceClient

logger = logging.getLogger(__name__)

_STRIP_HTML = re.compile(r"<[^>]+>")
# Re-set Teams presence before the 1-hour session expires (every 55 minutes)
_PRESENCE_REFRESH_INTERVAL: int = 3300


class GraphPollingBot:
    """Background asyncio task that polls Graph for new Teams chat messages
    and replies using the Agent CV query pipeline.

    Lifecycle is managed by the FastAPI lifespan in main.py.
    """

    def __init__(self, poll_interval: int = 10) -> None:
        self._poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        # Only process messages created after the bot started
        self._started_at: datetime = datetime.now(timezone.utc)
        # chat_id -> set of already-processed message IDs (within this run)
        self._seen: dict[str, set[str]] = {}
        self._bot_user_id: str | None = None
        # Tracks when presence was last refreshed (monotonic seconds)
        self._last_presence_set: float = 0.0
        # chat IDs for which acceptance has succeeded (survives across message polling)
        self._accepted_chats: set[str] = set()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._poll_loop(), name="graph-polling-bot"
            )
            logger.info(
                "Graph polling bot started (interval=%ss, account=%s, ignoring messages before %s)",
                self._poll_interval,
                settings.graph_user_email,
                self._started_at.isoformat(),
            )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("Graph polling bot stopped")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------ #
    # Polling loop                                                         #
    # ------------------------------------------------------------------ #

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Graph polling bot: unhandled error in tick")
            await asyncio.sleep(self._poll_interval)

    async def _tick(self) -> None:
        client = get_graph_client()

        # Resolve the bot's own user ID once so we never reply to ourselves
        if self._bot_user_id is None:
            me = await client.me.get()
            self._bot_user_id = me.id
            logger.info("Graph bot resolved user ID: %s", self._bot_user_id)

        # Keep the service account's Teams presence as Available
        now = time.monotonic()
        if now - self._last_presence_set >= _PRESENCE_REFRESH_INTERVAL:
            await self._set_presence_available()
            self._last_presence_set = now

        import httpx

        # Use httpx directly so we can pass includeHiddenChats=true — the SDK
        # does not expose this parameter and omitting it hides pending external chats.
        token = await asyncio.to_thread(get_access_token)
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.get(
                "https://graph.microsoft.com/v1.0/me/chats",
                params={"includeHiddenChats": "true", "$top": "50"},
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code != 200:
            logger.warning("Graph bot: GET /me/chats returned HTTP %s: %s", resp.status_code, resp.text[:200])
            return
        chats = resp.json().get("value", [])
        for chat in chats:
            chat_id = chat.get("id")
            if chat_id:
                await self._process_chat(client, chat_id)

    async def _accept_chat(self, chat_id: str) -> bool:
        """Accept a pending external/federated chat so the conversation stays open.

        Federated (cross-tenant) chats land in a hidden/pending state until the
        recipient explicitly accepts them.  POST /chats/{id}/unhideForUser is the
        correct Graph endpoint for this; it unhides the chat for the given user and
        clears the "needs to accept" block seen by the external sender.

        Returns True if the chat was successfully accepted, False otherwise
        (caller will retry on the next tick).

        Requires Chat.ReadWrite delegated permission (already needed for reading messages).
        """
        import httpx

        if not self._bot_user_id:
            logger.warning("Graph bot: _accept_chat skipped — bot user ID not yet resolved")
            return False

        logger.info("Graph bot: calling unhideForUser on %s", chat_id)
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
                logger.info("Graph bot: accepted (unhid) chat %s", chat_id)
                return True
            else:
                logger.warning(
                    "Graph bot: unhideForUser returned HTTP %s for %s: %s",
                    resp.status_code,
                    chat_id,
                    resp.text[:300],
                )
                return False
        except Exception:
            logger.exception("Graph bot: failed to accept chat %s", chat_id)
            return False

    async def _send_greeting(self, token: str, chat_id: str) -> None:
        """Send an initial greeting message to unblock the external sender.

        For federated cross-tenant chats, sending a message from the bot's side
        is what fully completes the acceptance handshake and allows the external
        user to reply freely in their Teams client.
        """
        import httpx

        greeting = (
            "Hello! I'm the CV Finder assistant. "
            "Ask me to search for employees by certification, skill, or technology."
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.post(
                    f"https://graph.microsoft.com/v1.0/chats/{chat_id}/messages",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "body": {"contentType": "text", "content": greeting}
                    },
                )
            if resp.status_code in (200, 201):
                logger.info("Graph bot: sent greeting to %s", chat_id)
            else:
                logger.warning(
                    "Graph bot: greeting failed HTTP %s for %s: %s",
                    resp.status_code, chat_id, resp.text[:200],
                )
        except Exception:
            logger.exception("Graph bot: failed to send greeting to %s", chat_id)

    async def _set_presence_available(self) -> None:
        """Set the bot service account's Teams presence to Available.

        Uses the Graph REST API directly with a Bearer token because the Graph SDK
        does not expose a simple wrapper for the setPresence endpoint.

        Requires the Entra app registration to have the delegated permission
        ``Presence.ReadWrite`` consented by the tenant admin.
        """
        import httpx

        try:
            token = await asyncio.to_thread(get_access_token)
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.post(
                    "https://graph.microsoft.com/v1.0/me/presence/setPresence",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "sessionId": settings.teams_bot_app_id,
                        "availability": "Available",
                        "activity": "Available",
                        "expirationDuration": "PT1H",
                    },
                )
            if resp.status_code in (200, 204):
                logger.info("Graph bot: Teams presence set to Available")
            else:
                logger.warning(
                    "Graph bot: setPresence returned HTTP %s — "
                    "ensure Presence.ReadWrite is consented for app %s. Response: %s",
                    resp.status_code,
                    settings.teams_bot_app_id,
                    resp.text[:200],
                )
        except Exception:
            logger.exception("Graph bot: failed to set Teams presence")

    async def _process_chat(
        self, client: "GraphServiceClient", chat_id: str
    ) -> None:
        from kiota_abstractions.base_request_configuration import RequestConfiguration
        from msgraph.generated.chats.item.messages.messages_request_builder import (
            MessagesRequestBuilder,
        )

        # Track accepted chats separately from seen message IDs so that a failed
        # accept attempt is retried on the next tick (unlike _seen which persists).
        if chat_id not in self._seen:
            self._seen[chat_id] = set()
        if chat_id not in self._accepted_chats:
            accepted = await self._accept_chat(chat_id)
            if accepted:
                self._accepted_chats.add(chat_id)

        query_params = MessagesRequestBuilder.MessagesRequestBuilderGetQueryParameters(
            top=50,
        )
        msgs_page = await client.chats.by_chat_id(chat_id).messages.get(
            request_configuration=RequestConfiguration(query_parameters=query_params)
        )
        messages = msgs_page.value or []

        for msg in messages:
            msg_id = msg.id
            if not msg_id or msg_id in self._seen[chat_id]:
                continue

            # Skip messages that existed before the bot started
            created = getattr(msg, "created_date_time", None)
            if created is not None:
                # Normalise to UTC-aware for comparison
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created < self._started_at:
                    self._seen[chat_id].add(msg_id)
                    continue

            self._seen[chat_id].add(msg_id)

            # Skip messages sent by the bot itself
            from_field = getattr(msg, "from_property", None) or getattr(msg, "from_", None)
            sender_user = getattr(from_field, "user", None) if from_field else None
            sender_id = getattr(sender_user, "id", None) if sender_user else None
            if sender_id and sender_id == self._bot_user_id:
                continue

            # Only process regular chat messages (skip system events, typing, etc.)
            msg_type = getattr(msg, "message_type", None)
            msg_type_val = getattr(msg_type, "value", str(msg_type)) if msg_type else ""
            if msg_type_val != "message":
                continue

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
                continue

            logger.info(
                "Graph bot: new message %s in chat %s — %.80s", msg_id, chat_id, text
            )
            await self._handle_message(client, chat_id, msg_id, text, sender_id)

    async def _handle_message(
        self,
        client: "GraphServiceClient",
        chat_id: str,
        msg_id: str,
        text: str,
        sender_id: str | None,
    ) -> None:
        from msgraph.generated.models.chat_message import ChatMessage
        from msgraph.generated.models.item_body import ItemBody

        started = time.perf_counter()
        try:
            result = await asyncio.to_thread(handle_user_query, text, None, chat_id)
        except Exception:
            logger.exception(
                "Graph bot: query pipeline error for message %s", msg_id
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
            from msgraph.generated.models.body_type import BodyType
            reply = ChatMessage(body=ItemBody(
                content=_text_to_html(reply_text),
                content_type=BodyType.Html,
            ))
            await client.chats.by_chat_id(chat_id).messages.post(reply)
        except Exception:
            logger.exception(
                "Graph bot: failed to post reply for message %s", msg_id
            )


# ------------------------------------------------------------------ #
# Module-level singleton                                              #
# ------------------------------------------------------------------ #

_bot: GraphPollingBot | None = None


def get_graph_bot() -> GraphPollingBot:
    global _bot
    if _bot is None:
        _bot = GraphPollingBot(poll_interval=settings.graph_poll_interval)
    return _bot


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


def _text_to_html(text: str) -> str:
    """Convert LLM plain-text output (bullets, tables, newlines) to Teams-compatible HTML."""
    lines = text.split("\n")
    parts: list[str] = []
    in_list = False
    table_buf: list[str] = []

    def flush_table() -> None:
        if table_buf:
            parts.append(_parse_table(table_buf))
            table_buf.clear()

    for line in lines:
        stripped = line.strip()

        # Accumulate markdown pipe-table rows
        if _is_table_row(stripped):
            if in_list:
                parts.append("</ul>")
                in_list = False
            table_buf.append(stripped)
            continue

        # Non-table line — flush any buffered table first
        flush_table()

        if stripped.startswith("•"):
            if not in_list:
                parts.append("<ul>")
                in_list = True
            parts.append(f"<li>{_html.escape(stripped[1:].strip())}</li>")
        else:
            if in_list:
                parts.append("</ul>")
                in_list = False
            if stripped:
                parts.append(f"<p>{_html.escape(stripped)}</p>")

    flush_table()
    if in_list:
        parts.append("</ul>")

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