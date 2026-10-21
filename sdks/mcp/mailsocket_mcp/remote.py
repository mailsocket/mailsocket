"""Remote (Streamable HTTP) mailsocket MCP server — ``mailsocket-mcp-http``.

Serves the SAME tools as the stdio server at ``POST /mcp``. Every request is
authenticated with the caller's own mailsocket key
(``Authorization: Bearer ms_live_...``) and runs against a fresh SDK client
bound to that request only. The process holds no key of its own, so auth,
tenant scoping and every rate limit are enforced by the mailsocket API.

Hard rules (cross-tenant / SSRF safety):

* ``MAILSOCKET_API_KEY`` must NOT be set — the app refuses to start if it is,
  so a missing header can never silently act as some account.
* The upstream API URL comes ONLY from the process environment
  (``UPSTREAM_BASE_URL`` / ``UPSTREAM_HOST``), never from a request.
* Upstream requests carry a fresh header set (``Authorization`` + optional fixed
  ``Host`` + ``X-Forwarded-Proto: https``). Client headers are never forwarded.
* A key in the query string is rejected (400); keys never reach any log.

Environment:

* ``MCP_ALLOWED_HOSTS``  comma list of accepted Host headers
  (default ``mcp.mailsocket.app``; ``name:*`` allows any port).
* ``MCP_ALLOWED_ORIGINS`` comma list of accepted browser Origins
  (default ``https://<each allowed host>``). A missing Origin is fine.
* ``UPSTREAM_BASE_URL``  mailsocket API base (default the public API).
* ``UPSTREAM_HOST``      if set, sent as ``Host`` upstream together with
  ``X-Forwarded-Proto: https`` (needed when calling ``http://web:8000``).
* ``MCP_BIND_HOST`` / ``MCP_PORT`` for :func:`main` (default 0.0.0.0:8080).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import parse_qsl, unquote

from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mailsocket import Client
from mailsocket.client import DEFAULT_BASE_URL

from .server import (
    REMOTE_MAX_WAIT_TIMEOUT,
    __version__,
    _redact,
    bind_client,
    install_log_redaction,
    server,
)

__all__ = ["create_app", "main", "MAX_BODY_BYTES", "RemoteConfig"]

logger = logging.getLogger("mailsocket_mcp.remote")
access_logger = logging.getLogger("mailsocket_mcp.access")

MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"
SERVER_CARD_PATH = "/.well-known/mcp/server-card.json"
MAX_BODY_BYTES = 64 * 1024
KEY_RE = re.compile(r"^ms_live_[A-Za-z0-9_\-]{8,200}$")
# Any query parameter that looks like a credential is refused outright.
_CREDENTIAL_PARAMS = {"api_key", "apikey", "key", "token", "access_token", "authorization"}
_KEY_LIKE = re.compile(r"ms_live_", re.IGNORECASE)
# Access-log placeholder for any path that carries (or was refused for) a key.
REDACTED_PATH = "/mcp/<redacted>"
# How many extra percent-decoding rounds a path gets before key detection.
_MAX_PATH_DECODES = 3
WWW_AUTHENTICATE = 'Bearer realm="mailsocket"'
KEY_HINT = "Send a valid key as `Authorization: Bearer ms_live_...`."
DEFAULT_ALLOWED_HOST = "mcp.mailsocket.app"

REFUSE_ENV_KEY_MESSAGE = (
    "MAILSOCKET_API_KEY is set, but the remote MCP server must never hold a key "
    "of its own (each request brings its caller's key). Unset it and restart."
)


def _split(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


class RemoteConfig:
    """Process configuration, read ONCE from the environment."""

    def __init__(self, environ: dict[str, str] | None = None):
        env = dict(os.environ if environ is None else environ)
        if env.get("MAILSOCKET_API_KEY"):
            raise RuntimeError(REFUSE_ENV_KEY_MESSAGE)
        self.allowed_hosts = [h.lower() for h in _split(env.get("MCP_ALLOWED_HOSTS"))] or [
            DEFAULT_ALLOWED_HOST
        ]
        self.allowed_origins = _split(env.get("MCP_ALLOWED_ORIGINS")) or [
            f"https://{h}" for h in self.allowed_hosts if not h.endswith(":*")
        ]
        self.upstream_base_url = (env.get("UPSTREAM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        if not re.match(r"^https?://", self.upstream_base_url):
            raise RuntimeError("UPSTREAM_BASE_URL must be an http(s) URL.")
        upstream_host = (env.get("UPSTREAM_HOST") or "").strip()
        # Fixed, fresh upstream header set. Nothing from the client request is
        # ever copied in here, and CF-Connecting-IP is never set.
        self.upstream_headers: dict[str, str] = {}
        if upstream_host:
            self.upstream_headers = {"Host": upstream_host, "X-Forwarded-Proto": "https"}

    def host_allowed(self, host: str | None) -> bool:
        if not host:
            return False
        host = host.lower()
        for allowed in self.allowed_hosts:
            if host == allowed:
                return True
            if allowed.endswith(":*") and host.startswith(allowed[:-1]):
                return True
        return False


# -- small ASGI helpers ------------------------------------------------------


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> Response:
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status, headers=headers
    )


def _unauthorized(message: str) -> Response:
    return _error(401, "authentication_required", message, {"WWW-Authenticate": WWW_AUTHENTICATE})


def _bearer_key(headers: Headers) -> tuple[str | None, str | None]:
    """Return ``(key, None)`` or ``(None, reason)``. Never logs the value."""
    values = headers.getlist("authorization")
    if not values:
        return None, "Missing Authorization header. " + KEY_HINT
    if len(values) > 1:
        return None, "Send exactly one Authorization header. " + KEY_HINT
    scheme, _, token = values[0].strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not KEY_RE.match(token):
        return None, "Malformed API key. " + KEY_HINT
    return token, None


def _path_has_key(path: str) -> bool:
    """True if ``ms_live_`` (any case) appears in the path as given OR after
    up to ``_MAX_PATH_DECODES`` rounds of percent-decoding, so
    ``/mcp/MS_LIVE_…`` and double-encoded ``/mcp/%256Ds_live_…`` are caught."""
    candidate = path
    for _ in range(_MAX_PATH_DECODES + 1):
        if _KEY_LIKE.search(candidate):
            return True
        decoded = unquote(candidate)
        if decoded == candidate:
            return False
        candidate = decoded
    return False


def _log_path(path: str) -> str:
    """Path for the access log: a fixed placeholder whenever the path carries
    a key (the credential_in_url refusal uses the same test), else the path
    with any ``ms_live_`` token scrubbed."""
    if _path_has_key(path):
        return REDACTED_PATH
    return _redact(path, None)


def _query_has_credential(query_string: bytes) -> bool:
    try:
        pairs = parse_qsl(query_string.decode("latin-1"), keep_blank_values=True)
    except ValueError:
        return True
    for name, value in pairs:
        if name.lower() in _CREDENTIAL_PARAMS or _KEY_LIKE.search(value) or _KEY_LIKE.search(name):
            return True
    return False


def _redacting_send(send: Send, key: str) -> Send:
    """Wrap ``send`` so NO inner-app response body can carry the caller's key.

    The mcp library validates tool names/arguments BEFORE our tool wrapper
    runs and echoes rejected input (unknown tool name, pydantic
    ``input_value=…``) into the JSON-RPC result. So the body is buffered
    (stateless + ``json_response=True`` → one small JSON document, already
    capped upstream of us by the tool outputs), redacted with the same rules
    as tool errors (literal, url-encoded, fragments, any ``ms_live_`` token),
    and ``Content-Length`` recomputed. The key alphabet ``[A-Za-z0-9_-]``
    never needs JSON escaping, so replacing it with ``***`` keeps JSON valid.
    """
    start: Message | None = None
    chunks: list[bytes] = []

    async def wrapped(message: Message) -> None:
        nonlocal start
        if message["type"] == "http.response.start":
            start = message
            return
        if message["type"] == "http.response.body" and start is not None:
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            raw = b"".join(chunks)
            body = _redact(raw.decode("utf-8", "replace"), key).encode("utf-8") if raw else raw
            headers = [
                (name, value)
                for name, value in start.get("headers", [])
                if name.lower() != b"content-length"
            ]
            headers.append((b"content-length", str(len(body)).encode("latin-1")))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body, "more_body": False})
            start = None
            chunks.clear()
            return
        await send(message)

    return wrapped


async def _read_limited_body(receive: Receive, limit: int) -> tuple[bytes | None, Message | None]:
    """Buffer the body up to ``limit`` bytes; ``None`` means it was too large."""
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return bytes(body), message
        body.extend(message.get("body", b""))
        if len(body) > limit:
            return None, None
        if not message.get("more_body", False):
            return bytes(body), None


# -- the app -----------------------------------------------------------------


class RemoteMCPApp:
    """Outer ASGI app: host allowlist, body cap, auth, per-request client."""

    def __init__(self, config: RemoteConfig):
        self.config = config
        self.inner: ASGIApp = server.streamable_http_app(
            streamable_http_path=MCP_PATH,
            stateless_http=True,
            json_response=True,
            max_request_body_size=MAX_BODY_BYTES,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=list(config.allowed_hosts),
                allowed_origins=list(config.allowed_origins),
            ),
        )
        self._server_card: dict[str, Any] | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            # The inner app's lifespan runs the MCP session manager task group.
            await self.inner(scope, receive, send)
            return
        if scope["type"] != "http":
            await Response(status_code=404)(scope, receive, send)
            return

        started = time.monotonic()
        status_holder = {"status": 500}

        async def send_logged(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        try:
            await self._dispatch(scope, receive, send_logged)
        finally:
            # Access log: method, path (NO query string), status, duration.
            # Headers are never logged, so neither is the key. A path with a
            # key in it (any case, up to 3x percent-encoded) is replaced by a
            # fixed placeholder — never partially scrubbed.
            access_logger.info(
                "%s %s %s %.0fms",
                scope.get("method", "-"),
                _log_path(str(scope.get("path", "-"))),
                status_holder["status"],
                (time.monotonic() - started) * 1000,
            )

    async def _dispatch(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        headers = Headers(scope=scope)

        # Liveness probe for the container healthcheck (hit via 127.0.0.1, so
        # it is exempt from the Host allowlist; it reveals nothing).
        if path == HEALTH_PATH:
            await JSONResponse({"status": "ok"})(scope, receive, send)
            return

        if not self.config.host_allowed(headers.get("host")):
            await _error(421, "invalid_host", "Invalid Host header.")(scope, receive, send)
            return

        if _path_has_key(path):
            # e.g. POST /mcp/ms_live_… — refuse it (the logged path is
            # redacted too, but the request must not proceed either).
            await _error(
                400,
                "credential_in_url",
                "Never put the API key in the URL. " + KEY_HINT,
            )(scope, receive, send)
            return

        if _query_has_credential(scope.get("query_string", b"")):
            await _error(
                400,
                "credential_in_query",
                "Never put the API key in the URL. " + KEY_HINT,
            )(scope, receive, send)
            return

        if path == SERVER_CARD_PATH:
            await JSONResponse(await self._card())(scope, receive, send)
            return

        if path != MCP_PATH:
            await _error(404, "not_found", "Not found. The MCP endpoint is POST /mcp.")(
                scope, receive, send
            )
            return

        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                too_big = int(content_length) > MAX_BODY_BYTES
            except ValueError:
                too_big = True
            if too_big:
                await _error(413, "payload_too_large", "Request body too large.")(
                    scope, receive, send
                )
                return

        key, reason = _bearer_key(headers)
        if key is None:
            await _unauthorized(reason or KEY_HINT)(scope, receive, send)
            return

        if method != "POST":
            # Stateless + JSON responses: no GET SSE stream, no DELETE session.
            await Response(status_code=405, headers={"Allow": "POST"})(scope, receive, send)
            return

        body, trailing = await _read_limited_body(receive, MAX_BODY_BYTES)
        if body is None:
            await _error(413, "payload_too_large", "Request body too large.")(scope, receive, send)
            return
        replay: list[Message] = [{"type": "http.request", "body": body, "more_body": False}]
        if trailing is not None:
            replay.append(trailing)

        async def replay_receive() -> Message:
            if replay:
                return replay.pop(0)
            return await receive()

        # Fresh client per request: the caller's key, env-only upstream, fixed
        # header set. Bound via ContextVar for exactly this request, then reset
        # (also on error). Never cached across requests.
        client = Client(
            key,
            base_url=self.config.upstream_base_url,
            extra_headers=dict(self.config.upstream_headers),
            # A 429 on a wait surfaces at once as a tool error instead of the
            
            retry_wait_on_429=False,
        )
        with bind_client(
            client,
            key,
            max_wait_timeout=REMOTE_MAX_WAIT_TIMEOUT,
            key_hint=KEY_HINT,
            per_key_limits=True,
        ):
            await self.inner(scope, replay_receive, _redacting_send(send, key))

    async def _card(self) -> dict[str, Any]:
        if self._server_card is None:
            tools = await server.list_tools()
            self._server_card = {
                "serverInfo": {"name": "mailsocket", "version": __version__},
                "transport": {"type": "streamable-http", "url": MCP_PATH},
                "authentication": {"required": True, "schemes": ["bearer"]},
                "tools": [
                    json.loads(tool.model_dump_json(by_alias=True, exclude_none=True))
                    for tool in tools
                ],
                "resources": [],
                "prompts": [],
            }
        return self._server_card


def _configure_logging() -> None:
    """Make the header-free access log visible under a bare uvicorn launch.

    uvicorn only configures its own loggers; if nothing else configured
    logging, attach one stderr handler to ``mailsocket_mcp`` (idempotent).
    """
    root = logging.getLogger()
    pkg = logging.getLogger("mailsocket_mcp")
    if root.handlers or pkg.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    pkg.addHandler(handler)
    pkg.setLevel(logging.INFO)


def create_app(environ: dict[str, str] | None = None) -> RemoteMCPApp:
    """Build the ASGI app. Refuses (RuntimeError) if MAILSOCKET_API_KEY is set.

    Used as ``uvicorn --factory mailsocket_mcp.remote:create_app``.
    """
    _configure_logging()
    install_log_redaction()
    return RemoteMCPApp(RemoteConfig(environ))


def main() -> None:
    """Console entry point ``mailsocket-mcp-http``: serve over uvicorn."""
    import sys

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        app = create_app()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    uvicorn.run(
        app,
        host=os.environ.get("MCP_BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8080")),
        workers=1,
        limit_concurrency=64,
        timeout_keep_alive=5,
        access_log=False,  # uvicorn's access log includes the query string
        server_header=False,
    )


if __name__ == "__main__":
    main()
