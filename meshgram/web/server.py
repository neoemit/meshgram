"""The web app's HTTP server (stdlib asyncio, no extra dependencies).

Serves the page (``static/``), a JSON API and a Server-Sent Events stream of
live updates. Plugins add their data to the stream through ``WebServer.events``
(see ``EventHub``); the app registers the control panel's API routes.

Security: with ``web.password`` set, every request needs HTTP Basic auth.
Requests that change something (POST/PUT/PATCH/DELETE) must also come from the
page itself (same origin, JSON body), and are refused outright when the server
listens beyond this machine without a password (see ``WebConfig.allows_changes``).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import parse_qsl, unquote, urlsplit

from ..config import WebConfig
from ..status import StatusRegistry

LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
# Published path -> (file in STATIC_DIR, content type).
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/control.js": ("control.js", "text/javascript; charset=utf-8"),
}
CONFIG_PLACEHOLDER = "/*__MESHGRAM_CONFIG__*/{}"
MAX_REQUEST_HEAD_BYTES = 16 * 1024
MAX_BODY_BYTES = 256 * 1024
SSE_KEEPALIVE_SECONDS = 15.0
SSE_QUEUE_SIZE = 500
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
READ_ONLY_REASON = (
    "The control panel is read-only: set web.password in config.yaml to change settings "
    "from a browser (or listen on 127.0.0.1 only)"
)
# Leaflet comes from unpkg; map tiles may come from any server.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline' https://unpkg.com; img-src * data: blob:; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_STATUS_TEXT = {
    200: "OK",
    201: "Created",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Content Too Large",
    415: "Unsupported Media Type",
    422: "Unprocessable Content",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


class HttpError(Exception):
    def __init__(self, status: int, message: str, details: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


@dataclass(slots=True)
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes = b""
    params: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HttpError(400, f"The request body isn't valid JSON: {exc}") from None


@dataclass(slots=True)
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)


def json_response(data: Any, status: int = 200) -> Response:
    return Response(status, json.dumps(data, separators=(",", ":")).encode("utf-8"))


def no_content() -> Response:
    return Response(204, b"", "text/plain")


Handler = Callable[[Request], Awaitable[Response]]
SnapshotProvider = Callable[[], dict[str, Any]]


@dataclass(slots=True)
class _Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler


def _compile(path: str) -> re.Pattern[str]:
    """``/api/plugins/{name}`` -> a regex with a named group per ``{param}``."""
    parts = re.split(r"\{(\w+)\}", path)
    regex = "".join(re.escape(part) if index % 2 == 0 else f"(?P<{part}>[^/]+)" for index, part in enumerate(parts))
    return re.compile(f"^{regex}$")


class EventHub:
    """Live updates for open pages: a snapshot when they connect, then events.

    The snapshot merges what every provider returns (the app adds connection
    status, the packet_map plugin its nodes and packets). Providers come and go
    with plugins; ``resync`` sends everyone a fresh snapshot after such changes.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._providers: dict[str, SnapshotProvider] = {}

    def add_snapshot_provider(self, key: str, provider: SnapshotProvider) -> None:
        self._providers[key] = provider

    def remove_snapshot_provider(self, key: str) -> None:
        self._providers.pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {"type": "snapshot"}
        for key, provider in list(self._providers.items()):
            try:
                snapshot.update(provider())
            except Exception:
                LOGGER.exception("Web app: snapshot provider %s failed", key)
        return snapshot

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SSE_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A client that can't keep up gets disconnected and re-syncs on reconnect.
                self._subscribers.discard(queue)
                with contextlib.suppress(asyncio.QueueFull):
                    queue.get_nowait()
                    queue.put_nowait({"type": "overflow"})

    def resync(self) -> None:
        if self._subscribers:
            self.publish(self.snapshot())


