"""The control panel's JSON API: the radio (settings, channels, contacts) and the plugins.

    GET    /api/radio                                 overview: identity, radio settings, battery, stats
    PATCH  /api/radio                                 change settings (see RadioAdmin.update_settings)
    POST   /api/radio/advert                          {"flood": bool}
    POST   /api/radio/sync-clock                      set the radio's clock to this machine's
    POST   /api/radio/reboot
    GET    /api/radio/channels[?refresh=1]            configured channels, which plugins use them
    POST   /api/radio/channels                        {"name", "secret"?, "index"?} -> 201
    PUT    /api/radio/channels/{index}                {"name", "secret"?}
    DELETE /api/radio/channels/{index}
    POST   /api/radio/channels/{index}/messages       {"text"} -> 201
    GET    /api/radio/contacts[?refresh=1]
    DELETE /api/radio/contacts/{public_key}
    POST   /api/radio/contacts/{public_key}/reset-path
    GET    /api/radio/firmware[?board=…&refresh=1]    installed firmware, releases for the radio's board (or
                                                      the one given), whether it can be updated, the update job
    POST   /api/radio/firmware/update                 {"version", "board", "allow_downgrade"?} -> 202 job;
                                                      progress follows as {"type": "firmware", "job"} events
    GET    /api/plugins                               every plugin with its schema and (masked) settings
    PATCH  /api/plugins/{name}                        {"enabled"?, "settings"?}
    DELETE /api/plugins/{name}/overrides              back to config.yaml
    GET    /api/settings                              Meshgram's own settings (config.yaml but the plugins)
                                                      by section, with their schemas (secrets masked)
    PATCH  /api/settings/{section}                    the section's settings -> 200 (used after a restart)
    DELETE /api/settings/{section}/overrides          back to config.yaml (after a restart)
    POST   /api/restart                               restart Meshgram -> 202
    GET    /api/config                                config.yaml with the changes made here in it, what
                                                      changes, and a diff (secrets included)
    GET    /api/config.yaml                           the same file, as a download

Errors are ``{"error": message, "details"?: ...}`` with a fitting status code.
Successful changes are announced to every open page as ``{"type": "control",
"what": ...}`` events so they refresh.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..config import MeshgramSettings
from ..config_export import ConfigExport, ConfigExportError, export_config
from ..core_settings import LEAVES, SCHEMA, CoreSettings, UnknownSectionError
from ..firmware import FirmwareUpdater
from ..plugin import CHANNEL_FORMAT
from ..plugin_manager import PluginManager, UnknownPluginError
from ..radio_admin import RadioAdmin, RadioAdminError
from ..settings_schema import SettingsError
from ..text_utils import channel_message_max_bytes, utf8_len
from ..transport import RadioCommandError
from ..types import MeshPacketRef, SendMeshAction
from .server import HttpError, Request, Response, WebServer, json_response, no_content

LOGGER = logging.getLogger(__name__)

# Tells the control panel when a restart has happened: a new process, a new time.
STARTED_AT = time.time()

SendMesh = Callable[[SendMeshAction], Awaitable[MeshPacketRef]]


def message_max_bytes(settings: MeshgramSettings, payload_limit: int) -> int:
    """The longest channel message (UTF-8 bytes) that goes out in one packet, as the bridge sizes them."""
    return channel_message_max_bytes(settings.chunking, payload_limit)


def channel_usage(plugins: PluginManager, settings: MeshgramSettings) -> dict[int, list[str]]:
    """Which enabled plugins point at which channel slot (settings with ``format: channel``)."""
    usage: dict[int, list[str]] = defaultdict(list)
    for plugin in plugins.catalog():
        if not plugin["enabled"]:
            continue
        properties = (plugin["schema"] or {}).get("properties") or {}
        slots: set[int] = set()
        for key, schema in properties.items():
            value = plugin["settings"].get(key)
            if schema.get("format") == CHANNEL_FORMAT:
                values = [value]
            elif (schema.get("items") or {}).get("format") == CHANNEL_FORMAT:
                # config.yaml may hold a list or a "0,1" string.
                values = value.split(",") if isinstance(value, str) else value if isinstance(value, list) else []
            else:
                continue
            slots.update(slot for slot in map(_slot_number, values) if slot is not None)
        if plugin["name"] == "bridge" and not isinstance(plugin["settings"].get("channel"), int):
            slots.add(settings.meshcore.bridge_channel)
        for slot in slots:
            usage[slot].append(plugin["title"])
    return usage


def _slot_number(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _flag(request: Request, name: str) -> bool:
    return request.query.get(name, "").lower() in {"1", "true", "yes"}


def _index(request: Request) -> int:
    try:
        return int(request.params["index"])
    except ValueError:
        raise HttpError(404, "There's no such channel slot") from None


def _body(request: Request) -> dict[str, Any]:
    body = request.json()
    if not isinstance(body, dict):
        raise HttpError(400, "The request body must be a JSON object")
    return body


def _wrap(handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
    """Turn the domain errors into HTTP errors."""

    async def wrapped(request: Request) -> Response:
        try:
            return await handler(request)
        except RadioAdminError as exc:
            raise HttpError(exc.status, str(exc)) from None
        except RadioCommandError as exc:
            message = str(exc)
            raise HttpError(503 if "isn't connected" in message else 502, message) from None
        except SettingsError as exc:
            details = [{"path": path, "message": message} for path, message in exc.errors]
            raise HttpError(422, "Some settings aren't valid", details) from None
        except UnknownPluginError:
            raise HttpError(404, "There's no such plugin") from None
        except UnknownSectionError:
            raise HttpError(404, "There's no such settings section") from None

    return wrapped


def register_control_api(
    web: WebServer,
    *,
    settings: MeshgramSettings,
    admin: RadioAdmin,
    plugins: PluginManager,
    send_mesh: SendMesh,
    core: Optional[CoreSettings] = None,
    restart: Optional[Callable[[], None]] = None,
    firmware: Optional[FirmwareUpdater] = None,
) -> None:
    def announce(what: str) -> None:
        web.events.publish({"type": "control", "what": what})

    if firmware is not None:
        finished: set[str] = set()

        def firmware_changed(job: dict[str, Any]) -> None:
            web.events.publish({"type": "firmware", "job": job})
            # A finished update changed what the radio reports.
            if job.get("state") != "running" and job.get("id") not in finished:
                finished.add(job.get("id"))
                announce("radio")

        firmware.on_change = firmware_changed
        web.events.add_snapshot_provider("firmware", firmware.snapshot)

    def route(method: str, path: str):
        def decorator(handler: Callable[[Request], Awaitable[Response]]):
            web.route(method, path, _wrap(handler))
            return handler

        return decorator

    # --- Radio -----------------------------------------------------------------------

    @route("GET", "/api/radio")
    async def get_radio(request: Request) -> Response:
        return json_response(await admin.overview())

    @route("PATCH", "/api/radio")
    async def patch_radio(request: Request) -> Response:
        overview = await admin.update_settings(_body(request))
        announce("radio")
        return json_response(overview)

    @route("POST", "/api/radio/advert")
    async def post_advert(request: Request) -> Response:
        flood = _body(request).get("flood", False)
        if not isinstance(flood, bool):
            raise HttpError(400, "flood must be true or false")
        await admin.send_advert(flood)
        return no_content()

    @route("POST", "/api/radio/sync-clock")
    async def post_sync_clock(request: Request) -> Response:
        return json_response({"device_time": await admin.sync_clock()})

    @route("POST", "/api/radio/reboot")
    async def post_reboot(request: Request) -> Response:
        await admin.reboot()
        return no_content()

    # --- Channels ----------------------------------------------------------------------

    def channels_payload() -> dict[str, Any]:
        payload = admin.list_channels(include_secrets=web.config.allows_changes)
        usage = channel_usage(plugins, settings)
        for channel in payload["channels"]:
            channel["used_by"] = usage.get(channel["index"], [])
        payload["connected"] = admin.transport.is_connected
        payload["message_max_bytes"] = message_max_bytes(settings, admin.transport.payload_limit)
        return payload

    @route("GET", "/api/radio/channels")
    async def get_channels(request: Request) -> Response:
        if _flag(request, "refresh") and admin.transport.is_connected:
            await admin.transport.refresh_channels()
        return json_response(channels_payload())

    @route("POST", "/api/radio/channels")
    async def post_channel(request: Request) -> Response:
        body = _body(request)
        channel = await admin.add_channel(body.get("name"), body.get("secret"), body.get("index"))
        announce("channels")
        return json_response(channel, 201)

    @route("PUT", "/api/radio/channels/{index}")
    async def put_channel(request: Request) -> Response:
        body = _body(request)
        channel = await admin.update_channel(_index(request), body.get("name"), body.get("secret"))
        announce("channels")
        return json_response(channel)

    @route("DELETE", "/api/radio/channels/{index}")
    async def delete_channel(request: Request) -> Response:
        await admin.remove_channel(_index(request))
        announce("channels")
        return no_content()

    @route("POST", "/api/radio/channels/{index}/messages")
    async def post_message(request: Request) -> Response:
        index = _index(request)
        text = _body(request).get("text")
        if not isinstance(text, str) or not text.strip():
            raise HttpError(400, "Type a message first")
        text = text.strip()
        limit = message_max_bytes(settings, admin.transport.payload_limit)
        if utf8_len(text) > limit:
            raise HttpError(400, f"The message is too long: at most {limit} bytes fit in one packet")
        if not any(channel["index"] == index for channel in admin.list_channels(include_secrets=False)["channels"]):
            raise HttpError(404, f"Slot {index} has no channel")
        if not admin.transport.is_connected:
            raise RadioCommandError("The radio isn't connected")
        packet_id = await send_mesh(SendMeshAction(text=text, channel_index=index))
        LOGGER.info("Message sent to channel slot %s from the web app", index)
        return json_response({"id": packet_id}, 201)

    # --- Contacts --------------------------------------------------------------------------

    @route("GET", "/api/radio/contacts")
    async def get_contacts(request: Request) -> Response:
        if _flag(request, "refresh") and admin.transport.is_connected:
            await admin.transport.refresh_contacts()
        return json_response({"connected": admin.transport.is_connected, "contacts": admin.list_contacts()})

    @route("DELETE", "/api/radio/contacts/{public_key}")
    async def delete_contact(request: Request) -> Response:
        await admin.remove_contact(request.params["public_key"])
        announce("contacts")
        return no_content()

    @route("POST", "/api/radio/contacts/{public_key}/reset-path")
    async def post_reset_path(request: Request) -> Response:
        await admin.reset_contact_path(request.params["public_key"])
        announce("contacts")
        return no_content()

    # --- Firmware ----------------------------------------------------------------------------

    if firmware is not None:

        @route("GET", "/api/radio/firmware")
        async def get_firmware(request: Request) -> Response:
            board = request.query.get("board") or None
            return json_response(await firmware.describe(board=board, refresh=_flag(request, "refresh")))

        @route("POST", "/api/radio/firmware/update")
        async def post_firmware_update(request: Request) -> Response:
            body = _body(request)
            unknown = set(body) - {"version", "board", "allow_downgrade"}
            if unknown:
                raise HttpError(400, f"Unknown fields: {', '.join(sorted(unknown))}")
            job = await firmware.start(
                version=body.get("version"),
                board=body.get("board"),
                allow_downgrade=body.get("allow_downgrade", False),
            )
            return json_response(job, 202)

    # --- Plugins ------------------------------------------------------------------------------

    @route("GET", "/api/plugins")
    async def get_plugins(request: Request) -> Response:
        return json_response({"plugins": plugins.catalog()})

    @route("PATCH", "/api/plugins/{name}")
    async def patch_plugin(request: Request) -> Response:
        body = _body(request)
        unknown = set(body) - {"enabled", "settings"}
        if unknown:
            raise HttpError(400, f"Unknown fields: {', '.join(sorted(unknown))}")
        enabled = body.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise HttpError(400, "enabled must be true or false")
        try:
            plugin = await plugins.update(request.params["name"], enabled=enabled, settings=body.get("settings"))
        except OSError as exc:
            raise HttpError(500, f"Couldn't save the change: {exc}") from None
        announce("plugins")
        return json_response(plugin)

    @route("DELETE", "/api/plugins/{name}/overrides")
    async def delete_overrides(request: Request) -> Response:
        try:
            plugin = await plugins.reset(request.params["name"])
        except OSError as exc:
            raise HttpError(500, f"Couldn't save the change: {exc}") from None
        announce("plugins")
        return json_response(plugin)

    # --- Meshgram's own settings ------------------------------------------------------------------

    def settings_payload(described: dict[str, Any]) -> dict[str, Any]:
        return {**described, "can_restart": restart is not None, "started_at": STARTED_AT}

    if core is not None:

        @route("GET", "/api/settings")
        async def get_settings(request: Request) -> Response:
            return json_response(settings_payload(core.describe()))

        @route("PATCH", "/api/settings/{section}")
        async def patch_settings(request: Request) -> Response:
            try:
                described = await core.update(request.params["section"], _body(request))
            except OSError as exc:
                raise HttpError(500, f"Couldn't save the change: {exc}") from None
            announce("settings")
            return json_response(settings_payload(described))

        @route("DELETE", "/api/settings/{section}/overrides")
        async def delete_settings_overrides(request: Request) -> Response:
            try:
                described = await core.reset(request.params["section"])
            except OSError as exc:
                raise HttpError(500, f"Couldn't save the change: {exc}") from None
            announce("settings")
            return json_response(settings_payload(described))

    if restart is not None:

        @route("POST", "/api/restart")
        async def post_restart(request: Request) -> Response:
            if firmware is not None and firmware.busy:
                raise HttpError(409, "The radio's firmware is being updated; restart when it's done")
            restart()
            return json_response({"restarting": True}, 202)

    # --- Config file ----------------------------------------------------------------------------

    async def exported_config(request: Request) -> ConfigExport:
        web.check_secret_access(request)
        path = settings.config_path
        try:
            text = await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
        except OSError as exc:
            raise HttpError(409, f"Can't read {path}: {exc.strerror or exc}") from None
        try:
            settings_overrides = core.overrides() if core is not None else {}
            return await asyncio.to_thread(export_config, text, plugins.overrides(), path=path, settings=settings_overrides)
        except ConfigExportError as exc:
            raise HttpError(409, str(exc)) from None

    def plugin_title(name: str) -> str:
        try:
            return plugins.describe(name)["title"]
        except UnknownPluginError:
            return name  # Added to config.yaml since Meshgram started.

    def setting_title(path: str) -> str:
        """ "Radio › Connection › Serial device" for meshcore.connection.serial_device."""
        titles, schema = [], SCHEMA
        for key in path.split("."):
            schema = (schema.get("properties") or {}).get(key) or {}
            titles.append(schema.get("title") or key)
        return " › ".join(titles)

    @route("GET", "/api/config")
    async def get_config(request: Request) -> Response:
        export = await exported_config(request)
        return json_response({
            "path": str(Path(settings.config_path).absolute()),
            "yaml": export.text,
            "diff": export.diff,
            "changes": [
                {"name": change.name, "title": plugin_title(change.name), "enabled": change.enabled, "settings": change.settings}
                for change in export.changes
            ],
            "settings_changes": [
                {"path": path, "title": setting_title(path), "secret": bool(LEAVES[path].get("writeOnly"))}
                for path in export.settings_changes
            ],
            # Plugins with changes saved in the data directory (config.yaml may have them already).
            "saved": [{"name": name, "title": plugin_title(name)} for name in plugins.overrides()],
            "saved_settings": [setting_title(path) for path in (core.overrides() if core is not None else {})],
            "problems": export.problems,
        })

    @route("GET", "/api/config.yaml")
    async def get_config_yaml(request: Request) -> Response:
        export = await exported_config(request)
        return Response(
            200,
            export.text.encode("utf-8"),
            "application/yaml; charset=utf-8",
            {"Content-Disposition": 'attachment; filename="config.yaml"'},
        )
