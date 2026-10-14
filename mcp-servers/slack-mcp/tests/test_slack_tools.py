from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from slack_self_mcp.slack_tools import (
    MESSAGE_SELF_ARGUMENT,
    MESSAGE_SELF_TOOL_NAME,
    SlackApiError,
    SlackTools,
    tool_definitions,
)

IDENTITY = {"ok": True, "user_id": "U_SELF", "url": "https://example.slack.com/"}


@contextlib.asynccontextmanager
async def _tools(handler: Callable[[httpx.Request], httpx.Response]) -> AsyncIterator[SlackTools]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        yield SlackTools(token="xoxp-token", client=client)


def test_tool_schema_exposes_only_message() -> None:
    definition = tool_definitions()[0]

    assert definition.name == MESSAGE_SELF_TOOL_NAME
    assert definition.inputSchema["properties"] == {
        MESSAGE_SELF_ARGUMENT: {
            "type": "string",
            "description": "Message to send to your own Slack direct message.",
        }
    }
    assert definition.inputSchema["required"] == [MESSAGE_SELF_ARGUMENT]


@pytest.mark.asyncio
async def test_sends_to_the_tokens_own_user() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if request.url.path.endswith("auth.test"):
            return httpx.Response(200, json=IDENTITY)
        return httpx.Response(200, json={"ok": True, "channel": "D_SELF", "ts": "1791498863.717709"})

    async with _tools(handler) as tools:
        result = json.loads(await tools.message_self("hello"))

    assert calls == [{}, {"channel": "U_SELF", "text": "hello"}]
    assert result["message_context"] == {"message_ts": "1791498863.717709", "channel_id": "D_SELF"}
    assert result["message_link"] == "https://example.slack.com/archives/D_SELF/p1791498863717709"


@pytest.mark.asyncio
async def test_blank_message_is_rejected_without_calling_slack() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Slack must not be called for a blank message")

    async with _tools(handler) as tools:
        with pytest.raises(ValueError) as exc_info:
            await tools.message_self("   ")

    assert str(exc_info.value) == "Message text is required."


@pytest.mark.asyncio
async def test_identity_requires_user_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async with _tools(handler) as tools:
        with pytest.raises(SlackApiError) as exc_info:
            await tools.message_self("hello")

    assert exc_info.value.error == "missing_user_id"


@pytest.mark.asyncio
async def test_token_error_is_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})

    async with _tools(handler) as tools:
        with pytest.raises(SlackApiError) as exc_info:
            await tools.message_self("hello")

    assert exc_info.value.is_token_error is True
    assert "invalid_auth" in str(exc_info.value)


@pytest.mark.asyncio
async def test_send_failure_is_not_classified_as_token_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("auth.test"):
            return httpx.Response(200, json=IDENTITY)
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    async with _tools(handler) as tools:
        with pytest.raises(SlackApiError) as exc_info:
            await tools.message_self("hello")

    assert exc_info.value.is_token_error is False
