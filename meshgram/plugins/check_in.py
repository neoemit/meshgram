"""Weekly check-ins: nodes post a keyword ("#MeshCoreMonday") on a channel and get counted.

A channel message containing the keyword (any case, anywhere in the text) on a
check-in day is a check-in, whether this radio heard it or another MeshMapper
observer did (observers' packets come through the transport, with their bytes
only from MeshMapper's subscriber account: the free live feed has no text). The
radio answers on the channel with the sender's rank this week, streak and the
number of participants so far; asking again gives the same rank with the current
count.

Channel messages carry the sender's name, not its key, so a name is matched to
the one node that advertises it (adverts the radio and observers hear, and the
radio's contacts). A name no node (or more than one) has is counted under the
name and linked to the node once its advert is heard; see ``check_in_store``.

Everything is kept in an SQLite database in the data directory. The control
panel manages it (``static/check_in.js``) through the routes in
``check_in_api``, which also serve the leaderboard to other sites.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import time
import zoneinfo
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, Optional

from meshgram.config import _as_bool, data_dir
from meshgram.meshcore_packets import (
    PAYLOAD_TYPE_ADVERT,
    PAYLOAD_TYPE_GRP_TXT,
    decode_advert,
    decrypt_group_text,
    parse_packet,
)
from meshgram.plugin import CHANNEL_FORMAT, BasePlugin
from meshgram.plugins.check_in_api import register_check_in_routes
from meshgram.plugins.check_in_scoring import (
    WEEKDAY_NAMES,
    Standing,
    check_in_window,
    is_check_in_day,
    local_moment,
    parse_week,
    standings,
    week_start,
)
from meshgram.plugins.check_in_store import (
    SOURCE_OBSERVER,
    SOURCE_RADIO,
    SOURCES,
    BackupCheckIn,
    BackupParticipant,
    CheckInError,
    CheckInRecord,
    CheckInStore,
    NodeSighting,
    SummaryRecord,
)
from meshgram.radio_admin import channel_kind
from meshgram.text_utils import channel_message_max_bytes, split_text_by_bytes, utf8_len
from meshgram.types import MeshTextEvent, PluginAction, PluginContext, SendMeshAction

LOGGER = logging.getLogger(__name__)

DEFAULT_KEYWORD = "#MeshCoreMonday"
DEFAULT_TITLE = "MeshCore Monday"
DEFAULT_TEMPLATE = (
    "📟 {title}\n"
    "🏅 {name} is #{rank} to check in this week!\n"
    "🔥 Streak: {streak_weeks}\n"
    "👥 {participants_label} so far"
)
DEFAULT_DB_FILE = "check_in.sqlite3"
DEFAULT_TIMEZONE = "UTC"
DEFAULT_COOLDOWN_SECONDS = 30.0
# What the bridge would send in one packet with the default settings.
FALLBACK_MESSAGE_BYTES = 120
FALLBACK_CHUNK_DELAY_MS = 2500
NODE_FLUSH_SECONDS = 5.0
CONTACT_REFRESH_SECONDS = 60.0
MAX_IMPORT_RECORDS = 5000
MAX_BACKUP_CHECK_INS = 200_000
MAX_STREAK = 1000
MAX_NAME_LENGTH = 64
MAX_TEXT_LENGTH = 500
# A backup file says what it is, and which version of the format it's in.
BACKUP_FORMAT = "meshgram-check-ins"
BACKUP_VERSION = 1
CHANGE_EVENT = {"type": "control", "what": "check_in"}
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
DAY_ALIASES = {name[:3].lower(): index for index, name in enumerate(WEEKDAY_NAMES)}


def time_zones() -> list[str]:
    """IANA time zone names for the settings form (``Etc/GMT+5`` and the like left out: their signs confuse)."""
    zones = {
        zone
        for zone in zoneinfo.available_timezones()
        if "/" in zone and not zone.startswith(("Etc/", "posix/", "right/", "SystemV/"))
    }
    return [DEFAULT_TIMEZONE, *sorted(zones)]


def resolve_zone(name: str) -> tzinfo:
    if not name or name.upper() == "UTC":
        return timezone.utc
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        LOGGER.warning("Check-ins: unknown time zone %r; using UTC", name)
        return timezone.utc


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _days(value: Any) -> frozenset[int]:
    """Weekdays (0 = Monday) from a list or a "0,1" / "mon, tue" string."""
    if value is None:
        return frozenset({0})
    items = value.split(",") if isinstance(value, str) else value if isinstance(value, (list, tuple, set)) else [value]
    days: set[int] = set()
    for item in items:
        text = str(item).strip().lower()
        if text.isdigit() and 0 <= int(text) <= 6:
            days.add(int(text))
        elif text[:3] in DAY_ALIASES:
            days.add(DAY_ALIASES[text[:3]])
    return frozenset(days)


@dataclass(frozen=True, slots=True)
class CheckInConfig:
    channel: int = 0
    keyword: str = DEFAULT_KEYWORD
    title: str = DEFAULT_TITLE
    # Weekdays check-ins count on (0 = Monday); empty: any day.
    days: frozenset[int] = frozenset({0})
    timezone: str = DEFAULT_TIMEZONE
    reply: bool = True
    reply_to_observed: bool = True
    reply_template: str = DEFAULT_TEMPLATE
    reply_cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    api_token: str = ""
    db_path: Path = field(default_factory=lambda: data_dir() / DEFAULT_DB_FILE)

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "CheckInConfig":
        keyword = str(settings.get("keyword") or "").strip() or DEFAULT_KEYWORD
        template = str(settings.get("reply_template") or "").strip() or DEFAULT_TEMPLATE
        return cls(
            channel=max(0, _as_int(settings.get("channel"), 0)),
            keyword=keyword,
            title=str(settings.get("title") or "").strip() or DEFAULT_TITLE,
            days=_days(settings.get("days")),
            timezone=str(settings.get("timezone") or "").strip() or DEFAULT_TIMEZONE,
            reply=_as_bool(settings.get("reply"), True),
            reply_to_observed=_as_bool(settings.get("reply_to_observed"), True),
            reply_template=template,
            reply_cooldown_seconds=max(0.0, _as_float(settings.get("reply_cooldown_seconds"), DEFAULT_COOLDOWN_SECONDS)),
            api_token=str(settings.get("api_token") or "").strip(),
            # A relative path is relative to MESHGRAM_DATA_DIR (an absolute one is used as-is).
            db_path=data_dir() / str(settings.get("db_path") or DEFAULT_DB_FILE),
        )

    @property
    def zone(self) -> tzinfo:
        return resolve_zone(self.timezone)


def split_sender(text: str) -> tuple[Optional[str], str]:
    """``"<name>: <message>"`` -> (name, message), as the transport splits channel messages."""
    text = text.strip("\0").strip()
    index = text.find(": ")
    if not 0 < index <= 32:
        return None, text
    sender, body = text[:index].strip(), text[index + 2 :].strip()
    if not sender or "\n" in sender or "\r" in sender or not body:
        return None, text
    return sender, body


def message_key(channel: str, sender_timestamp: Any, sender: str, body: str) -> Optional[str]:
    """One message, however many radios heard it: the sender's own timestamp tells repeats apart."""
    if sender_timestamp is None:
        return None
    return hashlib.sha1(f"{channel}|{sender_timestamp}|{sender}|{body}".encode("utf-8")).hexdigest()


