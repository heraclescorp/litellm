# bloomberg-mcp

An MCP server for Bloomberg Data License last prices, served on `127.0.0.1:8001` inside the LiteLLM image and
registered in `config/mcp-servers.yml` as `Bloomberg`

```text
tools/list -> submit_last_price_request, get_last_price_result
submit_last_price_request(tickers) -> request_id
get_last_price_result(request_id)  -> pending | rows with NAME, SECURITY_TYP, PX_LAST
```

Data License requests are asynchronous, so the work is split into a submit tool and a result tool rather than one
call that blocks for minutes. Data License bills per security, so a request is capped at 20 tickers and every ticker
must carry its yellow key (`USGG10YR Index`, `USOSFR5 Curncy`, `IBM US Equity`)

## Credentials

The server reads `BLOOMBERG_CLIENT_ID` and `BLOOMBERG_CLIENT_SECRET` from its environment and exchanges them at the
Bloomberg SSO token endpoint with the OAuth2 client credentials grant. Users never hold a Bloomberg credential: LiteLLM
decides which keys and teams may call the server, and every call uses the gateway's Data License account. A missing
credential fails the call closed instead of reaching Bloomberg

## Run and test

```bash
uv run pytest
uv run uvicorn bloomberg_mcp.mcp_server:app --host 127.0.0.1 --port 8001
```

`GET /health` returns `{"status": "ok"}` and `POST /mcp` is MCP streamable HTTP
