from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, Protocol, Union

if TYPE_CHECKING:
    from .reply_links import ReplyLinkRegistry


MeshPacketRef = Union[int, str]


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
class MeshTextEvent:
    from_id: Optional[str]
    to_id: Optional[str]
    packet_id: Optional[MeshPacketRef]
    reply_id: Optional[MeshPacketRef]
    channel_index: int
    text: str
    sender_label: str
    raw_packet: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MeshReactionEvent:
    from_id: Optional[str]
    to_id: Optional[str]
    packet_id: Optional[MeshPacketRef]
    target_packet_id: MeshPacketRef
    channel_index: int
    emoji: str
    sender_label: str
    raw_packet: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SendTelegramAction:
    chat_id: int
    text: str
    reply_to_message_id: Optional[int] = None
    bridge_source_meshtastic_packet_id: Optional[MeshPacketRef] = None


@dataclass(slots=True)
class SendTelegramReactionAction:
    chat_id: int
    message_id: int
    emoji: str
    is_big: bool = False


@dataclass(slots=True)
class SendMeshAction:
    text: str
    destination_id: Optional[Union[int, str]] = None
    channel_index: int = 0
    reply_id: Optional[MeshPacketRef] = None
    want_ack: bool = False
    delay_ms: int = 0
    retry_max_attempts: int = 1
    retry_initial_delay_ms: int = 0
    retry_backoff_factor: float = 1.0
    wait_for_ack: bool = False
    ack_timeout_ms: int = 0
    sequence_id: Optional[str] = None
    sequence_index: Optional[int] = None
    sequence_total: Optional[int] = None
    abort_on_failure: bool = False
    require_packet_id: bool = False
    bridge_source_telegram_chat_id: Optional[int] = None
    bridge_source_telegram_message_id: Optional[int] = None
    bridge_canonical_for_telegram_message: bool = False


@dataclass(slots=True)
class SendMeshReactionAction:
    emoji: str
    target_packet_id: MeshPacketRef
    destination_id: Optional[Union[int, str]] = None
    channel_index: int = 0
    want_ack: bool = False
    retry_max_attempts: int = 3
    retry_initial_delay_ms: int = 500
    retry_backoff_factor: float = 2.0


# Backward-compatible aliases (deprecated, retained so out-of-tree plugins and
# any old imports keep working).
MeshtasticTextEvent = MeshTextEvent
MeshtasticReactionEvent = MeshReactionEvent
SendMeshtasticAction = SendMeshAction
SendMeshtasticReactionAction = SendMeshReactionAction


PluginAction = Union[
    SendTelegramAction,
    SendTelegramReactionAction,
    SendMeshAction,
    SendMeshReactionAction,
]


@dataclass(slots=True)
class PluginContext:
    settings: "MeshgramSettings"
    telegram_group_id: int
    mesh_payload_limit: int
    local_node_id: Optional[str]
    reply_links: Optional["ReplyLinkRegistry"] = None

    @property
    def meshtastic_payload_limit(self) -> int:
        return self.mesh_payload_limit


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

    async def on_mesh_message(
        self,
        event: MeshTextEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        ...

    async def on_mesh_reaction(
        self,
        event: MeshReactionEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        ...
