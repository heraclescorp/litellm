#!/bin/sh
set -eu

BASE_CONFIG=/etc/litellm/config.yaml
MCP_CONFIG=/app/mcp/mcp-servers.yml
MERGED_CONFIG=/tmp/litellm-merged.yaml

if [ "${1:-}" = "--config" ]; then
    if [ "$#" -lt 2 ]; then
        echo "--config requires a path" >&2
        exit 2
    fi
    BASE_CONFIG=$2
    shift 2
elif [ "${1:-}" != "" ] && [ "${1#--config=}" != "$1" ]; then
    BASE_CONFIG=${1#--config=}
    shift
fi

umask 077
cat "$BASE_CONFIG" > "$MERGED_CONFIG"
printf '\n' >> "$MERGED_CONFIG"
cat "$MCP_CONFIG" >> "$MERGED_CONFIG"

exec litellm --config "$MERGED_CONFIG" "$@"