class WebServer:
    def __init__(self, config: WebConfig, status: Optional[StatusRegistry] = None):
        self.config = config
        self.status = status
        self.events = EventHub()
        self.events.add_snapshot_provider("web", self._snapshot)
        self._routes: list[_Route] = []
        self._server: Optional[asyncio.AbstractServer] = None
        self._static: dict[str, tuple[bytes, str]] = {}
        self._client_tasks: set[asyncio.Task[Any]] = set()

    @property
    def port(self) -> Optional[int]:
        if self._server is None or not self._server.sockets:
            return None
        return self._server.sockets[0].getsockname()[1]

    def route(self, method: str, path: str, handler: Handler) -> None:
        self._routes.append(_Route(method.upper(), _compile(path), handler))

    def control_state(self) -> dict[str, Any]:
        allowed = self.config.allows_changes
        return {"allows_changes": allowed, "read_only_reason": None if allowed else READ_ONLY_REASON}

    def _snapshot(self) -> dict[str, Any]:
        return {
            "connections": self.status.snapshot() if self.status is not None else [],
            "control": self.control_state(),
        }

    def _on_status(self, entry: dict[str, Any]) -> None:
        self.events.publish({"type": "connection", "connection": entry})

    # --- Lifecycle ----------------------------------------------------------------

    async def start(self) -> None:
        client_config = {
            "title": self.config.title,
            "tile_url": self.config.tile_url,
            "tile_attribution": self.config.tile_attribution,
        }
        config_json = json.dumps(client_config).replace("</", "<\\/")
        for path, (name, content_type) in STATIC_FILES.items():
            body = (STATIC_DIR / name).read_text(encoding="utf-8")
            if name == "index.html":
                body = body.replace(CONFIG_PLACEHOLDER, config_json)
            self._static[path] = (body.encode("utf-8"), content_type)

        self._server = await asyncio.start_server(self._handle_client, self.config.host, self.config.port)
        if self.status is not None:
            self.status.add_listener(self._on_status)
        LOGGER.info("Web app: serving on http://%s:%s/", self.config.host, self.port)
        if not self.config.allows_changes:
            LOGGER.warning("Web app: %s", READ_ONLY_REASON)

    async def stop(self) -> None:
        if self.status is not None:
            self.status.remove_listener(self._on_status)
        if self._server is not None:
            self._server.close()
        for task in list(self._client_tasks):
            task.cancel()
        for task in list(self._client_tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._server is not None:
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    # --- Requests ---------------------------------------------------------------------

    def _authorized(self, headers: dict[str, str]) -> bool:
        if not self.config.password:
            return True
        scheme, _, credentials = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            decoded = base64.b64decode(credentials.strip(), validate=True).decode("utf-8")
        except Exception:
            return False
        _, _, password = decoded.partition(":")
        return hmac.compare_digest(password.encode("utf-8"), self.config.password.encode("utf-8"))

    @staticmethod
    def _same_origin(headers: dict[str, str]) -> bool:
        """Whether a request comes from this app's own page (cross-site request forgery guard)."""
        site = headers.get("sec-fetch-site")
        if site and site not in {"same-origin", "none"}:
            return False
        origin = headers.get("origin")
        if not origin:
            return True
        hosts = {headers.get("host", "").lower(), headers.get("x-forwarded-host", "").split(",")[0].strip().lower()}
        return urlsplit(origin).netloc.lower() in hosts - {""}

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        try:
            await self._serve_request(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError, asyncio.LimitOverrunError):
            pass
        except asyncio.CancelledError:
            pass
        except Exception:
            LOGGER.exception("Web app: request failed")
        finally:
            if task is not None:
                self._client_tasks.discard(task)
            with contextlib.suppress(Exception):
                writer.close()

    async def _serve_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
        if len(head) > MAX_REQUEST_HEAD_BYTES:
            await self._send(writer, _error(400, "Request too large"))
            return
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            await self._send(writer, _error(400, "Bad request"))
            return
        method, target, _ = parts
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()

        if not self._authorized(headers):
            await self._send(
                writer,
                _error(401, "Authentication required"),
                extra_headers={"WWW-Authenticate": 'Basic realm="meshgram", charset="UTF-8"'},
            )
            return

        try:
            length = int(headers.get("content-length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            await self._send(writer, _error(413 if length > 0 else 400, "Request body too large" if length > 0 else "Bad Content-Length"))
            return
        body = await asyncio.wait_for(reader.readexactly(length), timeout=10.0) if length else b""

        split = urlsplit(target)
        request = Request(
            method=method.upper(),
            path=unquote(split.path),
            query=dict(parse_qsl(split.query)),
            headers=headers,
            body=body,
        )
        if request.method == "GET" and request.path == "/api/events":
            await self._stream_events(writer)
            return
        response = await self._dispatch(request)
        await self._send(writer, response, include_body=request.method != "HEAD")

    async def _dispatch(self, request: Request) -> Response:
        if request.path in self._static and request.method in {"GET", "HEAD"}:
            body, content_type = self._static[request.path]
            headers = {"Content-Security-Policy": CONTENT_SECURITY_POLICY} if content_type.startswith("text/html") else {}
            return Response(200, body, content_type, headers)
        if request.path == "/healthz" and request.method in {"GET", "HEAD"}:
            return Response(200, b"ok", "text/plain")

        path_matched = False
        for route in self._routes:
            match = route.pattern.match(request.path)
            if match is None:
                continue
            path_matched = True
            if route.method != request.method and not (route.method == "GET" and request.method == "HEAD"):
                continue
            request.params = {key: unquote(value) for key, value in match.groupdict().items()}
            try:
                if request.method in MUTATING_METHODS:
                    self._check_change_allowed(request)
                return await route.handler(request)
            except HttpError as exc:
                return _error(exc.status, exc.message, exc.details)
            except Exception:
                LOGGER.exception("Web app: %s %s failed", request.method, request.path)
                return _error(500, "Something went wrong; see the Meshgram log")
        if path_matched:
            return _error(405, "Method not allowed")
        return _error(404, "Not found")

    def _check_change_allowed(self, request: Request) -> None:
        if not self._same_origin(request.headers):
            raise HttpError(403, "Changes must come from the Meshgram page itself")
        if request.method != "DELETE" or request.body:
            content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type != "application/json":
                raise HttpError(415, "Send changes as application/json")
        if not self.config.allows_changes:
            raise HttpError(403, READ_ONLY_REASON)

    async def _send(
        self,
        writer: asyncio.StreamWriter,
        response: Response,
        include_body: bool = True,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> None:
        headers = {
            "Content-Type": response.content_type,
            "Content-Length": str(len(response.body)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            # OpenStreetMap refuses tile requests without a Referer, so not "no-referrer".
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Connection": "close",
            **response.headers,
            **(extra_headers or {}),
        }
        head = f"HTTP/1.1 {response.status} {_STATUS_TEXT.get(response.status, 'OK')}\r\n"
        head += "".join(f"{name}: {value}\r\n" for name, value in headers.items()) + "\r\n"
        writer.write(head.encode("latin-1") + (response.body if include_body else b""))
        await writer.drain()

    async def _stream_events(self, writer: asyncio.StreamWriter) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-store\r\n"
            b"X-Content-Type-Options: nosniff\r\n"
            b"Connection: close\r\n"
            b"X-Accel-Buffering: no\r\n\r\n"
            b"retry: 3000\n\n"
        )
        queue = self.events.subscribe()
        try:
            await self._send_event(writer, self.events.snapshot())
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    writer.write(b": keepalive\n\n")
                    await writer.drain()
                    continue
                if event.get("type") == "overflow":
                    return
                await self._send_event(writer, event)
        finally:
            self.events.unsubscribe(queue)

    @staticmethod
    async def _send_event(writer: asyncio.StreamWriter, event: dict[str, Any]) -> None:
        writer.write(b"data: " + json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n\n")
        await writer.drain()


def _error(status: int, message: str, details: Any = None) -> Response:
    body: dict[str, Any] = {"error": message}
    if details is not None:
        body["details"] = details
    return json_response(body, status)
