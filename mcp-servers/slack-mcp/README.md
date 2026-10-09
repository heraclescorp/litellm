# slack-self-mcp

A single-tool MCP server that sends a Slack DM to **only** the authenticated caller.

```text
tools/list → slack_message_self
tools/call → auth.test → chat.postMessage(channel=<own user_id>)
```

The caller cannot choose a recipient. The tool accepts one argument, `message`.

## Credential flow

LiteLLM owns the Slack OAuth grant. It stores the per-user Slack token and injects it as
`Authorization: Bearer <token>` on every MCP request to this server. This server stores no
credential and needs no Slack client secret.

```text
LiteLLM (OAuth client, token store)
  → Authorization: Bearer <slack user token>
    → slack-self-mcp
      → Slack Web API
```

## LiteLLM configuration

```yaml
mcp_servers:
  slack_self:
    transport: http
    url: http://slack-self-mcp.internal:8000/mcp
    auth_type: oauth2
    oauth2_flow: authorization_code
    issuer: https://mcp.slack.com
    client_id: os.environ/SLACK_CLIENT_ID
    client_secret: os.environ/SLACK_CLIENT_SECRET
    scopes:
      - chat:write
      - im:write
      - users:read
    available_on_public_internet: false
    allow_all_keys: false
```

Pinning `issuer` makes LiteLLM anchor on Slack's RFC 8414 metadata, so this server never has
to publish OAuth metadata. Users authorize Slack once per server entry; a separate entry from
the official Slack MCP server means a second authorization.

Gating uses the standard MCP mechanisms: `allowed_tools`, `mcp_tool_permissions`, and
`mcp_toolsets`.

## Build and run

`buildmcp.sh` (invoked by `deploy.sh`) packages this server into `.mcp-build/slack-mcp/`, and
`aven.Dockerfile` copies `.mcp-build/` into the LiteLLM image at `/app/mcp/`.

```bash
./buildmcp.sh
# → .mcp-build/slack-mcp/site-packages/{slack_self_mcp,uvicorn,...}
```

The artifact is a relocatable dependency tree, not a virtualenv, so it is run with the
consuming image's interpreter and `PYTHONPATH`:

```bash
PYTHONPATH=/app/mcp/slack-mcp/site-packages \
  python3 -m uvicorn slack_self_mcp.server:app --host 0.0.0.0 --port 8000
```

Requires Python >= 3.12 in the consuming image. In the LiteLLM image that means running it as
its own process with a command override, since the image entrypoint starts the proxy.

For local development, run straight from the source tree:

```bash
uv run --with mcp --with httpx --with starlette --with uvicorn \
  uvicorn slack_self_mcp.server:app --host 127.0.0.1 --port 8099
```

Endpoints:

```text
GET  /health → {"status": "ok"}
POST /mcp    → MCP streamable HTTP
```

`tools/list` is unauthenticated so clients can enumerate before holding a credential. `tools/call` fails closed when no bearer token was forwarded.

## Behavior

- Missing or blank `message` → `isError` result, no Slack call
- No forwarded token → `isError` result, no Slack call
- Slack token error (`invalid_auth`, `token_revoked`, ...) → `isError` result naming the code
- Success → JSON `{"message_link": ..., "message_context": {"message_ts": ..., "channel_id": ...}}`

The token is never logged and never returned in a tool result.
