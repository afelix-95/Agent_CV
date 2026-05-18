"""
Teams integration via Microsoft Graph Change Notifications (webhooks).

The bot authenticates as the service account (GRAPH_USER_EMAIL) through the
registered Entra app using the ROPC flow (see services/graph_service.py).
At startup it creates two Graph subscriptions:
  - /me/chats/getAllMessages  → POST to /graph-notifications/teams
    Delivers a notification for every new chat message.
  - /me/chats                 → POST to /graph-notifications/teams-chats
    Delivers a notification the instant a new chat is created (e.g. an
    external user initiating contact).  The handler immediately unhides /
    accepts that chat so the message subscription starts firing for it
    without waiting for the periodic poller.

Both subscriptions expire after 60 minutes (Graph delegated-auth maximum)
and are renewed automatically by a background task every 50 minutes.
A periodic fallback poller (default every 30 min) also scans for any
hidden or pending chats that slipped through.
"""
from __future__ import annotations

import asyncio
import base64
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
# Regex to find inline hosted-content image references inside Teams HTML message bodies.
# Example: <img src="../hostedContents/aWQ9L.../$value" ...>
_HOSTED_IMG_RE = re.compile(r'\.\.[\/]hostedContents[\/]([^\/"]+)[\/]\$value', re.IGNORECASE)
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
# How often (seconds) to poll for pending/hidden chats — fallback safety net only
_CHAT_POLL_INTERVAL_S: int = 1800  # 30 minutes


