"""Reading and changing the radio: settings, channels and contacts (the control panel's radio API).

Everything here goes through ``MeshCoreTransport.command``, i.e. the
``meshcore`` library's commands, and only checks and normalizes values first.
The radio keeps what it's told across reboots; nothing is saved by Meshgram.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import secrets
import time
from typing import Any, Optional

from .meshcore_packets import NODE_TYPE_NAMES
from .transport import MeshCoreTransport, RadioCommandError

LOGGER = logging.getLogger(__name__)

# The well-known key of the "Public" channel every MeshCore radio starts with.
PUBLIC_CHANNEL_NAME = "Public"
PUBLIC_CHANNEL_SECRET = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")
# Names live in a 32-byte, NUL-terminated field.
MAX_NAME_BYTES = 31
# LoRa bandwidths (kHz) the radios accept.
LORA_BANDWIDTHS = (7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125.0, 250.0, 500.0)
# Telemetry sharing: who may request this node's base/location/environment telemetry.
TELEMETRY_MODES = {0: "nobody", 1: "contacts allowed to", 2: "everyone"}
REBOOT_DISCONNECT_DELAY_SECONDS = 1.0


class RadioAdminError(ValueError):
    """A request the radio can't carry out as asked (bad value, unknown slot, ...)."""

    status = 400


class RadioConflictError(RadioAdminError):
    status = 409


class RadioNotFoundError(RadioAdminError):
    status = 404


def hashtag_secret(name: str) -> bytes:
    """Hashtag channels ("#name") are keyed by the first 16 bytes of SHA-256 of their name."""
    return hashlib.sha256(name.encode("utf-8")).digest()[:16]


def channel_kind(name: str, secret: bytes) -> str:
    if not name or not any(secret):
        return "empty"
    if secret == PUBLIC_CHANNEL_SECRET:
        return "public"
    if name.startswith("#") and secret == hashtag_secret(name):
        return "hashtag"
    return "private"