def pack_lines(text: str, max_bytes: int) -> list[str]:
    """Split a reply into as few messages of at most ``max_bytes`` as whole lines allow."""
    parts: list[str] = []
    current = ""
    for line in text.splitlines():
        pieces = split_text_by_bytes(line, max_bytes) if utf8_len(line) > max_bytes else [line]
        for piece in pieces:
            candidate = f"{current}\n{piece}" if current else piece
            if utf8_len(candidate) <= max_bytes:
                current = candidate
            else:
                parts.append(current)
                current = piece
    if current:
        parts.append(current)
    return parts


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def iso_time(timestamp: Optional[float]) -> Optional[str]:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_key(value: Any) -> Optional[str]:
    key = str(value or "").strip().upper()
    return key if KEY_RE.match(key) else None


def _packet_bytes(rx_log: dict[str, Any]) -> Optional[bytes]:
    raw_hex = str(rx_log.get("payload") or "").strip()
    if not raw_hex:
        # A local RX log's ``raw_hex`` is the whole frame: SNR byte, RSSI byte, then the packet.
        raw_hex = str(rx_log.get("raw_hex") or "").strip()[4:]
    if not raw_hex or len(raw_hex) % 2 or not HEX_RE.match(raw_hex):
        return None
    return bytes.fromhex(raw_hex)


