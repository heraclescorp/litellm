from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from mcp import types
from slack_self_mcp.mcp_server import NO_CREDENTIAL_MESSAGE, app, build_mcp_server, run_message_self
from slack_self_mcp.slack_tools import MESSAGE_SELF_TOOL_NAME

IDENTITY = {"ok": True, "user_id": "U_SELF", "url": "https://example.slack.com/"}


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _call_tool_request(name: str, arguments: dict[str, object]) -> types.CallToolRequest:
    return types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments),
    )


@pytest.mark.asyncio
async def test_lists_the_message_tool() -> None:
    handler = build_mcp_server().request_handlers[types.ListToolsRequest]

    result = await handler(types.ListToolsRequest(method="tools/list"))

    assert [tool.name for tool in result.root.tools] == [MESSAGE_SELF_TOOL_NAME]


@pytest.mark.asyncio
async def test_unknown_tool_is_an_error() -> None:
    handler = build_mcp_server().request_handlers[types.CallToolRequest]

    result = await handler(_call_tool_request("nope", {}))

    assert result.root.isError is True
    assert result.root.content[0].text == "Unknown tool: nope"


@pytest.mark.asyncio
async def test_health_endpoint_serves_through_the_asgi_app() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_rejects_missing_token_without_calling_slack() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Slack must not be called without a token")

    async with _client(handler) as client:
        result = await run_message_self("hello", None, client)

    assert result.isError is True
    assert result.content[0].text == NO_CREDENTIAL_MESSAGE


@pytest.mark.asyncio
async def test_rejects_blank_message_without_calling_slack() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Slack must not be called for a blank message")

    async with _client(handler) as client:
        result = await run_message_self("   ", "xoxp-token", client)

    assert result.isError is True
    assert result.content[0].text == "Message text is required."


@pytest.mark.asyncio
async def test_reports_token_error_for_reauthorization() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "token_revoked"})

    async with _client(handler) as client:
        result = await run_message_self("hello", "xoxp-token", client)

    assert result.isError is True
    assert "token_revoked" in result.content[0].text


@pytest.mark.asyncio
async def test_reports_transport_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to Slack", request=request)

    async with _client(handler) as client:
        result = await run_message_self("hello", "xoxp-token", client)

    assert result.isError is True
    assert "Slack request failed" in result.content[0].text


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
