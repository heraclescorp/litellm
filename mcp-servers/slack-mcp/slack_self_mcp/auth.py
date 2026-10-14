"""The per-request Slack credential that LiteLLM forwards on every MCP request.

LiteLLM owns the Slack OAuth grant and injects the per-user token as
``Authorization: Bearer ...``. This server stores no credential of its own.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Final

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

_authorization_header: Final[ContextVar[str | None]] = ContextVar("slack_self_authorization", default=None)


def bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    stripped: Final = token.strip()
    return stripped or None


def current_bearer_token() -> str | None:
    """The bearer token forwarded on the request being handled, if any."""
    return bearer_token(_authorization_header.get())


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