class CheckInPlugin(BasePlugin):
    name = "check_in"
    title = "Weekly check-ins"
    description = (
        "Counts the nodes that post a keyword (\"#MeshCoreMonday\") on a channel, heard by the radio or by "
        "MeshMapper observers, replies with their rank and streak, and serves a leaderboard for other sites."
    )
    settings_schema = {
        "type": "object",
        "properties": {
            "channel": {
                "type": "integer",
                "minimum": 0,
                "maximum": 255,
                "format": CHANNEL_FORMAT,
                "title": "Channel",
                "description": "Where check-ins are posted and answered.",
                "default": 0,
            },
            "keyword": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "title": "Check-in word or phrase",
                "description": "A message containing it, in any case, is a check-in.",
                "default": DEFAULT_KEYWORD,
            },
            "title": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "title": "Title",
                "description": "Heads the reply ({title}) and the leaderboard.",
                "default": DEFAULT_TITLE,
            },
            "days": {
                "type": "array",
                "title": "Check-in days",
                "description": "Check-ins count on these days only; on others the keyword is ignored. None ticked: any day.",
                "items": {"type": "integer", "enum": list(range(7)), "x-enum-titles": list(WEEKDAY_NAMES)},
                "uniqueItems": True,
                "default": [0],
                "x-keep-empty": True,
            },
            "timezone": {
                "type": "string",
                "title": "Time zone",
                "description": "Where days and weeks (Monday to Sunday) begin.",
                "enum": time_zones(),
                "default": DEFAULT_TIMEZONE,
            },
            "reply": {"type": "boolean", "title": "Reply to check-ins", "default": True},
            "reply_to_observed": {
                "type": "boolean",
                "title": "Also reply to check-ins only observers heard",
                "description": "Check-ins other MeshMapper observers heard (with the subscriber account) count either way.",
                "default": True,
            },
            "reply_template": {
                "type": "string",
                "minLength": 1,
                "maxLength": 400,
                "title": "Reply",
                "description": (
                    "Placeholders: {title}, {name}, {rank} (this week), {streak}, {streak_weeks} (\"3 weeks\"), "
                    "{participants}, {participants_label} (\"13 participants\"), {total}, {keyword}. "
                    "A reply longer than one radio message is split between lines."
                ),
                "default": DEFAULT_TEMPLATE,
                "x-multiline": True,
            },
            "reply_cooldown_seconds": {
                "type": "number",
                "minimum": 0,
                "title": "Quiet time per node (seconds)",
                "description": "Check-ins sooner than this after a reply to the same node still count, unanswered.",
                "default": DEFAULT_COOLDOWN_SECONDS,
            },
            "api_token": {
                "type": "string",
                "writeOnly": True,
                "maxLength": 200,
                "title": "Leaderboard API token",
                "description": (
                    "Lets another site read /api/plugins/check_in/leaderboard with \"Authorization: Bearer <token>\", "
                    "without the web app's password."
                ),
            },
            "db_path": {
                "type": "string",
                "minLength": 1,
                "title": "Database file",
                "description": "Relative to the data directory.",
                "default": DEFAULT_DB_FILE,
            },
        },
    }

    def __init__(self, settings: Optional[dict[str, Any]] = None):
        super().__init__(settings)
        self.config = CheckInConfig.from_settings(self.settings)
        # Swapped in tests.
        self.clock: Callable[[], float] = time.time
        self.store: Optional[CheckInStore] = None
        self._context: Optional[PluginContext] = None
        self._web: Any = None
        self._routes: list[Any] = []
        self._transport: Any = None
        self._lock = asyncio.Lock()
        self._last_reply: dict[int, float] = {}
        self._pending_nodes: dict[str, NodeSighting] = {}
        self._flush_task: Optional[asyncio.Task[None]] = None
        self._refresh_task: Optional[asyncio.Task[None]] = None

    # --- Lifecycle ----------------------------------------------------------------------

    async def on_startup(self, context: PluginContext) -> list[PluginAction]:
        self._context = context
        store = CheckInStore(self.config.db_path)
        # Raises CheckInStoreError: without its database the plugin doesn't start (the panel says why).
        await asyncio.to_thread(store.open)
        self.store = store
        self._flush_task = asyncio.create_task(self._flush_loop(), name="check-in-nodes")
        web = getattr(context, "web", None)
        if web is not None:
            self._web = web
            self._routes = register_check_in_routes(web, self)
            self._announce()
        else:
            LOGGER.info("Check-ins: the web app is off (web.enabled), so there's no leaderboard API")
        LOGGER.info("Check-ins: counting %r on channel %s (%s)", self.config.keyword, self.config.channel, store.path)
        return []

    async def on_mesh_connected(self, transport: Any, context: PluginContext) -> None:
        self._context = context
        self._transport = transport
        transport.add_rx_log_listener(self.handle_rx_log)
        transport.add_remote_rx_log_listener(self.handle_remote_rx_log)
        self._refresh_from_radio()
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_loop(), name="check-in-contacts")

    async def apply_settings(self, settings: dict[str, Any]) -> bool:
        """Everything but the database file changes live."""
        config = CheckInConfig.from_settings(settings)
        if config.db_path != self.config.db_path:
            return False
        self.settings = settings
        self.config = config
        self._announce()
        return True

    async def on_shutdown(self) -> None:
        for task in (self._refresh_task, self._flush_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._refresh_task = self._flush_task = None
        if self._transport is not None:
            self._transport.remove_rx_log_listener(self.handle_rx_log)
            self._transport.remove_remote_rx_log_listener(self.handle_remote_rx_log)
            self._transport = None
        if self._web is not None:
            for route in self._routes:
                self._web.remove_route(route)
            self._routes = []
            self._announce()
            self._web = None
        if self.store is not None:
            await self._flush_nodes()
            await asyncio.to_thread(self.store.close)
            self.store = None

    # --- Messages ---------------------------------------------------------------------------

    async def on_mesh_message(self, event: MeshTextEvent, context: PluginContext) -> list[PluginAction]:
        if event.to_id is not None or event.channel_index != self.config.channel:
            return []
        self._context = context
        raw = event.raw_packet if isinstance(event.raw_packet, dict) else {}
        sender, body = split_sender(str(raw.get("text") or ""))
        if sender is None and event.sender_label and event.sender_label != f"ch{event.channel_index}":
            sender, body = event.sender_label, event.text
        if sender is None:
            return []  # Nobody to count: the message has no "<name>: " in front.
        return await self._handle_message(
            sender,
            body,
            sender_timestamp=raw.get("sender_timestamp"),
            source=SOURCE_RADIO,
            observer_id=None,
            reply=True,
        )

    async def handle_rx_log(self, rx_log: dict[str, Any]) -> None:
        """What this radio heard: adverts name nodes (its channel messages arrive through on_mesh_message)."""
        raw = _packet_bytes(rx_log)
        parsed = parse_packet(raw) if raw else None
        if parsed and parsed["payload_type"] == PAYLOAD_TYPE_ADVERT:
            self._sight_advert(decode_advert(parsed["payload"]))

    async def handle_remote_rx_log(self, rx_log: dict[str, Any]) -> None:
        """What other MeshMapper observers heard: adverts, and check-ins on the channel."""
        source = rx_log.get("source_node")
        if isinstance(source, dict):
            self._sight(source.get("public_key"), source.get("name"), fill_only=True)
        raw = _packet_bytes(rx_log) if rx_log.get("payload") else None
        parsed = parse_packet(raw) if raw else None
        if not parsed:
            return
        if parsed["payload_type"] == PAYLOAD_TYPE_ADVERT:
            self._sight_advert(decode_advert(parsed["payload"]))
            return
        if parsed["payload_type"] != PAYLOAD_TYPE_GRP_TXT:
            return
        channel = self._channel_entry()
        decrypted = decrypt_group_text(parsed["payload"], [channel]) if channel else None
        if not decrypted:
            return
        sender, body = split_sender(decrypted["message"])
        if sender is None or self._is_own_name(sender):
            return  # Our own replies come back through observers too.
        actions = await self._handle_message(
            sender,
            body,
            sender_timestamp=decrypted.get("sender_timestamp"),
            source=SOURCE_OBSERVER,
            observer_id=_clean_key(rx_log.get("observer_id")),
            reply=self.config.reply_to_observed,
        )
        if actions:
            await self._send(actions)

    async def _handle_message(
        self,
        sender: str,
        body: str,
        *,
        sender_timestamp: Any,
        source: str,
        observer_id: Optional[str],
        reply: bool,
    ) -> list[PluginAction]:
        config = self.config
        if self.store is None or config.keyword.casefold() not in body.casefold():
            return []
        now = self.clock()
        moment = local_moment(now, config.zone)
        if not is_check_in_day(moment, config.days):
            LOGGER.info("Check-ins: ignoring %s's check-in: %s isn't a check-in day", sender, WEEKDAY_NAMES[moment.weekday()])
            return []
        record = CheckInRecord(
            sender_name=sender,
            text=body[:MAX_TEXT_LENGTH],
            received_at=now,
            week=week_start(moment.date()),
            source=source,
            observer_id=observer_id,
            message_key=message_key(self._channel_key(), sender_timestamp, sender, body),
        )
        async with self._lock:
            # A node advertised just now is known before its check-in is matched to it.
            await self._flush_nodes()
            participant_id = await asyncio.to_thread(self.store.record_check_in, record)
            if participant_id is None:
                return []  # Heard already, by the radio or another observer.
            histories = await asyncio.to_thread(self.store.histories)
        table = standings(histories, moment, config.days)
        mine = next(standing for standing in table if standing.participant_id == participant_id)
        participants = sum(1 for standing in table if standing.checked_in_this_week)
        LOGGER.info(
            "Check-ins: %s checked in (%s): #%s this week, streak %s, %s participants",
            sender,
            source,
            mine.week_rank,
            mine.streak,
            participants,
        )
        self._announce()
        if not (reply and config.reply):
            return []
        last = self._last_reply.get(participant_id)
        if last is not None and now - last < config.reply_cooldown_seconds:
            LOGGER.info("Check-ins: not replying to %s again so soon", sender)
            return []
        self._last_reply[participant_id] = now
        return self._reply_actions(self.render_reply(sender, mine, participants))

    def render_reply(self, name: str, standing: Standing, participants: int) -> str:
        values = {
            "title": self.config.title,
            "name": name,
            "rank": standing.week_rank,
            "streak": standing.streak,
            "streak_weeks": _count(standing.streak, "week"),
            "participants": participants,
            "participants_label": _count(participants, "participant"),
            "total": standing.total_check_ins,
            "keyword": self.config.keyword,
        }
        try:
            text = self.config.reply_template.format_map(values)
        except (KeyError, ValueError, IndexError, AttributeError) as exc:
            LOGGER.warning("Check-ins: the reply template doesn't work (%s); using the default one", exc)
            text = DEFAULT_TEMPLATE.format_map(values)
        return text.strip()

    def _reply_actions(self, text: str) -> list[PluginAction]:
        context = self._context
        chunking = getattr(getattr(context, "settings", None), "chunking", None)
        payload_limit = getattr(context, "mesh_payload_limit", None)
        if chunking is not None and isinstance(payload_limit, int):
            max_bytes = channel_message_max_bytes(chunking, payload_limit)
            delay_ms = max(chunking.inter_chunk_delay_ms, chunking.broadcast_min_inter_chunk_delay_ms)
            retry = {
                "retry_max_attempts": chunking.retry_max_attempts,
                "retry_initial_delay_ms": chunking.retry_initial_delay_ms,
                "retry_backoff_factor": chunking.retry_backoff_factor,
            }
        else:
            max_bytes, delay_ms, retry = FALLBACK_MESSAGE_BYTES, FALLBACK_CHUNK_DELAY_MS, {}
        return [
            SendMeshAction(text=part, channel_index=self.config.channel, delay_ms=delay_ms if index else 0, **retry)
            for index, part in enumerate(pack_lines(text, max_bytes))
        ]

    async def _send(self, actions: list[PluginAction]) -> None:
        execute = getattr(self._context, "execute_actions", None)
        if not callable(execute):
            LOGGER.warning("Check-ins: can't reply to a check-in an observer heard (no way to send here)")
            return
        await execute(actions, self.name)

    # --- The radio ------------------------------------------------------------------------

    def _channel_entry(self) -> Optional[dict[str, Any]]:
        """The check-in channel's name and key (for decrypting observers' packets)."""
        transport = self._transport
        slots = getattr(transport, "channel_slots", None) if transport is not None else None
        for slot in slots or []:
            if slot.get("index") == self.config.channel and slot.get("name") and any(slot.get("secret") or b""):
                return {"name": slot["name"], "secret": slot["secret"], "hash": slot["hash"]}
        return None

    def _channel_overview(self) -> Optional[dict[str, str]]:
        """Where to check in, for the leaderboard: the channel's name and kind (no key); None until the radio says."""
        channel = self._channel_entry()
        if channel is None:
            return None
        return {"name": channel["name"], "kind": channel_kind(channel["name"], channel["secret"])}

    def _channel_key(self) -> str:
        channel = self._channel_entry()
        return channel["hash"] if channel else f"slot{self.config.channel}"

    def _is_own_name(self, name: str) -> bool:
        transport = self._transport
        if transport is None:
            return False
        own = getattr(transport, "local_short_name", None) or (getattr(transport, "device_self_info", None) or {}).get("name")
        return bool(own) and str(own).strip().casefold() == name.strip().casefold()

    def _refresh_from_radio(self) -> None:
        contacts = getattr(self._transport, "contacts", None)
        if not isinstance(contacts, dict):
            return
        for key, contact in contacts.items():
            if isinstance(contact, dict):
                last_advert = _as_float(contact.get("last_advert"), 0.0) or None
                self._sight(key, contact.get("adv_name"), advert_at=last_advert)

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(CONTACT_REFRESH_SECONDS)
            self._refresh_from_radio()

    # --- Node directory -------------------------------------------------------------------

    def _sight_advert(self, advert: Optional[dict[str, Any]]) -> None:
        if advert:
            self._sight(advert.get("public_key"), advert.get("name"), advert_at=float(advert["advert_timestamp"]))

    def _sight(self, key: Any, name: Any, *, advert_at: Optional[float] = None, fill_only: bool = False) -> None:
        node_id = _clean_key(key)
        name = str(name or "").strip()[:MAX_NAME_LENGTH]
        if node_id is None or not name:
            return
        sighting = NodeSighting(node_id, name, advert_at, fill_only)
        pending = self._pending_nodes.get(node_id)
        if pending is not None:
            if fill_only and not pending.fill_only:
                return
            if (pending.advert_at or 0) > (advert_at or 0) and not pending.fill_only:
                return
        self._pending_nodes[node_id] = sighting

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(NODE_FLUSH_SECONDS)
            await self._flush_nodes()

    async def _flush_nodes(self) -> None:
        if not self._pending_nodes or self.store is None:
            return
        sightings, self._pending_nodes = list(self._pending_nodes.values()), {}
        try:
            changed = await asyncio.to_thread(self.store.upsert_nodes, sightings, self.clock())
        except Exception:
            LOGGER.exception("Check-ins: failed to save %d node names", len(sightings))
            return
        if changed:
            self._announce()

    def _announce(self) -> None:
        if self._web is not None:
            self._web.events.publish(dict(CHANGE_EVENT))

    # --- Leaderboard and management (check_in_api) ---------------------------------------------

    def _require_store(self) -> CheckInStore:
        if self.store is None:
            raise CheckInError("The check-in database isn't open", 503)
        return self.store

    def _place(self, week: date) -> float:
        """Midday of the week's first check-in day: where a check-in that wasn't heard goes."""
        first_day = min(self.config.days) if self.config.days else 0
        return datetime.combine(week + timedelta(days=first_day), dtime(12), tzinfo=self.config.zone).timestamp()

    async def _standings(self) -> tuple[datetime, list[Standing]]:
        moment = local_moment(self.clock(), self.config.zone)
        histories = await asyncio.to_thread(self._require_store().histories)
        return moment, standings(histories, moment, self.config.days)

    def _overview(self, moment: datetime, table: list[Standing]) -> dict[str, Any]:
        window = check_in_window(moment, self.config.days)
        return {
            "title": self.config.title,
            "keyword": self.config.keyword,
            "channel": self._channel_overview(),
            "timezone": self.config.timezone,
            "check_in_days": [WEEKDAY_NAMES[day] for day in sorted(self.config.days)],
            "week": week_start(moment.date()).isoformat(),
            "window": {
                "open": window.open,
                "opens_at": iso_time(window.opens_at.timestamp()),
                "closes_at": iso_time(window.closes_at.timestamp()),
            },
            "participants_this_week": sum(1 for standing in table if standing.checked_in_this_week),
            "generated_at": iso_time(moment.timestamp()),
        }

    async def leaderboard(self, limit: Optional[int] = None) -> dict[str, Any]:
        """Standings for other sites: no raw records, best streak first."""
        moment, table = await self._standings()
        entries = table[:limit] if limit else table
        return {
            **self._overview(moment, table),
            # This Meshgram's name (web.title), for sites to credit.
            "meshgram_title": getattr(getattr(self._web, "config", None), "title", None) or "Meshgram",
            "leaderboard": [
                {
                    "rank": standing.rank,
                    "id": standing.node_id,
                    "hash": standing.node_id[:2] if standing.node_id else None,
                    "name": standing.name,
                    "streak": standing.streak,
                    "best_streak": standing.best_streak,
                    "total_check_ins": standing.total_check_ins,
                    "last_check_in": iso_time(standing.last_check_in),
                    "checked_in_this_week": standing.checked_in_this_week,
                    "week_rank": standing.week_rank,
                    "resolved": standing.node_id is not None,
                }
                for standing in entries
            ],
        }

    async def participants(self) -> dict[str, Any]:
        """Everyone with their weeks, for the control panel."""
        moment = local_moment(self.clock(), self.config.zone)
        histories = await asyncio.to_thread(self._require_store().histories)
        table = standings(histories, moment, self.config.days)
        weeks = {history.participant_id: history for history in histories}
        return {
            **self._overview(moment, table),
            "participants": [
                {
                    "id": standing.participant_id,
                    "node_id": standing.node_id,
                    "name": standing.name,
                    "rank": standing.rank,
                    "streak": standing.streak,
                    "best_streak": standing.best_streak,
                    "total_check_ins": standing.total_check_ins,
                    "extra_check_ins": weeks[standing.participant_id].extra_check_ins,
                    "last_check_in": standing.last_check_in,
                    "checked_in_this_week": standing.checked_in_this_week,
                    "week_rank": standing.week_rank,
                    "weeks": sorted((week.isoformat() for week in weeks[standing.participant_id].weeks), reverse=True),
                }
                for standing in table
            ],
        }

    async def check_ins(self, participant_id: int) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._require_store().check_ins, participant_id)

    async def search_nodes(self, query: str) -> list[dict[str, Any]]:
        await self._flush_nodes()
        return await asyncio.to_thread(self._require_store().search_nodes, query)

    async def create_participant(self, name: str, node_id: Optional[str]) -> int:
        await self._flush_nodes()
        participant_id = await asyncio.to_thread(self._require_store().create_participant, name, node_id, self.clock())
        self._announce()
        return participant_id

    async def update_participant(self, participant_id: int, changes: dict[str, Any]) -> int:
        store = self._require_store()
        await self._flush_nodes()
        participant_id = await asyncio.to_thread(
            lambda: store.update_participant(participant_id, now=self.clock(), **changes)
        )
        self._announce()
        return participant_id

    async def delete_participant(self, participant_id: int) -> None:
        await asyncio.to_thread(self._require_store().delete_participant, participant_id)
        self._last_reply.pop(participant_id, None)
        self._announce()

    async def set_week(self, participant_id: int, week: date, present: bool) -> None:
        await asyncio.to_thread(self._require_store().set_week, participant_id, week, present, self._place)
        self._announce()

    async def set_streak(self, participant_id: int, streak: int, week: Optional[date]) -> None:
        last_week = week or self.default_as_of()
        await asyncio.to_thread(self._require_store().set_streak, participant_id, streak, last_week, self._place)
        self._announce()

    def default_as_of(self) -> date:
        """The week of the latest check-in day so far: this week once its first check-in day began."""
        moment = local_moment(self.clock(), self.config.zone)
        this_week = week_start(moment.date())
        days = self.config.days
        started = not days or any(day <= moment.weekday() for day in days)
        return this_week if started else this_week - timedelta(days=7)

    async def import_summary(
        self, records: list[Any], *, as_of: Optional[date], replace: bool, dry_run: bool
    ) -> dict[str, Any]:
        """Per-node summaries from another database; see ``CheckInStore.import_summary``."""
        if len(records) > MAX_IMPORT_RECORDS:
            raise CheckInError(f"Import at most {MAX_IMPORT_RECORDS} records at a time")
        default_week = as_of or self.default_as_of()
        parsed: list[SummaryRecord] = []
        errors: list[dict[str, Any]] = []
        for row, raw in enumerate(records, start=1):
            try:
                parsed.append(summary_record(row, raw, default_week))
            except ValueError as exc:
                errors.append({"row": row, "message": str(exc)})
        store = self._require_store()
        # Names just heard (the radio's contacts, say) are known before records are matched by them.
        await self._flush_nodes()
        result = await asyncio.to_thread(
            lambda: store.import_summary(
                parsed, replace=replace, dry_run=dry_run or bool(errors), place=self._place, now=self.clock()
            )
        )
        result["errors"] = sorted(errors + result["errors"], key=lambda error: error["row"])
        result["as_of"] = default_week.isoformat()
        if result["saved"]:
            LOGGER.info(
                "Check-ins: imported %d records (%d new, %d updated, %d linked)",
                len(parsed),
                result["created"],
                result["updated"],
                result["linked"],
            )
            self._last_reply.clear()
            self._announce()
        return result

    async def clear(self) -> dict[str, int]:
        """Delete every participant and check-in; how many there were."""
        counts = await asyncio.to_thread(self._require_store().clear)
        LOGGER.info("Check-ins: cleared %d participants and %d check-ins", counts["participants"], counts["check_ins"])
        self._last_reply.clear()
        self._announce()
        return counts

    async def backup(self) -> dict[str, Any]:
        """Every participant and check-in, as a file ``restore`` takes back (here or in another Meshgram)."""
        participants = await asyncio.to_thread(self._require_store().export_backup)
        return {
            "format": BACKUP_FORMAT,
            "version": BACKUP_VERSION,
            "exported_at": iso_time(self.clock()),
            "title": self.config.title,
            "timezone": self.config.timezone,
            "participants": participants,
        }

    async def restore(self, backup: Any, *, replace: bool, dry_run: bool) -> dict[str, Any]:
        """A backup's participants and check-ins; see ``CheckInStore.restore_backup``."""
        participants, errors = parse_backup(backup)
        store = self._require_store()
        result = await asyncio.to_thread(
            lambda: store.restore_backup(participants, replace=replace, dry_run=dry_run or bool(errors), now=self.clock())
        )
        result["errors"] = errors
        if result["saved"]:
            LOGGER.info(
                "Check-ins: restored a backup (%d new participants, %d updated, %d check-ins)",
                result["created"],
                result["updated"],
                result["check_ins"],
            )
            self._last_reply.clear()
            self._announce()
        return result

    async def export_rows(self) -> list[dict[str, Any]]:
        """Everyone as import records (plus the best streak), so data moves between databases."""
        moment, table = await self._standings()
        zone = self.config.zone
        return [
            {
                "node_id": standing.node_id or "",
                "name": standing.name,
                "streak": standing.streak,
                "total": standing.total_check_ins,
                "best_streak": standing.best_streak,
                "last_check_in": (
                    local_moment(standing.last_check_in, zone).date().isoformat() if standing.last_check_in else ""
                ),
            }
            for standing in table
        ]


