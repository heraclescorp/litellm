from __future__ import annotations

from pathlib import Path

import pytest
from slack_self_mcp.channel_filter import ChannelAllowlist, ChannelAllowlistError, channel_arguments

GENERAL_ID = "C91ST88S2"
ENG_TEAM_ID = "CKC8CBNH5"


def test_allowlist_matches_channel_id_name_and_bare_name() -> None:
    allowlist = ChannelAllowlist.from_entries([f"{GENERAL_ID} #general", f"{ENG_TEAM_ID} #Eng-Team"])

    assert allowlist.allows(GENERAL_ID)
    assert allowlist.allows("#general")
    assert allowlist.allows("general")
    assert allowlist.allows("#ENG-TEAM")
    assert allowlist.allows(" eng-team ")


def test_allowlist_rejects_channels_it_does_not_list() -> None:
    allowlist = ChannelAllowlist.from_entries([f"{GENERAL_ID} #general"])

    assert not allowlist.allows(ENG_TEAM_ID)
    assert not allowlist.allows("#design-team")
    assert not allowlist.allows("D04ABCDEF")
    assert not allowlist.allows("")


def test_allowlist_rejects_an_entry_it_cannot_interpret() -> None:
    with pytest.raises(ChannelAllowlistError, match="eng-team"):
        ChannelAllowlist.from_entries([f"{GENERAL_ID} #general", "eng-team"])


def test_a_channel_can_be_listed_without_a_name() -> None:
    allowlist = ChannelAllowlist.from_entries([f"{GENERAL_ID}"])

    assert allowlist.allows(GENERAL_ID)
    assert allowlist.ids_without_names() == frozenset({GENERAL_ID})


def test_a_name_only_line_is_rejected() -> None:
    with pytest.raises(ChannelAllowlistError, match="does not start with a channel ID"):
        ChannelAllowlist.from_entries(["#general"])


def test_an_annotation_that_is_not_a_name_is_rejected() -> None:
    with pytest.raises(ChannelAllowlistError, match="is not a channel name"):
        ChannelAllowlist.from_entries([f"{GENERAL_ID} general"])


def test_a_duplicate_channel_id_is_rejected() -> None:
    with pytest.raises(ChannelAllowlistError, match="listed twice"):
        ChannelAllowlist.from_entries([f"{GENERAL_ID} #general", f"{GENERAL_ID} #other"])


def test_allowlist_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ChannelAllowlistError, match="does not exist"):
        ChannelAllowlist.from_file(tmp_path / "absent.txt")


def test_allowlist_rejects_a_file_with_no_channels(tmp_path: Path) -> None:
    empty = tmp_path / "channels.txt"
    empty.write_text("\n\n")

    with pytest.raises(ChannelAllowlistError, match="lists no channels"):
        ChannelAllowlist.from_file(empty)


def test_allowlist_reads_names_and_ids_from_a_file(tmp_path: Path) -> None:
    channels = tmp_path / "channels.txt"
    channels.write_text(f"{GENERAL_ID} #general\n{ENG_TEAM_ID} #eng-team\n")

    allowlist = ChannelAllowlist.from_file(channels)

    assert allowlist.channel_ids == {GENERAL_ID, ENG_TEAM_ID}
    assert allowlist.channel_names == {"general", "eng-team"}


def test_allowlist_reads_the_name_annotation_on_the_same_line() -> None:
    allowlist = ChannelAllowlist.from_entries([f"{GENERAL_ID} #general", f"{ENG_TEAM_ID} #eng-team"])

    assert allowlist.name_for_id(GENERAL_ID) == "general"
    assert allowlist.name_for_id(ENG_TEAM_ID) == "eng-team"
    assert allowlist.name_for_id("C0NOTALLOWED") is None


def test_the_shipped_default_allowlist_resolves_next_to_the_package() -> None:
    from slack_self_mcp.mcp_server import PACKAGE_CHANNELS_FILE

    allowlist = ChannelAllowlist.from_file(PACKAGE_CHANNELS_FILE)

    assert allowlist.channel_ids
    assert allowlist.channel_names


def test_allowlist_reports_ids_listed_without_a_name() -> None:
    allowlist = ChannelAllowlist.from_entries([f"{GENERAL_ID} #general", "C0ORPHANID0"])

    assert allowlist.ids_without_names() == frozenset({"C0ORPHANID0"})


def test_allowlist_reads_the_name_annotation_from_a_file(tmp_path: Path) -> None:
    channels = tmp_path / "channels.txt"
    channels.write_text(f"{GENERAL_ID} #general\n")

    allowlist = ChannelAllowlist.from_file(channels)

    assert allowlist.name_for_id(GENERAL_ID) == "general"


def test_channel_arguments_collects_strings_and_lists() -> None:
    values, malformed = channel_arguments({"channel": GENERAL_ID, "channels": [ENG_TEAM_ID]}, ("channel", "channels"))

    assert values == [GENERAL_ID, ENG_TEAM_ID]
    assert malformed == []


def test_channel_arguments_reports_values_that_cannot_be_validated() -> None:
    values, malformed = channel_arguments({"channel": {"id": GENERAL_ID}}, ("channel",))

    assert values == []
    assert malformed == ["channel"]
