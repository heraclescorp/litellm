from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from mcp import types
from slack_self_mcp.channel_filter import ChannelAllowlist
from slack_self_mcp.mcp_server import build_mcp_server
from slack_self_mcp.upstream import NO_CREDENTIAL_MESSAGE, UpstreamProxy

GENERAL_ID = "C91ST88S2"
UPSTREAM_TOOL = "slack_read_channel"


SEARCH_PAYLOAD = json.dumps(
    {
        "results": (
            "# Search Results for: deploy\n\n## Messages (2 results)\n"
            "### Result 1 of 2\nChannel: #general (ID: C91ST88S2)\nText:\nhello\n\n"
            "### Result 2 of 2\nChannel: #notary-team (ID: C01BETH12US)\nText:\nsecret\n"
        ),
        "pagination_info": None,
    }
)


class FakeUpstreamSession:
    """Stands in for ``mcp.ClientSession`` and records every forwarded call."""

    def __init__(self, calls: list[tuple[str, dict[str, object]]], token: str | None, payload: str | None) -> None:
        self.calls = calls
        self.token = token
        self.payload = payload

    async def initialize(self) -> object:
        return object()

    async def list_tools(self) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=UPSTREAM_TOOL,
                    description="Read a channel",
                    inputSchema={
                        "type": "object",
                        "properties": {"channel": {"type": "string"}},
                        "required": ["channel"],
                    },
                ),
                types.Tool(name="slack_search_users", description="Search users", inputSchema={"type": "object"}),
            ]
        )

    async def call_tool(self, name: str, arguments: dict[str, object]) -> types.CallToolResult:
        self.calls.append((name, arguments))
        text = self.payload if self.payload is not None else f"upstream {name}"
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


class FakeUpstream:
    def __init__(self, payload: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.tokens: list[str | None] = []
        self.payload = payload

    @contextlib.asynccontextmanager
    async def session(self, token: str | None) -> AsyncIterator[FakeUpstreamSession]:
        self.tokens.append(token)
        yield FakeUpstreamSession(self.calls, token, self.payload)


def _allowlist() -> ChannelAllowlist:
    return ChannelAllowlist.from_entries([f"{GENERAL_ID} #general"])


def _proxy(upstream: FakeUpstream) -> UpstreamProxy:
    return UpstreamProxy(url="https://mcp.example/mcp", allowlist=_allowlist(), session_factory=upstream.session)


def _call_tool_request(name: str, arguments: dict[str, object]) -> types.CallToolRequest:
    return types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments),
    )


async def _call_tool(proxy: UpstreamProxy, name: str, arguments: dict[str, object]) -> types.CallToolResult:
    handler = build_mcp_server(proxy).request_handlers[types.CallToolRequest]
    result = await handler(_call_tool_request(name, arguments))
    return result.root


@pytest.fixture(autouse=True)
def _forwarded_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("slack_self_mcp.mcp_server.current_bearer_token", lambda: "xoxp-forwarded")


async def test_forwards_a_call_to_an_allowlisted_channel_id() -> None:
    upstream = FakeUpstream()
    proxy = _proxy(upstream)

    result = await _call_tool(proxy, UPSTREAM_TOOL, {"channel": GENERAL_ID})

    assert result.isError is False
    assert upstream.calls == [(UPSTREAM_TOOL, {"channel": GENERAL_ID})]
    assert set(upstream.tokens) == {"xoxp-forwarded"}


async def test_forwards_a_call_to_an_allowlisted_channel_name() -> None:
    upstream = FakeUpstream()

    result = await _call_tool(_proxy(upstream), UPSTREAM_TOOL, {"channel": "#general"})

    assert result.isError is False
    assert upstream.calls == [(UPSTREAM_TOOL, {"channel": "#general"})]


@pytest.mark.parametrize("channel", ["C0NOTALLOWED", "#random-channel", "D04ABCDEF"])
async def test_blocks_a_call_outside_the_allowlist_without_contacting_slack(channel: str) -> None:
    upstream = FakeUpstream()

    result = await _call_tool(_proxy(upstream), UPSTREAM_TOOL, {"channel": channel})

    assert result.isError is True
    assert channel in result.content[0].text
    assert upstream.calls == []


async def test_blocks_a_call_that_names_no_channel() -> None:
    upstream = FakeUpstream()

    result = await _call_tool(_proxy(upstream), "slack_search_users", {"query": "someone"})

    assert result.isError is True
    assert "named no channel" in result.content[0].text
    assert upstream.calls == []


async def test_allows_an_exempted_tool_to_run_without_a_channel() -> None:
    upstream = FakeUpstream()
    proxy = UpstreamProxy(
        url="https://mcp.example/mcp",
        allowlist=_allowlist(),
        tools_allowed_without_channel=frozenset({"slack_search_users"}),
        session_factory=upstream.session,
    )

    result = await _call_tool(proxy, "slack_search_users", {"query": "someone"})

    assert result.isError is False
    assert upstream.calls == [("slack_search_users", {"query": "someone"})]


async def test_blocks_a_channel_argument_that_cannot_be_validated() -> None:
    # Exercised below the MCP transport: the server rejects schema-invalid arguments
    # before tool dispatch, so the filter's own handling is tested directly.
    upstream = FakeUpstream()

    result = await _proxy(upstream).call_tool(UPSTREAM_TOOL, {"channel": {"id": GENERAL_ID}}, "xoxp-forwarded")

    assert result.isError is True
    assert "channel" in result.content[0].text
    assert upstream.calls == []