# Column names other databases (and spreadsheets) use for the import fields.
_FIELD_ALIASES = {
    "node_id": ("node_id", "public_key", "pubkey", "key", "id", "node"),
    "name": ("name", "node_name", "adv_name"),
    "streak": ("streak", "current_streak", "streak_weeks", "weeks"),
    "total": ("total", "total_check_ins", "total_checkins", "check_ins", "checkins"),
    "last_check_in": ("last_check_in", "last_checkin", "last_week", "last_seen", "as_of"),
}


def summary_record(row: int, raw: Any, default_week: date) -> SummaryRecord:
    """An import record (a dict with any of the alias keys) -> ``SummaryRecord``; ``ValueError`` says what's wrong."""
    if not isinstance(raw, dict):
        raise ValueError("must be an object")
    values = {str(key).strip().lower(): value for key, value in raw.items()}

    def pick(field_name: str) -> Any:
        for alias in _FIELD_ALIASES[field_name]:
            value = values.get(alias)
            if value is not None and str(value).strip() != "":
                return value
        return None

    node_id = pick("node_id")
    if node_id is not None:
        node_id = str(node_id).strip()
        if not HEX_RE.match(node_id) or not 2 <= len(node_id) <= 64:
            raise ValueError(f"node key {node_id!r} isn't a hex public key (or prefix)")
    name = pick("name")
    if name is not None:
        name = str(name).strip()
        if len(name) > MAX_NAME_LENGTH:
            raise ValueError(f"name is longer than {MAX_NAME_LENGTH} characters")
    if node_id is None and not name:
        raise ValueError("needs a node key or a name")

    streak = _whole_number(pick("streak"), "streak", MAX_STREAK)
    if streak is None:
        raise ValueError("needs a streak")
    total = _whole_number(pick("total"), "total", 100_000)
    if total is not None and total < streak:
        raise ValueError("total is smaller than the streak")
    last = pick("last_check_in")
    if last is None:
        last_week = default_week
    else:
        try:
            last_week = parse_week(str(last).strip()[:10])
        except ValueError:
            raise ValueError(f"last check-in {last!r} isn't a date (YYYY-MM-DD)") from None
    return SummaryRecord(row=row, node_id=node_id, name=name or None, streak=streak, total=total, last_week=last_week)


