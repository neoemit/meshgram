"""The check_in plugin's routes on the web app, added while the plugin runs.

    GET    /api/plugins/check_in/leaderboard[?limit=N]          standings for other sites (see below)
    GET    /api/plugins/check_in/participants                   everyone with their weeks (control panel)
    POST   /api/plugins/check_in/participants                   {"name", "node_id"?} -> 201
    PATCH  /api/plugins/check_in/participants/{id}              {"name"?, "node_id"?}; a key another
                                                                participant has merges the two
    DELETE /api/plugins/check_in/participants/{id}
    GET    /api/plugins/check_in/participants/{id}/check-ins    the check-ins on record, newest first
    PUT    /api/plugins/check_in/participants/{id}/weeks/{week} check in for that week (any date in it)
    DELETE /api/plugins/check_in/participants/{id}/weeks/{week} remove that week's check-ins
    PUT    /api/plugins/check_in/participants/{id}/streak       {"streak", "week"?}: that many weeks up to
                                                                ``week`` (default: the latest check-in week)
    POST   /api/plugins/check_in/import                         {"records": [...], "as_of"?, "mode"?:
                                                                "merge"|"replace", "dry_run"?}
    GET    /api/plugins/check_in/export?format=csv|json         everyone, in the import format
    GET    /api/plugins/check_in/nodes?q=                       known nodes, to link participants to

The leaderboard is a public route: it takes ``Authorization: Bearer <api_token>``
(the plugin's setting) as well as the web app's password, so a leaderboard site
can read it without the password. The other routes are the control panel's: the
web app's password and its checks on changes apply.
"""
from __future__ import annotations

import csv
import hmac
import io
import json
from datetime import date
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from meshgram.plugins.check_in_scoring import parse_week
from meshgram.plugins.check_in_store import CheckInError, CheckInStoreError
from meshgram.web.server import HttpError, Request, Response, WebServer, json_response, no_content

if TYPE_CHECKING:
    from meshgram.plugins.check_in import CheckInPlugin

PREFIX = "/api/plugins/check_in"
EXPORT_FIELDS = ("node_id", "name", "streak", "total", "best_streak", "last_check_in")

Handler = Callable[[Request], Awaitable[Response]]


def _wrap(handler: Handler) -> Handler:
    async def wrapped(request: Request) -> Response:
        try:
            return await handler(request)
        except CheckInError as exc:
            raise HttpError(exc.status, str(exc)) from None
        except CheckInStoreError as exc:
            raise HttpError(503, f"The check-in database can't be used: {exc}") from None

    return wrapped


def _body(request: Request) -> dict[str, Any]:
    body = request.json()
    if not isinstance(body, dict):
        raise HttpError(400, "The request body must be a JSON object")
    return body


def _participant_id(request: Request) -> int:
    try:
        return int(request.params["id"])
    except ValueError:
        raise HttpError(404, "There's no such participant") from None


def _week(value: Any, what: str = "week") -> date:
    try:
        return parse_week(str(value))
    except ValueError:
        raise HttpError(400, f"The {what} must be a date (YYYY-MM-DD)") from None


def _name(value: Any) -> str:
    name = str(value or "").strip() if isinstance(value, (str, type(None))) else ""
    if not name or len(name) > 64:
        raise HttpError(400, "The name must be 1 to 64 characters")
    return name


def _node_id(value: Any) -> Any:
    if value is None or value == "":
        return None
    key = str(value).strip().upper()
    if len(key) != 64 or any(char not in "0123456789ABCDEF" for char in key):
        raise HttpError(400, "The node key must be a public key: 64 hex characters")
    return key


def _bearer_matches(request: Request, token: str) -> bool:
    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(credentials.strip().encode("utf-8"), token.encode("utf-8"))


