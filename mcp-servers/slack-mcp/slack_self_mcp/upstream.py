"""Channel-filtered proxy in front of an upstream MCP server (Slack's official one).

LiteLLM points its Slack MCP entry at this server, which forwards ``tools/list`` and
``tools/call`` to the upstream MCP URL and rejects any call that names a channel outside
the allowlist. The Slack credential is the bearer token LiteLLM forwards: this server
never stores one, it passes it through.

No MCP session is kept open between requests, so one caller's credential is never pooled
for another caller.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from .channel_filter import DEFAULT_CHANNEL_ARGUMENTS, ChannelAllowlist, channel_arguments
from .query_scope import DEFAULT_MAX_SCOPE_CHANNELS, QueryScopeError, scope_query
from .result_filter import SearchPayloadError, filter_search_payload

logger = logging.getLogger(__name__)

NO_CREDENTIAL_MESSAGE: Final = "No Slack credential was forwarded for this request."
DEFAULT_UPSTREAM_TIMEOUT_SECONDS: Final = 30.0
DEFAULT_RESULT_FILTERED_TOOLS: Final = frozenset({"slack_search_public", "slack_search_public_and_private"})


class UpstreamSession(Protocol):
    """The slice of ``mcp.ClientSession`` this proxy drives, so tests can stand in for it."""

    async def initialize(self) -> object: ...

    async def list_tools(self) -> ListToolsResult: ...

    async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult: ...


SessionFactory = Callable[[str | None], contextlib.AbstractAsyncContextManager[UpstreamSession]]


def error_result(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], isError=True)


def upstream_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


@contextlib.asynccontextmanager
async def streamable_http_session(
    url: str,
    token: str | None,
    timeout_seconds: float,
) -> AsyncIterator[UpstreamSession]:
    """Open one upstream MCP session, authenticated with the forwarded Slack token."""
    async with streamablehttp_client(
        url,
        headers=upstream_headers(token),
        timeout=timeout_seconds,
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            yield session


@dataclass(frozen=True)
class UpstreamProxy:
    """Forwards MCP traffic upstream, refusing calls that leave the channel allowlist."""

    url: str
    allowlist: ChannelAllowlist
    channel_argument_names: Sequence[str] = DEFAULT_CHANNEL_ARGUMENTS
    tools_allowed_without_channel: frozenset[str] = frozenset()
    result_filtered_tools: frozenset[str] = DEFAULT_RESULT_FILTERED_TOOLS
    max_scope_channels: int = DEFAULT_MAX_SCOPE_CHANNELS
    timeout_seconds: float = DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    session_factory: SessionFactory | None = field(default=None, repr=False, compare=False)

    def _session(self, token: str | None) -> contextlib.AbstractAsyncContextManager[UpstreamSession]:
        if self.session_factory is not None:
            return self.session_factory(token)
        return streamable_http_session(self.url, token, self.timeout_seconds)

    async def list_tools(self, token: str | None) -> list[Tool]:
        """The upstream tools, or nothing when the caller has no credential yet."""
        if token is None:
            return []
        async with self._session(token) as session:
            await session.initialize()
            listed: Final = await session.list_tools()
        return list(listed.tools)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
        token: str | None,
    ) -> CallToolResult:
        rejection: Final = self.rejection_reason(name, arguments)
        if rejection is not None:
            logger.warning("Blocked MCP tool call: %s", rejection)
            return error_result(rejection)

        if token is None:
            return error_result(NO_CREDENTIAL_MESSAGE)

        upstream_arguments: Final = arguments
        if name in self.result_filtered_tools:
            try:
                upstream_arguments = self._scope_arguments(arguments)
            except QueryScopeError as exc:
                return error_result(f"{name} was blocked: {exc}")

        async with self._session(token) as session:
            await session.initialize()
            result: Final = await session.call_tool(name, upstream_arguments)

        if name in self.result_filtered_tools:
            return self._filter_result(name, result)
        return result

    def _scope_arguments(self, arguments: dict[str, object]) -> dict[str, object]:
        """Rewrite the search query so Slack itself only searches allowlisted channels."""
        query: Final = arguments.get("query")
        if not isinstance(query, str):
            return arguments

        scoped: Final = scope_query(query, self.allowlist, self.max_scope_channels)
        if scoped.unsearchable_ids:
            logger.warning(
                "%d allowlisted channel ID(s) have no name in the allowlist file and cannot be searched",
                len(scoped.unsearchable_ids),
            )
        if scoped.dropped_clauses:
            logger.info(
                "Search query named %d channel(s) outside the allowlist; searching %d allowed channel(s) instead",
                len(scoped.dropped_clauses),
                scoped.scoped_channels,
            )
        return {**arguments, "query": scoped.query}

    def _filter_result(self, name: str, result: CallToolResult) -> CallToolResult:
        """Trim a search payload to allowlisted channels before the caller sees it."""
        if result.isError or not result.content:
            return result

        payload: Final = getattr(result.content[0], "text", None)
        if not isinstance(payload, str):
            return error_result(f"{name} was blocked: its result could not be attributed to a channel.")

        try:
            filtered: Final = filter_search_payload(payload, self.allowlist)
        except SearchPayloadError as exc:
            logger.warning("Could not filter the %s result: %s", name, exc)
            return error_result(f"{name} result was withheld: {exc}")

        logger.info("%s returned %d allowlisted result(s), hid %d", name, filtered.kept, filtered.dropped)
        return CallToolResult(
            content=[TextContent(type="text", text=filtered.text)],
            isError=False,
        )

    def rejection_reason(self, name: str, arguments: dict[str, object]) -> str | None:
        """Why this call must not reach Slack, or None when it may proceed."""
        values, malformed = channel_arguments(arguments, self.channel_argument_names)

        if malformed:
            return f"{name} was blocked: channel argument(s) {sorted(malformed)} did not carry channel names or IDs."

        if not values:
            if name in self.tools_allowed_without_channel or name in self.result_filtered_tools:
                return None
            return (
                f"{name} was blocked: the call named no channel, and only allowlisted channels "
                "may be read. Add the tool to the server's allowed-without-channel list if it "
                "does not target a channel."
            )

        disallowed: Final = sorted({value for value in values if not self.allowlist.allows(value)})
        if disallowed:
            return (
                f"{name} was blocked: channel(s) {disallowed} are not in the allowlist "
                f"({len(self.allowlist.channel_ids)} channel IDs and "
                f"{len(self.allowlist.channel_names)} channel names are allowed)."
            )
        return None
