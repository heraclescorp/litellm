"""Narrow a Slack search query to the allowlisted channels before it leaves this server.

Filtering hits after the fact spends part of every result page on channels the caller will
never see, so a broad search surfaces fewer useful hits than the same search would without
the allowlist. Slack's search language can express the allowlist itself: ``in:#channel``
clauses are OR'd, so appending one per allowlisted channel makes the search itself scoped.

Slack resolves ``in:`` against channel names only, never IDs. A caller that scopes by ID is
therefore translated through the allowlist pairing; a caller that passes its own ``in:``
clause is intersected with the allowlist rather than replaced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from .channel_filter import CHANNEL_ID_PATTERN, ChannelAllowlist

IN_CLAUSE: Final = re.compile(r"\bin:(?P<channel>#[A-Za-z0-9_.\-]+|[A-Za-z0-9_.\-]+)", re.IGNORECASE)
DEFAULT_MAX_SCOPE_CHANNELS: Final = 150


class QueryScopeError(ValueError):
    """Raised when a query cannot be scoped without searching channels outside the allowlist."""


@dataclass(frozen=True)
class ScopedQuery:
    query: str
    scoped_channels: int
    dropped_clauses: tuple[str, ...]
    unsearchable_ids: frozenset[str]


def _allowed_name(raw_channel: str, allowlist: ChannelAllowlist) -> str | None:
    """The allowlisted channel name for a caller naming it by name or by ID."""
    candidate: Final = raw_channel.lstrip("#").strip()
    if not candidate:
        return None
    if CHANNEL_ID_PATTERN.match(candidate):
        return allowlist.name_for_id(candidate)
    folded: Final = candidate.casefold()
    return folded if folded in allowlist.channel_names else None


def scope_query(
    query: str,
    allowlist: ChannelAllowlist,
    max_scope_channels: int = DEFAULT_MAX_SCOPE_CHANNELS,
) -> ScopedQuery:
    """Rewrite ``query`` so Slack only searches allowlisted channels.

    Returns the rewritten query plus how many channels it scopes to, or raises
    :class:`QueryScopeError` when the caller named channels outside the allowlist and no
    allowed channel is left to search.
    """
    requested: Final = [match.group("channel") for match in IN_CLAUSE.finditer(query)]
    without_clauses: Final = IN_CLAUSE.sub("", query).strip()
    unsearchable: Final = allowlist.ids_without_names()

    if requested:
        resolved: Final = [(_allowed_name(name, allowlist), name) for name in requested]
        allowed_names: Final = list(dict.fromkeys(name for name, _ in resolved if name is not None))
        dropped: Final = tuple(name for resolved_name, name in resolved if resolved_name is None)
        if not allowed_names:
            raise QueryScopeError(
                f"Every channel named by in: ({', '.join(sorted(set(dropped)))}) is outside the allowlist."
            )
        clauses: Final = " ".join(f"in:#{name}" for name in allowed_names)
        return ScopedQuery(
            query=f"{without_clauses} {clauses}".strip(),
            scoped_channels=len(allowed_names),
            dropped_clauses=dropped,
            unsearchable_ids=unsearchable,
        )

    if len(allowlist.channel_names) > max_scope_channels:
        raise QueryScopeError(
            f"The allowlist has {len(allowlist.channel_names)} channels, more than the "
            f"{max_scope_channels} a single search can scope to."
        )

    clauses = " ".join(f"in:#{name}" for name in sorted(allowlist.channel_names))
    return ScopedQuery(
        query=f"{without_clauses} {clauses}".strip(),
        scoped_channels=len(allowlist.channel_names),
        dropped_clauses=(),
        unsearchable_ids=unsearchable,
    )
