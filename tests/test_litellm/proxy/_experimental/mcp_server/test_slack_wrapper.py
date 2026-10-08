import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from litellm.proxy._experimental.mcp_server.slack_wrapper import send_message_to_self
from litellm.proxy._types import UserAPIKeyAuth


@pytest.mark.asyncio
async def test_reads_authenticated_profile_then_sends_to_that_user() -> None:
    auth_context = SimpleNamespace(
        user_api_key_auth=UserAPIKeyAuth(api_key="test-key"),
        mcp_auth_header=None,
        mcp_server_auth_headers={},
        oauth2_headers={"Authorization": "Bearer upstream-token"},
        raw_headers={"x-request-id": "request-id"},
    )
    manager = MagicMock()
    manager.tool_name_to_mcp_server_name_mapping = {}
    manager.list_tools = AsyncMock(return_value=[])
    manager.call_tool = AsyncMock(
        side_effect=[
            SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(text=json.dumps({"result": "User ID: USELF\nDisplay Name: Self"}))],
            ),
            SimpleNamespace(isError=False, content=[SimpleNamespace(text="sent")]),
        ]
    )

    with (
        patch(
            "litellm.proxy._experimental.mcp_server.server.get_active_auth_context",
            return_value=auth_context,
        ),
        patch(
            "litellm.proxy._experimental.mcp_server.mcp_server_manager.global_mcp_server_manager",
            manager,
        ),
    ):
        result = await send_message_to_self("hello")

    assert result == "sent"
    manager.list_tools.assert_awaited_once()
    assert manager.call_tool.await_count == 2
    assert manager.call_tool.await_args_list[0].kwargs["name"] == "slack_read_user_profile"
    assert manager.call_tool.await_args_list[1].kwargs["name"] == "slack_send_message"
    assert manager.call_tool.await_args_list[1].kwargs["arguments"] == {
        "channel_id": "USELF",
        "message": "hello",
    }
    assert manager.call_tool.await_args_list[1].kwargs["oauth2_headers"] == auth_context.oauth2_headers


@pytest.mark.asyncio
async def test_skips_registration_when_slack_tools_already_mapped() -> None:
    auth_context = SimpleNamespace(
        user_api_key_auth=UserAPIKeyAuth(api_key="test-key"),
        mcp_auth_header=None,
        mcp_server_auth_headers={},
        oauth2_headers=None,
        raw_headers=None,
    )
    manager = MagicMock()
    manager.tool_name_to_mcp_server_name_mapping = {"slack_read_user_profile": "slack"}
    manager.list_tools = AsyncMock(return_value=[])
    manager.call_tool = AsyncMock(
        side_effect=[
            SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(text=json.dumps({"result": "User ID: USELF"}))],
            ),
            SimpleNamespace(isError=False, content=[SimpleNamespace(text="sent")]),
        ]
    )

    with (
        patch(
            "litellm.proxy._experimental.mcp_server.server.get_active_auth_context",
            return_value=auth_context,
        ),
        patch(
            "litellm.proxy._experimental.mcp_server.mcp_server_manager.global_mcp_server_manager",
            manager,
        ),
    ):
        result = await send_message_to_self("hello")

    assert result == "sent"
    manager.list_tools.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejects_profile_without_user_id_before_sending() -> None:
    auth_context = SimpleNamespace(
        user_api_key_auth=UserAPIKeyAuth(api_key="test-key"),
        mcp_auth_header=None,
        mcp_server_auth_headers={},
        oauth2_headers=None,
        raw_headers=None,
        client_ip=None,
    )
    manager = MagicMock()
    manager.tool_name_to_mcp_server_name_mapping = {"slack_read_user_profile": "slack"}
    manager.list_tools = AsyncMock(return_value=[])
    manager.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(text=json.dumps({"result": "Display Name: Self"}))],
        )
    )

    with (
        patch(
            "litellm.proxy._experimental.mcp_server.server.get_active_auth_context",
            return_value=auth_context,
        ),
        patch(
            "litellm.proxy._experimental.mcp_server.mcp_server_manager.global_mcp_server_manager",
            manager,
        ),
    ):
        result = await send_message_to_self("hello")

    assert result == "Slack profile did not provide authenticated user ID"
    manager.call_tool.assert_awaited_once()
