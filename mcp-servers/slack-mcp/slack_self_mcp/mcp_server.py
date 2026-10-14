"""Application startup: the MCP server, its ASGI app, and the tool-call boundary.

The wire contract lives here: which tools are listed, how a tool call is dispatched, and
how a failure is turned into an ``isError`` result. The tool implementations themselves
are in :mod:`slack_self_mcp.slack_tools` and the credential handling in
:mod:`slack_self_mcp.auth`.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any, Final

import httpx
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from .auth import CaptureAuthorization, current_bearer_token
from .slack_tools import (
    DEFAULT_TIMEOUT_SECONDS,
    MESSAGE_SELF_ARGUMENT,
    MESSAGE_SELF_TOOL_NAME,
    SlackApiError,
    SlackTools,
    tool_definitions,
)

logger = logging.getLogger(__name__)

MCP_MOUNT_PATH: Final[str] = "/mcp"
SERVER_NAME: Final[str] = "slack-self"
SERVER_VERSION: Final[str] = "0.1.0"
NO_CREDENTIAL_MESSAGE: Final[str] = "No Slack credential was forwarded for this request."


def error_result(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], isError=True)


async def run_message_self(
    message: str,
    token: str | None,
    client: httpx.AsyncClient | None = None,
) -> CallToolResult:
    """Tool logic, separate from the MCP transport so it is directly testable."""
    if token is None:
        return error_result(NO_CREDENTIAL_MESSAGE)

    active_client: Final = client if client is not None else httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        result_text: Final = await SlackTools(token=token, client=active_client).message_self(message)
    except ValueError as exc:
        return error_result(str(exc))
    except SlackApiError as exc:
        if exc.is_token_error:
            logger.warning("Slack rejected the forwarded credential: %s", exc.error)
        return error_result(str(exc))
    except httpx.HTTPError as exc:
        return error_result(f"Slack request failed: {exc}")
    finally:
        if client is None:
            await active_client.aclose()

    return CallToolResult(content=[TextContent(type="text", text=result_text)], isError=False)


def build_mcp_server() -> Server:
    server: Final = Server(name=SERVER_NAME, version=SERVER_VERSION)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return tool_definitions()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
        if name != MESSAGE_SELF_TOOL_NAME:
            return error_result(f"Unknown tool: {name}")

        args: Final = arguments or {}
        return await run_message_self(
            message=str(args.get(MESSAGE_SELF_ARGUMENT, "")),
            token=current_bearer_token(),
        )

    return server


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


class McpAsgiApp:
    """Serve the session manager at an exact path.

    ``Mount`` issues a 307 redirect for the bare mount path, and MCP clients do not
    follow redirects, so the ASGI entry point is bound to an exact route instead.
    """

    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self._session_manager = session_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._session_manager.handle_request(scope, receive, send)


def build_app() -> Starlette:
    session_manager: Final = StreamableHTTPSessionManager(app=build_mcp_server(), stateless=True)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    app: Final = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route(MCP_MOUNT_PATH, McpAsgiApp(session_manager), methods=["GET", "POST", "DELETE"]),
        ],
        lifespan=lifespan,
    )
    app.add_middleware(CaptureAuthorization)
    return app


app: Final = build_app()
