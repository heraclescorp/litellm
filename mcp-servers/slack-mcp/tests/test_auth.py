from __future__ import annotations

from typing import Any

import pytest
from slack_self_mcp.auth import CaptureAuthorization, bearer_token, current_bearer_token


def test_bearer_token_parsing() -> None:
    assert bearer_token("Bearer xoxp-1") == "xoxp-1"
    assert bearer_token("bearer xoxp-1") == "xoxp-1"
    assert bearer_token("Basic abc") is None
    assert bearer_token("Bearer   ") is None
    assert bearer_token(None) is None


@pytest.mark.asyncio
async def test_authorization_is_visible_only_inside_its_own_request() -> None:
    seen: list[str | None] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(current_bearer_token())

    scope: Any = {"type": "http", "headers": [(b"authorization", b"Bearer xoxp-1")]}
    await CaptureAuthorization(inner)(scope, None, None)

    assert seen == ["xoxp-1"]
    assert current_bearer_token() is None


@pytest.mark.asyncio
async def test_non_http_scope_passes_through_without_a_token() -> None:
    seen: list[str | None] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(current_bearer_token())

    await CaptureAuthorization(inner)({"type": "lifespan"}, None, None)

    assert seen == [None]
