from __future__ import annotations

import json

import httpx
import pytest
from slack_self_mcp.slack_api import SlackApiError, authenticated_identity, send_message_to_self

IDENTITY = {"ok": True, "user_id": "U_SELF", "url": "https://example.slack.com/"}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_sends_to_the_tokens_own_user() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if request.url.path.endswith("auth.test"):
            return httpx.Response(200, json=IDENTITY)
        return httpx.Response(200, json={"ok": True, "channel": "D_SELF", "ts": "1791498863.717709"})

    async with _client(handler) as client:
        result = json.loads(await send_message_to_self(client, "xoxp-token", "hello"))

    assert calls == [{}, {"channel": "U_SELF", "text": "hello"}]
    assert result["message_context"] == {"message_ts": "1791498863.717709", "channel_id": "D_SELF"}
    assert result["message_link"] == "https://example.slack.com/archives/D_SELF/p1791498863717709"


@pytest.mark.asyncio
async def test_identity_requires_user_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as client:
        with pytest.raises(SlackApiError) as exc_info:
            await authenticated_identity(client, "xoxp-token")

    assert exc_info.value.error == "missing_user_id"


@pytest.mark.asyncio
async def test_token_error_is_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})

    async with _client(handler) as client:
        with pytest.raises(SlackApiError) as exc_info:
            await send_message_to_self(client, "xoxp-token", "hello")

    assert exc_info.value.is_token_error is True
    assert "invalid_auth" in str(exc_info.value)


@pytest.mark.asyncio
async def test_send_failure_is_not_classified_as_token_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("auth.test"):
            return httpx.Response(200, json=IDENTITY)
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    async with _client(handler) as client:
        with pytest.raises(SlackApiError) as exc_info:
            await send_message_to_self(client, "xoxp-token", "hello")

    assert exc_info.value.is_token_error is False