def _check_name(name: Any, what: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise RadioAdminError(f"{what} must not be empty")
    name = name.strip()
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise RadioAdminError(f"{what} must be at most {MAX_NAME_BYTES} bytes")
    if any(ord(char) < 32 for char in name):
        raise RadioAdminError(f"{what} must not contain control characters")
    return name


def _parse_secret(value: Any) -> bytes:
    text = "".join(str(value or "").split()).lower()
    try:
        secret = bytes.fromhex(text)
    except ValueError:
        secret = b""
    if len(secret) != 16:
        raise RadioAdminError("The channel key must be 32 hex characters (16 bytes)")
    if not any(secret):
        raise RadioAdminError("The channel key must not be all zeros")
    return secret


def _number(value: Any, what: str, low: float, high: float, *, integer: bool = False) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RadioAdminError(f"{what} must be a number")
    if integer and value != int(value):
        raise RadioAdminError(f"{what} must be a whole number")
    if not low <= value <= high:
        raise RadioAdminError(f"{what} must be between {low:g} and {high:g}")
    return int(value) if integer else float(value)


def _flag(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise RadioAdminError(f"{what} must be true or false")
    return value


class RadioAdmin:
    def __init__(self, transport: MeshCoreTransport):
        self.transport = transport

    def _require_connection(self) -> None:
        if not self.transport.is_connected:
            raise RadioCommandError("The radio isn't connected")

    async def _optional(self, name: str, *args: Any) -> Optional[Any]:
        """A read the firmware may not support: its payload, or None."""
        try:
            return await self.transport.command(name, *args)
        except RadioCommandError as exc:
            LOGGER.debug("Radio %s unavailable: %s", name, exc)
            return None

    # --- Overview and settings --------------------------------------------------------

    async def overview(self) -> dict[str, Any]:
        if not self.transport.is_connected:
            return {"connected": False}

        info = self.transport.device_self_info
        device = self.transport.device_info
        battery = await self._optional("get_bat")
        stats = {
            "core": await self._optional("get_stats_core"),
            "radio": await self._optional("get_stats_radio"),
            "packets": await self._optional("get_stats_packets"),
        }
        tuning = await self._optional("get_tuning")
        clock = await self._optional("get_time")
        device_time = clock.get("time") if isinstance(clock, dict) else None

        return {
            "connected": True,
            "identity": {
                "name": info.get("name"),
                "public_key": info.get("public_key"),
                "type": NODE_TYPE_NAMES.get(info.get("adv_type"), "unknown"),
                "lat": info.get("adv_lat"),
                "lon": info.get("adv_lon"),
            },
            "device": {
                "model": device.get("model"),
                "firmware": device.get("ver"),
                "build": device.get("fw_build"),
                "max_contacts": device.get("max_contacts"),
                "max_channels": self.transport.max_channels,
                "repeat": device.get("repeat"),
            },
            "radio": {
                "freq": info.get("radio_freq"),
                "bw": info.get("radio_bw"),
                "sf": info.get("radio_sf"),
                "cr": info.get("radio_cr"),
                "tx_power": info.get("tx_power"),
                "max_tx_power": info.get("max_tx_power"),
            },
            "settings": {
                "adv_loc_policy": info.get("adv_loc_policy"),
                "manual_add_contacts": info.get("manual_add_contacts"),
                "multi_acks": info.get("multi_acks"),
                "telemetry_mode_base": info.get("telemetry_mode_base"),
                "telemetry_mode_loc": info.get("telemetry_mode_loc"),
                "telemetry_mode_env": info.get("telemetry_mode_env"),
                # Only firmware that reports it can change it.
                "path_hash_mode": device.get("path_hash_mode"),
            },
            # The firmware exchanges these in thousandths.
            "tuning": (
                {"rx_delay": tuning["rx_delay"] / 1000, "airtime_factor": tuning["airtime_factor"] / 1000}
                if isinstance(tuning, dict) and {"rx_delay", "airtime_factor"} <= tuning.keys()
                else None
            ),
            "battery": battery if isinstance(battery, dict) else None,
            "stats": {key: value for key, value in stats.items() if isinstance(value, dict)},
            "clock": (
                {"device_time": device_time, "drift_seconds": int(device_time - time.time())}
                if isinstance(device_time, (int, float))
                else None
            ),
        }

    async def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Apply the given settings (all are checked before any is sent); returns the new overview.

        Keys: ``name``, ``lat`` + ``lon``, ``adv_loc_policy`` (0/1), ``tx_power``,
        ``radio`` (``freq`` MHz, ``bw`` kHz, ``sf``, ``cr``), ``manual_add_contacts``,
        ``multi_acks`` (0/1), ``telemetry_mode_base``/``_loc``/``_env`` (0-2),
        ``path_hash_mode`` (0-2), ``tuning`` (``rx_delay``, ``airtime_factor``).
        """
        if not isinstance(changes, dict) or not changes:
            raise RadioAdminError("Nothing to change")
        self._require_connection()
        info = self.transport.device_self_info
        steps: list[tuple[str, str, tuple[Any, ...]]] = []
        unknown = set(changes) - {
            "name", "lat", "lon", "adv_loc_policy", "tx_power", "radio", "manual_add_contacts",
            "multi_acks", "telemetry_mode_base", "telemetry_mode_loc", "telemetry_mode_env",
            "path_hash_mode", "tuning",
        }  # fmt: skip
        if unknown:
            raise RadioAdminError(f"Unknown settings: {', '.join(sorted(unknown))}")

        if "name" in changes:
            steps.append(("name", "set_name", (_check_name(changes["name"], "The name"),)))
        if "lat" in changes or "lon" in changes:
            if "lat" not in changes or "lon" not in changes:
                raise RadioAdminError("Set the latitude and longitude together")
            lat = _number(changes["lat"], "The latitude", -90, 90)
            lon = _number(changes["lon"], "The longitude", -180, 180)
            steps.append(("position", "set_coords", (lat, lon)))
        if "adv_loc_policy" in changes:
            policy = _number(changes["adv_loc_policy"], "Location sharing", 0, 1, integer=True)
            steps.append(("location sharing", "set_advert_loc_policy", (policy,)))
        if "tx_power" in changes:
            max_power = info.get("max_tx_power") if isinstance(info.get("max_tx_power"), int) else 30
            steps.append(("TX power", "set_tx_power", (_number(changes["tx_power"], "The TX power", 1, max_power, integer=True),)))
        if "radio" in changes:
            radio = changes["radio"]
            if not isinstance(radio, dict) or set(radio) != {"freq", "bw", "sf", "cr"}:
                raise RadioAdminError("Radio settings need freq, bw, sf and cr")
            bw = _number(radio["bw"], "The bandwidth", 7.8, 500)
            if not any(abs(bw - allowed) < 0.05 for allowed in LORA_BANDWIDTHS):
                raise RadioAdminError(f"The bandwidth must be one of {', '.join(f'{b:g}' for b in LORA_BANDWIDTHS)} kHz")
            steps.append(
                (
                    "radio parameters",
                    "set_radio",
                    (
                        _number(radio["freq"], "The frequency", 137, 2500),
                        bw,
                        _number(radio["sf"], "The spreading factor", 5, 12, integer=True),
                        _number(radio["cr"], "The coding rate", 5, 8, integer=True),
                    ),
                )
            )
        if "manual_add_contacts" in changes:
            steps.append(("contact auto-add", "set_manual_add_contacts", (_flag(changes["manual_add_contacts"], "manual_add_contacts"),)))
        if "multi_acks" in changes:
            steps.append(("extra ACKs", "set_multi_acks", (_number(changes["multi_acks"], "Extra ACKs", 0, 1, integer=True),)))
        for part in ("base", "loc", "env"):
            key = f"telemetry_mode_{part}"
            if key in changes:
                steps.append((f"{part} telemetry sharing", f"set_{key}", (_number(changes[key], "Telemetry sharing", 0, 2, integer=True),)))
        if "path_hash_mode" in changes:
            if "path_hash_mode" not in self.transport.device_info:
                raise RadioAdminError("This firmware doesn't support changing the path hash size")
            steps.append(("path hash size", "set_path_hash_mode", (_number(changes["path_hash_mode"], "The path hash mode", 0, 2, integer=True),)))
        if "tuning" in changes:
            tuning = changes["tuning"]
            if not isinstance(tuning, dict) or set(tuning) != {"rx_delay", "airtime_factor"}:
                raise RadioAdminError("Tuning needs rx_delay and airtime_factor")
            rx_delay = _number(tuning["rx_delay"], "The RX delay", 0, 1000)
            airtime_factor = _number(tuning["airtime_factor"], "The airtime factor", 0, 100)
            steps.append(("tuning", "set_tuning", (round(rx_delay * 1000), round(airtime_factor * 1000))))

        applied: list[str] = []
        for label, command, args in steps:
            try:
                await self.transport.command(command, *args)
            except RadioCommandError as exc:
                done = f" ({', '.join(applied)} changed)" if applied else ""
                raise RadioCommandError(f"Couldn't change the {label}: {exc}{done}") from exc
            applied.append(label)
        LOGGER.info("Radio settings changed from the web app: %s", ", ".join(applied))
        await self.transport.refresh_self_info()
        return await self.overview()

    async def send_advert(self, flood: bool) -> None:
        self._require_connection()
        await self.transport.command("send_advert", flood=bool(flood))
        LOGGER.info("Sent a %s advert from the web app", "flood" if flood else "zero-hop")

    async def sync_clock(self) -> int:
        self._require_connection()
        now = int(time.time())
        await self.transport.command("set_time", now)
        LOGGER.info("Radio clock set from the web app")
        return now

    async def reboot(self) -> None:
        self._require_connection()
        await self.transport.command("reboot")
        LOGGER.warning("Radio rebooting (requested from the web app)")
        # The link drops while it restarts; reconnect rather than wait to notice.
        loop = asyncio.get_running_loop()
        loop.call_later(REBOOT_DISCONNECT_DELAY_SECONDS, self.transport.invalidate_connection)

    # --- Channels -------------------------------------------------------------------------

    def list_channels(self, *, include_secrets: bool) -> dict[str, Any]:
        channels = []
        for slot in self.transport.channel_slots:
            kind = channel_kind(slot["name"], slot["secret"])
            if kind == "empty":
                continue
            channel = {"index": slot["index"], "name": slot["name"], "hash": slot["hash"], "kind": kind}
            if include_secrets:
                channel["secret"] = slot["secret"].hex()
            channels.append(channel)
        return {"max_channels": self.transport.max_channels, "channels": channels}

    def _slot(self, index: Any) -> Optional[dict[str, Any]]:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < self.transport.max_channels:
            raise RadioNotFoundError(f"There's no channel slot {index}")
        return next((slot for slot in self.transport.channel_slots if slot["index"] == index), None)

    def _resolve_channel(self, name: Any, secret: Any, current: Optional[bytes] = None) -> tuple[str, bytes]:
        name = _check_name(name, "The channel name")
        if name.startswith("#"):
            if len(name) < 2 or any(char.isspace() for char in name):
                raise RadioAdminError("A hashtag channel name is # and a word, like #local")
            if secret not in (None, ""):
                raise RadioAdminError("Hashtag channels get their key from their name; leave the key empty")
            return name, hashtag_secret(name)
        if secret not in (None, ""):
            return name, _parse_secret(secret)
        if current is not None:
            return name, current
        if name.casefold() == PUBLIC_CHANNEL_NAME.casefold():
            return PUBLIC_CHANNEL_NAME, PUBLIC_CHANNEL_SECRET
        # A new private channel: a fresh random key to share with its members.
        return name, secrets.token_bytes(16)

    def _check_duplicate(self, secret: bytes, except_index: Optional[int] = None) -> None:
        for slot in self.transport.channel_slots:
            if slot["index"] != except_index and slot["name"] and slot["secret"] == secret:
                raise RadioConflictError(f"That channel is already on slot {slot['index']} ({slot['name']})")

    async def _write_channel(self, index: int, name: str, secret: bytes) -> dict[str, Any]:
        await self.transport.command("set_channel", index, name, secret)
        await self.transport.refresh_channels()
        slot = self._slot(index)
        if slot is None or slot["secret"] != secret:
            raise RadioCommandError("The radio didn't keep the channel")
        return {"index": index, "name": slot["name"], "hash": slot["hash"], "kind": channel_kind(slot["name"], secret)}

    async def add_channel(self, name: Any, secret: Any = None, index: Any = None) -> dict[str, Any]:
        """Add a channel: "#name" (key from the name), "Public", or a private one (given or new random key)."""
        self._require_connection()
        name, key = self._resolve_channel(name, secret)
        self._check_duplicate(key)
        if index is None:
            used = {slot["index"] for slot in self.transport.channel_slots if channel_kind(slot["name"], slot["secret"]) != "empty"}
            free = [slot for slot in range(self.transport.max_channels) if slot not in used]
            if not free:
                raise RadioConflictError(f"All {self.transport.max_channels} channel slots are in use; remove a channel first")
            index = free[0]
        else:
            slot = self._slot(index)
            if slot is not None and channel_kind(slot["name"], slot["secret"]) != "empty":
                raise RadioConflictError(f"Slot {index} already has {slot['name']}")
        channel = await self._write_channel(index, name, key)
        LOGGER.info("Channel %s added on slot %s from the web app", name, index)
        return channel

    async def update_channel(self, index: Any, name: Any, secret: Any = None) -> dict[str, Any]:
        """Rename a channel, or change its key (a hashtag name always sets the key from the name)."""
        self._require_connection()
        slot = self._slot(index)
        if slot is None or channel_kind(slot["name"], slot["secret"]) == "empty":
            raise RadioNotFoundError(f"Slot {index} has no channel")
        name, key = self._resolve_channel(name, secret, current=slot["secret"])
        self._check_duplicate(key, except_index=index)
        channel = await self._write_channel(index, name, key)
        LOGGER.info("Channel on slot %s changed from the web app (now %s)", index, name)
        return channel

    async def remove_channel(self, index: Any) -> None:
        self._require_connection()
        slot = self._slot(index)
        if slot is None or channel_kind(slot["name"], slot["secret"]) == "empty":
            raise RadioNotFoundError(f"Slot {index} has no channel")
        await self.transport.command("set_channel", index, "", bytes(16))
        await self.transport.refresh_channels()
        LOGGER.info("Channel %s removed from slot %s from the web app", slot["name"], index)

    # --- Contacts ----------------------------------------------------------------------------

    def list_contacts(self) -> list[dict[str, Any]]:
        contacts = []
        for key, contact in self.transport.contacts.items():
            public_key = str(contact.get("public_key") or key).lower()
            path_len = contact.get("out_path_len")
            contacts.append(
                {
                    "public_key": public_key,
                    "name": str(contact.get("adv_name") or "").strip() or None,
                    "type": NODE_TYPE_NAMES.get(contact.get("type"), "unknown"),
                    "last_advert": contact.get("last_advert") or None,
                    "last_modified": contact.get("lastmod") or None,
                    # -1: no known route yet, messages are flooded.
                    "path_len": path_len if isinstance(path_len, int) else None,
                    "lat": contact.get("adv_lat"),
                    "lon": contact.get("adv_lon"),
                }
            )
        contacts.sort(key=lambda contact: -(contact["last_advert"] or 0))
        return contacts

    def _contact_key(self, public_key: Any) -> str:
        key = str(public_key or "").strip().lower()
        for known in self.transport.contacts:
            if known.lower() == key:
                return known
        raise RadioNotFoundError("The radio has no such contact")

    async def remove_contact(self, public_key: Any) -> None:
        self._require_connection()
        key = self._contact_key(public_key)
        await self.transport.command("remove_contact", key)
        await self.transport.refresh_contacts()
        LOGGER.info("Contact %s removed from the web app", key[:12])

    async def reset_contact_path(self, public_key: Any) -> None:
        self._require_connection()
        key = self._contact_key(public_key)
        await self.transport.command("reset_path", key)
        await self.transport.refresh_contacts()
        LOGGER.info("Route to contact %s reset from the web app", key[:12])
