"""High-level client: identity and status, image upload, and one print with a clear outcome.

The printer is always the source of truth: connect() reads its identity and
capabilities, and nothing here works without a live link.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from . import protocol as p
from .profiles import ModelProfile, profile_for
from .transport import Transport, TransportError

log = logging.getLogger(__name__)


class PrinterError(Exception):
    """Base class for client errors."""


class NotConnected(PrinterError):
    """There is no link to the printer, or it dropped while waiting."""


class ReplyTimeout(PrinterError):
    def __init__(self, op: int) -> None:
        super().__init__(f"no reply to {p.op_name(op)}")
        self.op = op


class Refused(PrinterError):
    """The printer answered with a non-zero result code."""

    def __init__(self, reply: p.Reply) -> None:
        super().__init__(f"{p.op_name(reply.op)} refused: {reply.result_name} ({reply.result})")
        self.reply = reply


class Outcome(str, Enum):
    PRINTED = "printed"  # the printer finished and its film count dropped by one
    REFUSED = "refused"  # nothing can have printed; safe to try again
    UNCERTAIN = "uncertain"  # the job may still print; never send it again
    DRY_RUN = "dry_run"  # uploaded and cancelled, PRINT never sent


@dataclass(frozen=True)
class PrintOutcome:
    outcome: Outcome
    reason: str  # ok | no_film | busy | printer_error | disconnected | timeout | no_reply | rejected | not_confirmed
    stage: str  # precheck | upload | pre_print | print | completion
    result_code: int | None = None
    film_before: int | None = None
    film_after: int | None = None
    estimated_s: int | None = None
    print_state: int | None = None  # from the status reply, when that is what refused


@dataclass(frozen=True)
class PrinterInfo:
    """What the printer reports about itself, read on every connect."""

    manufacturer: str
    model: str
    serial: str
    firmware: str
    hardware: str
    software: str
    can_print: bool
    width: int
    height: int
    reported_max_bytes: int | None


# Codes both community decodes read as "no film", and the "busy" ones.
_NO_FILM_CODES = frozenset({177, 178})
_BUSY_CODES = frozenset({1, 194})
# Seen on our printer after a link dropped mid-upload: every upload command
# answers 133 until the printer is switched off and on.
STUCK_CODE = 133


def _refusal_reason(code: int) -> str:
    if code in _NO_FILM_CODES:
        return "no_film"
    if code in _BUSY_CODES:
        return "busy"
    if code == STUCK_CODE:
        return "printer_stuck"
    return "rejected"


@dataclass(frozen=True)
class _Prepared:
    film_before: int
    size: int
    ended_at: float  # loop time of the UPLOAD_END acknowledgement


TraceSink = Callable[[dict], None]


class InstaxPrinter:
    def __init__(
        self,
        transport: Transport,
        *,
        profile: ModelProfile | None = None,
        reply_timeout: float = 5.0,
        trace: TraceSink | None = None,
    ) -> None:
        self._transport = transport
        self._fixed_profile = profile
        self.profile = profile or profile_for(None)
        self.info: PrinterInfo | None = None
        self._reply_timeout = reply_timeout
        self._trace_sink = trace
        self._t0 = time.monotonic()
        self._lock = asyncio.Lock()
        self._pending: tuple[int, asyncio.Future[p.Reply]] | None = None
        self._acks: asyncio.Queue[p.Reply | None] | None = None  # frame acks during a windowed upload
        self._prepared: _Prepared | None = None
        self._connected = False
        self.unsolicited: deque[p.Reply] = deque(maxlen=50)

    async def __aenter__(self) -> InstaxPrinter:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def connected(self) -> bool:
        return self._connected and self._transport.is_open

    @property
    def transport(self) -> Transport:
        return self._transport

    @property
    def model(self) -> str | None:
        return self.info.model if self.info else None

    @property
    def max_jpeg_bytes(self) -> int:
        """The smaller of the model's working cap and the limit the printer reports."""
        reported = self.info.reported_max_bytes if self.info else None
        return min(self.profile.max_jpeg_bytes, reported) if reported else self.profile.max_jpeg_bytes

    def trace(self, kind: str, op: int | None = None, data: bytes = b"", **extra: object) -> None:
        """Record one event in the capture, if one was asked for."""
        if self._trace_sink:
            event = {"t": round(time.monotonic() - self._t0, 4), "kind": kind}
            if op is not None:
                event["op"] = p.op_name(op)
            if data:
                event["hex"] = data.hex(" ")
            event.update(extra)
            self._trace_sink(event)

    async def connect(self) -> PrinterInfo:
        """Open the link and read the printer's identity and capabilities.

        Raises TransportError if the printer is unreachable. If any identity
        query fails, the link is closed and the error raised: the SDK does not
        guess what the printer is.
        """
        self._t0 = time.monotonic()
        self._transport.bind(self._on_packet, self._on_disconnect)
        self.trace("connect_start", transport=type(self._transport).__name__)
        await self._transport.open()
        self._connected = True
        try:
            self.info = await self._read_info()
        except BaseException:
            await self.close()
            raise
        if self._fixed_profile is None:
            self.profile = profile_for(self.info.model)
        self.trace("connected", model=self.info.model, firmware=self.info.firmware, profile=self.profile.model)
        if not self.info.can_print:
            log.warning("printer reports no print function")
        return self.info

    async def _read_info(self) -> PrinterInfo:
        version = await self.version_info()
        text = {kind: await self.device_info(kind) for kind in p.DeviceInfo}
        support = await self.image_support()
        return PrinterInfo(
            manufacturer=text[p.DeviceInfo.MANUFACTURER],
            model=text[p.DeviceInfo.MODEL],
            serial=text[p.DeviceInfo.SERIAL],
            firmware=text[p.DeviceInfo.FIRMWARE],
            hardware=text[p.DeviceInfo.HARDWARE],
            software=text[p.DeviceInfo.SOFTWARE],
            can_print=version.can_print,
            width=support.width,
            height=support.height,
            reported_max_bytes=support.reported_max_bytes or None,
        )

    async def ensure_connected(self) -> None:
        """Reconnect if the link has dropped; cheap when it is up. Use before each job on a kept-open printer."""
        if not self.connected:
            await self.connect()

    async def close(self) -> None:
        self._connected = False
        self._prepared = None
        await self._transport.close()
        self.trace("closed")

    def _on_packet(self, packet: bytes) -> None:
        try:
            reply = p.decode_reply(packet)
        except p.ProtocolError as exc:
            self.trace("rx_bad", data=packet, error=str(exc))
            log.warning("dropping bad packet: %s", exc)
            return
        acks = self._acks
        if acks is not None and reply.op == p.Op.UPLOAD_DATA:
            self.trace("rx", reply.op, packet, result=reply.result, solicited=True)
            acks.put_nowait(reply)
            return
        pending = self._pending
        expected = bool(pending and pending[0] == reply.op and not pending[1].done())
        self.trace("rx", reply.op, packet, result=reply.result, solicited=expected)
        if expected:
            pending[1].set_result(reply)
        else:
            log.info("unsolicited %s reply, result %d", p.op_name(reply.op), reply.result)
            self.unsolicited.append(reply)

    def _on_disconnect(self) -> None:
        self._connected = False
        self._prepared = None
        self.trace("disconnected")
        if self._acks is not None:
            self._acks.put_nowait(None)
        pending = self._pending
        if pending and not pending[1].done():
            pending[1].set_exception(NotConnected("printer disconnected"))

    async def resync(self, flush_bytes: int = 1024) -> bool:
        """Flush a half-read message out of the printer, then check that it answers again.

        After an overrun our printer stopped answering anything, even on a new
        connection, until it was sent a block of zero bytes (zeros never form a
        command, so this cannot print or change anything); then it replied normally.
        """
        async with self._lock:
            if not self.connected:
                raise NotConnected("not connected")
            self.trace("resync", flush=flush_bytes)
            try:
                await self._transport.send(bytes(flush_bytes))
            except TransportError as exc:
                raise NotConnected(str(exc)) from exc
        await asyncio.sleep(0.3)
        try:
            await self._request_once(p.Op.VERSION_INFO, b"", None)
            return True
        except PrinterError:
            return False

    async def request(self, op: int, payload: bytes = b"", *, timeout: float | None = None) -> p.Reply:
        """Send one command and wait for its reply. Raises NotConnected or ReplyTimeout.

        A read-only query that times out is retried once after resync(); nothing
        else is ever resent here.
        """
        try:
            return await self._request_once(op, payload, timeout)
        except ReplyTimeout:
            if op not in p.READ_ONLY_OPS or not self.connected:
                raise
            log.warning("no reply to %s; resyncing the printer", p.op_name(op))
            if not await self.resync():
                raise
            return await self._request_once(op, payload, timeout)

    async def _request_once(self, op: int, payload: bytes, timeout: float | None) -> p.Reply:
        async with self._lock:
            if not self.connected:
                raise NotConnected("not connected")
            future: asyncio.Future[p.Reply] = asyncio.get_running_loop().create_future()
            self._pending = (op, future)
            packet = p.encode(op, payload)
            self.trace("tx", op, packet)
            try:
                try:
                    await self._transport.send(packet)
                except TransportError as exc:
                    raise NotConnected(str(exc)) from exc
                return await asyncio.wait_for(future, timeout or self._reply_timeout)
            except TimeoutError:
                self.trace("timeout", op)
                raise ReplyTimeout(op) from None
            finally:
                self._pending = None

    async def _ok(self, op: int, payload: bytes = b"") -> p.Reply:
        reply = await self.request(op, payload)
        if not reply.ok:
            raise Refused(reply)
        return reply

    # Read-only queries.

    async def version_info(self) -> p.VersionInfo:
        return p.parse_version_info((await self._ok(p.Op.VERSION_INFO)).data)

    async def device_info(self, kind: p.DeviceInfo) -> str:
        _, text = p.parse_device_info((await self._ok(p.Op.DEVICE_INFO, bytes([kind]))).data)
        return text

    async def _function_info(self, kind: p.FunctionInfo) -> bytes:
        return (await self._ok(p.Op.FUNCTION_INFO, bytes([kind]))).data

    async def image_support(self) -> p.ImageSupport:
        return p.parse_image_support(await self._function_info(p.FunctionInfo.IMAGE_SUPPORT))

    async def battery(self) -> p.Battery:
        return p.parse_battery(await self._function_info(p.FunctionInfo.BATTERY))

    async def status(self) -> p.PrinterStatus:
        return p.parse_printer_status(await self._function_info(p.FunctionInfo.PRINTER_STATUS))

    async def history(self) -> p.PrintHistory:
        return p.parse_history(await self._function_info(p.FunctionInfo.PRINT_HISTORY))

    # Upload. None of this prints: only the PRINT command does.

    def _check_size(self, jpeg: bytes) -> None:
        if len(jpeg) > self.max_jpeg_bytes:
            raise ValueError(f"image is {len(jpeg)} bytes; this printer takes at most {self.max_jpeg_bytes}")

    async def upload(
        self,
        jpeg: bytes,
        *,
        window: int | None = None,
        on_frame: Callable[[int, float], None] | None = None,
    ) -> int:
        """Send an image into the printer's buffer and return the number of frames sent.

        The frame size is the one the printer asks for in its UPLOAD_START reply.
        Up to `window` frames (default: the profile's) are sent before waiting for
        the first acknowledgement; every acknowledgement must name the right frame.
        on_frame(index, seconds) is called as each frame is acknowledged.
        """
        self._check_size(jpeg)
        self._prepared = None
        start = await self._ok(p.Op.UPLOAD_START, p.upload_start_payload(len(jpeg)))
        frame_size = p.parse_frame_size(start.data)
        if frame_size is None or not 1 <= frame_size <= 4096:
            raise PrinterError(f"printer gave no usable frame size: {start.data.hex(' ')}")
        frames = p.split_frames(jpeg, frame_size)
        await self._send_frames(frames, max(1, window or self.profile.upload_window), on_frame)
        await self._ok(p.Op.UPLOAD_END)
        return len(frames)

    async def _send_frames(self, frames: list[bytes], window: int, on_frame) -> None:
        loop = asyncio.get_running_loop()
        gap = self.profile.packet_gap_s
        async with self._lock:
            if not self.connected:
                raise NotConnected("not connected")
            acks: asyncio.Queue[p.Reply | None] = asyncio.Queue()
            self._acks = acks
            in_flight: dict[int, float] = {}  # frame index -> time sent
            next_index = 0
            try:
                while next_index < len(frames) or in_flight:
                    while next_index < len(frames) and len(in_flight) < window:
                        packet = p.encode(p.Op.UPLOAD_DATA, p.upload_data_payload(next_index, frames[next_index]))
                        self.trace("tx", p.Op.UPLOAD_DATA, packet)
                        try:
                            await self._transport.send(packet)
                        except TransportError as exc:
                            raise NotConnected(str(exc)) from exc
                        in_flight[next_index] = loop.time()
                        next_index += 1
                        if gap:
                            await asyncio.sleep(gap)
                    try:
                        reply = await asyncio.wait_for(acks.get(), self._reply_timeout)
                    except TimeoutError:
                        self.trace("timeout", p.Op.UPLOAD_DATA)
                        raise ReplyTimeout(p.Op.UPLOAD_DATA) from None
                    if reply is None:
                        raise NotConnected("printer disconnected")
                    if not reply.ok:
                        raise Refused(reply)
                    # Match by the index the printer names, so acknowledgements may come in any order.
                    acked = p.parse_frame_ack(reply.data)
                    if acked is None:
                        acked = min(in_flight)
                    if acked not in in_flight:
                        raise PrinterError(f"acknowledgement for frame {acked}, which is not in flight")
                    sent = in_flight.pop(acked)
                    if on_frame:
                        on_frame(acked, loop.time() - sent)
            finally:
                self._acks = None

    async def cancel_upload(self) -> None:
        """Drop an upload that is still in progress. Best effort; never prints.

        Our printer refuses this (130) once UPLOAD_END has been acknowledged; a
        finished upload stays in its buffer until the next UPLOAD_START replaces it.
        """
        self._prepared = None
        if not self.connected:
            return
        try:
            await self.request(p.Op.UPLOAD_CANCEL)
        except PrinterError as exc:
            log.warning("cancel failed: %s", exc)

    # One print, in two steps so the slow part can happen early.

    async def prepare(self, jpeg: bytes) -> PrintOutcome | None:
        """Check the printer and upload the image, without printing.

        Returns None when the printer is ready for print_prepared(), or a REFUSED
        outcome. Call it as soon as the image is final (for example while the
        shopper pays); nothing can print until print_prepared().
        """
        if not self.connected or self.info is None:
            raise NotConnected("connect to the printer first")
        self._check_size(jpeg)
        self._prepared = None
        try:
            before = await self.status()
        except (PrinterError, p.ProtocolError) as exc:
            return _refused_by(exc, "precheck")
        reason = _status_refusal(before)
        if reason:
            return PrintOutcome(Outcome.REFUSED, reason, "precheck", film_before=before.film_left,
                                print_state=before.print_state)
        try:
            await self.upload(jpeg)
        except (PrinterError, p.ProtocolError) as exc:
            await self.cancel_upload()
            return _refused_by(exc, "upload", before.film_left)
        self._prepared = _Prepared(before.film_left, len(jpeg), asyncio.get_running_loop().time())
        return None

    @property
    def prepared(self) -> bool:
        """True while an image uploaded by prepare() is waiting for print_prepared()."""
        return self._prepared is not None and self.connected

    async def print_prepared(self, *, completion_timeout: float = 60.0, poll_interval: float = 2.0) -> PrintOutcome:
        """Print the image prepare() uploaded on this connection, and say what happened.

        Before PRINT nothing can come out of the printer, so failures are REFUSED.
        Once PRINT is sent, a lost reply or an unconfirmed finish is UNCERTAIN: the
        job may still print, so callers must not send it again. PRINT is sent at
        most once per prepare().
        """
        prepared, self._prepared = self._prepared, None
        if prepared is None or not self.connected:
            return PrintOutcome(Outcome.REFUSED, "not_prepared", "pre_print")
        try:
            # The official app re-reads the film count between END and PRINT.
            check = await self.status()
        except (PrinterError, p.ProtocolError) as exc:
            return _refused_by(exc, "pre_print", prepared.film_before)
        reason = _status_refusal(check)
        if reason:
            return PrintOutcome(Outcome.REFUSED, reason, "pre_print", film_before=prepared.film_before,
                                print_state=check.print_state)
        loop = asyncio.get_running_loop()
        wait = prepared.ended_at + self.profile.pre_print_delay_s - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)

        # From here on the job may exist inside the printer.
        film_before = check.film_left
        try:
            reply = await self.request(p.Op.PRINT)
        except NotConnected:
            return PrintOutcome(Outcome.UNCERTAIN, "disconnected", "print", film_before=film_before)
        except ReplyTimeout:
            return PrintOutcome(Outcome.UNCERTAIN, "no_reply", "print", film_before=film_before)
        if not reply.ok:
            return PrintOutcome(Outcome.REFUSED, _refusal_reason(reply.result), "print", reply.result, film_before)
        estimated = p.parse_print_reply(reply.data)
        return await self._await_completion(film_before, estimated, completion_timeout, poll_interval)

    async def print_jpeg(
        self,
        jpeg: bytes,
        *,
        execute: bool = True,
        completion_timeout: float = 60.0,
        poll_interval: float = 2.0,
    ) -> PrintOutcome:
        """prepare() then print_prepared(). With execute=False, stop after the upload (a dry run)."""
        refused = await self.prepare(jpeg)
        if refused:
            return refused
        if not execute:
            film = self._prepared.film_before
            self._prepared = None  # the image stays in the printer's buffer until the next upload
            return PrintOutcome(Outcome.DRY_RUN, "ok", "pre_print", film_before=film, film_after=film)
        return await self.print_prepared(completion_timeout=completion_timeout, poll_interval=poll_interval)

    async def _await_completion(
        self, film_before: int, estimated: int | None, timeout: float, interval: float
    ) -> PrintOutcome:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        # Stay quiet while the printer works: one report says polling mid-print crashes some models.
        await asyncio.sleep(min(estimated or 0, timeout))
        last: p.PrinterStatus | None = None
        while True:
            try:
                last = await self.status()
            except NotConnected:
                return PrintOutcome(
                    Outcome.UNCERTAIN, "disconnected", "completion", film_before=film_before, estimated_s=estimated
                )
            except (ReplyTimeout, Refused, p.ProtocolError) as exc:
                log.info("status poll during print failed: %s", exc)
            else:
                if last.print_state == p.PRINT_STATE_IDLE and last.film_left == film_before - 1:
                    return PrintOutcome(
                        Outcome.PRINTED, "ok", "completion",
                        film_before=film_before, film_after=last.film_left, estimated_s=estimated,
                    )
                if last.print_state not in (p.PRINT_STATE_IDLE, p.PRINT_STATE_PROCESSING):
                    return PrintOutcome(
                        Outcome.UNCERTAIN, "printer_error", "completion",
                        film_before=film_before, film_after=last.film_left, estimated_s=estimated,
                        print_state=last.print_state,
                    )
            if loop.time() + interval > deadline:
                return PrintOutcome(
                    Outcome.UNCERTAIN, "not_confirmed", "completion",
                    film_before=film_before, film_after=last.film_left if last else None, estimated_s=estimated,
                )
            await asyncio.sleep(interval)


def _status_refusal(status: p.PrinterStatus) -> str | None:
    if status.film_left == 0:
        return "no_film"
    if status.print_state == p.PRINT_STATE_PROCESSING:
        return "busy"
    if status.print_state != p.PRINT_STATE_IDLE:
        return "printer_error"
    return None


def _refused_by(exc: Exception, stage: str, film_before: int | None = None) -> PrintOutcome:
    if isinstance(exc, Refused):
        code = exc.reply.result
        return PrintOutcome(Outcome.REFUSED, _refusal_reason(code), stage, code, film_before)
    if isinstance(exc, NotConnected):
        reason = "disconnected"
    elif isinstance(exc, ReplyTimeout):
        reason = "timeout"
    else:
        reason = "rejected"
    return PrintOutcome(Outcome.REFUSED, reason, stage, film_before=film_before)
