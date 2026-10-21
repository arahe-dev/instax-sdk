"""Packet format and message codecs for the instax Link Bluetooth protocol.

Everything here follows PROTOCOL.md, which records where each fact comes from
and whether it has been checked against a real printer.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

OUT_HEADER = b"Ab"  # 0x41 0x62: client to printer
IN_HEADER = b"aB"  # 0x61 0x42: printer to client
MIN_LENGTH = 7  # header 2 + length 2 + opcode 2 + checksum 1
JPEG = 2


class ProtocolError(ValueError):
    """A packet or reply that does not follow the instax layout."""


class Op(IntEnum):
    """Command codes: function byte in the high 8 bits, operation in the low 8."""

    VERSION_INFO = 0x0000
    DEVICE_INFO = 0x0001
    FUNCTION_INFO = 0x0002
    SHUT_DOWN = 0x0100
    RESET = 0x0101
    AUTO_SLEEP = 0x0102
    BLE_CONNECT = 0x0103
    UPLOAD_START = 0x1000
    UPLOAD_DATA = 0x1001
    UPLOAD_END = 0x1002
    UPLOAD_CANCEL = 0x1003
    PRINT = 0x1080
    EJECT_FILM_COVER = 0x1081
    ADDITIONAL_INFO = 0x3010


READ_ONLY_OPS = frozenset({Op.VERSION_INFO, Op.DEVICE_INFO, Op.FUNCTION_INFO, Op.ADDITIONAL_INFO})


class DeviceInfo(IntEnum):
    MANUFACTURER = 0
    MODEL = 1
    SERIAL = 2
    HARDWARE = 3
    FIRMWARE = 4
    SOFTWARE = 5


class FunctionInfo(IntEnum):
    IMAGE_SUPPORT = 0
    BATTERY = 1
    PRINTER_STATUS = 2
    PRINT_HISTORY = 3


# Result byte names from one community decode of the official app. Another
# source labels 179-181 differently, so these are hints, not facts.
RESULT_NAMES = {
    0: "ok",
    1: "printer_busy",
    2: "camera_busy",
    127: "pending",
    128: "unsupported_command",
    129: "parameter_error",
    130: "sequence_error",
    131: "in_use",
    132: "security_code_error",
    133: "timeout",
    144: "low_power",
    145: "no_battery",
    146: "battery_overheat",
    147: "battery_charge_fault",
    176: "cover_open",
    177: "no_film",
    178: "no_paper",
    179: "image_error",
    180: "jammed",
    181: "print_fault",
    192: "memory_full",
    193: "post_view_print_error",
    194: "now_printing",
    240: "software_error",
    241: "hardware_error",
    242: "mechanical_error",
}

PRINT_STATE_IDLE = 0
PRINT_STATE_PROCESSING = 127


def op_name(op: int) -> str:
    try:
        return Op(op).name
    except ValueError:
        return f"op_{op:04x}"


def result_name(code: int) -> str:
    return RESULT_NAMES.get(code, f"unknown_{code}")


def checksum(data: bytes) -> int:
    """The byte that makes the whole packet sum to 0xFF (mod 256)."""
    return (255 - sum(data)) & 255


def _frame(header: bytes, op: int, body: bytes) -> bytes:
    length = MIN_LENGTH + len(body)
    if length > 0xFFFF:
        raise ProtocolError(f"packet too long: {length} bytes")
    packet = header + struct.pack(">HH", length, op) + body
    return packet + bytes([checksum(packet)])


def encode(op: int, payload: bytes = b"") -> bytes:
    """Build a client-to-printer packet."""
    return _frame(OUT_HEADER, op, payload)


def encode_reply(op: int, result: int, data: bytes = b"") -> bytes:
    """Build a printer-to-client packet (used by the simulator and tests)."""
    return _frame(IN_HEADER, op, bytes([result]) + data)


def _check(packet: bytes, header: bytes, minimum: int) -> None:
    if len(packet) < minimum or packet[:2] != header:
        raise ProtocolError(f"not an instax packet: {packet[:8].hex(' ')}")
    length = struct.unpack_from(">H", packet, 2)[0]
    if length != len(packet):
        raise ProtocolError(f"length field {length} != packet size {len(packet)}")
    if sum(packet) & 255 != 255:
        raise ProtocolError(f"bad checksum: {packet.hex(' ')}")


@dataclass(frozen=True)
class Reply:
    op: int
    result: int
    data: bytes
    raw: bytes

    @property
    def ok(self) -> bool:
        return self.result == 0

    @property
    def result_name(self) -> str:
        return result_name(self.result)


def decode_reply(packet: bytes) -> Reply:
    """Parse a printer-to-client packet: opcode, result byte, data."""
    _check(packet, IN_HEADER, MIN_LENGTH + 1)
    op = struct.unpack_from(">H", packet, 4)[0]
    return Reply(op=op, result=packet[6], data=bytes(packet[7:-1]), raw=bytes(packet))


def decode_request(packet: bytes) -> tuple[int, bytes]:
    """Parse a client-to-printer packet into (opcode, payload)."""
    _check(packet, OUT_HEADER, MIN_LENGTH)
    op = struct.unpack_from(">H", packet, 4)[0]
    return op, bytes(packet[6:-1])


class FrameReader:
    """Rebuilds whole packets from a byte stream that arrives in arbitrary pieces."""

    def __init__(self, header: bytes = IN_HEADER) -> None:
        self._header = header
        self._buf = bytearray()

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf += chunk
        packets = []
        while True:
            start = self._buf.find(self._header)
            if start < 0:
                # Keep the last byte in case it is the first half of a header.
                del self._buf[:-1]
                return packets
            del self._buf[:start]
            if len(self._buf) < 4:
                return packets
            length = struct.unpack_from(">H", self._buf, 2)[0]
            if length < MIN_LENGTH:
                del self._buf[:2]  # a false header inside noise; resync
                continue
            if len(self._buf) < length:
                return packets
            packets.append(bytes(self._buf[:length]))
            del self._buf[:length]


# Request payloads.

def upload_start_payload(size: int, picture_type: int = JPEG, option: int = 0) -> bytes:
    return struct.pack(">BBBBI", picture_type, option, 0, 0, size)


def upload_data_payload(index: int, frame: bytes) -> bytes:
    return struct.pack(">I", index) + frame


def split_frames(data: bytes, frame_size: int) -> list[bytes]:
    """Cut image data into frames, zero-padding the last one to full size."""
    frames = [data[i:i + frame_size] for i in range(0, len(data), frame_size)]
    if frames and len(frames[-1]) < frame_size:
        frames[-1] += bytes(frame_size - len(frames[-1]))
    return frames


# Reply data layouts.

def _expect_type(data: bytes, kind: FunctionInfo, minimum: int) -> None:
    if len(data) < minimum or data[0] != kind:
        raise ProtocolError(f"expected {kind.name} reply, got {data.hex(' ')}")


@dataclass(frozen=True)
class VersionInfo:
    functions: int
    versions: bytes

    @property
    def can_print(self) -> bool:
        return bool(self.functions & 0x01)


def parse_version_info(data: bytes) -> VersionInfo:
    if not data:
        raise ProtocolError("empty version info")
    return VersionInfo(functions=data[0], versions=bytes(data[1:]))


def parse_device_info(data: bytes) -> tuple[int, str]:
    """(kind, text) from a DEVICE_INFO reply."""
    if len(data) < 2 or len(data) < 2 + data[1]:
        raise ProtocolError(f"short device info: {data.hex(' ')}")
    return data[0], data[2:2 + data[1]].decode("ascii", "replace")


@dataclass(frozen=True)
class ImageSupport:
    width: int
    height: int
    picture_type: int
    picture_option: int
    reported_max_bytes: int | None  # the printer's claim; prints above 55 KB are untested
    raw: bytes


def parse_image_support(data: bytes) -> ImageSupport:
    _expect_type(data, FunctionInfo.IMAGE_SUPPORT, 5)
    width, height = struct.unpack_from(">HH", data, 1)
    max_bytes = struct.unpack_from(">I", data, 7)[0] if len(data) >= 11 else None
    return ImageSupport(
        width=width,
        height=height,
        picture_type=data[5] if len(data) > 5 else 0,
        picture_option=data[6] if len(data) > 6 else 0,
        reported_max_bytes=max_bytes,
        raw=bytes(data),
    )


@dataclass(frozen=True)
class Battery:
    level_code: int
    percent: int
    charger_type: int
    charger_state: int
    raw: bytes


def parse_battery(data: bytes) -> Battery:
    _expect_type(data, FunctionInfo.BATTERY, 3)
    padded = bytes(data) + bytes(max(0, 5 - len(data)))
    return Battery(padded[1], padded[2], padded[3], padded[4], bytes(data))


@dataclass(frozen=True)
class PrinterStatus:
    film_left: int
    battery_level: int  # 0-7 scale; unverified
    charging: bool
    cover_open: bool  # status bits are unverified; decisions use print_state
    operating: bool
    error: bool
    operation_info: int
    print_state: int  # 0 idle, 127 processing; anything else is a fault
    print_wait_s: int
    error_detail: int
    raw: bytes

    @property
    def busy(self) -> bool:
        return self.print_state == PRINT_STATE_PROCESSING


def parse_printer_status(data: bytes) -> PrinterStatus:
    _expect_type(data, FunctionInfo.PRINTER_STATUS, 4)
    padded = bytes(data) + bytes(max(0, 9 - len(data)))
    film, status, state, wait = padded[1], padded[2], padded[3], padded[4]
    return PrinterStatus(
        film_left=film & 0x0F,
        battery_level=(film >> 4) & 0x07,
        charging=bool(film & 0x80),
        cover_open=bool(status & 0x01),
        operating=bool(status & 0x02),
        error=bool(status & 0x04),
        operation_info=status >> 4,
        print_state=state,
        print_wait_s=wait,
        error_detail=struct.unpack_from(">I", padded, 5)[0],
        raw=bytes(data),
    )


@dataclass(frozen=True)
class PrintHistory:
    total_prints: int
    total_ejects: int
    raw: bytes


def parse_history(data: bytes) -> PrintHistory:
    _expect_type(data, FunctionInfo.PRINT_HISTORY, 9)
    prints, ejects = struct.unpack_from(">II", data, 1)
    return PrintHistory(prints, ejects, bytes(data))


def parse_frame_size(data: bytes) -> int | None:
    """Frame size the printer asks for, from the UPLOAD_START reply."""
    return struct.unpack_from(">I", data)[0] if len(data) >= 4 else None


def parse_frame_ack(data: bytes) -> int | None:
    """Frame index echoed in an UPLOAD_DATA reply."""
    return struct.unpack_from(">I", data)[0] if len(data) >= 4 else None


def parse_print_reply(data: bytes) -> int | None:
    """Estimated seconds until the print is out, from the PRINT reply."""
    return data[0] if data else None


@dataclass(frozen=True)
class Link3Status:
    film_left: int
    battery_percent: int
    charging: bool
    raw: bytes


def parse_link3_status(value: bytes) -> Link3Status:
    """The mini Link 3 FFF1 characteristic (read over GATT, not a protocol reply)."""
    if len(value) < 10:
        raise ProtocolError(f"short Link 3 status: {value.hex(' ')}")
    return Link3Status(value[0], value[8] * 100 // 200, value[9] != 0xFF, bytes(value))
