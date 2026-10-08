from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from litellm.proxy._types import UserAPIKeyAuth

SLACK_SERVER_NAME: Final[str] = "slack"
SLACK_READ_USER_PROFILE_TOOL_NAME: Final[str] = "slack_read_user_profile"
SLACK_SEND_MESSAGE_TOOL_NAME: Final[str] = "slack_send_message"


def _result_text(result: object) -> str:
    content: Final = getattr(result, "content", ())
    return "\n".join(text for item in content if (text := getattr(item, "text", None)))


def _extract_profile_user_id(profile_result: object) -> str:
    profile_text: Final = profile_result.get("result") if isinstance(profile_result, dict) else profile_result
    if not isinstance(profile_text, str):
        raise ValueError("Slack profile did not provide authenticated user ID")

    match: Final = re.search(r"^User ID:\s*([A-Z0-9]+)\s*$", profile_text, re.MULTILINE)
    if match is None:
        raise ValueError("Slack profile did not provide authenticated user ID")
    return match.group(1)


async def _register_slack_tools(
    user_api_key_auth: UserAPIKeyAuth,
    mcp_auth_header: str | None,
    mcp_server_auth_headers: dict[str, dict[str, str]] | None,
) -> None:
    """Populate LiteLLM's tool→server map for Slack before the first Slack tool call.

    Startup mapping skips servers that need a per-user OAuth token, so on a cold
    process ``call_tool`` cannot resolve a Slack tool name until a listing has run.
    """
    from litellm.proxy._experimental.mcp_server.mcp_server_manager import (
        global_mcp_server_manager,
    )
    from litellm.proxy._experimental.mcp_server.utils import normalize_server_name

    mapping: Final = global_mcp_server_manager.tool_name_to_mcp_server_name_mapping
    normalized_slack: Final = normalize_server_name(SLACK_SERVER_NAME)
    if any(normalize_server_name(owner) == normalized_slack for owner in mapping.values()):
        return

    await global_mcp_server_manager.list_tools(
        user_api_key_auth=user_api_key_auth,
        mcp_auth_header=mcp_auth_header,
        mcp_server_auth_headers=mcp_server_auth_headers,
    )


async def send_message_to_self(
    message: str,
    user_api_key_auth: UserAPIKeyAuth | None = None,
    mcp_auth_header: str | None = None,
    mcp_server_auth_headers: dict[str, dict[str, str]] | None = None,
    oauth2_headers: dict[str, str] | None = None,
    raw_headers: dict[str, str] | None = None,
    client_ip: str | None = None,
) -> str:
    """Read the authenticated Slack profile and send only to that user's DM."""
    if not message.strip():
        return "Message text is required."

    from litellm.proxy._experimental.mcp_server.mcp_server_manager import (
        global_mcp_server_manager,
    )
    from litellm.proxy._experimental.mcp_server.server import get_active_auth_context

    auth_context = get_active_auth_context()
    resolved_user_api_key_auth: Final = user_api_key_auth or getattr(auth_context, "user_api_key_auth", None)
    if resolved_user_api_key_auth is None:
        return "MCP authentication context is unavailable."

    resolved_mcp_auth_header: Final = mcp_auth_header or getattr(auth_context, "mcp_auth_header", None)
    resolved_mcp_server_auth_headers: Final = mcp_server_auth_headers or getattr(
        auth_context, "mcp_server_auth_headers", None
    )
    resolved_oauth2_headers: Final = oauth2_headers or getattr(auth_context, "oauth2_headers", None)
    resolved_raw_headers: Final = raw_headers or getattr(auth_context, "raw_headers", None)

    try:
        await _register_slack_tools(
            user_api_key_auth=resolved_user_api_key_auth,
            mcp_auth_header=resolved_mcp_auth_header,
            mcp_server_auth_headers=resolved_mcp_server_auth_headers,
        )
        profile_result: Final = await global_mcp_server_manager.call_tool(
            server_name=SLACK_SERVER_NAME,
            name=SLACK_READ_USER_PROFILE_TOOL_NAME,
            arguments={},
            user_api_key_auth=resolved_user_api_key_auth,
            mcp_auth_header=resolved_mcp_auth_header,
            mcp_server_auth_headers=resolved_mcp_server_auth_headers,
            oauth2_headers=resolved_oauth2_headers,
            raw_headers=resolved_raw_headers,
        )
        if getattr(profile_result, "isError", False):
            return f"Slack profile lookup failed: {_result_text(profile_result)}"

        profile_payload: Final = json.loads(_result_text(profile_result))
        slack_user_id: Final = _extract_profile_user_id(profile_payload)
        send_result: Final = await global_mcp_server_manager.call_tool(
            server_name=SLACK_SERVER_NAME,
            name=SLACK_SEND_MESSAGE_TOOL_NAME,
            arguments={"channel_id": slack_user_id, "message": message},
            user_api_key_auth=resolved_user_api_key_auth,
            mcp_auth_header=resolved_mcp_auth_header,
            mcp_server_auth_headers=resolved_mcp_server_auth_headers,
            oauth2_headers=resolved_oauth2_headers,
            raw_headers=resolved_raw_headers,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return str(exc)
    except Exception as exc:
        return f"Failed to message yourself in Slack: {exc}"

    if getattr(send_result, "isError", False):
        return f"Slack message failed: {_result_text(send_result)}"
    return _result_text(send_result) or "Message sent successfully to your personal Slack DM."
