"""The tool API: what this server exposes, and the Slack calls behind each tool.

Slack Web API methods are private to :class:`SlackTools`. Only the tool calls are public,
and every one of them acts on the forwarded credential's own identity, never on a
recipient the caller chooses.
"""

from __future__ import annotations

import json
from typing import Any, Final

import httpx
from mcp.types import Tool

SLACK_API_BASE_URL: Final[str] = "https://slack.com/api"
DEFAULT_TIMEOUT_SECONDS: Final[float] = 15.0

MESSAGE_SELF_TOOL_NAME: Final[str] = "slack_message_self"
MESSAGE_SELF_TOOL_DESCRIPTION: Final[str] = "Send a private Slack message only to the authenticated Slack user."
MESSAGE_SELF_ARGUMENT: Final[str] = "message"
MESSAGE_SELF_ARGUMENT_DESCRIPTION: Final[str] = "Message to send to your own Slack direct message."

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


def tool_definitions() -> list[Tool]:
    """The tools this server exposes."""
    return [
        Tool(
            name=MESSAGE_SELF_TOOL_NAME,
            description=MESSAGE_SELF_TOOL_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    MESSAGE_SELF_ARGUMENT: {
                        "type": "string",
                        "description": MESSAGE_SELF_ARGUMENT_DESCRIPTION,
                    }
                },
                "required": [MESSAGE_SELF_ARGUMENT],
            },
        )
    ]


class SlackTools:
    """Tool calls backed by the Slack Web API, for one forwarded credential.

    The credential and the HTTP client are internal; callers reach Slack only through the
    public tool calls, each of which raises :class:`SlackApiError` on a Slack-side failure
    or ``ValueError`` on an invalid argument.
    """

    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self._token = token
        self._client = client

    async def message_self(self, message: str) -> str:
        """Send ``message`` to the token owner's own DM and return a JSON result string."""
        if not message.strip():
            raise ValueError("Message text is required.")

        user_id, workspace_url = await self._authenticated_identity()
        posted: Final = await self._call_slack_api(
            "chat.postMessage",
            {"channel": user_id, "text": message},
        )

        channel_id: Final = posted.get("channel")
        message_ts: Final = posted.get("ts")
        result: Final = {
            "message_link": self._message_link(
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

    async def _call_slack_api(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        response: Final = await self._client.post(
            f"{SLACK_API_BASE_URL}/{method}",
            headers={"Authorization": f"Bearer {self._token}"},
            json=payload,
        )
        response.raise_for_status()
        data: Final = response.json()
        if not isinstance(data, dict) or data.get("ok") is not True:
            error: Final = data.get("error", "invalid_response") if isinstance(data, dict) else "invalid_response"
            raise SlackApiError(method, str(error))
        return data

    async def _authenticated_identity(self) -> tuple[str, str]:
        """Return the token's own Slack user ID and workspace URL."""
        identity: Final = await self._call_slack_api("auth.test", {})
        user_id: Final = identity.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise SlackApiError("auth.test", "missing_user_id")

        workspace_url: Final = identity.get("url")
        return user_id, workspace_url if isinstance(workspace_url, str) else ""

    @staticmethod
    def _message_link(workspace_url: str, channel_id: str, message_ts: str) -> str | None:
        if not workspace_url or not channel_id or not message_ts:
            return None
        return f"{workspace_url.rstrip('/')}/archives/{channel_id}/p{message_ts.replace('.', '')}"
