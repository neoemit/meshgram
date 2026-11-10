"""A MeshSOS channel in a Telegram group, worked with buttons instead of typed text.

The bridge plugin hands its group to a desk when its mode is "sos". The desk keeps the
open incidents the way the boards do (meshgram.sos_protocol), shows each as one message
with ACK and Resolved buttons, and keeps a pinned message with the SOS button. A press
goes out on the radio in the name of whoever pressed, with the incident's id, so it can't
land on the wrong alarm. Nothing typed in the group reaches the radio: on an SOS channel
any stray text starts an alarm on every board.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import sos_protocol as protocol
from .storage import write_private_json
from .types import TelegramButtonEvent, TelegramMessageEvent

LOGGER = logging.getLogger(__name__)

# Callback data of the buttons: "ms:<verb>[:<argument>]".
PREFIX = "ms:"
SOS = f"{PREFIX}sos"
ACK = f"{PREFIX}ack:"
RESOLVE = f"{PREFIX}res:"
YES = f"{PREFIX}yes:"
NO = f"{PREFIX}no:"

# An SOS sent from Telegram is repeated on the radio like a board repeats its own, until someone
# acknowledges it (MeshSOS defaults: every 30 s, five times in all).
RETRY_SECONDS = 30.0
MAX_SENDS = 5
# How long a "send it?" question waits for its answer.
CONFIRM_SECONDS = 30.0
# Someone typing gets pointed to the buttons at most this often.
HINT_SECONDS = 20.0
TICK_SECONDS = 1.0
MAX_OPEN = 8
CLOSED_MEMORY = 32

PANEL_TEXT = (
    "🆘 <b>MeshSOS</b>\n"
    "Press the button to call for help: everyone here and every board on the radio is alerted.\n"
    "Alarms appear below with buttons to answer them. Typed messages are not sent to the radio."
)
SOS_LABEL = "🚨 SOS: I need help"
HINT_TEXT = "Typed messages aren't sent to the radio. Use the buttons."


@dataclass(slots=True)
class Incident:
    key: str
    sender: str
    text: str
    opened: float
    ids: list[str] = field(default_factory=list)
    aware: list[str] = field(default_factory=list)
    message_id: Optional[int] = None
    # "radio", or "telegram" for an SOS sent with the button here.
    origin: str = "radio"
    # Who pressed the button (their Telegram name), for an SOS sent from here.
    by: str = ""
    sends: int = 0
    retrying: bool = False
    next_send: float = 0.0

    @property
    def incident_id(self) -> Optional[str]:
        return self.ids[-1] if self.ids else None


@dataclass(slots=True)
class Question:
    """A "send it?" message, answered by the person who pressed."""

    token: str
    user_id: Optional[int]
    asked: str  # their Telegram first name
    name: str  # the name the action goes out under
    action: str  # "sos" or "resolve"
    key: Optional[str]
    expires: float
    message_id: Optional[int] = None


class SosDesk:
    def __init__(
        self,
        *,
        label: str,
        state_path: Optional[Path],
        bot: Callable[[], Any],
        chat_id: Callable[[], int],
        send_mesh: Callable[[str], Awaitable[None]],
        radio_online: Callable[[], bool],
        radio_name: Callable[[Any], str],
        now: Callable[[], float] = time.time,
    ):
        self._label = label
        self._path = state_path
        self._bot = bot
        self._chat_id = chat_id
        self._send_mesh = send_mesh
        self._radio_online = radio_online
        self._radio_name = radio_name
        self._now = now
        self._incidents: list[Incident] = []  # oldest first
        self._closed: list[str] = []
        self._panel_id: Optional[int] = None
        self._panel_checked = False
        self._questions: dict[str, Question] = {}
        self._last_hint = 0.0
        self._task: Optional[asyncio.Task[None]] = None
        self._lock = asyncio.Lock()

    # --- Lifecycle --------------------------------------------------------------------------------

    async def start(self) -> None:
        await asyncio.to_thread(self._load)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for question in list(self._questions.values()):
            await self._drop_question(question)

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("%s: its SOS desk failed a round", self._label)
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self) -> None:
        """One round of what time brings: the pinned panel, unanswered questions, SOS repeats."""
        async with self._lock:
            if not self._panel_checked and self._bot() is not None:
                await self._ensure_panel()
            now = self._now()
            for question in [item for item in self._questions.values() if item.expires <= now]:
                await self._drop_question(question)
            for incident in list(self._incidents):
                if incident.retrying and incident.next_send <= now:
                    await self._send_sos(incident)
                    await self._refresh(incident)
                    self._save()

    @property
    def incidents(self) -> list[Incident]:
        return list(self._incidents)

    # --- From the radio ---------------------------------------------------------------------------

    async def on_mesh(self, sender: str, text: str) -> None:
        message = protocol.parse(sender, text)
        if message is None:
            return
        async with self._lock:
            if message.kind == protocol.ALARM:
                await self._mesh_alarm(message)
            elif message.kind == protocol.ACK:
                await self._mesh_ack(message)
            else:
                await self._mesh_resolved(message)

    async def _mesh_alarm(self, message: protocol.SosMessage) -> None:
        incident_id = message.incident_id
        if incident_id and incident_id in self._closed:
            return  # A late repeat of an incident that's resolved.
        text = protocol.display_text(message.body)
        known = self._by_sender(message.sender)
        if known is not None and incident_id and incident_id in known.ids:
            return  # A repeat: never a second incident.
        if known is not None:
            # More from someone whose incident is open: the same incident, with the new text.
            if incident_id:
                known.ids.append(incident_id)
            known.text = text
            await self._supersede(known)
            self._incidents.remove(known)
            self._incidents.append(known)
            await self._post(known)
            self._save()
            return
        incident = Incident(
            key=self._new_key(),
            sender=message.sender,
            text=text,
            opened=self._now(),
            ids=[incident_id] if incident_id else [],
        )
        await self._open(incident)

    async def _mesh_ack(self, message: protocol.SosMessage) -> None:
        incident = self._target(message.incident_id)
        if incident is None:
            if message.incident_id is None:
                await self._send(f"[{html.escape(message.sender)}] {html.escape(message.body)}")
            return
        if incident.sender == message.sender or message.sender in incident.aware:
            return
        await self._add_aware(incident, message.sender)

    async def _mesh_resolved(self, message: protocol.SosMessage) -> None:
        incident = self._target(message.incident_id)
        if incident is None:
            if message.incident_id is None:
                await self._send(f"[{html.escape(message.sender)}] {html.escape(message.body)}")
            return
        await self._close(incident, message.sender)

    # --- From Telegram ----------------------------------------------------------------------------

    async def on_typed(self, event: TelegramMessageEvent) -> None:
        """Someone wrote in the group: nothing is sent, they're shown the buttons."""
        now = self._now()
        if now - self._last_hint < HINT_SECONDS:
            return
        self._last_hint = now
        await self._send(HINT_TEXT, keyboard=[[(SOS_LABEL, SOS)]], reply_to=event.message_id)

    async def on_button(self, event: TelegramButtonEvent) -> None:
        data = event.data
        async with self._lock:
            if data == SOS:
                await self._ask_sos(event)
            elif data.startswith(ACK):
                await self._press_ack(event, data[len(ACK):])
            elif data.startswith(RESOLVE):
                await self._ask_resolve(event, data[len(RESOLVE):])
            elif data.startswith(YES):
                await self._answer(event, data[len(YES):], confirmed=True)
            elif data.startswith(NO):
                await self._answer(event, data[len(NO):], confirmed=False)
            else:
                await self._reply(event)

    async def _ask_sos(self, event: TelegramButtonEvent) -> None:
        name = self._radio_name(event)
        if self._by_sender(name) is not None:
            await self._reply(event, f"An SOS from {name} is already open. Use its Resolved button when it's over.", alert=True)
            return
        await self._ask(
            event,
            action="sos",
            key=None,
            name=name,
            text=f"🚨 <b>{html.escape(_first_name(event))}</b>, send an SOS as <b>{html.escape(name)}</b>?",
            yes="🚨 Yes, send SOS",
        )

    async def _ask_resolve(self, event: TelegramButtonEvent, key: str) -> None:
        incident = await self._open_incident(event, key)
        if incident is None:
            return
        await self._ask(
            event,
            action="resolve",
            key=key,
            name=self._radio_name(event),
            text=(
                f"🟢 <b>{html.escape(_first_name(event))}</b>, is <b>{html.escape(incident.sender)}</b>'s SOS over? "
                "Every board stops its alarm."
            ),
            yes="🟢 Yes, resolved",
        )

    async def _ask(self, event: TelegramButtonEvent, *, action: str, key: Optional[str], name: str, text: str, yes: str) -> None:
        for question in self._questions.values():
            if (question.user_id, question.action, question.key) == (event.sender_id, action, key):
                await self._reply(event, "Answer the question at the bottom of the chat.")
                return
        question = Question(
            token=secrets.token_hex(4),
            user_id=event.sender_id,
            asked=_first_name(event),
            name=name,
            action=action,
            key=key,
            expires=self._now() + CONFIRM_SECONDS,
        )
        question.message_id = await self._send(
            text, keyboard=[[(yes, YES + question.token), ("Cancel", NO + question.token)]]
        )
        if question.message_id is None:
            await self._reply(event, "Telegram didn't take the question. Press again.", alert=True)
            return
        self._questions[question.token] = question
        await self._reply(event)

    async def _answer(self, event: TelegramButtonEvent, token: str, *, confirmed: bool) -> None:
        question = self._questions.get(token)
        if question is None:
            await self._reply(event, "That question has expired. Press the button again.", alert=True)
            await self._delete(event.message_id)
            return
        if question.user_id != event.sender_id:
            await self._reply(event, f"This question is for {question.asked}.", alert=True)
            return
        await self._drop_question(question)
        if not confirmed:
            await self._reply(event, "Cancelled.")
            return
        if question.action == "sos":
            await self._press_sos(event, question.name)
            return
        incident = await self._open_incident(event, question.key or "")
        if incident is None:
            return
        await self._send_mesh(protocol.relayed(question.name, protocol.build_resolved(incident.incident_id)))
        await self._close(incident, question.name)
        await self._reply(event, self._sent_note(f"Resolved as {question.name}."))

    async def _press_sos(self, event: TelegramButtonEvent, name: str) -> None:
        if self._by_sender(name) is not None:
            await self._reply(event, f"An SOS from {name} is already open.", alert=True)
            return
        incident = Incident(
            key=self._new_key(),
            sender=name,
            text=protocol.SOS_TEXT,
            opened=self._now(),
            ids=[self._new_incident_id()],
            origin="telegram",
            by=" ".join(event.sender_display_name.split()),
            retrying=True,
        )
        await self._send_sos(incident)
        await self._open(incident)
        await self._reply(event, self._sent_note(f"SOS sent as {name}."))

    async def _press_ack(self, event: TelegramButtonEvent, key: str) -> None:
        incident = await self._open_incident(event, key)
        if incident is None:
            return
        name = self._radio_name(event)
        if name == incident.sender:
            await self._reply(event, "This is your own SOS: the others acknowledge it.", alert=True)
            return
        if name in incident.aware:
            await self._reply(event, f"{name} is already listed as aware.")
            return
        await self._send_mesh(protocol.relayed(name, protocol.build_ack(incident.incident_id)))
        await self._add_aware(incident, name)
        await self._reply(event, self._sent_note(f"ACK sent as {name}."))

    async def _open_incident(self, event: TelegramButtonEvent, key: str) -> Optional[Incident]:
        """The open incident a button belongs to; tells the presser when it's gone."""
        incident = next((item for item in self._incidents if item.key == key), None)
        if incident is None:
            await self._reply(event, "This alarm is closed.", alert=True)
        return incident

    def _sent_note(self, text: str) -> str:
        return text if self._radio_online() else f"{text} The radio is offline: only this group has seen it."

    # --- Incidents --------------------------------------------------------------------------------

    def _by_sender(self, sender: str) -> Optional[Incident]:
        return next((item for item in self._incidents if item.sender == sender), None)

    def _target(self, incident_id: Optional[str]) -> Optional[Incident]:
        """The incident an ACK or RESOLVED is for: the one it names, else the newest."""
        if incident_id:
            return next((item for item in self._incidents if incident_id in item.ids), None)
        return self._incidents[-1] if self._incidents else None

    def _new_key(self) -> str:
        return secrets.token_hex(4)

    def _new_incident_id(self) -> str:
        used = {incident_id for item in self._incidents for incident_id in item.ids} | set(self._closed)
        for _ in range(16):
            incident_id = protocol.new_incident_id()
            if incident_id not in used:
                break
        return incident_id

    async def _open(self, incident: Incident) -> None:
        while len(self._incidents) >= MAX_OPEN:
            await self._close(self._incidents[0], None)
        self._incidents.append(incident)
        await self._post(incident)
        self._save()

    async def _add_aware(self, incident: Incident, name: str) -> None:
        incident.aware.append(name)
        incident.retrying = False  # As on a board: the first ACK stops the repeats.
        await self._refresh(incident)
        await self._send(
            f"✅ <b>{html.escape(name)}</b> is aware of <b>{html.escape(incident.sender)}</b>'s SOS",
            reply_to=incident.message_id,
        )
        self._save()

    async def _close(self, incident: Incident, by: Optional[str]) -> None:
        """Resolved by ``by``; None when it only makes room for a newer incident."""
        if incident in self._incidents:
            self._incidents.remove(incident)
        self._closed = [*self._closed, *incident.ids][-CLOSED_MEMORY:]
        for question in [item for item in self._questions.values() if item.key == incident.key]:
            await self._drop_question(question)
        minutes = max(0, round((self._now() - incident.opened) / 60))
        if by is None:
            ending = "No longer tracked here: too many alarms are open."
            title = f"⚪ <b>Closed here: {html.escape(incident.sender)}</b>"
        else:
            ending = f"Resolved by {html.escape(by)} after {_duration(minutes)}"
            title = f"🟢 <b>Resolved: {html.escape(incident.sender)}</b>"
        lines = [title, html.escape(incident.text), ending]
        if incident.aware:
            lines.append("✅ Aware: " + html.escape(", ".join(incident.aware)))
        await self._edit(incident.message_id, "\n".join(lines))
        if by is not None:
            await self._send(
                f"🟢 <b>{html.escape(incident.sender)}</b>'s SOS is resolved (by {html.escape(by)})",
                reply_to=incident.message_id,
            )
        self._save()

    async def _send_sos(self, incident: Incident) -> None:
        """Send (or repeat) an SOS that started here."""
        await self._send_mesh(protocol.relayed(incident.sender, protocol.build_sos(incident.incident_id or "")))
        incident.sends += 1
        incident.retrying = incident.sends < MAX_SENDS
        incident.next_send = self._now() + RETRY_SECONDS

    # --- What the group sees ----------------------------------------------------------------------

    def _render(self, incident: Incident) -> str:
        lines = [f"🚨 <b>SOS: {html.escape(incident.sender)}</b>", html.escape(incident.text)]
        if incident.origin == "telegram":
            lines.append(f"Sent from Telegram by {html.escape(incident.by or incident.sender)} · on the radio {incident.sends}×")
        if incident.aware:
            lines.append("✅ Aware: " + html.escape(", ".join(incident.aware)))
        else:
            lines.append("⏳ Nobody has acknowledged yet")
        return "\n".join(lines)

    def _buttons(self, incident: Incident) -> list[list[tuple[str, str]]]:
        return [[("✅ ACK: I'm aware", ACK + incident.key), ("🟢 Resolved", RESOLVE + incident.key)]]

    async def _post(self, incident: Incident) -> None:
        incident.message_id = await self._send(self._render(incident), keyboard=self._buttons(incident))

    async def _refresh(self, incident: Incident) -> None:
        await self._edit(incident.message_id, self._render(incident), keyboard=self._buttons(incident))

    async def _supersede(self, incident: Incident) -> None:
        await self._edit(
            incident.message_id,
            f"🚨 <b>SOS: {html.escape(incident.sender)}</b>\nMore from {html.escape(incident.sender)}: see the newer message below.",
        )

    async def _ensure_panel(self) -> None:
        """Keep one message with the SOS button, pinned when the bot may pin."""
        self._panel_checked = True
        bot = self._bot()
        keyboard = _markup([[(SOS_LABEL, SOS)]])
        if self._panel_id is not None:
            try:
                await bot.edit_message_text(
                    chat_id=self._chat_id(), message_id=self._panel_id, text=PANEL_TEXT, parse_mode="HTML", reply_markup=keyboard
                )
                return
            except Exception as exc:
                if "not modified" in str(exc).lower():
                    return  # It's there, and says what it should.
                LOGGER.info("%s: its SOS button message is gone (%s); posting a new one", self._label, exc)
        self._panel_id = await self._send(PANEL_TEXT, keyboard=[[(SOS_LABEL, SOS)]], quiet=True)
        if self._panel_id is None:
            self._panel_checked = False  # Telegram unreachable: try again next round.
            return
        self._save()
        try:
            await bot.pin_chat_message(chat_id=self._chat_id(), message_id=self._panel_id, disable_notification=True)
        except Exception as exc:
            LOGGER.warning(
                "%s: can't pin the message with the SOS button (%s). Let the bot pin messages, or pin it by hand.",
                self._label,
                exc,
            )

    async def _drop_question(self, question: Question) -> None:
        self._questions.pop(question.token, None)
        await self._delete(question.message_id)

    # --- Telegram calls: a failure is logged, never raised ----------------------------------------

    async def _send(
        self,
        text: str,
        *,
        keyboard: Optional[list[list[tuple[str, str]]]] = None,
        reply_to: Optional[int] = None,
        quiet: bool = False,
    ) -> Optional[int]:
        bot = self._bot()
        if bot is None:
            LOGGER.warning("%s: its bot isn't connected; nothing was posted in the group", self._label)
            return None
        kwargs: dict[str, Any] = {"chat_id": self._chat_id(), "text": text, "parse_mode": "HTML"}
        if keyboard:
            kwargs["reply_markup"] = _markup(keyboard)
        if quiet:
            kwargs["disable_notification"] = True
        try:
            if reply_to is not None:
                # Still posted when the message it answers has been deleted.
                kwargs.update(reply_to_message_id=reply_to, allow_sending_without_reply=True)
            sent = await bot.send_message(**kwargs)
            return getattr(sent, "message_id", None)
        except Exception as exc:
            LOGGER.warning("%s: Telegram didn't take a message (%s)", self._label, exc)
            return None

    async def _edit(self, message_id: Optional[int], text: str, *, keyboard: Optional[list[list[tuple[str, str]]]] = None) -> None:
        bot = self._bot()
        if bot is None or message_id is None:
            return
        try:
            await bot.edit_message_text(
                chat_id=self._chat_id(),
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=_markup(keyboard) if keyboard else None,
            )
        except Exception as exc:
            if "not modified" not in str(exc).lower():
                LOGGER.warning("%s: Telegram didn't update a message (%s)", self._label, exc)

    async def _delete(self, message_id: Optional[int]) -> None:
        bot = self._bot()
        if bot is None or message_id is None:
            return
        try:
            await bot.delete_message(chat_id=self._chat_id(), message_id=message_id)
        except Exception as exc:
            LOGGER.debug("%s: Telegram didn't delete a message (%s)", self._label, exc)

    async def _reply(self, event: TelegramButtonEvent, text: Optional[str] = None, *, alert: bool = False) -> None:
        """Answer a press: Telegram shows ``text`` to the one who pressed (a pop-up for ``alert``)."""
        bot = self._bot()
        if bot is None or not event.query_id:
            return
        try:
            await bot.answer_callback_query(callback_query_id=event.query_id, text=text, show_alert=alert)
        except Exception as exc:
            LOGGER.debug("%s: Telegram didn't take the answer to a press (%s)", self._label, exc)

    # --- State kept across restarts, so the buttons on older messages keep working ------------------

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            LOGGER.warning("%s: can't read %s (%s)", self._label, self._path, exc)
            return
        if not isinstance(data, dict):
            return
        panel = data.get("panel_message_id")
        self._panel_id = panel if isinstance(panel, int) else None
        self._closed = [str(item) for item in data.get("closed") or []][-CLOSED_MEMORY:]
        fields = set(Incident.__slots__)
        for item in data.get("incidents") or []:
            try:
                self._incidents.append(Incident(**{key: value for key, value in item.items() if key in fields}))
            except TypeError:
                continue

    def _save(self) -> None:
        if self._path is None:
            return
        payload = {
            "panel_message_id": self._panel_id,
            "closed": self._closed,
            "incidents": [asdict(incident) for incident in self._incidents],
        }
        try:
            write_private_json(self._path, payload)
        except OSError as exc:
            LOGGER.warning("%s: can't save %s (%s)", self._label, self._path, exc)


def _markup(keyboard: list[list[tuple[str, str]]]) -> Any:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in keyboard]
    )


def _first_name(event: Any) -> str:
    name = " ".join(str(event.sender_display_name or "").split())
    return name.split(" ", 1)[0] if name else "You"


def _duration(minutes: int) -> str:
    if minutes < 1:
        return "less than a minute"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"
