FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY pyproject.toml ./
COPY slack_self_mcp ./slack_self_mcp
RUN pip install --no-cache-dir .

EXPOSE 8000
USER 65534:65534

CMD ["uvicorn", "slack_self_mcp.server:app", "--host", "0.0.0.0", "--port", "8000"]
