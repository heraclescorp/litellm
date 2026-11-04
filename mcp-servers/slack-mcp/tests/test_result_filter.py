from __future__ import annotations

import json

import pytest
from slack_self_mcp.channel_filter import ChannelAllowlist
from slack_self_mcp.result_filter import SearchPayloadError, filter_search_payload

GENERAL_ID = "C91ST88S2"
OUTSIDE_ID = "C0BUJT64B18"

DETAILED_BODY = """# Search Results for: deploy

## Messages (3 results)
### Result 1 of 3
Channel: #ops-eve-beta-test-group (ID: C0BUJT64B18)
From: Murtada Shah <murtada@aven.com> (ID: U90BXHX88)
Message_ts: 1793135174.412869
Text:
re-deploy Eve after changes?

### Result 2 of 3
Channel: #general (ID: C91ST88S2)
From: Saro Vasudevan <saro@aven.com> (ID: U01NP160MV3)
Message_ts: 1793468115.440959
Text:
AI as a validator

### Result 3 of 3
Channel: #notary-team (ID: C01BETH12US)
From: Davi Costa <davi@aven.com> (ID: U07D2HE75)
Message_ts: 1793135711.530669
Text:
pushing a major fix
"""

CONCISE_BODY = """# Search Results for: deploy

## Messages (3 results)
(3 results)
1. #ops-eve-beta-test-group - Murtada Shah: re-deploy Eve? 2026-10-27 15:06:14 CST
2. #general - Saro Vasudevan: AI as a validator 2026-10-31 11:35:15 CST
3. #notary-team - Davi Costa: pushing a major fix 2026-10-29 11:57:32 CST
"""


def _allowlist() -> ChannelAllowlist:
    return ChannelAllowlist.from_entries([f"{GENERAL_ID} #general"])


def _payload(body: str, pagination: str | None = "For the next page use cursor `abc`\n") -> str:
    return json.dumps({"results": body, "pagination_info": pagination})


def test_detailed_results_keep_only_allowlisted_channels() -> None:
    filtered = filter_search_payload(_payload(DETAILED_BODY), _allowlist())

    payload = json.loads(filtered.text)
    assert "#general" in payload["results"]
    assert "ops-eve-beta-test-group" not in payload["results"]
    assert "notary-team" not in payload["results"]
    assert (filtered.kept, filtered.dropped) == (1, 2)


def test_detailed_results_are_renumbered_and_the_section_count_is_rewritten() -> None:
    filtered = filter_search_payload(_payload(DETAILED_BODY), _allowlist())

    body = json.loads(filtered.text)["results"]
    assert "## Messages (1 results)" in body
    assert "### Result 1 of 1" in body
    assert "### Result 2 of 2" not in body


def test_concise_results_keep_only_allowlisted_channels() -> None:
    filtered = filter_search_payload(_payload(CONCISE_BODY), _allowlist())

    body = json.loads(filtered.text)["results"]
    assert "#general - Saro Vasudevan" in body
    assert "ops-eve-beta-test-group" not in body
    assert "notary-team" not in body
    assert "1. #general" in body


def test_pagination_info_is_preserved() -> None:
    filtered = filter_search_payload(_payload(DETAILED_BODY), _allowlist())

    assert json.loads(filtered.text)["pagination_info"] == "For the next page use cursor `abc`\n"


def test_filtering_reports_how_many_results_were_hidden_without_naming_them() -> None:
    filtered = filter_search_payload(_payload(DETAILED_BODY), _allowlist())

    body = json.loads(filtered.text)["results"]
    assert "2 result(s) from channels outside the allowlist were hidden" in body
    assert "notary" not in body


def test_a_section_with_no_allowlisted_results_is_removed() -> None:
    files_section = """# Search Results for: deploy

## Messages (1 results)
### Result 1 of 1
Channel: #general (ID: C91ST88S2)
From: Saro Vasudevan
Text:
hello

## Files (1 results)
### Result 1 of 1
Title: deployment logs
File ID: F0A3AMN43K9
"""

    filtered = filter_search_payload(_payload(files_section), _allowlist())

    body = json.loads(filtered.text)["results"]
    assert "## Files" not in body
    assert "deployment logs" not in body
    assert (filtered.kept, filtered.dropped) == (1, 1)


def test_payload_without_sections_is_rejected() -> None:
    with pytest.raises(SearchPayloadError, match="no result sections"):
        filter_search_payload(_payload("# Search Results for: deploy\n"), _allowlist())


def test_non_json_payload_is_rejected() -> None:
    with pytest.raises(SearchPayloadError, match="not JSON"):
        filter_search_payload("upstream said something unexpected", _allowlist())


def test_payload_without_results_string_is_rejected() -> None:
    with pytest.raises(SearchPayloadError, match="no 'results'"):
        filter_search_payload(json.dumps({"pagination_info": None}), _allowlist())