def parse_backup(data: Any) -> tuple[list[BackupParticipant], list[dict[str, Any]]]:
    """A backup file's participants, and what's wrong with any of them (``{"row", "message"}``).

    Raises ``CheckInError`` for a file that isn't a backup at all.
    """
    if not isinstance(data, dict) or data.get("format") != BACKUP_FORMAT:
        raise CheckInError("That isn't a check-in backup (made with Export… → Download backup)")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise CheckInError("The backup doesn't say which version of the format it's in")
    if version > BACKUP_VERSION:
        raise CheckInError("The backup was made by a newer Meshgram: update this one to restore it")
    items = data.get("participants")
    if not isinstance(items, list):
        raise CheckInError("The backup has no list of participants")
    if len(items) > MAX_IMPORT_RECORDS:
        raise CheckInError(f"Restore at most {MAX_IMPORT_RECORDS} participants at a time")
    check_ins = sum(len(item["check_ins"]) for item in items if isinstance(item, dict) and isinstance(item.get("check_ins"), list))
    if check_ins > MAX_BACKUP_CHECK_INS:
        raise CheckInError(f"Restore at most {MAX_BACKUP_CHECK_INS} check-ins at a time")
    participants: list[BackupParticipant] = []
    errors: list[dict[str, Any]] = []
    for row, item in enumerate(items, start=1):
        try:
            participants.append(backup_participant(row, item))
        except ValueError as exc:
            errors.append({"row": row, "message": str(exc)})
    return participants, errors


