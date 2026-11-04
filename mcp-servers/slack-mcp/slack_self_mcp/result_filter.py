"""Trim channel-scoped search payloads down to the allowlist before the caller sees them.

A Slack search returns hits from every channel the token can see, and its result payload
names the channel of each hit:

    ## Messages (5 results)
    ### Result 1 of 5
    Channel: #ops-eve-beta-test-group (ID: C0BUJT64B18)
    ...

    ## Messages (5 results)
    (5 results)
    1. #eng-team - Arnav Saraogi: ...

This module rewrites that payload so only allowlisted channels survive, renumbering the
entries and section counts, and drops entries it cannot attribute to a channel (files, for
instance, carry no channel).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final

from .channel_filter import ChannelAllowlist

SECTION_HEADER: Final = re.compile(r"^##\s+(?P<title>.+?)\s+\((?P<count>\d+)\s+results?\)\s*$")
COUNT_LINE: Final = re.compile(r"^\(\d+\s+results?\)\s*$")
DETAILED_ENTRY: Final = re.compile(r"^###\s+Result\s+\d+\s+of\s+\d+\s*$")
DETAILED_CHANNEL: Final = re.compile(r"^Channel:\s+(?P<name>#\S+)\s+\(ID:\s+(?P<id>[A-Z0-9]+)\)\s*$")
CONCISE_ENTRY: Final = re.compile(r"^(?P<index>\d+)\.\s+(?P<name>#\S+)\s+-")
FILTERED_NOTE: Final = "_Note: {dropped} result(s) from channels outside the allowlist were hidden._"
NO_RESULTS: Final = re.compile(r"^\s*No results found\.?\s*$")


class SearchPayloadError(ValueError):
    """Raised when an upstream search payload cannot be filtered safely."""


@dataclass(frozen=True)
class FilteredSearchResult:
    text: str
    kept: int
    dropped: int


def _section_channel(name: str, channel_id: str | None, allowlist: ChannelAllowlist) -> bool:
    if channel_id is not None and allowlist.allows(channel_id):
        return True
    return allowlist.allows(name)


def _detailed_entries(lines: list[str]) -> list[list[str]]:
    entries: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if DETAILED_ENTRY.match(line):
            if current:
                entries.append(current)
                current = []
            continue
        if current or line.strip():
            current.append(line)
    if current:
        entries.append(current)
    return entries


def _concise_entries(lines: list[str]) -> list[list[str]]:
    entries: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if COUNT_LINE.match(line):
            continue
        if CONCISE_ENTRY.match(line):
            if current:
                entries.append(current)
                current = []
        if current or line.strip():
            current.append(line)
    if current:
        entries.append(current)
    return entries


def _entry_is_allowed(entry: list[str], allowlist: ChannelAllowlist) -> bool:
    for line in entry:
        detailed: Final = DETAILED_CHANNEL.match(line.strip())
        if detailed:
            return _section_channel(detailed.group("name"), detailed.group("id"), allowlist)
        concise: Final = CONCISE_ENTRY.match(line)
        if concise:
            return _section_channel(concise.group("name"), None, allowlist)
    return False


def _render_detailed(title: str, entries: list[list[str]]) -> list[str]:
    rendered: Final = [f"## {title} ({len(entries)} results)"]
    for index, entry in enumerate(entries, start=1):
        rendered.append(f"### Result {index} of {len(entries)}")
        rendered.extend(entry)
    return rendered


def _render_concise(title: str, entries: list[list[str]]) -> list[str]:
    rendered: Final = [f"## {title} ({len(entries)} results)", f"({len(entries)} results)"]
    for index, entry in enumerate(entries, start=1):
        rendered.append(re.sub(r"^\d+\.\s+", f"{index}. ", entry[0]))
        rendered.extend(entry[1:])
    return rendered


def filter_search_text(body: str, allowlist: ChannelAllowlist) -> FilteredSearchResult:
    """Drop hits from channels outside ``allowlist`` from a rendered search result body."""
    lines: Final = body.splitlines()
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []

    for line in lines:
        header: Final = SECTION_HEADER.match(line)
        if header:
            sections.append((header.group("title"), []))
            continue
        if sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)

    if not sections:
        # An empty search is a legitimate answer ("No results found."), not a payload we
        # failed to understand; only unreadable structure is an error.
        if any(NO_RESULTS.match(line) for line in lines):
            return FilteredSearchResult(text=body, kept=0, dropped=0)
        raise SearchPayloadError("Search payload has no result sections to filter.")

    kept_total = 0
    dropped_total = 0
    rendered: list[str] = list(preamble)

    for title, section_lines in sections:
        is_detailed: Final = any(DETAILED_ENTRY.match(line) for line in section_lines)
        entries: Final = _detailed_entries(section_lines) if is_detailed else _concise_entries(section_lines)
        allowed: Final = [entry for entry in entries if _entry_is_allowed(entry, allowlist)]

        dropped_total += len(entries) - len(allowed)
        kept_total += len(allowed)
        if not allowed:
            continue
        rendered.extend(_render_detailed(title, allowed) if is_detailed else _render_concise(title, allowed))

    if dropped_total:
        rendered.extend(["", FILTERED_NOTE.format(dropped=dropped_total)])

    return FilteredSearchResult(text="\n".join(rendered), kept=kept_total, dropped=dropped_total)


def filter_search_payload(payload_text: str, allowlist: ChannelAllowlist) -> FilteredSearchResult:
    """Apply :func:`filter_search_text` to the JSON envelope an MCP search tool returns."""
    try:
        payload: Final = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise SearchPayloadError(f"Search payload is not JSON: {exc}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("results"), str):
        raise SearchPayloadError("Search payload has no 'results' string to filter.")

    filtered: Final = filter_search_text(payload["results"], allowlist)
    payload["results"] = filtered.text
    return FilteredSearchResult(
        text=json.dumps(payload, ensure_ascii=False),
        kept=filtered.kept,
        dropped=filtered.dropped,
    )
