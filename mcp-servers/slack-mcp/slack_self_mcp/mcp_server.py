"""Application startup: the MCP server, its ASGI app, and the tool-call boundary.

The wire contract lives here: which tools are listed, how a tool call is dispatched, and
how a failure is turned into an ``isError`` result. The tool implementations themselves
are in :mod:`slack_self_mcp.slack_tools` and the credential handling in
:mod:`slack_self_mcp.auth`.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
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
from .guard import (
    DEFAULT_SEARCH_TOOLS,
    NO_CREDENTIAL_MESSAGE,
    ChannelAllowlist,
    UpstreamProxy,
    error_result,
)
from .slack_tools import (
    DEFAULT_TIMEOUT_SECONDS,
    MESSAGE_SELF_ARGUMENT,
    MESSAGE_SELF_TOOL_NAME,
    SlackApiError,
    SlackTools,
    tool_definitions,
)

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Surface this server's own INFO lines; the root logger stays at uvicorn's default."""
    if logger.handlers:
        return
    handler: Final = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


MCP_MOUNT_PATH: Final[str] = "/mcp"
SERVER_NAME: Final[str] = "slack-self"
SERVER_VERSION: Final[str] = "0.1.0"
UPSTREAM_URL_ENV: Final[str] = "SLACK_MCP_UPSTREAM_URL"
CHANNELS_FILE_ENV: Final[str] = "SLACK_MCP_CHANNELS_FILE"
SEARCH_TOOLS_ENV: Final[str] = "SLACK_MCP_SEARCH_TOOLS"
PACKAGE_CHANNELS_FILE: Final = Path(__file__).resolve().parent.parent / "channels.txt"


def _name_set(env_name: str, default: frozenset[str]) -> frozenset[str]:
    raw: Final = os.environ.get(env_name)
    if raw is None:
        return default
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def upstream_proxy_from_env() -> UpstreamProxy | None:
    """The channel-filtered upstream proxy, when one is configured.

    A configured upstream with an unusable allowlist raises at startup: serving without
    the filter would hand every Slack channel to whoever holds a token.
    """
    upstream_url: Final = os.environ.get(UPSTREAM_URL_ENV, "").strip()
    if not upstream_url:
        return None

    channels_file: Final = os.environ.get(CHANNELS_FILE_ENV, "").strip() or str(PACKAGE_CHANNELS_FILE)
    proxy: Final = UpstreamProxy(
        url=upstream_url,
        allowlist=ChannelAllowlist.from_file(channels_file),
        search_tools=_name_set(SEARCH_TOOLS_ENV, DEFAULT_SEARCH_TOOLS),
    )
    logger.info(
        "Proxying MCP upstream %s with %d channel IDs and %d channel names allowlisted",
        upstream_url,
        len(proxy.allowlist.channel_ids),
        len(proxy.allowlist.channel_names),
    )
    return proxy


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


def build_mcp_server(proxy: UpstreamProxy | None = None) -> Server:
    """The MCP server. ``proxy`` adds the channel-filtered upstream tools to this one."""
    server: Final = Server(name=SERVER_NAME, version=SERVER_VERSION)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        tools: Final = tool_definitions()
        if proxy is not None:
            try:
                tools.extend(await proxy.list_tools(current_bearer_token()))
            except Exception as exc:
                logger.warning("Could not list upstream MCP tools: %s", exc)
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
        args: Final = arguments or {}

        if name == MESSAGE_SELF_TOOL_NAME:
            return await run_message_self(
                message=str(args.get(MESSAGE_SELF_ARGUMENT, "")),
                token=current_bearer_token(),
            )

        if proxy is not None:
            try:
                return await proxy.call_tool(name, args, current_bearer_token())
            except Exception as exc:
                logger.warning("Upstream MCP call %s failed: %s", name, exc)
                return error_result(f"Upstream MCP call failed: {exc}")

        return error_result(f"Unknown tool: {name}")

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
    _configure_logging()
    session_manager: Final = StreamableHTTPSessionManager(
        app=build_mcp_server(upstream_proxy_from_env()),
        stateless=True,
    )

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