def backup_participant(row: int, raw: Any) -> BackupParticipant:
    """One participant of a backup -> ``BackupParticipant``; ``ValueError`` says what's wrong."""
    if not isinstance(raw, dict):
        raise ValueError("must be an object")
    node_id = raw.get("node_id")
    if node_id is not None and node_id != "":
        node_id = _clean_key(node_id) if isinstance(node_id, str) else None
        if node_id is None:
            raise ValueError("node_id must be a public key (64 hex characters) or null")
    else:
        node_id = None
    name = raw.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= MAX_NAME_LENGTH:
        raise ValueError(f"name must be 1 to {MAX_NAME_LENGTH} characters")
    extra = raw.get("extra_check_ins") or 0
    if isinstance(extra, bool) or not isinstance(extra, int) or not 0 <= extra <= 100_000:
        raise ValueError("extra_check_ins must be a whole number from 0 to 100000")
    items = raw.get("check_ins") or []
    if not isinstance(items, list):
        raise ValueError("check_ins must be a list")
    check_ins = []
    for index, item in enumerate(items, start=1):
        try:
            check_ins.append(_backup_check_in(item))
        except ValueError as exc:
            raise ValueError(f"check-in {index}: {exc}") from None
    return BackupParticipant(row, node_id, name.strip(), extra, tuple(check_ins))


