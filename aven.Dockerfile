ARG LITELLM_BASE_IMAGE=litellm-base:local
FROM ${LITELLM_BASE_IMAGE}

ARG MCP_HOME=/app/mcp

COPY --chown=65534:0 .mcp-build/ ${MCP_HOME}/
COPY --chown=65534:0 config/mcp-servers.yml ${MCP_HOME}/mcp-servers.yml
COPY --chown=65534:0 docker/merge-and-start.sh /app/docker/merge-and-start.sh

USER 65534

ENTRYPOINT ["/app/docker/merge-and-start.sh"]