from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

import pytest
from mcp import types
from slack_self_mcp.guard import (
    NO_CREDENTIAL_MESSAGE,
    ChannelAllowlist,
    ChannelAllowlistError,
    QueryScopeError,
    UpstreamProxy,
)
from slack_self_mcp.mcp_server import build_mcp_server

GENERAL_ID = "C91ST88S2"
ENG_TEAM_ID = "CKC8CBNH5"
SEARCH_TOOL: Final = "slack_search_public"


class FakeUpstreamSession:
    """Stands in for ``mcp.ClientSession`` and records every forwarded call."""

    def __init__(self, calls: list[tuple[str, dict[str, object]]]) -> None:
        self.calls = calls

    async def initialize(self) -> object:
        return object()

    async def list_tools(self) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=SEARCH_TOOL,
                    description="Search messages",
                    inputSchema={"type": "object", "properties": {"query": {"type": "string"}}},
                ),
                types.Tool(
                    name="slack_read_channel",
                    description="Read a channel",
                    inputSchema={"type": "object", "properties": {"channel_id": {"type": "string"}}},
                ),
            ]
        )

    async def call_tool(self, name: str, arguments: dict[str, object]) -> types.CallToolResult:
        self.calls.append((name, arguments))
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"upstream {name}")])


class FakeUpstream:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    @contextlib.asynccontextmanager
    async def session(self, _: str | None) -> AsyncIterator[FakeUpstreamSession]:
        yield FakeUpstreamSession(self.calls)

    def calls_for(self, tool: str) -> list[dict[str, object]]:
        return [args for name, args in self.calls if name == tool]


def _allowlist() -> ChannelAllowlist:
    return ChannelAllowlist.from_entries([f"{GENERAL_ID} #general", f"{ENG_TEAM_ID} #eng-team"])


def _proxy(upstream: FakeUpstream, **kwargs: object) -> UpstreamProxy:
    defaults: dict[str, object] = {
        "url": "https://mcp.example/mcp",
        "allowlist": _allowlist(),
        "session_factory": upstream.session,
    }
    return UpstreamProxy(**{**defaults, **kwargs})  # type: ignore[arg-type]


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


# ── The allowlist ────────────────────────────────────────────────────────────


def test_allowlist_matches_channel_id_name_and_bare_name() -> None:
    allowlist = ChannelAllowlist.from_entries([f"{GENERAL_ID} #general", f"{ENG_TEAM_ID} #Eng-Team"])

    assert allowlist.allows(GENERAL_ID)
    assert allowlist.allows("#general")
    assert allowlist.allows("#ENG-TEAM")
    assert not allowlist.allows("C0NOTALLOWED")
    assert not allowlist.allows("D04ABCDEF")


def test_a_line_must_start_with_a_channel_id() -> None:
    with pytest.raises(ChannelAllowlistError, match="does not start with a channel ID"):
        ChannelAllowlist.from_entries(["#general"])


def test_a_duplicate_channel_id_is_rejected() -> None:
    with pytest.raises(ChannelAllowlistError, match="listed twice"):
        ChannelAllowlist.from_entries([f"{GENERAL_ID} #general", f"{GENERAL_ID} #other"])


def test_a_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ChannelAllowlistError, match="does not exist"):
        ChannelAllowlist.from_file(tmp_path / "absent.txt")


def test_the_shipped_allowlist_resolves_next_to_the_package() -> None:
    from slack_self_mcp.mcp_server import PACKAGE_CHANNELS_FILE

    assert ChannelAllowlist.from_file(PACKAGE_CHANNELS_FILE).channel_ids


# ── Scoping ──────────────────────────────────────────────────────────────────


def test_scoping_appends_one_clause_per_channel() -> None:
    assert _allowlist().scope("deploy") == "deploy in:#eng-team in:#general"