def _backup_check_in(raw: Any) -> BackupCheckIn:
    if not isinstance(raw, dict):
        raise ValueError("must be an object")
    try:
        week = parse_week(str(raw.get("week") or ""))
    except ValueError:
        raise ValueError("week must be a date (YYYY-MM-DD)") from None
    received_at = raw.get("received_at")
    if isinstance(received_at, bool) or not isinstance(received_at, (int, float)) or not 0 < received_at < 1e11:
        raise ValueError("received_at must be a Unix time in seconds")
    source = raw.get("source")
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    observer_id = raw.get("observer_id")
    if observer_id is not None:
        observer_id = _clean_key(observer_id) if isinstance(observer_id, str) else None
        if observer_id is None:
            raise ValueError("observer_id must be a public key or null")
    return BackupCheckIn(
        week=week,
        received_at=float(received_at),
        source=source,
        sender_name=_optional_text(raw.get("sender_name"), "sender_name", MAX_NAME_LENGTH),
        text=_optional_text(raw.get("text"), "text", MAX_TEXT_LENGTH),
        observer_id=observer_id,
        message_key=_optional_text(raw.get("message_key"), "message_key", 128),
    )


def _optional_text(value: Any, label: str, maximum: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{label} must be text of at most {maximum} characters, or null")
    return value


def _whole_number(value: Any, label: str, maximum: int) -> Optional[int]:
    if value is None:
        return None
    try:
        number = float(str(value).strip())
    except ValueError:
        raise ValueError(f"{label} {value!r} isn't a number") from None
    if not number.is_integer() or not 0 <= number <= maximum:
        raise ValueError(f"{label} must be a whole number from 0 to {maximum}")
    return int(number)
