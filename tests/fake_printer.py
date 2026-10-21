"""A fake printer for unit tests only; the SDK itself always talks to a real printer.

Its read-only replies are the ones our mini Link 3 (FI033, firmware 0102) gave
on 2026-10-19. Upload and print behaviour follows PROTOCOL.md and stays an
assumption until the extraction runs and a real print confirm it.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, field

from instax.protocol import (
    PRINT_STATE_IDLE,
    PRINT_STATE_PROCESSING,
    DeviceInfo,
    FunctionInfo,
    Op,
    decode_request,
    encode_reply,
)
from instax.transport import Transport, TransportError

UNSUPPORTED = 128
PARAMETER_ERROR = 129
SEQUENCE_ERROR = 130
NO_FILM = 178  # what a real printer answered to PRINT with no film (community capture)
NOW_PRINTING = 194
STUCK = 133

# Recorded from our printer.
VERSION_DATA = bytes.fromhex("01 00 02 00 00 00 00 00")
BATTERY_DATA = bytes.fromhex("01 03 5f 00 10")


@dataclass
class FakeConfig:
    model: str = "FI033"
    serial: str = "70000000"  # the real printer's serial is not published
    firmware: str = "0102"
    film_left: int = 10
    width: int = 600
    height: int = 800
    reported_max_bytes: int = 153_600
    frame_size: int = 900
    end_time_s: int = 16  # the estimate sent back in the PRINT reply
    print_seconds: float = 0.05  # how long the fake print takes
    # Faults.
    fail_open: bool = False
    silent_ops: set[int] = field(default_factory=set)  # commands that never get a reply
    fail_frame: int | None = None  # this frame index is answered with an error
    refuse_print_code: int = 0  # PRINT is answered with this result code
    drop_after_print: bool = False  # PRINT is accepted, then the link drops before the reply
    print_without_film_drop: bool = False  # the print "finishes" but the film count never drops
    no_frame_size: bool = False  # UPLOAD_START reply carries no frame size
    stuck: bool = False  # every upload command answers 133, as ours did after a link dropped mid-upload


class FakePrinter(Transport):
    def __init__(self, config: FakeConfig | None = None) -> None:
        super().__init__()
        self.config = config or FakeConfig()
        self.requests: list[tuple[int, bytes]] = []
        self.prints = 0  # physical prints started
        self.flushed = 0  # zero bytes received from resync()
        self.film_left = self.config.film_left
        self._open = False
        self._printing = False
        self._uploading = False
        self._uploaded = False
        self._expected = 0
        self._received = 0
        self._next_frame = 0

    @property
    def is_open(self) -> bool:
        return self._open

    def count(self, op: int) -> int:
        return sum(1 for sent, _ in self.requests if sent == op)

    async def open(self) -> None:
        if self.config.fail_open:
            raise TransportError("fake: no instax printer found")
        self._open = True

    async def close(self) -> None:
        self._open = False

    async def send(self, packet: bytes) -> None:
        if not self._open:
            raise TransportError("not connected")
        if not packet.startswith(b"Ab"):
            self.flushed += len(packet)  # resync bytes: the real printer discards them too
            return
        op, payload = decode_request(packet)
        self.requests.append((op, payload))
        if op in self.config.silent_ops:
            return
        reply = self._handle(op, payload)
        if reply is not None:
            self._later(0, self._reply, reply)

    def _later(self, delay: float, fn, *args) -> None:
        loop = asyncio.get_running_loop()
        if delay:
            loop.call_later(delay, fn, *args)
        else:
            loop.call_soon(fn, *args)  # FIFO, like the real link; call_later ties come back in any order

    def _reply(self, packet: bytes) -> None:
        if self._open:
            self._deliver(packet)

    def _drop(self) -> None:
        if self._open:
            self._open = False
            self._lost()

    def _handle(self, op: int, payload: bytes) -> bytes | None:
        c = self.config
        if op == Op.VERSION_INFO:
            return encode_reply(op, 0, VERSION_DATA)
        if op == Op.DEVICE_INFO:
            texts = {
                DeviceInfo.MANUFACTURER: "FUJIFILM",
                DeviceInfo.MODEL: c.model,
                DeviceInfo.SERIAL: c.serial,
                DeviceInfo.HARDWARE: "0000",
                DeviceInfo.FIRMWARE: c.firmware,
                DeviceInfo.SOFTWARE: "0003",
            }
            kind = payload[0] if payload else -1
            if kind not in texts:
                return encode_reply(op, PARAMETER_ERROR)
            text = texts[kind].encode("ascii")
            return encode_reply(op, 0, bytes([kind, len(text)]) + text)
        if op == Op.FUNCTION_INFO:
            return self._function_info(op, payload[0] if payload else -1)
        if c.stuck and op in (Op.UPLOAD_START, Op.UPLOAD_DATA, Op.UPLOAD_END, Op.UPLOAD_CANCEL):
            return encode_reply(op, STUCK)
        if op == Op.UPLOAD_START:
            return self._upload_start(op, payload)
        if op == Op.UPLOAD_DATA:
            return self._upload_data(op, payload)
        if op == Op.UPLOAD_END:
            if not self._uploading or self._received < self._expected:
                return encode_reply(op, SEQUENCE_ERROR)
            self._uploading, self._uploaded = False, True
            return encode_reply(op, 0)
        if op == Op.UPLOAD_CANCEL:
            self._uploading = self._uploaded = False
            return encode_reply(op, 0)
        if op == Op.PRINT:
            return self._print(op)
        return encode_reply(op, UNSUPPORTED)

    def _function_info(self, op: int, kind: int) -> bytes:
        c = self.config
        if kind == FunctionInfo.IMAGE_SUPPORT:
            data = struct.pack(">BHHBBI", kind, c.width, c.height, 2, 0x7B, c.reported_max_bytes) + bytes(4)
        elif kind == FunctionInfo.BATTERY:
            data = BATTERY_DATA
        elif kind == FunctionInfo.PRINTER_STATUS:
            film = (self.film_left & 0x0F) | 0x30
            state = PRINT_STATE_PROCESSING if self._printing else PRINT_STATE_IDLE
            data = bytes([kind, film, 0x02 if self._printing else 0, state, 0x0B]) + bytes(4)
        elif kind == FunctionInfo.PRINT_HISTORY:
            data = struct.pack(">BII", kind, self.prints, 2)
        else:
            return encode_reply(op, PARAMETER_ERROR)
        return encode_reply(op, 0, data)

    def _upload_start(self, op: int, payload: bytes) -> bytes:
        if len(payload) != 8:
            return encode_reply(op, PARAMETER_ERROR)
        if self._printing:
            return encode_reply(op, NOW_PRINTING)
        size = struct.unpack_from(">I", payload, 4)[0]
        if not 0 < size <= self.config.reported_max_bytes:
            return encode_reply(op, PARAMETER_ERROR)
        self._uploading, self._uploaded = True, False
        self._expected, self._received, self._next_frame = size, 0, 0
        if self.config.no_frame_size:
            return encode_reply(op, 0)
        return encode_reply(op, 0, struct.pack(">I", self.config.frame_size))

    def _upload_data(self, op: int, payload: bytes) -> bytes:
        if not self._uploading or len(payload) < 4:
            return encode_reply(op, SEQUENCE_ERROR)
        index = struct.unpack_from(">I", payload)[0]
        frame = payload[4:]
        if index == self.config.fail_frame:
            self._uploading = False
            return encode_reply(op, PARAMETER_ERROR)
        if index != self._next_frame or len(frame) != self.config.frame_size:
            return encode_reply(op, SEQUENCE_ERROR)
        self._next_frame += 1
        self._received += len(frame)
        return encode_reply(op, 0, struct.pack(">I", index))

    def _print(self, op: int) -> bytes | None:
        c = self.config
        if not self._uploaded:
            return encode_reply(op, SEQUENCE_ERROR)
        if self._printing:
            return encode_reply(op, NOW_PRINTING)
        if self.film_left == 0:
            return encode_reply(op, NO_FILM)
        if c.refuse_print_code:
            return encode_reply(op, c.refuse_print_code)
        self._uploaded = False
        self._printing = True
        self.prints += 1
        self._later(c.print_seconds, self._finish_print)
        if c.drop_after_print:
            self._later(0, self._drop)
            return None
        return encode_reply(op, 0, bytes([c.end_time_s]))

    def _finish_print(self) -> None:
        self._printing = False
        if not self.config.print_without_film_drop:
            self.film_left -= 1
