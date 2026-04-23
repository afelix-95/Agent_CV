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
import logging
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from agent_cv.config import settings
from agent_cv.services.agent_service import handle_user_query
from agent_cv.services.graph_service import get_graph_client
from agent_cv.services.query_service import audit_query, infer_intent

if TYPE_CHECKING:
    from msgraph import GraphServiceClient

logger = logging.getLogger(__name__)

_STRIP_HTML = re.compile(r"<[^>]+>")


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
            logger.debug("Graph bot resolved user ID: %s", self._bot_user_id)

        chats_page = await client.me.chats.get()
        for chat in chats_page.value or []:
            if chat.id:
                await self._process_chat(client, chat.id)

    async def _process_chat(
        self, client: "GraphServiceClient", chat_id: str
    ) -> None:
        from kiota_abstractions.base_request_configuration import RequestConfiguration
        from msgraph.generated.chats.item.messages.messages_request_builder import (
            MessagesRequestBuilder,
        )

        if chat_id not in self._seen:
            self._seen[chat_id] = set()

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
            )

        try:
            reply = ChatMessage(body=ItemBody(content=reply_text))
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
) -> None:
    try:
        audit_query(
            query_text=query_text,
            query_language=query_language,
            response_language=response_language,
            result_count=result_count,
            latency_ms=latency_ms,
            normalized_intent=infer_intent(query_text),
            aad_object_id=sender_id,
            chat_id=chat_id,
        )
    except Exception:
        logger.exception("Graph bot: failed to write query audit log")