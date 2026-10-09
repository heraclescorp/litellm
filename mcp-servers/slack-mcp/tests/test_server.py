from __future__ import annotations

import json

import httpx
import pytest
from slack_self_mcp.server import MESSAGE_SELF_TOOL_NAME, bearer_token, run_message_self, tool_definition

IDENTITY = {"ok": True, "user_id": "U_SELF", "url": "https://example.slack.com/"}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_tool_schema_exposes_only_message() -> None:
    definition = tool_definition()

    assert definition.name == MESSAGE_SELF_TOOL_NAME
    assert definition.inputSchema["properties"] == {
        "message": {"type": "string", "description": "Message to send to your own Slack direct message."}
    }
    assert definition.inputSchema["required"] == ["message"]


def test_bearer_token_parsing() -> None:
    assert bearer_token("Bearer xoxp-1") == "xoxp-1"
    assert bearer_token("bearer xoxp-1") == "xoxp-1"
    assert bearer_token("Basic abc") is None
    assert bearer_token("Bearer   ") is None
    assert bearer_token(None) is None


@pytest.mark.asyncio
async def test_rejects_blank_message_without_calling_slack() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Slack must not be called for a blank message")

    async with _client(handler) as client:
        result = await run_message_self("   ", "xoxp-token", client)

    assert result.isError is True
    assert result.content[0].text == "Message text is required."


@pytest.mark.asyncio
async def test_rejects_missing_token_without_calling_slack() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Slack must not be called without a token")

    async with _client(handler) as client:
        result = await run_message_self("hello", None, client)

    assert result.isError is True
    assert "No Slack credential" in result.content[0].text


@pytest.mark.asyncio
async def test_reports_token_error_for_reauthorization() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "token_revoked"})

    async with _client(handler) as client:
        result = await run_message_self("hello", "xoxp-token", client)

    assert result.isError is True
    assert "token_revoked" in result.content[0].text


@pytest.mark.asyncio
async def test_success_returns_message_link() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("auth.test"):
            return httpx.Response(200, json=IDENTITY)
        return httpx.Response(200, json={"ok": True, "channel": "D_SELF", "ts": "1791498863.717709"})

    async with _client(handler) as client:
        result = await run_message_self("hello", "xoxp-token", client)

    assert result.isError is False
    assert json.loads(result.content[0].text)["message_link"] == (
        "https://example.slack.com/archives/D_SELF/p1791498863717709"
    )