async def _download_graph_image(chat_id: str, message_id: str, hosted_content_id: str) -> str | None:
    """Download an inline image from a Teams message via the Graph hosted-contents endpoint.

    Returns a base64 data-URL string (``data:<mime>;base64,...``) or None on failure.
    The same delegated Graph token used for all other Graph calls is sufficient.
    """
    _MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB encoded limit
    try:
        token = await asyncio.to_thread(get_access_token)
        url = (
            f"https://graph.microsoft.com/v1.0"
            f"/chats/{chat_id}/messages/{message_id}"
            f"/hostedContents/{hosted_content_id}/$value"
        )
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as http:
            resp = await http.get(url, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            raw = resp.content
            mime = resp.headers.get("content-type", "image/png").split(";")[0].strip()
            if not mime.startswith("image/"):
                mime = "image/png"
        b64 = base64.b64encode(raw).decode("utf-8")
        if len(b64) > _MAX_IMAGE_BYTES:
            logger.warning(
                "Teams webhook bot: hosted-content image too large (%d bytes encoded) — skipping",
                len(b64),
            )
            return None
        return f"data:{mime};base64,{b64}"
    except Exception:
        logger.debug(
            "Teams webhook bot: failed to download hostedContent %s",
            hosted_content_id,
            exc_info=True,
        )
        return None


class TeamsWebhookBot:
    """Manages a Graph change-notification subscription and processes incoming
    Teams chat messages via the Agent CV query pipeline.

    Lifecycle (managed by the FastAPI lifespan in main.py):
      await bot.start()  — resolves bot identity, creates subscription, starts renewal task
                          and pending-chat poller
      await bot.stop()   — cancels both background tasks, deletes subscription
      await bot.handle_notification(resource, client_state)  — called from the webhook route
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._bot_user_id: str | None = None
        # Subscription ID for /me/chats/getAllMessages (new message events)
        self._subscription_id: str | None = None
        # Subscription ID for /me/chats (new chat created events)
        self._chat_subscription_id: str | None = None
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
        self._poll_task = asyncio.create_task(
            self._pending_chat_poll_loop(),
            name="pending-chat-poller",
        )
        logger.info(
            "Teams webhook bot initialised (subscription will be created once server is ready, account=%s)",
            settings.graph_user_email,
        )

    async def stop(self) -> None:
        for task in (self._task, self._poll_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        try:
            token = await asyncio.to_thread(get_access_token)
            async with httpx.AsyncClient(timeout=10.0) as http:
                for sub_id in filter(None, [self._subscription_id, self._chat_subscription_id]):
                    try:
                        await http.delete(
                            f"https://graph.microsoft.com/v1.0/subscriptions/{sub_id}",
                            headers={"Authorization": f"Bearer {token}"},
                        )
                        logger.info("Teams webhook bot: deleted subscription %s", sub_id)
                    except Exception:
                        logger.exception(
                            "Teams webhook bot: failed to delete subscription %s on shutdown", sub_id
                        )
        except Exception:
            logger.exception("Teams webhook bot: failed to acquire token for subscription cleanup")

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

        # Clean up any orphaned subscriptions from previous runs before creating a new one.
        # Without this, every restart leaves a stale subscription that Graph may keep
        # delivering to, or which can cause duplicate/missed notifications.
        await self._cleanup_stale_subscriptions()

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
                        "Teams webhook bot: failed to create initial message subscription after %d attempts",
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

        # Create the /me/chats subscription (new-chat events) with the same retry logic
        for attempt in range(1, 6):
            try:
                await self._create_chat_subscription()
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt == 5:
                    logger.exception(
                        "Teams webhook bot: failed to create initial chat subscription after %d attempts — "
                        "falling back to poller only",
                        attempt,
                    )
                else:
                    wait = attempt * 10
                    logger.warning(
                        "Teams webhook bot: chat subscription attempt %d failed, retrying in %ds",
                        attempt,
                        wait,
                    )
                    await asyncio.sleep(wait)

        await self._renewal_loop(renew_interval)

    async def _cleanup_stale_subscriptions(self) -> None:
        """Delete any existing Graph subscriptions pointing at our notificationUrl.

        This prevents duplicate delivery when the container restarts without a
        graceful shutdown (e.g. after a rebuild or OOM kill).
        """
        try:
            token = await asyncio.to_thread(get_access_token)
            async with httpx.AsyncClient(timeout=15.0) as http:
                resp = await http.get(
                    "https://graph.microsoft.com/v1.0/subscriptions",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code != 200:
                    logger.warning(
                        "Teams webhook bot: could not list subscriptions (HTTP %s) — skipping cleanup",
                        resp.status_code,
                    )
                    return

                our_urls = {
                    f"{settings.webhook_base_url}/graph-notifications/teams".rstrip("/"),
                    f"{settings.webhook_base_url}/graph-notifications/teams-chats".rstrip("/"),
                }
                stale = [
                    s["id"]
                    for s in resp.json().get("value", [])
                    if s.get("notificationUrl", "").rstrip("/") in our_urls
                ]

                for sub_id in stale:
                    del_resp = await http.delete(
                        f"https://graph.microsoft.com/v1.0/subscriptions/{sub_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    if del_resp.status_code in (200, 204):
                        logger.info("Teams webhook bot: deleted stale subscription %s", sub_id)
                    else:
                        logger.warning(
                            "Teams webhook bot: failed to delete stale subscription %s (HTTP %s)",
                            sub_id,
                            del_resp.status_code,
                        )
        except Exception:
            logger.exception("Teams webhook bot: error during stale subscription cleanup")

    async def _create_chat_subscription(self) -> None:
        """Create (or renew) a Graph subscription on /me/chats so we are notified
        immediately when a new chat appears — e.g. an external user initiating contact."""
        expiry = datetime.now(timezone.utc) + timedelta(seconds=_SUBSCRIPTION_LIFETIME_S)
        expiry_str = expiry.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        token = await asyncio.to_thread(get_access_token)

        async with httpx.AsyncClient(timeout=15.0) as http:
            if self._chat_subscription_id:
                resp = await http.patch(
                    f"https://graph.microsoft.com/v1.0/subscriptions/{self._chat_subscription_id}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={"expirationDateTime": expiry_str},
                )
                if resp.status_code == 200:
                    logger.info(
                        "Teams webhook bot: chat subscription %s renewed until %s",
                        self._chat_subscription_id,
                        expiry_str,
                    )
                    return
                logger.warning(
                    "Teams webhook bot: chat subscription renew returned HTTP %s — will recreate",
                    resp.status_code,
                )
                self._chat_subscription_id = None

            resp = await http.post(
                "https://graph.microsoft.com/v1.0/subscriptions",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "changeType": "created",
                    "notificationUrl": f"{settings.webhook_base_url}/graph-notifications/teams-chats",
                    "resource": "/me/chats",
                    "expirationDateTime": expiry_str,
                    "clientState": settings.webhook_secret,
                },
            )

        if resp.status_code == 201:
            self._chat_subscription_id = resp.json()["id"]
            logger.info(
                "Teams webhook bot: created chat subscription %s (expires %s)",
                self._chat_subscription_id,
                expiry_str,
            )
        else:
            raise RuntimeError(
                f"Failed to create Graph chat subscription: HTTP {resp.status_code}: {resp.text[:300]}"
            )

    async def _renewal_loop(self, interval: int) -> None:
        while True:
            await asyncio.sleep(interval)
            # Renew both subscriptions; failures on either are logged but don't abort the loop
            for create_fn, label in [
                (self._create_subscription, "message"),
                (self._create_chat_subscription, "chat"),
            ]:
                for attempt in range(1, 6):
                    try:
                        await create_fn()
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if attempt == 5:
                            logger.exception(
                                "Teams webhook bot: %s subscription renewal failed after %d attempts — "
                                "will retry at next interval",
                                label,
                                attempt,
                            )
                        else:
                            wait = attempt * 15
                            logger.warning(
                                "Teams webhook bot: %s subscription renewal attempt %d failed, retrying in %ds",
                                label,
                                attempt,
                                wait,
                            )
                            await asyncio.sleep(wait)

    # ------------------------------------------------------------------ #
    # Pending / hidden chat poller                                        #
    # ------------------------------------------------------------------ #

    async def _pending_chat_poll_loop(self) -> None:
        """Periodically fetch all chats visible to the service account and
        unhide / accept any that are hidden or have a pending membership.

        This fixes two problems:
        1. External users whose first message lands as a pending request —
           Graph does not fire change notifications for pending chats, so the
           reactive _accept_chat() path is never reached.  The poller accepts
           those chats proactively so subsequent messages are notified normally.
        2. Previously-accepted external chats that Teams re-hides after a
           period of inactivity — once hidden the subscription stops delivering
           notifications for that chat.

        The loop runs every _CHAT_POLL_INTERVAL_S seconds (default 5 min).
        """
        # Short initial delay so startup noise settles first
        await asyncio.sleep(15)
        while True:
            try:
                await self._accept_pending_and_hidden_chats()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Teams webhook bot: error in pending-chat poll loop")
            await asyncio.sleep(_CHAT_POLL_INTERVAL_S)

    async def _accept_pending_and_hidden_chats(self) -> None:
        """Single pass: list all chats, then unhide hidden ones and accept
        any pending memberships the service account has been invited to."""
        if not self._bot_user_id:
            return

        token = await asyncio.to_thread(get_access_token)
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(timeout=20.0) as http:
            # 1. Fetch all chats (the service account may have many; page through them)
            url: str | None = (
                "https://graph.microsoft.com/v1.0/me/chats"
                "?$select=id,chatType,viewpoint&$top=50"
            )
            chats_processed = 0
            while url:
                resp = await http.get(url, headers=headers)
                if resp.status_code != 200:
                    logger.warning(
                        "Teams webhook bot: chat list returned HTTP %s — %s",
                        resp.status_code,
                        resp.text[:300],
                    )
                    return
                data = resp.json()
                for chat in data.get("value", []):
                    chat_id: str = chat.get("id", "")
                    if not chat_id:
                        continue
                    viewpoint = chat.get("viewpoint") or {}
                    is_hidden = viewpoint.get("isHidden", False)
                    if is_hidden:
                        logger.info(
                            "Teams webhook bot: chat %s is hidden — calling unhideForUser",
                            chat_id,
                        )
                        await self._unhide_chat(http, headers, chat_id)
                    chats_processed += 1
                url = data.get("@odata.nextLink")

            logger.debug(
                "Teams webhook bot: pending-chat poll complete (%d chats checked)",
                chats_processed,
            )

            # 2. Accept any pending membership invitations.
            # Graph exposes these under /me/joinedTeams for Teams channels, but for
            # 1:1 and group chats the pending state is surfaced via the viewpoint
            # isHidden flag handled above.  Additionally check the
            # /me/chats?$filter=... pending endpoint if available.
            await self._accept_pending_memberships(http, headers)

    async def _unhide_chat(
        self,
        http: httpx.AsyncClient,
        headers: dict,
        chat_id: str,
    ) -> None:
        """Call unhideForUser to make a hidden chat visible again so the
        Graph subscription resumes delivering notifications for it."""
        resp = await http.post(
            f"https://graph.microsoft.com/v1.0/chats/{chat_id}/unhideForUser",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "user": {
                    "@odata.type": "#microsoft.graph.teamworkUserIdentity",
                    "id": self._bot_user_id,
                    "tenantId": settings.teams_bot_tenant_id,
                }
            },
        )
        if resp.status_code in (200, 204):
            logger.info("Teams webhook bot: unhid chat %s via poller", chat_id)
            self._accepted_chats.add(chat_id)
        else:
            logger.warning(
                "Teams webhook bot: unhideForUser (poller) returned HTTP %s for %s: %s",
                resp.status_code,
                chat_id,
                resp.text[:300],
            )

    async def _accept_pending_memberships(
        self,
        http: httpx.AsyncClient,
        headers: dict,
    ) -> None:
        """Accept pending chat membership invitations via the members endpoint.

        When an external user starts a brand-new 1:1 with the service account,
        Teams creates a chatMember entry in state 'pending'.  We enumerate all
        1:1 chats and accept any pending membership we find for the bot's own
        user ID.
        """
        # List 1:1 chats only (oneOnOne), paging through results
        url: str | None = (
            "https://graph.microsoft.com/v1.0/me/chats"
            "?$filter=chatType eq 'oneOnOne'&$select=id&$top=50"
        )
        while url:
            resp = await http.get(url, headers=headers)
            if resp.status_code != 200:
                logger.debug(
                    "Teams webhook bot: membership poll: chat list HTTP %s", resp.status_code
                )
                return
            data = resp.json()
            for chat in data.get("value", []):
                chat_id: str = chat.get("id", "")
                if not chat_id:
                    continue
                await self._accept_pending_member(http, headers, chat_id)
            url = data.get("@odata.nextLink")

    async def _accept_pending_member(
        self,
        http: httpx.AsyncClient,
        headers: dict,
        chat_id: str,
    ) -> None:
        """Accept pending membership for the bot's own account in a given chat."""
        members_resp = await http.get(
            f"https://graph.microsoft.com/v1.0/chats/{chat_id}/members",
            headers=headers,
        )
        if members_resp.status_code != 200:
            return
        for member in members_resp.json().get("value", []):
            user_id = (member.get("userId") or "")
            membership_id = member.get("id", "")
            # Only act on our own membership if it is in a pending/unknown state
            if user_id != self._bot_user_id:
                continue
            visible_history_start = member.get("visibleHistoryStartDateTime")
            # Graph does not expose a "pending" boolean directly; a missing
            # visibleHistoryStartDateTime on our own membership is a reliable
            # indicator that the membership has not been acknowledged yet.
            if visible_history_start is not None:
                continue
            if not membership_id:
                continue
            accept_resp = await http.post(
                f"https://graph.microsoft.com/v1.0/chats/{chat_id}/members/{membership_id}/acceptMembership",
                headers={**headers, "Content-Type": "application/json"},
                json={},
            )
            if accept_resp.status_code in (200, 204):
                logger.info(
                    "Teams webhook bot: accepted pending membership %s in chat %s",
                    membership_id,
                    chat_id,
                )
                self._accepted_chats.add(chat_id)
            else:
                logger.warning(
                    "Teams webhook bot: acceptMembership returned HTTP %s for chat %s: %s",
                    accept_resp.status_code,
                    chat_id,
                    accept_resp.text[:300],
                )

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
        # Extract inline images from hosted contents (pasted screenshots, etc.)
        images: list[str] = []
        if content_type_val == "html" and raw_content:
            hosted_ids = _HOSTED_IMG_RE.findall(raw_content)
            for hid in hosted_ids[:4]:  # cap at 4 images per message
                data_url = await _download_graph_image(chat_id, message_id, hid)
                if data_url:
                    images.append(data_url)
                    logger.debug(
                        "Teams webhook bot: downloaded hosted-content image %s (%d chars)",
                        hid,
                        len(data_url),
                    )

        text = (
            _STRIP_HTML.sub("", raw_content).strip()
            if content_type_val == "html"
            else raw_content.strip()
        )

        if not text and not images:
            return

        # Provide a neutral fallback query when the user sends only an image
        if not text and images:
            text = "Analisa a imagem em anexo."

        logger.info(
            "Teams webhook bot: new message %s in chat %s — %.80s", message_id, chat_id, text
        )
        await self._handle_message(client, chat_id, message_id, text, sender_id, images or None)

    async def handle_chat_notification(self, resource: str, client_state: str) -> None:
        """Handle a Graph change notification for /me/chats (new chat created).

        Graph fires this the instant a new chat appears in the service account's
        list — typically when an external user initiates first contact.  We
        immediately unhide / accept the chat so the message subscription starts
        delivering notifications for it without waiting for the periodic poller.
        """
        if client_state != settings.webhook_secret:
            logger.warning(
                "Teams webhook bot: chat notification with unexpected clientState, ignoring"
            )
            return

        # resource is typically "chats('chatId')" or "chats/chatId"
        m = re.search(r"chats[/(']+([^/')]+)", resource)
        if not m:
            logger.warning(
                "Teams webhook bot: could not parse chat resource URL: %s", resource
            )
            return

        chat_id = m.group(1)
        logger.info(
            "Teams webhook bot: new chat notification for %s — accepting immediately", chat_id
        )

        if chat_id not in self._accepted_chats:
            token = await asyncio.to_thread(get_access_token)
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(timeout=10.0) as http:
                await self._unhide_chat(http, headers, chat_id)
                await self._accept_pending_member(http, headers, chat_id)

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
        images: list[str] | None = None,
    ) -> None:
        from msgraph.generated.models.body_type import BodyType
        from msgraph.generated.models.chat_message import ChatMessage
        from msgraph.generated.models.item_body import ItemBody

        started = time.perf_counter()
        result = None
        try:
            result = await asyncio.to_thread(handle_user_query, text, None, chat_id, images)
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
            reply_chunks = _split_into_chunks(reply_text)
            _result_lang = getattr(result, "language", "en")
            for chunk_index, chunk in enumerate(reply_chunks):
                if len(reply_chunks) > 1:
                    label = (
                        f"*Parte {chunk_index + 1}/{len(reply_chunks)}*\n\n"
                        if _result_lang == "pt"
                        else f"*Part {chunk_index + 1}/{len(reply_chunks)}*\n\n"
                    )
                    chunk = label + chunk
                reply = ChatMessage(body=ItemBody(
                    content=_text_to_html(chunk),
                    content_type=BodyType.Html,
                ))
                await client.chats.by_chat_id(chat_id).messages.post(reply)
                if chunk_index < len(reply_chunks) - 1:
                    await asyncio.sleep(0.3)
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


