from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, Protocol, Union

if TYPE_CHECKING:
    from .config import MeshgramSettings
    from .status import StatusRegistry
    from .web import WebServer


# MeshCore has no numeric packet IDs: messages are identified by opaque strings
# (``expected_ack`` codes, or digests of the sender, timestamp and text).
MeshPacketRef = str


@dataclass(slots=True)
class TelegramMessageEvent:
    chat_id: int
    message_id: int
    text: Optional[str]
    text_source: Optional[str]
    is_from_bot: bool
    sender_display_name: str
    has_media: bool
    raw_message: Any = None


@dataclass(slots=True)
class MeshTextEvent:
    from_id: Optional[str]
    # The local node for direct messages; None for channel messages.
    to_id: Optional[str]
    packet_id: Optional[MeshPacketRef]
    # The channel slot, or -1 for direct messages.
    channel_index: int
    text: str
    sender_label: str
    raw_packet: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SendTelegramAction:
    chat_id: int
    text: str


@dataclass(slots=True)
class SendMeshAction:
    text: str
    # A contact's public key (or prefix) for a direct message; None sends to ``channel_index``.
    destination_id: Optional[str] = None
    channel_index: int = 0
    # Direct messages only: wait up to ``ack_timeout_ms`` for the recipient's ACK.
    want_ack: bool = False
    wait_for_ack: bool = False
    ack_timeout_ms: int = 0
    delay_ms: int = 0
    retry_max_attempts: int = 1
    retry_initial_delay_ms: int = 0
    retry_backoff_factor: float = 1.0
    sequence_id: Optional[str] = None
    sequence_index: Optional[int] = None
    sequence_total: Optional[int] = None
    abort_on_failure: bool = False


PluginAction = Union[SendTelegramAction, SendMeshAction]


@dataclass(slots=True)
class PluginContext:
    settings: "MeshgramSettings"
    telegram_group_id: int
    mesh_payload_limit: int
    local_node_id: Optional[str]
    # Connection status of the radio, Telegram and plugin services (see meshgram.status).
    status: Optional["StatusRegistry"] = None
    # The web app, when it runs; plugins publish live data to its pages through it.
    web: Optional["WebServer"] = None


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

    async def on_mesh_message(
        self,
        event: MeshTextEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        ...
