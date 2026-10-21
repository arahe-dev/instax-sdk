"""Bluetooth Classic transport (the printer's "(EDR)" device) over an RFCOMM socket.

Our mini Link 3 speaks the instax protocol on RFCOMM channel 6 (found by
`instax rfcomm-scan`), about 25 times faster than over BLE. Windows added the
(EDR) device on the first connection without a prompt.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading

from .protocol import IN_HEADER, FrameReader
from .transport import Transport, TransportError

log = logging.getLogger(__name__)


class RfcommTransport(Transport):
    def __init__(self, address: str, channel: int = 6, *, connect_timeout: float = 15.0,
                 connect_attempts: int = 3) -> None:
        super().__init__()
        self._connect_attempts = connect_attempts
        if not hasattr(socket, "AF_BLUETOOTH"):
            raise TransportError("this Python build has no Bluetooth socket support")
        self._address = address
        self._channel = channel
        self._connect_timeout = connect_timeout
        self._sock: socket.socket | None = None
        self._reader = FrameReader(IN_HEADER)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closing = False

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    async def open(self) -> None:
        """Connect, retrying: Windows briefly refuses (WinError 10051/10060) while an earlier link closes."""
        for attempt in range(1, self._connect_attempts + 1):
            try:
                await self._open_once()
                return
            except TransportError as exc:
                if attempt == self._connect_attempts:
                    raise
                log.warning("RFCOMM connect attempt %d failed: %s", attempt, exc)
                await asyncio.sleep(2.0 * attempt)

    async def _open_once(self) -> None:
        self._loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        sock.settimeout(self._connect_timeout)
        try:
            await asyncio.to_thread(sock.connect, (self._address, self._channel))
        except OSError as exc:
            sock.close()
            raise TransportError(
                f"RFCOMM connect to {self._address} channel {self._channel} failed: {exc}"
            ) from exc
        sock.settimeout(None)
        self._closing = False
        self._reader.reset()
        self._sock = sock
        self._thread = threading.Thread(target=self._read_loop, args=(sock,), name="instax-rfcomm", daemon=True)
        self._thread.start()
        log.info("RFCOMM connected to %s channel %d", self._address, self._channel)

    def _read_loop(self, sock: socket.socket) -> None:
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                self._call(self._feed, data)
        except OSError:
            pass
        finally:
            if not self._closing:
                self._call(self._on_lost)

    def _call(self, fn, *args) -> None:
        try:
            self._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            pass  # the event loop has already closed

    def _feed(self, data: bytes) -> None:
        for packet in self._reader.feed(data):
            self._deliver(packet)

    def _on_lost(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()
            log.warning("printer disconnected")
            self._lost()

    async def send(self, packet: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise TransportError("not connected")
        try:
            await asyncio.to_thread(sock.sendall, packet)
        except OSError as exc:
            raise TransportError(f"RFCOMM send failed: {exc}") from exc

    async def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        self._closing = True
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 2.0)
