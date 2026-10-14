"""An MCP server that messages only the authenticated Slack user.

LiteLLM owns the Slack OAuth grant and forwards the per-user token as
``Authorization: Bearer ...`` on each request, so this package stores no credential and
never accepts a recipient from the caller. Start it with ``slack_self_mcp.mcp_server:app``.
"""

from __future__ import annotations
