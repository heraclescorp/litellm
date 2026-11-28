"""Bloomberg Data License REST client for last-price requests."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

import httpx

BASE_URL: Final = "https://api.bloomberg.com"
TOKEN_URL: Final = "https://bsso.blpprofessional.com/ext/api/as/token.oauth2"
API_HEADERS: Final = {"api-version": "2"}
# Data License bills per security requested.
MAX_TICKERS_PER_REQUEST: Final = 20
LAST_PRICE_FIELDS: Final = ("NAME", "SECURITY_TYP", "PX_LAST")
YELLOW_KEYS: Final = ("Index", "Curncy", "Govt", "Corp", "Equity", "Comdty", "Mtge", "Muni", "Pfd", "M-Mkt")
TICKER_PATTERN: Final = re.compile(rf"^[A-Z0-9][A-Z0-9 ./-]{{0,30}} ({'|'.join(YELLOW_KEYS)})$")
SCHEDULED_SUBSCRIPTION: Final = "scheduled"


@dataclass(frozen=True, slots=True)
class Credentials:
    client_id: str
    client_secret: str


@dataclass(frozen=True, slots=True)
class InvalidTickers:
    reason: str
    tickers: tuple[str, ...]
    kind: Literal["invalid_tickers"] = "invalid_tickers"


@dataclass(frozen=True, slots=True)
class Submitted:
    request_id: str
    tickers: tuple[str, ...]
    kind: Literal["submitted"] = "submitted"


@dataclass(frozen=True, slots=True)
class Pending:
    request_id: str
    kind: Literal["pending"] = "pending"


@dataclass(frozen=True, slots=True)
class Ready:
    request_id: str
    snapshot_start_time: str
    rows: tuple[Mapping[str, object], ...]
    kind: Literal["ready"] = "ready"


SubmitOutcome = InvalidTickers | Submitted
ResultOutcome = Pending | Ready


def normalize_tickers(tickers: Sequence[str]) -> InvalidTickers | tuple[str, ...]:
    unique: Final = tuple(dict.fromkeys(ticker.strip() for ticker in tickers))
    if not unique or len(unique) > MAX_TICKERS_PER_REQUEST:
        return InvalidTickers(reason=f"Pass between 1 and {MAX_TICKERS_PER_REQUEST} tickers.", tickers=unique)
    invalid: Final = tuple(ticker for ticker in unique if not TICKER_PATTERN.match(ticker))
    if invalid:
        return InvalidTickers(
            reason="Each ticker needs its Bloomberg yellow key, e.g. 'USGG10YR Index'.", tickers=invalid
        )
    return unique


def last_price_request(tickers: tuple[str, ...], title: str) -> dict[str, object]:
    return {
        "@type": "DataRequest",
        "title": title,
        "description": "MCP last price lookup",
        "universe": {
            "@type": "Universe",
            "contains": [
                {"@type": "Identifier", "identifierType": "TICKER", "identifierValue": ticker} for ticker in tickers
            ],
        },
        "fieldList": {"@type": "DataFieldList", "contains": [{"mnemonic": field} for field in LAST_PRICE_FIELDS]},
        "trigger": {"@type": "SubmitTrigger"},
        "formatting": {"@type": "MediaType", "outputMediaType": "application/json"},
    }


class DataLicenseClient:
    def __init__(self, credentials: Credentials, client: httpx.AsyncClient) -> None:
        self._credentials = credentials
        self._client = client

    async def _access_token(self) -> str:
        response: Final = await self._client.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self._credentials.client_id, self._credentials.client_secret),
        )
        response.raise_for_status()
        return str(response.json()["access_token"])

    async def _get(self, path: str, token: str, params: Mapping[str, str] | None = None) -> object:
        response: Final = await self._client.get(
            f"{BASE_URL}{path}",
            params=params,
            headers={**API_HEADERS, "Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return response.json()

    async def _catalog_id(self, token: str) -> str:
        catalogs: Final = await self._get("/eap/catalogs/", token)
        assert isinstance(catalogs, dict)
        return next(
            str(catalog["identifier"])
            for catalog in catalogs["contains"]
            if catalog["subscriptionType"] == SCHEDULED_SUBSCRIPTION
        )

    async def submit_last_prices(self, tickers: Sequence[str]) -> SubmitOutcome:
        normalized: Final = normalize_tickers(tickers)
        if isinstance(normalized, InvalidTickers):
            return normalized
        token: Final = await self._access_token()
        catalog_id: Final = await self._catalog_id(token)
        response: Final = await self._client.post(
            f"{BASE_URL}/eap/catalogs/{catalog_id}/requests/",
            json=last_price_request(normalized, f"McpLastPriceRequest_{uuid.uuid4().hex[:6]}"),
            headers={**API_HEADERS, "Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return Submitted(request_id=str(response.json()["request"]["identifier"]), tickers=normalized)

    async def last_price_result(self, request_id: str) -> ResultOutcome:
        token: Final = await self._access_token()
        catalog_id: Final = await self._catalog_id(token)
        responses_path: Final = f"/eap/catalogs/{catalog_id}/content/responses/"
        listing: Final = await self._get(responses_path, token, {"requestIdentifier": request_id})
        assert isinstance(listing, dict)
        ready: Final = listing["contains"]
        if not ready:
            return Pending(request_id=request_id)
        latest: Final = ready[0]
        rows: Final = await self._get(f"{responses_path}{latest['key']}", token)
        assert isinstance(rows, list)
        return Ready(
            request_id=request_id,
            snapshot_start_time=str(latest["metadata"]["DL_SNAPSHOT_START_TIME"]),
            rows=tuple(rows),
        )
