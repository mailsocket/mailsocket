# syntax=docker/dockerfile:1
# Remote MCP server (Streamable HTTP) — `mcp` compose service.
#
# A pure API client: installs the Python SDK + MCP package from THIS repo's
# source (so prod runs the gated code, not whatever is on PyPI) and talks to
# the web container with each caller's own Bearer key. No DB access, no
# secrets, no .env. Build from the repo root:
#   docker build -f deploy/mcp/Dockerfile .
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
COPY deploy/mcp/constraints.txt /src/constraints.txt
COPY sdks/python/ /src/sdks/python/
COPY sdks/mcp/ /src/sdks/mcp/

# Every runtime dependency (uvicorn, mcp, starlette, sse-starlette, anyio,
# pydantic, … the full transitive closure) is pinned in constraints.txt to the
# versions the suites were run against; the two mailsocket packages come from
# THIS repo's source.
RUN pip install -c /src/constraints.txt \
        "uvicorn==0.53.0" \
        "mcp==2.2.0" \
        /src/sdks/python \
        /src/sdks/mcp \
    && rm -rf /src \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /nonexistent --no-create-home app

WORKDIR /
USER app
EXPOSE 8080

# Container-local liveness probe (python-slim has no curl). /healthz is exempt
# from the Host allowlist and reveals nothing.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"]

# --factory: create_app() reads config from env once and REFUSES to start if
# MAILSOCKET_API_KEY is set. --no-access-log: uvicorn's access log would
# include query strings; the app writes its own header-free access log.
CMD ["uvicorn", "--factory", "mailsocket_mcp.remote:create_app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--limit-concurrency", "64", "--timeout-keep-alive", "5", \
     "--no-access-log", "--no-server-header", "--log-level", "info"]
