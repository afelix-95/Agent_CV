from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import logging
import os
import time
from typing import Any

from agent_cv.config import settings
from agent_cv.services.agent_service import handle_user_query
from agent_cv.services.query_service import audit_query, infer_intent

try:
    from microsoft_agents.activity import Activity, ActivityTypes, load_configuration_from_env
    from microsoft_agents.authentication.msal import MsalConnectionManager
    from microsoft_agents.hosting.core import (
        AgentApplication,
        Authorization,
        MemoryStorage,
        TurnContext,
        TurnState,
    )
    from microsoft_agents.hosting.fastapi import CloudAdapter
except ImportError:  # pragma: no cover - handled at runtime when SDK is absent
    Activity = None
    ActivityTypes = None
    load_configuration_from_env = None
    MsalConnectionManager = None
    AgentApplication = None
    Authorization = None
    MemoryStorage = None
    TurnContext = None
    TurnState = None
    CloudAdapter = None


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TeamsAgentRuntime:
    agent_app: Any
    adapter: Any


def teams_sdk_available() -> bool:
    return all(
        item is not None
        for item in (
            Activity,
            ActivityTypes,
            load_configuration_from_env,
            MsalConnectionManager,
            AgentApplication,
            Authorization,
            MemoryStorage,
            TurnState,
            CloudAdapter,
        )
    )


def teams_sdk_configured() -> bool:
    env = _teams_sdk_env()
    required = (
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID",
    )
    return all(bool(env.get(key)) for key in required)


def teams_setup_issue() -> str | None:
    if not teams_sdk_available():
        return "Microsoft 365 Agents SDK packages are not installed."
    if teams_sdk_configured():
        return None
    return (
        "Teams SDK is not configured. Set CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID, "
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET, and "
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID, or populate the legacy "
        "TEAMS_BOT_APP_ID, TEAMS_BOT_APP_PASSWORD, and TEAMS_BOT_TENANT_ID values."
    )


@lru_cache(maxsize=1)
def get_teams_agent_runtime() -> TeamsAgentRuntime:
    issue = teams_setup_issue()
    if issue:
        raise RuntimeError(issue)

    sdk_config = load_configuration_from_env(_teams_sdk_env())
    storage = MemoryStorage()
    connection_manager = MsalConnectionManager(**sdk_config)
    adapter = CloudAdapter(connection_manager=connection_manager)
    authorization = Authorization(storage, connection_manager, **sdk_config)
    agent_app = AgentApplication[TurnState](
        storage=storage,
        adapter=adapter,
        authorization=authorization,
        **sdk_config.get("AGENTAPPLICATION", {}),
    )

    _register_handlers(agent_app)
    return TeamsAgentRuntime(agent_app=agent_app, adapter=adapter)


def _register_handlers(agent_app: Any) -> None:
    @agent_app.conversation_update("membersAdded")
    async def on_members_added(context: TurnContext, _state: TurnState) -> bool:
        await context.send_activity(_welcome_message())
        return True

    @agent_app.activity(ActivityTypes.invoke)
    async def on_invoke(context: TurnContext, _state: TurnState) -> None:
        invoke_response = Activity(
            type=ActivityTypes.invoke_response,
            value={"status": 200},
        )
        await context.send_activity(invoke_response)

    @agent_app.activity(ActivityTypes.message)
    async def on_message(context: TurnContext, _state: TurnState) -> None:
        text = (context.activity.text or "").strip()
        if not text:
            await context.send_activity(_empty_query_message())
            return

        started = time.perf_counter()
        conversation = getattr(context.activity, "conversation", None)
        conversation_id = getattr(conversation, "id", None)
        result = handle_user_query(text, None, conversation_id)

        _safe_audit(
            query_text=text,
            query_language=None,
            response_language=result.language,
            result_count=result.total_results,
            latency_ms=int((time.perf_counter() - started) * 1000),
            teams_user_id=_teams_user_id(context),
        )
        await context.send_activity(_build_teams_reply(result.summary, result.answer, result.language, result.has_more))

    @agent_app.error
    async def on_error(context: TurnContext, error: Exception) -> None:
        logger.exception("Unhandled Teams agent error", exc_info=error)
        await context.send_activity(_error_message())


def _teams_sdk_env() -> dict[str, str]:
    env = {key.upper(): value for key, value in os.environ.items() if isinstance(value, str)}

    _set_if_missing(
        env,
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID",
        settings.teams_bot_app_id,
    )
    _set_if_missing(
        env,
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET",
        settings.teams_bot_app_password,
    )
    _set_if_missing(
        env,
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID",
        settings.teams_bot_tenant_id,
    )
    return env


def _set_if_missing(env: dict[str, str], key: str, value: str) -> None:
    if value and not env.get(key):
        env[key] = value


def _build_teams_reply(summary: str, answer: str, language: str, has_more: bool) -> str:
    lines: list[str] = []
    if summary:
        lines.append(summary)
    if answer:
        if lines:
            lines.append("")
        lines.append(answer)
    if has_more:
        lines.extend([
            "",
            "Peça 'mostrar mais resultados' para continuar." if language == "pt" else "Ask 'show more results' to continue.",
        ])
    return "\n".join(lines)


def _welcome_message() -> str:
    return (
        "Hello. I can answer questions about certifications and employee experience. "
        "Try: 'Who has Red Hat certifications?' or 'Show experience with Cisco systems'."
    )


def _empty_query_message() -> str:
    return "Please send a question about certifications or employee experience."


def _error_message() -> str:
    return "An unexpected error occurred while processing your Teams message."


def _teams_user_id(context: Any) -> str | None:
    sender = getattr(context.activity, "from_property", None)
    return getattr(sender, "id", None)


def _safe_audit(
    query_text: str,
    query_language: str | None,
    response_language: str,
    result_count: int,
    latency_ms: int,
    teams_user_id: str | None,
) -> None:
    try:
        audit_query(
            query_text=query_text,
            query_language=query_language,
            response_language=response_language,
            result_count=result_count,
            latency_ms=latency_ms,
            normalized_intent=infer_intent(query_text),
            teams_user_id=teams_user_id,
        )
    except Exception:
        logger.exception("Failed to write Teams query audit log")