def test_scoping_keeps_an_allowlisted_caller_clause_and_drops_the_rest() -> None:
    assert _allowlist().scope("deploy in:#notary-team in:#general") == "deploy in:#general"


def test_scoping_translates_a_caller_clause_naming_a_channel_id() -> None:
    assert _allowlist().scope(f"deploy in:{GENERAL_ID}") == "deploy in:#general"


def test_scoping_refuses_a_query_aimed_only_outside_the_allowlist() -> None:
    with pytest.raises(QueryScopeError, match="outside the allowlist"):
        _allowlist().scope("deploy in:#notary-team")


# ── Enforcement ──────────────────────────────────────────────────────────────


async def test_forwards_a_call_to_an_allowlisted_channel() -> None:
    upstream = FakeUpstream()

    result = await _call_tool(_proxy(upstream), "slack_read_channel", {"channel_id": GENERAL_ID})

    assert result.isError is False
    assert upstream.calls_for("slack_read_channel") == [{"channel_id": GENERAL_ID}]


@pytest.mark.parametrize("channel", ["C0NOTALLOWED", "#random-channel", "D04ABCDEF"])
async def test_blocks_a_channel_outside_the_allowlist(channel: str) -> None:
    upstream = FakeUpstream()

    result = await _call_tool(_proxy(upstream), "slack_read_channel", {"channel_id": channel})

    assert result.isError is True
    assert channel in result.content[0].text
    assert upstream.calls == []


async def test_blocks_a_tool_that_names_no_channel() -> None:
    upstream = FakeUpstream()

    result = await _call_tool(_proxy(upstream), "slack_read_file", {"file_id": "F0123"})

    assert result.isError is True
    assert "named no channel" in result.content[0].text
    assert upstream.calls == []


async def test_a_search_tool_may_run_without_naming_a_channel() -> None:
    upstream = FakeUpstream()

    result = await _call_tool(_proxy(upstream), SEARCH_TOOL, {"query": "deploy"})

    assert result.isError is False
    assert upstream.calls_for(SEARCH_TOOL) == [{"query": "deploy in:#eng-team in:#general"}]


async def test_a_non_search_tool_that_names_no_channel_is_blocked() -> None:
    upstream = FakeUpstream()

    result = await _call_tool(_proxy(upstream), "slack_search_users", {"query": "someone"})

    assert result.isError is True
    assert "named no channel" in result.content[0].text
    assert upstream.calls == []


async def test_refuses_to_forward_without_a_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("slack_self_mcp.mcp_server.current_bearer_token", lambda: None)
    upstream = FakeUpstream()

    result = await _call_tool(_proxy(upstream), "slack_read_channel", {"channel_id": GENERAL_ID})

    assert result.isError is True
    assert result.content[0].text == NO_CREDENTIAL_MESSAGE
    assert upstream.calls == []


async def test_upstream_failures_surface_as_tool_errors() -> None:
    @contextlib.asynccontextmanager
    async def broken_session(_: str | None) -> AsyncIterator[FakeUpstreamSession]:
        raise RuntimeError("upstream down")
        yield FakeUpstreamSession([])  # pragma: no cover - keeps this a generator

    proxy = UpstreamProxy(
        url="https://mcp.example/mcp",
        allowlist=_allowlist(),
        session_factory=broken_session,
    )

    result = await _call_tool(proxy, "slack_read_channel", {"channel_id": GENERAL_ID})

    assert result.isError is True
    assert "upstream down" in result.content[0].text


# ── Wiring ───────────────────────────────────────────────────────────────────


async def test_lists_the_local_tool_and_the_upstream_tools() -> None:
    handler = build_mcp_server(_proxy(FakeUpstream())).request_handlers[types.ListToolsRequest]

    result = await handler(types.ListToolsRequest(method="tools/list"))

    assert [tool.name for tool in result.root.tools] == ["slack_message_self", SEARCH_TOOL, "slack_read_channel"]


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
    assert proxy.allowlist.allows(GENERAL_ID)
