from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Optional, TYPE_CHECKING

from ..config import MESHCORE_BACKEND, MESHTASTIC_BACKEND, MeshgramSettings
from ..types import (
    MeshPacketRef,
    MeshReactionEvent,
    MeshTextEvent,
    SendMeshAction,
    SendMeshReactionAction,
)

if TYPE_CHECKING:
    import asyncio


MeshTextCallback = Callable[[MeshTextEvent], Awaitable[None]]
MeshReactionCallback = Callable[[MeshReactionEvent], Awaitable[None]]


class MeshTransport(ABC):
    """Backend-neutral interface between MeshgramApp and a mesh radio.

    Implementations wrap either Meshtastic or MeshCore. The app communicates
    in terms of normalized ``MeshTextEvent`` / ``MeshReactionEvent`` objects
    and emits ``SendMeshAction`` / ``SendMeshReactionAction`` instances.
    """

    backend_name: str = ""
    supports_reactions: bool = True
    supports_reply_threading: bool = True
    supports_wait_for_ack: bool = False

    def __init__(self, settings: MeshgramSettings):
        self.settings = settings
        self.local_node_id: Optional[str] = None

    # --- Lifecycle ----------------------------------------------------------

    @abstractmethod
    async def connect(
        self,
        loop: "asyncio.AbstractEventLoop",
        on_text: MeshTextCallback,
        on_reaction: MeshReactionCallback,
    ) -> None:
        """Open the underlying connection and start dispatching events."""

    @abstractmethod
    def invalidate_connection(self) -> None:
        """Tear down any active connection. Idempotent."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        ...

    def close(self) -> None:
        self.invalidate_connection()

    # --- Sending ------------------------------------------------------------

    @abstractmethod
    async def asend_text(self, action: SendMeshAction) -> object:
        """Send a text message. Returns a backend-specific result object."""

    @abstractmethod
    async def asend_reaction(self, action: SendMeshReactionAction) -> object:
        """Send a reaction. May be a no-op if the backend has no reaction support."""

    async def wait_for_ack(self) -> None:
        """Block until the next ACK is received. Default no-op."""

    # --- Metadata -----------------------------------------------------------

    @property
    def payload_limit(self) -> int:
        """Maximum on-air payload size in bytes (used by chunking)."""
        return 233

    @abstractmethod
    def resolve_sender_label(
        self,
        from_id: Optional[str],
        from_num: Optional[int] = None,
    ) -> str:
        ...

    def refresh_local_node_id(self) -> None:
        """Refresh ``local_node_id`` from the live device. Default no-op."""

    # --- Helpers ------------------------------------------------------------

    @staticmethod
    def extract_packet_id(sent_result: object) -> Optional[MeshPacketRef]:
        """Pull a packet identifier out of a backend-specific send result."""
        if sent_result is None:
            return None
        if isinstance(sent_result, (int, str)):
            return sent_result
        if isinstance(sent_result, dict):
            value = sent_result.get("id")
            if isinstance(value, (int, str)):
                return value
            return None
        value = getattr(sent_result, "id", None)
        if isinstance(value, (int, str)):
            return value
        return None


def create_transport(settings: MeshgramSettings) -> MeshTransport:
    backend = settings.mesh.backend
    if backend == MESHCORE_BACKEND:
        from .meshcore import MeshCoreTransport

        return MeshCoreTransport(settings)
    if backend == MESHTASTIC_BACKEND:
        from .meshtastic import MeshtasticTransport

        return MeshtasticTransport(settings)
    raise ValueError(f"Unknown mesh backend: {backend!r}")


__all__ = [
    "MeshTransport",
    "MeshTextCallback",
    "MeshReactionCallback",
    "create_transport",
]
