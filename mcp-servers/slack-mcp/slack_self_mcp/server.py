"""A single-tool MCP server that messages only the authenticated Slack user.

LiteLLM owns the Slack OAuth grant and injects the per-user Slack token as
``Authorization: Bearer ...`` on every MCP request to this server. The server never
stores a credential and never accepts a recipient from the caller.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Any, Final

import httpx
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .slack_api import DEFAULT_TIMEOUT_SECONDS, SlackApiError, send_message_to_self

logger = logging.getLogger(__name__)

MCP_MOUNT_PATH: Final[str] = "/mcp"
MESSAGE_SELF_TOOL_NAME: Final[str] = "slack_message_self"
MESSAGE_SELF_TOOL_DESCRIPTION: Final[str] = "Send a private Slack message only to the authenticated Slack user."

_authorization_header: Final[ContextVar[str | None]] = ContextVar("slack_self_authorization", default=None)


def bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    stripped: Final = token.strip()
    return stripped or None


def tool_definition() -> Tool:
    return Tool(
        name=MESSAGE_SELF_TOOL_NAME,
        description=MESSAGE_SELF_TOOL_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Message to send to your own Slack direct message.",
                }
            },
            "required": ["message"],
        },
    )


async def run_message_self(
    message: str,
    token: str | None,
    client: httpx.AsyncClient | None = None,
) -> CallToolResult:
    """Tool logic, separate from the MCP transport so it is directly testable."""
    if not message.strip():
        return CallToolResult(content=[TextContent(type="text", text="Message text is required.")], isError=True)
    if token is None:
        return CallToolResult(
            content=[TextContent(type="text", text="No Slack credential was forwarded for this request.")],
            isError=True,
        )

    active_client: Final = client if client is not None else httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        result_text: Final = await send_message_to_self(active_client, token, message)
    except SlackApiError as exc:
        if exc.is_token_error:
            logger.warning("Slack rejected the forwarded credential: %s", exc.error)
        return CallToolResult(content=[TextContent(type="text", text=str(exc))], isError=True)
    except httpx.HTTPError as exc:
        return CallToolResult(content=[TextContent(type="text", text=f"Slack request failed: {exc}")], isError=True)
    finally:
        if client is None:
            await active_client.aclose()

    return CallToolResult(content=[TextContent(type="text", text=result_text)], isError=False)


def build_mcp_server() -> Server:
    server: Final = Server(name="slack-self", version="0.1.0")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [tool_definition()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
        if name != MESSAGE_SELF_TOOL_NAME:
            return CallToolResult(content=[TextContent(type="text", text=f"Unknown tool: {name}")], isError=True)

        args: Final = arguments or {}
        return await run_message_self(
            message=str(args.get("message", "")),
            token=bearer_token(_authorization_header.get()),
        )

    return server


class CaptureAuthorization:
    """Expose the inbound Authorization header to the tool handler for this request.

    The header is captured but not enforced at the HTTP layer: clients must be able to
    enumerate tools before holding a credential, and a 401 here would also break
    upstream OAuth discovery. The tool itself fails closed when no credential arrived.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        reset_token: Final = _authorization_header.set(Request(scope).headers.get("authorization"))
        try:
            await self.app(scope, receive, send)
        finally:
            _authorization_header.reset(reset_token)


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
