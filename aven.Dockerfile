ARG LITELLM_BASE_IMAGE=litellm-base:local
FROM ${LITELLM_BASE_IMAGE}

ARG MCP_HOME=/app/mcp
ARG TARGETARCH

COPY --chown=65534:0 .mcp-build/slack-mcp/${TARGETARCH}/ ${MCP_HOME}/slack-mcp/
COPY --chown=65534:0 config/mcp-servers.yml ${MCP_HOME}/mcp-servers.yml
COPY --chown=65534:0 docker/merge_and_start.py /app/docker/merge_and_start.py
COPY --chown=65534:0 docker/start_with_mcp.py /app/docker/start_with_mcp.py
RUN PYTHONPATH=${MCP_HOME}/slack-mcp/site-packages python -c "from slack_self_mcp.mcp_server import app; assert app is not None"

USER 65534

ENTRYPOINT ["python", "/app/docker/start_with_mcp.py"]