def _apply_inline_markdown(raw: str) -> str:
    """Convert inline markdown to HTML. Accepts raw (un-escaped) text."""
    # Extract markdown links [label](url) before HTML-escaping so & in URLs
    # doesn't get mangled. Replace them with a placeholder, escape the rest,
    # then restore.
    _LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
    placeholders: list[tuple[str, str, str]] = []

    def _stash_link(m: re.Match) -> str:
        idx = len(placeholders)
        placeholders.append((f"\x00LINK{idx}\x00", m.group(1), m.group(2)))
        return placeholders[-1][0]

    raw_with_stash = _LINK_RE.sub(_stash_link, raw)
    text = _html.escape(raw_with_stash)

    # Restore links
    for placeholder, label, url in placeholders:
        escaped_placeholder = _html.escape(placeholder)
        text = text.replace(
            escaped_placeholder,
            f'<a href="{url}">{_html.escape(label)}</a>',
        )

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
    """Convert LLM markdown output (bullets, nested bullets, tables, headings, bold/italic)
    to Teams-compatible HTML.

    List depth state machine:
      depth 0 — not in any list
      depth 1 — inside <ul>, current <li> is OPEN (no closing tag yet)
      depth 2 — inside nested <ul> within the open <li> at depth 1
    """
    lines = text.split("\n")
    parts: list[str] = []
    depth = 0  # 0 | 1 | 2
    table_buf: list[str] = []

    def flush_table() -> None:
        if table_buf:
            parts.append(_parse_table(table_buf))
            table_buf.clear()

    def close_lists() -> None:
        nonlocal depth
        if depth == 2:
            parts.append("</ul></li></ul>")
        elif depth == 1:
            parts.append("</li></ul>")
        depth = 0

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" \t"))

        # Accumulate markdown pipe-table rows
        if _is_table_row(stripped):
            close_lists()
            table_buf.append(stripped)
            continue

        flush_table()

        # ATX headings: # H1, ## H2, ### H3
        heading_match = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading_match:
            close_lists()
            level = len(heading_match.group(1))
            parts.append(f"<h{level}>{_html.escape(heading_match.group(2))}</h{level}>")
            continue

        # Horizontal rule --- or ***
        if re.fullmatch(r"[-*_]{3,}", stripped):
            close_lists()
            parts.append("<hr/>")
            continue

        # Bullet points: •, -, *, + (top-level when indent < 2, nested otherwise)
        bullet_match = re.match(r"^[•\-\*\+]\s+(.*)", stripped)
        if bullet_match:
            content = _apply_inline_markdown(bullet_match.group(1).strip())
            if indent < 2:
                # Top-level bullet
                if depth == 2:
                    parts.append("</ul></li>")  # close nested ul, keep outer ul open
                    depth = 1
                    parts.append(f"<li>{content}")
                elif depth == 1:
                    parts.append(f"</li><li>{content}")
                else:
                    parts.append(f"<ul><li>{content}")
                    depth = 1
            else:
                # Indented bullet → nested
                if depth == 0:
                    parts.append(f"<ul><li>{content}")
                    depth = 1
                elif depth == 1:
                    parts.append(f"<ul><li>{content}</li>")
                    depth = 2
                else:
                    parts.append(f"<li>{content}</li>")
            continue

        # Numbered list: 1. item
        numbered_match = re.match(r"^(\s*)\d+\.\s+(.*)", line)
        if numbered_match:
            content = _apply_inline_markdown(numbered_match.group(2).strip())
            item_indent = len(numbered_match.group(1))
            if item_indent < 2:
                if depth == 2:
                    parts.append("</ul></li>")
                    depth = 1
                    parts.append(f"<li>{content}")
                elif depth == 1:
                    parts.append(f"</li><li>{content}")
                else:
                    parts.append(f"<ul><li>{content}")
                    depth = 1
            else:
                if depth == 0:
                    parts.append(f"<ul><li>{content}")
                    depth = 1
                elif depth == 1:
                    parts.append(f"<ul><li>{content}</li>")
                    depth = 2
                else:
                    parts.append(f"<li>{content}</li>")
            continue

        close_lists()
        if stripped:
            content = _apply_inline_markdown(stripped)
            parts.append(f"<p>{content}</p>")

    flush_table()
    close_lists()

    return "".join(parts)


# Maximum characters per Teams message. Teams supports large HTML bodies, but
# keeping chunks reasonable improves readability and avoids API edge cases.
_MAX_CHUNK_CHARS = 12_000


def _split_into_chunks(text: str) -> list[str]:
    """Split a markdown response into chunks at double-newline (paragraph) boundaries.

    Tries to keep each chunk under _MAX_CHUNK_CHARS. Falls back to splitting on
    single newlines if a single paragraph is itself oversized.
    """
    if len(text) <= _MAX_CHUNK_CHARS:
        return [text]

    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        # +2 for the "\n\n" separator we'll add between paragraphs
        needed = len(para) + (2 if current else 0)
        if current and current_len + needed > _MAX_CHUNK_CHARS:
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = len(para)
        else:
            current.append(para)
            current_len += needed

    if current:
        chunks.append("\n\n".join(current))

    return chunks or [text]


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