def register_check_in_routes(web: WebServer, plugin: "CheckInPlugin") -> list[Any]:
    """Add the routes; returns them for ``WebServer.remove_route`` when the plugin stops."""
    routes: list[Any] = []

    def route(method: str, path: str, *, public: bool = False):
        def decorator(handler: Handler) -> Handler:
            routes.append(web.route(method, f"{PREFIX}{path}", _wrap(handler), public=public))
            return handler

        return decorator

    def may_read_leaderboard(request: Request) -> bool:
        token = plugin.config.api_token
        if token and _bearer_matches(request, token):
            return True
        if token and not web.config.password:
            # Without a web password anyone may use the web app, but the token was asked for.
            return False
        return web.is_authorized(request.headers)

    @route("GET", "/leaderboard", public=True)
    async def leaderboard(request: Request) -> Response:
        if not may_read_leaderboard(request):
            body = json.dumps({"error": "A valid API token is required"}).encode("utf-8")
            return Response(401, body, headers={"WWW-Authenticate": 'Bearer realm="meshgram"'})
        try:
            limit = max(0, int(request.query.get("limit") or 0)) or None
        except ValueError:
            raise HttpError(400, "limit must be a whole number") from None
        return json_response(await plugin.leaderboard(limit))

    @route("GET", "/participants")
    async def participants(request: Request) -> Response:
        return json_response(await plugin.participants())

    @route("POST", "/participants")
    async def create_participant(request: Request) -> Response:
        body = _body(request)
        participant_id = await plugin.create_participant(_name(body.get("name")), _node_id(body.get("node_id")))
        return json_response({"id": participant_id}, 201)

    @route("PATCH", "/participants/{id}")
    async def update_participant(request: Request) -> Response:
        body = _body(request)
        unknown = set(body) - {"name", "node_id"}
        if unknown:
            raise HttpError(400, f"Unknown fields: {', '.join(sorted(unknown))}")
        changes: dict[str, Any] = {}
        if "name" in body:
            changes["name"] = _name(body["name"])
        if "node_id" in body:
            changes["node_id"] = _node_id(body["node_id"])
        participant_id = await plugin.update_participant(_participant_id(request), changes)
        return json_response({"id": participant_id})

    @route("DELETE", "/participants/{id}")
    async def delete_participant(request: Request) -> Response:
        await plugin.delete_participant(_participant_id(request))
        return no_content()

    @route("GET", "/participants/{id}/check-ins")
    async def check_ins(request: Request) -> Response:
        return json_response({"check_ins": await plugin.check_ins(_participant_id(request))})

    @route("PUT", "/participants/{id}/weeks/{week}")
    async def add_week(request: Request) -> Response:
        await plugin.set_week(_participant_id(request), _week(request.params["week"]), True)
        return no_content()

    @route("DELETE", "/participants/{id}/weeks/{week}")
    async def remove_week(request: Request) -> Response:
        await plugin.set_week(_participant_id(request), _week(request.params["week"]), False)
        return no_content()

    @route("PUT", "/participants/{id}/streak")
    async def set_streak(request: Request) -> Response:
        body = _body(request)
        streak = body.get("streak")
        if isinstance(streak, bool) or not isinstance(streak, int) or not 0 <= streak <= 1000:
            raise HttpError(400, "The streak must be a whole number from 0 to 1000")
        week = _week(body["week"]) if body.get("week") else None
        await plugin.set_streak(_participant_id(request), streak, week)
        return no_content()

    @route("POST", "/import")
    async def import_summary(request: Request) -> Response:
        body = _body(request)
        records = body.get("records")
        if not isinstance(records, list):
            raise HttpError(400, "records must be a list")
        mode = body.get("mode") or "merge"
        if mode not in {"merge", "replace"}:
            raise HttpError(400, "mode must be merge or replace")
        as_of = _week(body["as_of"], "as_of date") if body.get("as_of") else None
        result = await plugin.import_summary(records, as_of=as_of, replace=mode == "replace", dry_run=bool(body.get("dry_run")))
        return json_response(result)

    @route("GET", "/export")
    async def export(request: Request) -> Response:
        rows = await plugin.export_rows()
        if (request.query.get("format") or "json").lower() == "csv":
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=EXPORT_FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            body, content_type, extension = buffer.getvalue().encode("utf-8"), "text/csv; charset=utf-8", "csv"
        else:
            body = json.dumps({"records": rows}, indent=2, ensure_ascii=False).encode("utf-8")
            content_type, extension = "application/json", "json"
        disposition = f'attachment; filename="check-ins-{date.today().isoformat()}.{extension}"'
        return Response(200, body, content_type, {"Content-Disposition": disposition})

    @route("GET", "/nodes")
    async def nodes(request: Request) -> Response:
        return json_response({"nodes": await plugin.search_nodes(request.query.get("q", ""))})

    return routes
