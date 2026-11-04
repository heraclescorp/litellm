from __future__ import annotations

import pytest
from slack_self_mcp.channel_filter import ChannelAllowlist
from slack_self_mcp.query_scope import QueryScopeError, scope_query

GENERAL_ID = "C91ST88S2"
ENG_TEAM_ID = "CKC8CBNH5"


def _allowlist() -> ChannelAllowlist:
    return ChannelAllowlist.from_entries([f"{GENERAL_ID} #general", f"{ENG_TEAM_ID} #eng-team"])


def test_appends_one_clause_per_allowlisted_channel() -> None:
    scoped = scope_query("do automations have email posting", _allowlist())

    assert scoped.query.startswith("do automations have email posting ")
    assert "in:#eng-team" in scoped.query
    assert "in:#general" in scoped.query
    assert scoped.scoped_channels == 2
    assert scoped.dropped_clauses == ()


def test_keeps_a_caller_clause_that_is_allowlisted() -> None:
    scoped = scope_query("deploy in:#general", _allowlist())

    assert scoped.query == "deploy in:#general"
    assert scoped.scoped_channels == 1
    assert scoped.dropped_clauses == ()


def test_drops_a_caller_clause_outside_the_allowlist() -> None:
    scoped = scope_query("deploy in:#notary-team in:#eng-team", _allowlist())

    assert "notary-team" not in scoped.query
    assert "in:#eng-team" in scoped.query
    assert scoped.dropped_clauses == ("#notary-team",)


def test_rejects_a_query_scoped_only_to_channels_outside_the_allowlist() -> None:
    with pytest.raises(QueryScopeError, match="outside the allowlist"):
        scope_query("deploy in:#notary-team", _allowlist())


def test_clauses_are_matched_case_insensitively_and_without_the_hash() -> None:
    scoped = scope_query("deploy in:#ENG-TEAM", _allowlist())

    assert scoped.query == "deploy in:#eng-team"


def test_refuses_to_scope_an_allowlist_larger_than_one_query_can_hold() -> None:
    allowlist = ChannelAllowlist.from_entries([f"C0CHANNEL{index:02d} #channel-{index}" for index in range(5)])

    with pytest.raises(QueryScopeError, match="more than the 3"):
        scope_query("deploy", allowlist, max_scope_channels=3)


def test_does_not_duplicate_a_clause_for_the_same_channel() -> None:
    scoped = scope_query("deploy in:#general in:general", _allowlist())

    assert scoped.query.count("in:#general") == 1


def test_a_caller_clause_naming_a_channel_id_is_translated_to_its_name() -> None:
    scoped = scope_query(f"deploy in:{GENERAL_ID}", _allowlist())

    assert scoped.query == "deploy in:#general"
    assert scoped.dropped_clauses == ()


def test_a_caller_clause_naming_an_unknown_id_is_dropped() -> None:
    scoped = scope_query(f"deploy in:{GENERAL_ID} in:C0NOTALLOWED", _allowlist())

    assert scoped.query == "deploy in:#general"
    assert scoped.dropped_clauses == ("C0NOTALLOWED",)


def test_ids_without_names_are_reported_as_unsearchable() -> None:
    allowlist = ChannelAllowlist.from_entries([f"{GENERAL_ID} #general", "C0ORPHANID0"])

    scoped = scope_query("deploy", allowlist)

    assert scoped.unsearchable_ids == frozenset({"C0ORPHANID0"})
    assert "in:#general" in scoped.query
