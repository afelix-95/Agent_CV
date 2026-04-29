from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Any

import msal
from azure.core.credentials import AccessToken
from msgraph import GraphServiceClient

from agent_cv.config import settings

logger = logging.getLogger(__name__)

_DEFAULT_SCOPES = ["https://graph.microsoft.com/.default"]


class _RopcCredential:
    """Delegated-access TokenCredential using confidential-client ROPC flow via MSAL.

    The bot service account (username/password) authenticates through the registered
    Entra application (client_id/client_secret).  MSAL handles in-memory token caching
    and silent refresh automatically via acquire_token_silent before falling back to a
    fresh ROPC grant.

    Note: ROPC requires the service account to have no MFA and no conditional-access
    policies that block non-interactive sign-in.  Federated identities are not supported.
    """

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        username: str,
        password: str,
        scopes: list[str],
    ) -> None:
        self._msal_app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
        )
        self._username = username
        self._password = password
        self._scopes = scopes

    # azure.core.credentials.TokenCredential protocol
    def get_token(
        self,
        *scopes: str,
        claims: str | None = None,  # noqa: ARG002
        tenant_id: str | None = None,  # noqa: ARG002
        **kwargs: Any,
    ) -> AccessToken:
        effective_scopes = list(scopes) if scopes else self._scopes

        # Try the MSAL in-memory cache / silent refresh first
        accounts = self._msal_app.get_accounts(username=self._username)
        result: dict[str, Any] | None = None
        if accounts:
            result = self._msal_app.acquire_token_silent(
                scopes=effective_scopes,
                account=accounts[0],
            )

        # Fall back to a fresh ROPC grant if no cached token is available
        if not result:
            result = self._msal_app.acquire_token_by_username_password(
                username=self._username,
                password=self._password,
                scopes=effective_scopes,
            )

        if not result or "access_token" not in result:
            error = result.get("error", "unknown_error") if result else "no_response"
            description = result.get("error_description", "") if result else ""
            raise RuntimeError(
                f"Microsoft Graph ROPC token acquisition failed [{error}]: {description}"
            )

        expires_on = int(time.time()) + int(result.get("expires_in", 3600))
        return AccessToken(result["access_token"], expires_on)


def graph_configured() -> bool:
    """Return True when all required Microsoft Graph settings are present."""
    return all(
        bool(v)
        for v in (
            settings.teams_bot_app_id,
            settings.teams_bot_app_password,
            settings.teams_bot_tenant_id,
            settings.graph_user_email,
            settings.graph_user_password,
        )
    )


def graph_setup_issue() -> str | None:
    """Return a human-readable description of missing Graph configuration, or None."""
    if graph_configured():
        return None
    missing = [
        name
        for name, value in (
            ("TEAMS_BOT_APP_ID", settings.teams_bot_app_id),
            ("TEAMS_BOT_APP_PASSWORD", settings.teams_bot_app_password),
            ("TEAMS_BOT_TENANT_ID", settings.teams_bot_tenant_id),
            ("GRAPH_USER_EMAIL", settings.graph_user_email),
            ("GRAPH_USER_PASSWORD", settings.graph_user_password),
        )
        if not bool(value)
    ]
    return f"Microsoft Graph is not configured. Missing: {', '.join(missing)}."


@lru_cache(maxsize=1)
def _get_credential() -> _RopcCredential:
    """Return the cached ROPC credential for the bot service account."""
    issue = graph_setup_issue()
    if issue:
        raise RuntimeError(issue)

    scopes = settings.graph_scopes.split() if settings.graph_scopes else _DEFAULT_SCOPES
    return _RopcCredential(
        tenant_id=settings.teams_bot_tenant_id,
        client_id=settings.teams_bot_app_id,
        client_secret=settings.teams_bot_app_password,
        username=settings.graph_user_email,
        password=settings.graph_user_password,
        scopes=scopes,
    )


@lru_cache(maxsize=1)
def get_graph_client() -> GraphServiceClient:
    """Return a cached Microsoft Graph client authenticated via delegated ROPC flow.

    The bot service account (GRAPH_USER_EMAIL / GRAPH_USER_PASSWORD) authenticates
    on behalf of a user through the registered Entra application (TEAMS_BOT_APP_ID).
    The tenant admin must pre-consent all required delegated permissions for the app
    registration; the scope GRAPH_SCOPES controls which permissions are requested
    (defaults to 'https://graph.microsoft.com/.default').

    Raises RuntimeError if Graph settings are incomplete.
    """
    credential = _get_credential()
    scopes = settings.graph_scopes.split() if settings.graph_scopes else _DEFAULT_SCOPES
    return GraphServiceClient(credentials=credential, scopes=scopes)


def get_access_token() -> str:
    """Return a raw Bearer access token for the configured ROPC service account.

    Uses the cached MSAL token (with silent refresh) so repeated calls are cheap.
    """
    scopes = settings.graph_scopes.split() if settings.graph_scopes else _DEFAULT_SCOPES
    token = _get_credential().get_token(*scopes)
    return token.token
