"""Telegram messages as the plugins see them."""
from __future__ import annotations

from typing import Any, Optional

from .types import TelegramButtonEvent, TelegramMessageEvent

_MEDIA_FIELDS = (
    "animation",
    "audio",
    "document",
    "photo",
    "sticker",
    "video",
    "video_note",
    "voice",
)


def message_has_media(message: Any) -> bool:
    return any(getattr(message, field, None) for field in _MEDIA_FIELDS)


def event_from_message(message: Any) -> Optional[TelegramMessageEvent]:
    """The event for a python-telegram-bot ``Message``; None for one without a chat."""
    if not message:
        return None
    chat = message.chat
    if not chat:
        return None

    text: Optional[str] = None
    text_source: Optional[str] = None
    if message.text is not None:
        text = message.text
        text_source = "text"
    elif message.caption is not None:
        text = message.caption
        text_source = "caption"

    from_user = message.from_user
    sender_display_name = "Unknown"
    is_from_bot = False
    sender_id: Optional[int] = None
    sender_username: Optional[str] = None
    if from_user is not None:
        sender_display_name = from_user.full_name or from_user.username or str(from_user.id)
        is_from_bot = bool(from_user.is_bot)
        sender_id = from_user.id
        sender_username = from_user.username or None

    return TelegramMessageEvent(
        chat_id=chat.id,
        message_id=message.message_id,
        text=text,
        text_source=text_source,
        is_from_bot=is_from_bot,
        sender_display_name=sender_display_name,
        has_media=message_has_media(message),
        raw_message=message,
        sender_id=sender_id,
        sender_username=sender_username,
    )


def button_event_from_query(query: Any) -> Optional[TelegramButtonEvent]:
    """The event for a python-telegram-bot ``CallbackQuery``; None when it isn't a button under
    a message in a chat (an inline-mode button, or a message too old for Telegram to name)."""
    if not query or query.data is None:
        return None
    message = query.message
    chat = getattr(message, "chat", None)
    if message is None or chat is None:
        return None
    user = query.from_user
    return TelegramButtonEvent(
        chat_id=chat.id,
        message_id=message.message_id,
        data=str(query.data),
        query_id=query.id,
        sender_display_name=(user.full_name or user.username or str(user.id)) if user is not None else "Unknown",
        sender_id=user.id if user is not None else None,
        sender_username=(user.username or None) if user is not None else None,
        raw_query=query,
    )
