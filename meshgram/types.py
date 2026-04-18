from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, Protocol, Union

if TYPE_CHECKING:
    from .reply_links import ReplyLinkRegistry


@dataclass(slots=True)
class TelegramMessageEvent:
    chat_id: int
    message_id: int
    reply_to_message_id: Optional[int]
    text: Optional[str]
    text_source: Optional[str]
    is_from_bot: bool
    sender_display_name: str
    has_media: bool
    raw_message: Any = None


@dataclass(slots=True)
class TelegramReactionEvent:
    chat_id: int
    message_id: int
    emoji: str
    is_from_bot: bool
    raw_reaction: Any = None


@dataclass(slots=True)
class MeshtasticTextEvent:
    from_id: Optional[str]
    to_id: Optional[str]
    packet_id: Optional[int]
    reply_id: Optional[int]
    channel_index: int
    text: str
    sender_label: str
    raw_packet: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MeshtasticReactionEvent:
    from_id: Optional[str]
    to_id: Optional[str]
    packet_id: Optional[int]
    target_packet_id: int
    channel_index: int
    emoji: str
    sender_label: str
    raw_packet: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SendTelegramAction:
    chat_id: int
    text: str
    reply_to_message_id: Optional[int] = None
    bridge_source_meshtastic_packet_id: Optional[int] = None


@dataclass(slots=True)
class SendTelegramReactionAction:
    chat_id: int
    message_id: int
    emoji: str
    is_big: bool = False


@dataclass(slots=True)
class SendMeshtasticAction:
    text: str
    destination_id: Optional[Union[int, str]] = None
    channel_index: int = 0
    reply_id: Optional[int] = None
    want_ack: bool = False
    delay_ms: int = 0
    retry_max_attempts: int = 1
    retry_initial_delay_ms: int = 0
    retry_backoff_factor: float = 1.0
    sequence_id: Optional[str] = None
    sequence_index: Optional[int] = None
    sequence_total: Optional[int] = None
    abort_on_failure: bool = False
    require_packet_id: bool = False
    bridge_source_telegram_chat_id: Optional[int] = None
    bridge_source_telegram_message_id: Optional[int] = None
    bridge_canonical_for_telegram_message: bool = False


@dataclass(slots=True)
class SendMeshtasticReactionAction:
    emoji: str
    target_packet_id: int
    destination_id: Optional[Union[int, str]] = None
    channel_index: int = 0
    want_ack: bool = False
    retry_max_attempts: int = 3
    retry_initial_delay_ms: int = 500
    retry_backoff_factor: float = 2.0


PluginAction = Union[
    SendTelegramAction,
    SendTelegramReactionAction,
    SendMeshtasticAction,
    SendMeshtasticReactionAction,
]


@dataclass(slots=True)
class PluginContext:
    settings: "MeshgramSettings"
    telegram_group_id: int
    meshtastic_payload_limit: int
    local_node_id: Optional[str]
    reply_links: Optional["ReplyLinkRegistry"] = None


class Plugin(Protocol):
    name: str

    async def on_startup(self, context: PluginContext) -> list[PluginAction]:
        ...

    async def on_telegram_message(
        self,
        event: TelegramMessageEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        ...

    async def on_telegram_reaction(
        self,
        event: TelegramReactionEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        ...

    async def on_meshtastic_message(
        self,
        event: MeshtasticTextEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        ...

    async def on_meshtastic_reaction(
        self,
        event: MeshtasticReactionEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        ...
