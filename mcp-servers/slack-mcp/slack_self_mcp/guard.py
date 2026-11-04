"""The channel whitelist, and the proxy that keeps Slack inside it.

The allowlist is one channel per line, ID first, with an optional name annotation:

    C91ST88S2  #general
    CKC8CBNH5  #eng-team

Two checks enforce it, and nothing else:

- a tool call that names a channel is refused unless that channel is listed;
- a tool call that searches channels has ``in:#channel`` appended for every listed channel,
  because Slack ORs those clauses, so the search itself never reaches the rest. The tool names
  that count as channel searches are configured rather than inferred: ``slack_search_users``
  also takes a ``query``, and ``in:`` clauses would make it return nothing.

The Slack credential is the bearer token LiteLLM forwards; this server stores none.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

logger = logging.getLogger(__name__)

CHANNEL_ID_PATTERN: Final = re.compile(r"^[CG][A-Z0-9]{6,}$")
IN_CLAUSE: Final = re.compile(r"\bin:(?P<channel>#[A-Za-z0-9_.\-]+|[A-Za-z0-9_.\-]+)", re.IGNORECASE)

DEFAULT_CHANNEL_ARGUMENTS: Final = ("channel", "channel_id", "channels", "channel_ids")
DEFAULT_SEARCH_TOOLS: Final = frozenset({"slack_search_public"})
NO_CREDENTIAL_MESSAGE: Final = "No Slack credential was forwarded for this request."
DEFAULT_UPSTREAM_TIMEOUT_SECONDS: Final = 30.0
SCOPE_WARNING_CHANNELS: Final = 150


class ChannelAllowlistError(ValueError):
    """Raised when the allowlist cannot be built from its configured source."""


class QueryScopeError(ValueError):
    """Raised when a query cannot be scoped without searching channels outside the allowlist."""


@dataclass(frozen=True)
class ChannelAllowlist:
    channel_ids: frozenset[str]
    names_by_id: Mapping[str, str]
    scope_clauses: str

    @classmethod
    def from_entries(cls, entries: Sequence[object]) -> ChannelAllowlist:
        channel_ids: set[str] = set()
        names_by_id: dict[str, str] = {}

        for line_number, entry in enumerate(entries, start=1):
            if not isinstance(entry, str):
                raise ChannelAllowlistError(f"Channel entry {entry!r} is not a string.")
            stripped: Final = entry.strip()
            if not stripped:
                continue

            channel_id, _, annotation = stripped.partition(" ")
            if not CHANNEL_ID_PATTERN.match(channel_id):
                raise ChannelAllowlistError(
                    f"Line {line_number}: {stripped!r} does not start with a channel ID. "
                    "Write the ID first and the name after it, e.g. 'C91ST88S2 #general'."
                )
            if channel_id in channel_ids:
                raise ChannelAllowlistError(f"Line {line_number}: channel ID {channel_id} is listed twice.")
            channel_ids.add(channel_id)

            trailing: Final = annotation.strip()
            if not trailing:
                continue
            if not trailing.startswith("#") or not trailing[1:].strip():
                raise ChannelAllowlistError(
                    f"Line {line_number}: {trailing!r} is not a channel name. Write it as '#name'."
                )
            names_by_id[channel_id] = trailing[1:].strip().casefold()

        return cls(
            channel_ids=frozenset(channel_ids),
            names_by_id=MappingProxyType(names_by_id),
            scope_clauses=" ".join(f"in:#{name}" for name in sorted(names_by_id.values())),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> ChannelAllowlist:
        channel_file: Final = Path(path)
        if not channel_file.is_file():
            raise ChannelAllowlistError(f"Channels file {str(channel_file)!r} does not exist.")
        allowlist: Final = cls.from_entries(channel_file.read_text().splitlines())
        if not allowlist.channel_ids:
            raise ChannelAllowlistError(f"Channels file {str(channel_file)!r} lists no channels.")
        return allowlist

    @property
    def channel_names(self) -> frozenset[str]:
        return frozenset(self.names_by_id.values())

    def allows(self, value: str) -> bool:
        candidate: Final = value.strip()
        if not candidate:
            return False
        if candidate.startswith("#"):
            return candidate[1:].strip().casefold() in self.channel_names
        if CHANNEL_ID_PATTERN.match(candidate):
            return candidate in self.channel_ids
        return candidate.casefold() in self.channel_names

    def scope(self, query: str) -> str:
        """Rewrite ``query`` so Slack only searches allowlisted channels."""
        requested: Final = [match.group("channel") for match in IN_CLAUSE.finditer(query)]
        without_clauses: Final = IN_CLAUSE.sub("", query).strip()

        if requested:
            allowed: Final = [name for name in (self._name_for(name) for name in requested) if name is not None]
            if not allowed:
                raise QueryScopeError(
                    f"Every channel named by in: ({', '.join(sorted(requested))}) is outside the allowlist."
                )
            return f"{without_clauses} {' '.join(f'in:#{name}' for name in dict.fromkeys(allowed))}".strip()

        return f"{without_clauses} {self.scope_clauses}".strip()

    def _name_for(self, raw_channel: str) -> str | None:
        """The allowlisted name for a caller naming a channel by name or by ID."""
        candidate: Final = raw_channel.lstrip("#").strip()
        if not candidate:
            return None
        if CHANNEL_ID_PATTERN.match(candidate):
            return self.names_by_id.get(candidate)
        folded: Final = candidate.casefold()
        return folded if folded in self.channel_names else None


def channel_arguments(arguments: object, names: Sequence[str]) -> tuple[list[str], list[str]]:
    """The channel values named by a tool call, plus arguments carrying no usable value.

    An argument that is present but is neither a string nor a list of strings cannot be
    validated, so it is reported rather than ignored.
    """
    if not isinstance(arguments, Mapping):
        return [], []
    typed_arguments: Final = arguments

    values: list[str] = []
    malformed: list[str] = []
    for argument_name in names:
        if argument_name not in typed_arguments:
            continue
        raw_value = typed_arguments[argument_name]
        if isinstance(raw_value, str):
            values.append(raw_value)
        elif isinstance(raw_value, list) and all(isinstance(item, str) for item in raw_value):
            values.extend(item for item in raw_value if isinstance(item, str))
        else:
            malformed.append(argument_name)
    return values, malformed


class UpstreamSession(Protocol):
    """The slice of ``mcp.ClientSession`` this proxy drives, so tests can stand in for it."""

    async def initialize(self) -> object: ...

    async def list_tools(self) -> ListToolsResult: ...

    async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult: ...


SessionFactory = Callable[[str | None], contextlib.AbstractAsyncContextManager[UpstreamSession]]


def error_result(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], isError=True)


@contextlib.asynccontextmanager
async def streamable_http_session(
    url: str,
    token: str | None,
    timeout_seconds: float,
) -> AsyncIterator[UpstreamSession]:
    """Open one upstream MCP session, authenticated with the forwarded Slack token."""
    headers: Final = {"Authorization": f"Bearer {token}"} if token else {}
    async with streamablehttp_client(url, headers=headers, timeout=timeout_seconds) as (read, write, _):
        async with ClientSession(read, write) as session:
            yield session


@dataclass(frozen=True)
class UpstreamProxy:
    """Forwards MCP traffic upstream, confining it to the channel allowlist."""

    url: str
    allowlist: ChannelAllowlist
    channel_argument_names: Sequence[str] = DEFAULT_CHANNEL_ARGUMENTS
    search_tools: frozenset[str] = DEFAULT_SEARCH_TOOLS
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

        try:
            upstream_arguments: Final = self._scoped_arguments(name, arguments)
        except QueryScopeError as exc:
            return error_result(f"{name} was blocked: {exc}")

        async with self._session(token) as session:
            await session.initialize()
            return await session.call_tool(name, upstream_arguments)

    def rejection_reason(self, name: str, arguments: dict[str, object]) -> str | None:
        """Why this call must not reach Slack, or None when it may proceed."""
        values, malformed = channel_arguments(arguments, self.channel_argument_names)

        if malformed:
            return f"{name} was blocked: channel argument(s) {sorted(malformed)} did not carry channel names or IDs."

        if not values:
            # A channel search names no channel by construction: it is scoped instead.
            if name in self.search_tools:
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

    def _scoped_arguments(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        """Scope a channel search to the allowlist; every other call is forwarded as-is."""
        if name not in self.search_tools:
            return arguments
        query: Final = arguments.get("query")
        if not isinstance(query, str):
            return arguments

        if len(self.allowlist.names_by_id) > SCOPE_WARNING_CHANNELS:
            logger.warning(
                "Allowlist has %d named channels; the scoped query may exceed what Slack accepts",
                len(self.allowlist.names_by_id),
            )
        return {**arguments, "query": self.allowlist.scope(query)}
