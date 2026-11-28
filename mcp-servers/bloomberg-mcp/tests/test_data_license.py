from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from bloomberg_mcp.data_license import (
    MAX_TICKERS_PER_REQUEST,
    TOKEN_URL,
    Credentials,
    DataLicenseClient,
    InvalidTickers,
    Pending,
    Ready,
    Submitted,
)

CATALOGS = {
    "contains": [
        {"identifier": "999", "subscriptionType": "bulk"},
        {"identifier": "64192", "subscriptionType": "scheduled"},
    ]
}


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> DataLicenseClient:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DataLicenseClient(Credentials(client_id="cid", client_secret="secret"), http_client)


def _bloomberg(requests: list[httpx.Request], responses_listing: dict[str, object], rows: list[dict[str, object]]):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        url = str(request.url)
        if url == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "tok", "token_type": "Bearer"})
        if url.endswith("/eap/catalogs/"):
            return httpx.Response(200, json=CATALOGS)
        if request.method == "POST" and url.endswith("/eap/catalogs/64192/requests/"):
            return httpx.Response(201, json={"request": {"identifier": "req-1"}})
        if "/content/responses/?" in url:
            return httpx.Response(200, json=responses_listing)
        if url.endswith("/content/responses/resp-key"):
            return httpx.Response(200, json=rows)
        return httpx.Response(404)

    return handler


async def test_submit_authenticates_targets_the_scheduled_catalog_and_dedupes_tickers() -> None:
    requests: list[httpx.Request] = []
    client = _client(_bloomberg(requests, {"contains": []}, []))

    outcome = await client.submit_last_prices(["USGG10YR Index", " TSFR1M Index", "USGG10YR Index"])

    assert outcome == Submitted(request_id="req-1", tickers=("USGG10YR Index", "TSFR1M Index"))
    token_request, catalogs_request, submit_request = requests
    assert token_request.headers["authorization"].startswith("Basic ")
    assert b"grant_type=client_credentials" in token_request.content
    assert catalogs_request.headers["authorization"] == "Bearer tok"
    assert submit_request.headers["api-version"] == "2"
    body = json.loads(submit_request.content)
    assert [item["identifierValue"] for item in body["universe"]["contains"]] == ["USGG10YR Index", "TSFR1M Index"]
    assert [field["mnemonic"] for field in body["fieldList"]["contains"]] == ["NAME", "SECURITY_TYP", "PX_LAST"]


@pytest.mark.parametrize(
    "tickers",
    [
        [],
        ["USGG10YR"],
        ["usgg10yr index"],
        ["USGG10YR Index' OR 1=1 --"],
        [f"T{index} Index" for index in range(MAX_TICKERS_PER_REQUEST + 1)],
    ],
)
async def test_submit_rejects_invalid_ticker_lists_without_calling_bloomberg(tickers: list[str]) -> None:
    requests: list[httpx.Request] = []
    client = _client(_bloomberg(requests, {"contains": []}, []))

    outcome = await client.submit_last_prices(tickers)

    assert isinstance(outcome, InvalidTickers)
    assert requests == []


async def test_result_is_pending_until_bloomberg_lists_a_response() -> None:
    requests: list[httpx.Request] = []
    client = _client(_bloomberg(requests, {"contains": []}, []))

    outcome = await client.last_price_result("req-1")

    assert outcome == Pending(request_id="req-1")
    assert requests[-1].url.params["requestIdentifier"] == "req-1"


async def test_result_returns_rows_and_snapshot_time_when_ready() -> None:
    rows = [{"IDENTIFIER": "USGG10YR Index", "PX_LAST": 4.1234}]
    listing = {"contains": [{"key": "resp-key", "metadata": {"DL_SNAPSHOT_START_TIME": "2026-11-24T12:00:00"}}]}
    client = _client(_bloomberg([], listing, rows))

    outcome = await client.last_price_result("req-1")

    assert outcome == Ready(request_id="req-1", snapshot_start_time="2026-11-24T12:00:00", rows=tuple(rows))


async def test_rejected_credentials_surface_as_http_status_error() -> None:
    client = _client(lambda request: httpx.Response(401, json={"error": "invalid_client"}))

    with pytest.raises(httpx.HTTPStatusError):
        await client.submit_last_prices(["USGG10YR Index"])
