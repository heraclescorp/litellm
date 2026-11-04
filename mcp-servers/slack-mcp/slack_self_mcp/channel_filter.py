"""Which Slack channels this MCP server is allowed to touch.

One channel per line: the channel ID, then its name as a ``#name`` annotation.

    C91ST88S2  #general
    CKC8CBNH5  #eng-team
    C0ABCDE123

The ID identifies the channel everywhere a caller names one. The name is needed in exactly
two places, because Slack's own data uses names there: ``in:`` search clauses resolve channel
names only, and the concise search format reports channels by name alone. A channel listed
without a name can still be read and matched, but it cannot be searched.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

CHANNEL_ID_PATTERN: Final = re.compile(r"^[CG][A-Z0-9]{6,}$")

DEFAULT_CHANNEL_ARGUMENTS: Final = ("channel", "channel_id", "channels", "channel_ids")


class ChannelAllowlistError(ValueError):
    """Raised when the allowlist cannot be built from its configured source."""


@dataclass(frozen=True)
class ChannelAllowlist:
    channel_ids: frozenset[str]
    channel_names: frozenset[str]
    name_by_id: Mapping[str, str]

    @classmethod
    def from_entries(cls, entries: Sequence[object]) -> ChannelAllowlist:
        channel_ids: set[str] = set()
        name_by_id: dict[str, str] = {}

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
            name_by_id[channel_id] = trailing[1:].strip().casefold()

        return cls(
            channel_ids=frozenset(channel_ids),
            channel_names=frozenset(name_by_id.values()),
            name_by_id=MappingProxyType(name_by_id),
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

    def allows(self, value: str) -> bool:
        candidate: Final = value.strip()
        if not candidate:
            return False
        if candidate.startswith("#"):
            return candidate[1:].strip().casefold() in self.channel_names
        if CHANNEL_ID_PATTERN.match(candidate):
            return candidate in self.channel_ids
        return candidate.casefold() in self.channel_names

    def name_for_id(self, channel_id: str) -> str | None:
        """The channel's name, when the allowlist lists one for it."""
        return self.name_by_id.get(channel_id)

    def ids_without_names(self) -> frozenset[str]:
        """IDs that cannot be searched, because Slack's search scope needs a name."""
        return self.channel_ids - self.name_by_id.keys()


def channel_arguments(arguments: object, names: Sequence[str]) -> tuple[list[str], list[str]]:
    """The channel values named by a tool call, plus channel arguments carrying no usable value.

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
