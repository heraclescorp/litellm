from __future__ import annotations

import json

import httpx
import pytest

from bloomberg_mcp.data_license import TOKEN_URL, Credentials, DataLicenseClient
from bloomberg_mcp.mcp_server import RESULT_TOOL, SUBMIT_TOOL, call_with_env_credentials, dispatch, tool_definitions


def _payload(result) -> dict[str, object]:
    return json.loads(result.content[0].text)


def test_lists_only_the_two_read_only_last_price_tools() -> None:
    tools = tool_definitions()

    assert [tool.name for tool in tools] == [SUBMIT_TOOL, RESULT_TOOL]
    assert all(tool.annotations and tool.annotations.readOnlyHint for tool in tools)


async def test_submit_tool_returns_the_request_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "tok"})
        if request.method == "GET":
            return httpx.Response(200, json={"contains": [{"identifier": "1", "subscriptionType": "scheduled"}]})
        return httpx.Response(201, json={"request": {"identifier": "req-9"}})

    client = DataLicenseClient(Credentials("cid", "secret"), httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    result = await dispatch(SUBMIT_TOOL, {"tickers": ["FDTR Index"]}, client)

    assert result.isError is False
    assert _payload(result) == {"request_id": "req-9", "tickers": ["FDTR Index"], "kind": "submitted"}


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        (SUBMIT_TOOL, {"tickers": "FDTR Index"}),
        (SUBMIT_TOOL, {"tickers": ["FDTR"]}),
        (RESULT_TOOL, {}),
        ("unknown", {}),
    ],
)
async def test_bad_calls_are_errors_without_reaching_bloomberg(name: str, arguments: dict[str, object]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected Bloomberg call to {request.url}")

    client = DataLicenseClient(Credentials("cid", "secret"), httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    result = await dispatch(name, arguments, client)

    assert result.isError is True


async def test_missing_credentials_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BLOOMBERG_CLIENT_ID", raising=False)
    monkeypatch.delenv("BLOOMBERG_CLIENT_SECRET", raising=False)

    result = await call_with_env_credentials(SUBMIT_TOOL, {"tickers": ["FDTR Index"]})

    assert result.isError is True
    assert "not configured" in _payload(result)["error"]
