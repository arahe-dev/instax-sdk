"""Transports move whole protocol packets between the client and a printer."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable


class TransportError(ConnectionError):
    """The link to the printer could not be opened, or a send failed."""


class Transport(ABC):
    def __init__(self) -> None:
        self._on_packet: Callable[[bytes], None] | None = None
        self._on_disconnect: Callable[[], None] | None = None

    def bind(self, on_packet: Callable[[bytes], None], on_disconnect: Callable[[], None]) -> None:
        """Set where whole inbound packets and link loss are reported."""
        self._on_packet = on_packet
        self._on_disconnect = on_disconnect

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def send(self, packet: bytes) -> None:
        """Send one complete protocol packet; the transport splits it if it must."""

    def _deliver(self, packet: bytes) -> None:
        if self._on_packet:
            self._on_packet(packet)

    def _lost(self) -> None:
        if self._on_disconnect:
            self._on_disconnect()
