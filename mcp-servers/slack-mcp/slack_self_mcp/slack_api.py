from __future__ import annotations

import json
from typing import Any, Final

import httpx

SLACK_API_BASE_URL: Final[str] = "https://slack.com/api"
DEFAULT_TIMEOUT_SECONDS: Final[float] = 15.0

# Slack error codes that mean the forwarded credential is unusable, so the caller
# must re-authorize rather than retry.
TOKEN_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "account_inactive",
        "expired_token",
        "invalid_auth",
        "invalid_token",
        "not_authed",
        "token_expired",
        "token_revoked",
    }
)


class SlackApiError(RuntimeError):
    def __init__(self, method: str, error: str) -> None:
        super().__init__(f"Slack {method} failed: {error}")
        self.method: Final = method
        self.error: Final = error

    @property
    def is_token_error(self) -> bool:
        return self.error in TOKEN_ERROR_CODES


async def call_slack_api(
    client: httpx.AsyncClient,
    token: str,
    method: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    response: Final = await client.post(
        f"{SLACK_API_BASE_URL}/{method}",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    response.raise_for_status()
    data: Final = response.json()
    if not isinstance(data, dict) or data.get("ok") is not True:
        error: Final = data.get("error", "invalid_response") if isinstance(data, dict) else "invalid_response"
        raise SlackApiError(method, str(error))
    return data


async def authenticated_identity(client: httpx.AsyncClient, token: str) -> tuple[str, str]:
    """Return the token's own Slack user ID and workspace URL."""
    identity: Final = await call_slack_api(client, token, "auth.test", {})
    user_id: Final = identity.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        raise SlackApiError("auth.test", "missing_user_id")

    workspace_url: Final = identity.get("url")
    return user_id, workspace_url if isinstance(workspace_url, str) else ""


def _message_link(workspace_url: str, channel_id: str, message_ts: str) -> str | None:
    if not workspace_url or not channel_id or not message_ts:
        return None
    return f"{workspace_url.rstrip('/')}/archives/{channel_id}/p{message_ts.replace('.', '')}"


async def send_message_to_self(client: httpx.AsyncClient, token: str, message: str) -> str:
    """Send ``message`` to the token owner's own DM and return a JSON result string."""
    user_id, workspace_url = await authenticated_identity(client, token)
    posted: Final = await call_slack_api(
        client,
        token,
        "chat.postMessage",
        {"channel": user_id, "text": message},
    )

    channel_id: Final = posted.get("channel")
    message_ts: Final = posted.get("ts")
    result: Final = {
        "message_link": _message_link(
            workspace_url,
            channel_id if isinstance(channel_id, str) else "",
            message_ts if isinstance(message_ts, str) else "",
        ),
        "message_context": {
            "message_ts": message_ts,
            "channel_id": channel_id,
        },
    }
    return json.dumps(result)
