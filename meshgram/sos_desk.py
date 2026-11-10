"""A MeshSOS channel in a Telegram group, worked with buttons instead of typed text.

The bridge plugin hands its group to a desk when its mode is "sos". Like the boards
(meshgram.sos_protocol), the desk knows one emergency or none: it shows the emergency as
one message with ACK and Resolved buttons, and keeps a pinned message with the SOS
button. While an emergency is open nobody can start another; anyone but its callers can
acknowledge it, and anyone can resolve it. A press goes out on the radio under the
presser's Telegram first name. Nothing typed in the group reaches the radio: on an SOS
channel any stray text starts an alarm on every board.
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
CLOSED_MEMORY = 32
MAX_NAMES = 500

PANEL_TEXT = (
    "🆘 <b>MeshSOS</b>\n"
    "Press the button to call for help: everyone here and every board on the radio is alerted.\n"
    "The emergency appears below with buttons to answer it. Typed messages are not sent to the radio."
)
SOS_LABEL = "🚨 SOS: I need help"
HINT_TEXT = "Typed messages aren't sent to the radio. Use the buttons."
ALREADY_ACTIVE = "An emergency is already active. Answer it with the buttons on its message."


@dataclass(slots=True)
class Emergency:
    key: str
    # Who called, first caller first: a board's name, or a Telegram user's first name.
    callers: list[str]
    text: str
    opened: float
    # Never shown: an id tells a repeat of an SOS from a new one, and a late message about an
    # earlier emergency from one about this. Newest last.
    ids: list[str] = field(default_factory=list)
    aware: list[str] = field(default_factory=list)
    message_id: Optional[int] = None
    # True for an SOS sent with the button here: the desk repeats it on the radio.
    from_telegram: bool = False
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
    name: str  # the presser, as they're named on the radio
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
        now: Callable[[], float] = time.time,
    ):
        self._label = label
        self._path = state_path
        self._bot = bot
        self._chat_id = chat_id
        self._send_mesh = send_mesh
        self._radio_online = radio_online
        self._now = now
        self._emergency: Optional[Emergency] = None
        self._closed: list[str] = []
        # Telegram user ID -> the name they go by on the radio (see name_for).
        self._names: dict[int, str] = {}
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
            emergency = self._emergency
            if emergency is not None and emergency.retrying and emergency.next_send <= now:
                await self._send_sos(emergency)
                await self._refresh(emergency)
                self._save()

    @property
    def emergency(self) -> Optional[Emergency]:
        return self._emergency

    # --- Names ------------------------------------------------------------------------------------

    def name_for(self, event: Any) -> str:
        """The name a Telegram user goes by on the radio: their first name.

        Two people with the same first name must stay two people (an ACK counts once per
        name), so the second gets their last initial, or a number. A name is kept for as long
        as the person keeps their first name.
        """
        words = _clean_name(event.sender_display_name).split()
        first = (words[0] if words else "Telegram")[: protocol.MAX_NAME_LENGTH]
        user_id = event.sender_id
        if user_id is None:
            return first
        known = self._names.get(user_id)
        if known is not None and known.split()[0] == first:
            return known
        taken = {name.casefold() for other, name in self._names.items() if other != user_id}
        candidates = [first]
        if len(words) > 1:
            candidates.append(f"{first[: protocol.MAX_NAME_LENGTH - 2]} {words[-1][0]}")
        candidates.extend(f"{first[: protocol.MAX_NAME_LENGTH - 3]} {number}" for number in range(2, 100))
        name = next((candidate for candidate in candidates if candidate.casefold() not in taken), first)
        self._names.pop(user_id, None)
        self._names[user_id] = name  # newest last
        while len(self._names) > MAX_NAMES:
            del self._names[next(iter(self._names))]
        self._save()
        return name

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
            return  # A late repeat of an emergency that's resolved.
        text = protocol.display_text(message.body)
        emergency = self._emergency
        if emergency is None:
            await self._open(
                Emergency(
                    key=secrets.token_hex(4),
                    callers=[message.sender],
                    text=text,
                    opened=self._now(),
                    ids=[incident_id] if incident_id else [],
                )
            )
            return
        if incident_id and incident_id in emergency.ids:
            return  # A repeat: nothing new.
        # Something new while it's open: still the one emergency, shown again so everyone notices.
        if incident_id:
            emergency.ids.append(incident_id)
        if message.sender not in emergency.callers:
            emergency.callers.append(message.sender)
        elif message.sender == emergency.callers[0]:
            emergency.text = text
        await self._supersede(emergency)
        await self._post(emergency)
        self._save()

    def _is_for(self, emergency: Emergency, incident_id: Optional[str]) -> bool:
        """False for an ACK or RESOLVED that names another emergency than the open one."""
        return not incident_id or not emergency.ids or incident_id in emergency.ids

    async def _mesh_ack(self, message: protocol.SosMessage) -> None:
        emergency = self._emergency
        if emergency is None:
            await self._show_stray(message)
            return
        if not self._is_for(emergency, message.incident_id):
            return
        if message.sender in emergency.callers or message.sender in emergency.aware:
            return
        await self._add_aware(emergency, message.sender)

    async def _mesh_resolved(self, message: protocol.SosMessage) -> None:
        emergency = self._emergency
        if emergency is None:
            await self._show_stray(message)
            return
        if not self._is_for(emergency, message.incident_id):
            return
        await self._close(emergency, message.sender)

    async def _show_stray(self, message: protocol.SosMessage) -> None:
        """Someone typed "ack" or "resolved" on the radio with nothing open: shown as it is."""
        if message.incident_id is None:
            await self._send(f"[{html.escape(message.sender)}] {html.escape(message.body)}")

    # --- From Telegram ----------------------------------------------------------------------------

    async def on_typed(self, event: TelegramMessageEvent) -> None:
        """Someone wrote in the group: nothing is sent, they're shown the buttons."""
        now = self._now()
        if now - self._last_hint < HINT_SECONDS:
            return
        self._last_hint = now
        keyboard = None if self._emergency is not None else [[(SOS_LABEL, SOS)]]
        await self._send(HINT_TEXT, keyboard=keyboard, reply_to=event.message_id)

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
        if self._emergency is not None:
            await self._reply(event, ALREADY_ACTIVE, alert=True)
            return
        name = self.name_for(event)
        await self._ask(
            event,
            action="sos",
            key=None,
            name=name,
            text=f"🚨 <b>{html.escape(name)}</b>, send an SOS? Everyone here and every board is alerted.",
            yes="🚨 Yes, send SOS",
        )

    async def _ask_resolve(self, event: TelegramButtonEvent, key: str) -> None:
        emergency = await self._open_emergency(event, key)
        if emergency is None:
            return
        name = self.name_for(event)
        await self._ask(
            event,
            action="resolve",
            key=key,
            name=name,
            text=(
                f"🟢 <b>{html.escape(name)}</b>, is the emergency over? "
                "Every board stops its alarm, and a new SOS can be sent."
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
            await self._reply(event, f"This question is for {question.name}.", alert=True)
            return
        await self._drop_question(question)
        if not confirmed:
            await self._reply(event, "Cancelled.")
            return
        if question.action == "sos":
            await self._press_sos(event, question.name)
            return
        emergency = await self._open_emergency(event, question.key or "")
        if emergency is None:
            return
        await self._send_mesh(protocol.relayed(question.name, protocol.build_resolved(emergency.incident_id)))
        await self._close(emergency, question.name)
        await self._reply(event, self._sent_note("Resolved."))

    async def _press_sos(self, event: TelegramButtonEvent, name: str) -> None:
        if self._emergency is not None:
            # One arrived while the question was open.
            await self._reply(event, ALREADY_ACTIVE, alert=True)
            return
        emergency = Emergency(
            key=secrets.token_hex(4),
            callers=[name],
            text=protocol.SOS_TEXT,
            opened=self._now(),
            ids=[self._new_incident_id()],
            from_telegram=True,
            retrying=True,
        )
        await self._send_sos(emergency)
        await self._open(emergency)
        await self._reply(event, self._sent_note("SOS sent."))

    async def _press_ack(self, event: TelegramButtonEvent, key: str) -> None:
        emergency = await self._open_emergency(event, key)
        if emergency is None:
            return
        name = self.name_for(event)
        if name in emergency.callers:
            await self._reply(event, "You called this SOS: the others acknowledge it.", alert=True)
            return
        if name in emergency.aware:
            await self._reply(event, "You're already listed as aware.")
            return
        await self._send_mesh(protocol.relayed(name, protocol.build_ack(emergency.incident_id)))
        await self._add_aware(emergency, name)
        await self._reply(event, self._sent_note("ACK sent."))

    async def _open_emergency(self, event: TelegramButtonEvent, key: str) -> Optional[Emergency]:
        """The open emergency, when the pressed button belongs to it; else the presser is told it's over."""
        emergency = self._emergency
        if emergency is None or emergency.key != key:
            await self._reply(event, "This emergency is closed.", alert=True)
            return None
        return emergency

    def _sent_note(self, text: str) -> str:
        return text if self._radio_online() else f"{text} The radio is offline: only this group has seen it."

    # --- The emergency ----------------------------------------------------------------------------

    def _new_incident_id(self) -> str:
        for _ in range(16):
            incident_id = protocol.new_incident_id()
            if incident_id not in self._closed:
                break
        return incident_id

    async def _open(self, emergency: Emergency) -> None:
        self._emergency = emergency
        # A question about sending an SOS has its answer: one is open.
        for question in [item for item in self._questions.values() if item.action == "sos"]:
            await self._drop_question(question)
        await self._post(emergency)
        self._save()

    async def _add_aware(self, emergency: Emergency, name: str) -> None:
        emergency.aware.append(name)
        emergency.retrying = False  # As on a board: the first ACK stops the repeats.
        await self._refresh(emergency)
        await self._send(f"✅ <b>{html.escape(name)}</b> is aware", reply_to=emergency.message_id)
        self._save()

    async def _close(self, emergency: Emergency, by: str) -> None:
        self._emergency = None
        self._closed = [*self._closed, *emergency.ids][-CLOSED_MEMORY:]
        for question in list(self._questions.values()):
            await self._drop_question(question)
        minutes = max(0, round((self._now() - emergency.opened) / 60))
        lines = [
            f"🟢 <b>Resolved: {html.escape(_names(emergency.callers))}</b>",
            html.escape(emergency.text),
            f"Resolved by {html.escape(by)} after {_duration(minutes)}",
        ]
        if emergency.aware:
            lines.append("✅ Aware: " + html.escape(", ".join(emergency.aware)))
        await self._edit(emergency.message_id, "\n".join(lines))
        await self._send(
            f"🟢 Resolved by <b>{html.escape(by)}</b>. A new SOS can be sent.",
            reply_to=emergency.message_id,
        )
        self._save()

    async def _send_sos(self, emergency: Emergency) -> None:
        """Send (or repeat) an SOS that started here."""
        await self._send_mesh(protocol.relayed(emergency.callers[0], protocol.build_sos(emergency.ids[0])))
        emergency.sends += 1
        emergency.retrying = emergency.sends < MAX_SENDS
        emergency.next_send = self._now() + RETRY_SECONDS

    # --- What the group sees ----------------------------------------------------------------------

    def _render(self, emergency: Emergency) -> str:
        lines = [f"🚨 <b>SOS: {html.escape(_names(emergency.callers))}</b>", html.escape(emergency.text)]
        if emergency.from_telegram:
            lines.append(f"Sent from Telegram · on the radio {emergency.sends}×")
        if emergency.aware:
            lines.append("✅ Aware: " + html.escape(", ".join(emergency.aware)))
        else:
            lines.append("⏳ Nobody has acknowledged yet")
        return "\n".join(lines)

    def _buttons(self, emergency: Emergency) -> list[list[tuple[str, str]]]:
        return [[("✅ ACK: I'm aware", ACK + emergency.key), ("🟢 Resolved", RESOLVE + emergency.key)]]

    async def _post(self, emergency: Emergency) -> None:
        emergency.message_id = await self._send(self._render(emergency), keyboard=self._buttons(emergency))

    async def _refresh(self, emergency: Emergency) -> None:
        await self._edit(emergency.message_id, self._render(emergency), keyboard=self._buttons(emergency))

    async def _supersede(self, emergency: Emergency) -> None:
        await self._edit(emergency.message_id, "🚨 <b>SOS</b>\nThere is more: see the newer message below.")

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
        if reply_to is not None:
            # Still posted when the message it answers has been deleted.
            kwargs.update(reply_to_message_id=reply_to, allow_sending_without_reply=True)
        try:
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
        names = data.get("names")
        if isinstance(names, dict):
            for user_id, name in names.items():
                if str(user_id).lstrip("-").isdigit() and isinstance(name, str) and name:
                    self._names[int(user_id)] = name
        saved = data.get("emergency")
        if isinstance(saved, dict):
            fields = set(Emergency.__slots__)
            try:
                emergency = Emergency(**{key: value for key, value in saved.items() if key in fields})
            except TypeError:
                emergency = None
            if emergency is not None and emergency.callers:
                self._emergency = emergency

    def _save(self) -> None:
        if self._path is None:
            return
        payload = {
            "panel_message_id": self._panel_id,
            "closed": self._closed,
            "names": {str(user_id): name for user_id, name in self._names.items()},
            "emergency": asdict(self._emergency) if self._emergency is not None else None,
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


def _clean_name(display_name: Any) -> str:
    """A Telegram name without what a name on the radio can't hold (see sos_protocol)."""
    return " ".join("".join(char for char in str(display_name or "") if char not in "<>:").split())


def _names(names: list[str]) -> str:
    return ", ".join(names)


def _duration(minutes: int) -> str:
    if minutes < 1:
        return "less than a minute"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"