async def test_blocks_a_list_argument_with_one_channel_outside_the_allowlist() -> None:
    upstream = FakeUpstream()
    proxy = UpstreamProxy(
        url="https://mcp.example/mcp",
        allowlist=_allowlist(),
        channel_argument_names=("channels",),
        session_factory=upstream.session,
    )

    result = await proxy.call_tool(UPSTREAM_TOOL, {"channels": [GENERAL_ID, "C0NOTALLOWED"]}, "xoxp-forwarded")

    assert result.isError is True
    assert "C0NOTALLOWED" in result.content[0].text
    assert upstream.calls == []


async def test_search_runs_without_a_channel_and_hides_other_channels_from_the_caller() -> None:
    upstream = FakeUpstream(payload=SEARCH_PAYLOAD)

    result = await _call_tool(_proxy(upstream), "slack_search_public", {"query": "deploy"})

    assert result.isError is False
    text = result.content[0].text
    assert "hello" in text
    assert "notary-team" not in text
    assert "secret" not in text
    forwarded = upstream.calls[0]
    assert forwarded[0] == "slack_search_public"
    assert str(forwarded[1]["query"]).startswith("deploy")
    assert "in:#general" in str(forwarded[1]["query"])


async def test_search_query_is_scoped_to_the_allowlist_before_it_is_forwarded() -> None:
    upstream = FakeUpstream(payload=SEARCH_PAYLOAD)

    await _call_tool(_proxy(upstream), "slack_search_public", {"query": "deploy"})

    forwarded_query = str(upstream.calls[0][1]["query"])
    assert forwarded_query == "deploy in:#general"


async def test_a_caller_scope_outside_the_allowlist_is_refused_without_calling_upstream() -> None:
    upstream = FakeUpstream(payload=SEARCH_PAYLOAD)

    result = await _call_tool(_proxy(upstream), "slack_search_public", {"query": "deploy in:#notary-team"})

    assert result.isError is True
    assert "outside the allowlist" in result.content[0].text
    assert upstream.calls == []


async def test_a_search_payload_that_cannot_be_filtered_is_withheld() -> None:
    upstream = FakeUpstream(payload="not a json payload")

    result = await _call_tool(_proxy(upstream), "slack_search_public", {"query": "deploy"})

    assert result.isError is True
    assert "withheld" in result.content[0].text


async def test_lists_the_local_tool_and_the_upstream_tools() -> None:
    upstream = FakeUpstream()
    handler = build_mcp_server(_proxy(upstream)).request_handlers[types.ListToolsRequest]

    result = await handler(types.ListToolsRequest(method="tools/list"))

    assert [tool.name for tool in result.root.tools] == ["slack_message_self", UPSTREAM_TOOL, "slack_search_users"]


async def test_omits_upstream_tools_when_no_credential_was_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("slack_self_mcp.mcp_server.current_bearer_token", lambda: None)
    upstream = FakeUpstream()
    handler = build_mcp_server(_proxy(upstream)).request_handlers[types.ListToolsRequest]

    result = await handler(types.ListToolsRequest(method="tools/list"))

    assert [tool.name for tool in result.root.tools] == ["slack_message_self"]


async def test_refuses_to_forward_without_a_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("slack_self_mcp.mcp_server.current_bearer_token", lambda: None)
    upstream = FakeUpstream()

    result = await _call_tool(_proxy(upstream), UPSTREAM_TOOL, {"channel": GENERAL_ID})

    assert result.isError is True
    assert result.content[0].text == NO_CREDENTIAL_MESSAGE
    assert upstream.calls == []


async def test_upstream_failures_surface_as_tool_errors() -> None:
    @contextlib.asynccontextmanager
    async def broken_session(_: str | None) -> AsyncIterator[FakeUpstreamSession]:
        raise RuntimeError("upstream down")
        yield FakeUpstreamSession([], None)  # pragma: no cover - unreachable, keeps this a generator

    proxy = UpstreamProxy(url="https://mcp.example/mcp", allowlist=_allowlist(), session_factory=broken_session)

    result = await _call_tool(proxy, UPSTREAM_TOOL, {"channel": GENERAL_ID})

    assert result.isError is True
    assert "upstream down" in result.content[0].text


def test_proxy_from_env_returns_none_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    from slack_self_mcp.mcp_server import upstream_proxy_from_env

    monkeypatch.delenv("SLACK_MCP_UPSTREAM_URL", raising=False)

    assert upstream_proxy_from_env() is None


def test_proxy_from_env_loads_the_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from slack_self_mcp.mcp_server import upstream_proxy_from_env

    channels = tmp_path / "channels.txt"
    channels.write_text(f"{GENERAL_ID} #general\n")
    monkeypatch.setenv("SLACK_MCP_UPSTREAM_URL", "https://mcp.example/mcp")
    monkeypatch.setenv("SLACK_MCP_CHANNELS_FILE", str(channels))

    proxy = upstream_proxy_from_env()

    assert proxy is not None
    assert proxy.url == "https://mcp.example/mcp"
    assert proxy.allowlist.allows(GENERAL_ID)


def test_proxy_from_env_fails_closed_on_a_missing_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from slack_self_mcp.channel_filter import ChannelAllowlistError
    from slack_self_mcp.mcp_server import upstream_proxy_from_env

    monkeypatch.setenv("SLACK_MCP_UPSTREAM_URL", "https://mcp.example/mcp")
    monkeypatch.setenv("SLACK_MCP_CHANNELS_FILE", str(tmp_path / "absent.txt"))

    with pytest.raises(ChannelAllowlistError):
        upstream_proxy_from_env()
