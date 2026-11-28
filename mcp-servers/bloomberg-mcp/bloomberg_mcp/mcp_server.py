"""The MCP server: tool definitions, dispatch, and the ASGI app LiteLLM reaches on localhost."""

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import asdict
from typing import Final

import httpx
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from .data_license import MAX_TICKERS_PER_REQUEST, Credentials, DataLicenseClient, ResultOutcome, SubmitOutcome

logger: Final = logging.getLogger(__name__)

SERVER_NAME: Final = "bloomberg"
SERVER_VERSION: Final = "0.1.0"
MCP_PATH: Final = "/mcp"
CLIENT_ID_ENV: Final = "BLOOMBERG_CLIENT_ID"
CLIENT_SECRET_ENV: Final = "BLOOMBERG_CLIENT_SECRET"
REQUEST_TIMEOUT_SECONDS: Final = 60.0
SUBMIT_TOOL: Final = "submit_last_price_request"
RESULT_TOOL: Final = "get_last_price_result"


def tool_definitions() -> list[Tool]:
    return [
        Tool(
            name=SUBMIT_TOOL,
            description=(
                f"Request Bloomberg Data License NAME, SECURITY_TYP and PX_LAST for up to {MAX_TICKERS_PER_REQUEST} "
                "tickers. Tickers include the yellow key, e.g. 'USGG10YR Index', 'TSFR1M Index', 'USOSFR5 Curncy', "
                "'IBM US Equity'. Bloomberg bills per security, so request only what the question needs. Returns a "
                f"request_id; Bloomberg takes seconds to minutes, so call {RESULT_TOOL} with it afterwards."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tickers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": MAX_TICKERS_PER_REQUEST,
                    }
                },
                "required": ["tickers"],
                "additionalProperties": False,
            },
            annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        ),
        Tool(
            name=RESULT_TOOL,
            description=(
                f"Fetch the result of a {SUBMIT_TOOL} request. Returns status 'pending' until Bloomberg finishes. "
                "PX_LAST is Bloomberg's stated value (rates and yields in percent): quote it exactly with the "
                "snapshot time and never round, combine, or estimate a missing ticker."
            ),
            inputSchema={
                "type": "object",
                "properties": {"request_id": {"type": "string", "minLength": 1}},
                "required": ["request_id"],
                "additionalProperties": False,
            },
            annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True),
        ),
    ]


def credentials_from_env() -> Credentials | None:
    client_id: Final = os.environ.get(CLIENT_ID_ENV, "").strip()
    client_secret: Final = os.environ.get(CLIENT_SECRET_ENV, "").strip()
    if not client_id or not client_secret:
        return None
    return Credentials(client_id=client_id, client_secret=client_secret)


def _text_result(payload: Mapping[str, object], is_error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, default=str))], isError=is_error)


def outcome_result(outcome: SubmitOutcome | ResultOutcome) -> CallToolResult:
    match outcome.kind:
        case "invalid_tickers":
            return _text_result(asdict(outcome), is_error=True)
        case "submitted" | "pending" | "ready":
            return _text_result(asdict(outcome))


async def dispatch(name: str, arguments: Mapping[str, object], client: DataLicenseClient) -> CallToolResult:
    if name == SUBMIT_TOOL:
        tickers: Final = arguments.get("tickers")
        if not isinstance(tickers, list) or not all(isinstance(ticker, str) for ticker in tickers):
            return _text_result({"error": "tickers must be a list of strings"}, is_error=True)
        submitted: Final = await client.submit_last_prices(tickers)
        logger.info("Bloomberg last price request outcome=%s tickers=%d", submitted.kind, len(submitted.tickers))
        return outcome_result(submitted)
    if name == RESULT_TOOL:
        request_id: Final = arguments.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            return _text_result({"error": "request_id is required"}, is_error=True)
        result: Final = await client.last_price_result(request_id.strip())
        logger.info("Bloomberg last price result request_id=%s outcome=%s", request_id, result.kind)
        return outcome_result(result)
    return _text_result({"error": f"Unknown tool: {name}"}, is_error=True)


def build_mcp_server(call: Callable[[str, Mapping[str, object]], Awaitable[CallToolResult]]) -> Server:
    server: Final = Server(name=SERVER_NAME, version=SERVER_VERSION)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return tool_definitions()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, object] | None) -> CallToolResult:
        return await call(name, arguments or {})

    return server


async def call_with_env_credentials(name: str, arguments: Mapping[str, object]) -> CallToolResult:
    credentials: Final = credentials_from_env()
    if credentials is None:
        logger.error("Bloomberg tool %s called without %s and %s set", name, CLIENT_ID_ENV, CLIENT_SECRET_ENV)
        return _text_result({"error": "Bloomberg credentials are not configured on the gateway."}, is_error=True)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as http_client:
        try:
            return await dispatch(name, arguments, DataLicenseClient(credentials, http_client))
        except httpx.HTTPStatusError as exc:
            # Response bodies can echo request details; the status code is enough to triage.
            logger.warning("Bloomberg returned HTTP %d for tool %s", exc.response.status_code, name)
            return _text_result({"error": f"Bloomberg returned HTTP {exc.response.status_code}"}, is_error=True)
        except httpx.HTTPError as exc:
            logger.warning("Bloomberg request failed for tool %s: %s", name, type(exc).__name__)
            return _text_result({"error": f"Bloomberg request failed ({type(exc).__name__})"}, is_error=True)


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


class McpAsgiApp:
    """Bound to an exact route because MCP clients do not follow the redirect ``Mount`` issues for its bare path."""

    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self._session_manager = session_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._session_manager.handle_request(scope, receive, send)


def build_app() -> Starlette:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    session_manager: Final = StreamableHTTPSessionManager(
        app=build_mcp_server(call_with_env_credentials), stateless=True
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route(MCP_PATH, McpAsgiApp(session_manager), methods=["GET", "POST", "DELETE"]),
        ],
        lifespan=lifespan,
    )


app: Final = build_app()
