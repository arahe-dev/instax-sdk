"""Data extraction against the real printer: everything except printing.

Each experiment sends only what it says and records every packet. Uploads are
always cancelled. PRINT, and every command that changes a printer setting, is
blocked here (SafePrinter), so no film is used and nothing persists.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from collections.abc import Callable
from dataclasses import asdict, replace

from . import protocol as p
from .client import InstaxPrinter, PrinterError
from .transport import Transport, TransportError

BLOCKED = frozenset({p.Op.PRINT, p.Op.SHUT_DOWN, p.Op.RESET, p.Op.AUTO_SLEEP, p.Op.BLE_CONNECT, p.Op.EJECT_FILM_COVER})


class SafePrinter(InstaxPrinter):
    """An InstaxPrinter that refuses to send PRINT or any settings command."""

    async def request(self, op: int, payload: bytes = b"", *, timeout: float | None = None) -> p.Reply:
        if op in BLOCKED:
            raise PermissionError(f"{p.op_name(op)} is blocked during extraction")
        return await super().request(op, payload, timeout=timeout)


async def _raw(printer: InstaxPrinter, op: int, payload: bytes = b"") -> dict:
    """Send one command and describe the reply; never raises for printer errors."""
    loop = asyncio.get_running_loop()
    packet = p.encode(op, payload)
    shown = packet if len(packet) <= 48 else packet[:24]
    entry: dict = {"request": shown.hex(" ") + ("" if shown is packet else f" ... ({len(packet)} bytes)")}
    started = loop.time()
    try:
        reply = await printer.request(op, payload)
        entry.update(result=reply.result, result_name=reply.result_name, data=reply.data.hex(" "), reply=reply.raw.hex(" "))
    except PrinterError as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"
    entry["ms"] = round((loop.time() - started) * 1000)
    return entry


def _jsonable(value):
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if isinstance(value, (bytes, bytearray)):
        return value.hex(" ")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


async def _queries(printer: InstaxPrinter) -> dict:
    out = {"version": await _raw(printer, p.Op.VERSION_INFO)}
    for kind in range(9):  # 0-5 are known; 6-8 are listed by one source (system id, regulatory, PnP)
        out[f"device_info.{kind}"] = await _raw(printer, p.Op.DEVICE_INFO, bytes([kind]))
    for kind in range(6):  # 4 and 5 are camera info in one source; the printer may refuse them
        out[f"function_info.{kind}"] = await _raw(printer, p.Op.FUNCTION_INFO, bytes([kind]))
    for kind in range(2):
        out[f"additional_info.{kind}"] = await _raw(printer, p.Op.ADDITIONAL_INFO, bytes([kind]))
    return out


async def _status_series(printer: InstaxPrinter, count: int, gap: float) -> list[dict]:
    series = []
    for _ in range(count):
        series.append({
            "status": await _raw(printer, p.Op.FUNCTION_INFO, bytes([p.FunctionInfo.PRINTER_STATUS])),
            "battery": await _raw(printer, p.Op.FUNCTION_INFO, bytes([p.FunctionInfo.BATTERY])),
        })
        await asyncio.sleep(gap)
    return series


def _test_images(width: int, height: int, cap: int) -> dict[str, bytes]:
    from PIL import Image

    from .imaging import orientation_pattern, prepare_jpeg

    noise = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    return {
        "pattern": prepare_jpeg(orientation_pattern(width, height), width, height, cap),
        "noise_near_cap": prepare_jpeg(noise, width, height, cap),
    }


async def _upload_timing(printer: InstaxPrinter, images: dict[str, bytes], gaps: list[float]) -> list[dict]:
    runs = []
    base = printer.profile
    for name, jpeg in images.items():
        for gap in gaps if name == "pattern" else gaps[:1]:
            printer.profile = replace(base, packet_gap_s=gap)
            acks: list[float] = []
            run = {"image": name, "bytes": len(jpeg), "gap_s": gap}
            started = time.perf_counter()
            try:
                run["frames"] = await printer.upload(jpeg, on_frame=lambda _i, s: acks.append(s))
                run["result"] = "uploaded"
            except (PrinterError, ValueError) as exc:
                run["result"] = f"{type(exc).__name__}: {exc}"
            run["seconds"] = round(time.perf_counter() - started, 2)
            if acks:
                run["ack_ms"] = {
                    "min": round(min(acks) * 1000), "median": round(statistics.median(acks) * 1000),
                    "max": round(max(acks) * 1000),
                }
            run["cancel"] = await _raw(printer, p.Op.UPLOAD_CANCEL)
            run["status_after"] = await _raw(printer, p.Op.FUNCTION_INFO, bytes([p.FunctionInfo.PRINTER_STATUS]))
            runs.append(run)
    printer.profile = base
    return runs


def _ack_stats(acks: list[float]) -> dict:
    return {"min": round(min(acks) * 1000), "median": round(statistics.median(acks) * 1000),
            "max": round(max(acks) * 1000)}


async def _window_timing(printer: InstaxPrinter, jpeg: bytes, windows: list[int]) -> list[dict]:
    """Upload the same image with more and more frames in flight; stop at the first failure."""
    runs = []
    base = printer.profile
    printer.profile = replace(base, packet_gap_s=0.0)
    try:
        for window in windows:
            acks: list[float] = []
            run = {"window": window, "bytes": len(jpeg)}
            started = time.perf_counter()
            try:
                run["frames"] = await printer.upload(jpeg, window=window, on_frame=lambda _i, s: acks.append(s))
                run["result"] = "uploaded"
            except (PrinterError, ValueError) as exc:
                run["result"] = f"{type(exc).__name__}: {exc}"
            run["seconds"] = round(time.perf_counter() - started, 3)
            if acks:
                run["ack_ms"] = _ack_stats(acks)
            run["status_after"] = await _raw(printer, p.Op.FUNCTION_INFO, bytes([p.FunctionInfo.PRINTER_STATUS]))
            runs.append(run)
            if run["result"] != "uploaded":
                break
    finally:
        printer.profile = base
    return runs


async def _cycle_timing(printer: InstaxPrinter, jpeg: bytes, window: int, count: int = 3) -> list[dict]:
    """Time what one print does before PRINT: status, upload, status (PRINT itself is blocked here)."""
    base = printer.profile
    printer.profile = replace(base, packet_gap_s=0.0, upload_window=window)
    out = []
    try:
        for _ in range(count):
            started = time.perf_counter()
            entry = {"window": window, "bytes": len(jpeg)}
            try:
                await printer.status()
                entry["frames"] = await printer.upload(jpeg)
                await printer.status()
                entry["result"] = "ok"
            except (PrinterError, ValueError) as exc:
                entry["result"] = f"{type(exc).__name__}: {exc}"
            entry["seconds"] = round(time.perf_counter() - started, 3)
            out.append(entry)
    finally:
        printer.profile = base
    return out


def _best_window(runs: list[dict] | None) -> int:
    ok = [r for r in runs or [] if r.get("result") == "uploaded"]
    return min(ok, key=lambda r: r["seconds"])["window"] if ok else 1


async def _size_limits(printer: InstaxPrinter) -> list[dict]:
    reported = printer.info.reported_max_bytes or 153_600
    out = []
    for size in sorted({55_000, 100_000, reported, reported + 1, 1_000_000}):
        out.append({"size": size, "start": await _raw(printer, p.Op.UPLOAD_START, p.upload_start_payload(size)),
                    "cancel": await _raw(printer, p.Op.UPLOAD_CANCEL)})
    return out


async def _sequence_errors(printer: InstaxPrinter, frame_size: int) -> dict:
    zero_frame = bytes(frame_size)
    two_frames = p.upload_start_payload(2 * frame_size)
    out = {"cancel_with_nothing_uploaded": await _raw(printer, p.Op.UPLOAD_CANCEL),
           "end_without_start": await _raw(printer, p.Op.UPLOAD_END),
           "data_without_start": await _raw(printer, p.Op.UPLOAD_DATA, p.upload_data_payload(0, zero_frame))}
    await _raw(printer, p.Op.UPLOAD_CANCEL)
    out["skip_frame_0"] = [await _raw(printer, p.Op.UPLOAD_START, two_frames),
                           await _raw(printer, p.Op.UPLOAD_DATA, p.upload_data_payload(1, zero_frame)),
                           await _raw(printer, p.Op.UPLOAD_CANCEL)]
    out["end_after_1_of_2_frames"] = [await _raw(printer, p.Op.UPLOAD_START, two_frames),
                                      await _raw(printer, p.Op.UPLOAD_DATA, p.upload_data_payload(0, zero_frame)),
                                      await _raw(printer, p.Op.UPLOAD_END),
                                      await _raw(printer, p.Op.UPLOAD_CANCEL)]
    out["short_frame"] = [await _raw(printer, p.Op.UPLOAD_START, two_frames),
                          await _raw(printer, p.Op.UPLOAD_DATA, p.upload_data_payload(0, zero_frame[:100])),
                          await _raw(printer, p.Op.UPLOAD_CANCEL)]
    return out


async def run(
    make_transport: Callable[[Callable[[str, bytes], None] | None], Transport],
    out_dir: str,
    *,
    soak_cycles: int = 10,
    gaps: tuple[float, ...] = (0.075, 0.05, 0.025, 0.0),
    windows: tuple[int, ...] = (1, 2, 4, 8, 16, 64),
) -> dict:
    """Run every experiment; write report.json and trace.jsonl to out_dir."""
    os.makedirs(out_dir, exist_ok=True)
    trace_file = open(os.path.join(out_dir, "trace.jsonl"), "w", encoding="utf-8")

    def sink(event: dict) -> None:
        trace_file.write(json.dumps(event) + "\n")
        trace_file.flush()  # keep the trace even if the run dies

    report: dict = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "errors": {}}
    printer: SafePrinter | None = None

    def gatt_notify(uuid: str, value: bytes) -> None:
        if printer:
            printer.trace("gatt_notify", data=value, uuid=uuid)

    async def step(name: str, make) -> None:
        """Run one experiment; a failure is recorded and the next experiment still runs."""
        if not printer.connected:
            report["errors"][name] = "skipped: printer not connected"
            return
        try:
            report[name] = await make()
        except Exception as exc:
            report["errors"][name] = f"{type(exc).__name__}: {exc}"
            printer.trace("experiment_failed", name=name, error=str(exc))

    try:
        printer = SafePrinter(make_transport(gatt_notify), trace=sink)
        started = time.perf_counter()
        info = await printer.connect()
        report["connect_s"] = round(time.perf_counter() - started, 2)
        report["info"] = _jsonable(info)
        report["profile"] = _jsonable(printer.profile)
        transport = printer.transport
        snapshot = getattr(transport, "snapshot", None)
        if snapshot:
            await step("gatt_first", snapshot)
        await step("queries", lambda: _queries(printer))
        await step("status_series", lambda: _status_series(printer, 3, 2.0))
        images = _test_images(info.width, info.height, printer.max_jpeg_bytes)
        await step("upload_timing", lambda: _upload_timing(printer, images, list(gaps)))
        await step("window_timing", lambda: _window_timing(printer, images["noise_near_cap"], list(windows)))
        await step("cycle_timing", lambda: _cycle_timing(printer, images["noise_near_cap"], printer.profile.upload_window))
        await step("size_limits", lambda: _size_limits(printer))
        await step("sequence_errors", lambda: _sequence_errors(printer, 900))
        await step("status_end", lambda: _raw(printer, p.Op.FUNCTION_INFO, bytes([p.FunctionInfo.PRINTER_STATUS])))
        if snapshot:
            await step("gatt_last", snapshot)
        report["unsolicited"] = [r.raw.hex(" ") for r in printer.unsolicited]
    except (TransportError, PrinterError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if printer:
            await printer.close()

    soak = []
    for i in range(soak_cycles):
        cycle = SafePrinter(make_transport(None), trace=sink)
        started = time.perf_counter()
        try:
            await cycle.connect()
            status = await cycle.status()
            soak.append({"ok": True, "s": round(time.perf_counter() - started, 2), "film": status.film_left})
        except (TransportError, PrinterError, p.ProtocolError) as exc:
            soak.append({"ok": False, "s": round(time.perf_counter() - started, 2), "error": str(exc)})
        finally:
            await cycle.close()
        await asyncio.sleep(1.0)
    report["soak"] = soak
    report["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    trace_file.close()
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    return report


async def rfcomm_scan(address: str, channels: range, *, timeout: float = 8.0) -> list[dict]:
    """Try each RFCOMM channel on the Classic device and see whether it speaks the instax protocol."""
    from .rfcomm import RfcommTransport

    out = []
    for channel in channels:
        entry: dict = {"channel": channel}
        printer = SafePrinter(RfcommTransport(address, channel, connect_timeout=timeout, connect_attempts=1),
                              reply_timeout=3.0)
        started = time.perf_counter()
        try:
            info = await asyncio.wait_for(printer.connect(), timeout + 10)
            entry.update(result="instax protocol", info=_jsonable(info))
        except TimeoutError:
            entry["result"] = "connect timed out"
        except TransportError as exc:
            entry["result"] = f"no connection: {exc}"
        except PrinterError as exc:
            entry["result"] = f"connected, but not the instax protocol: {exc}"
        finally:
            await printer.close()
        entry["s"] = round(time.perf_counter() - started, 2)
        out.append(entry)
    return out


def summary(report: dict) -> str:
    lines = []
    if "error" in report:
        lines.append(f"stopped early: {report['error']}")
    info = report.get("info") or {}
    if info:
        lines.append(f"printer {info.get('model')} fw {info.get('firmware')} serial {info.get('serial')}, "
                     f"{info.get('width')}x{info.get('height')}, reported max {info.get('reported_max_bytes')} bytes; "
                     f"connect {report.get('connect_s')} s")
    for run in report.get("upload_timing", []):
        lines.append(f"upload {run['image']} {run['bytes']} B gap {run['gap_s']}s: {run.get('result')} "
                     f"{run.get('frames', '?')} frames in {run['seconds']} s, ack {run.get('ack_ms')}")
    for run in report.get("window_timing", []):
        lines.append(f"window {run['window']:2}: {run.get('result')} {run.get('frames', '?')} frames "
                     f"in {run['seconds']} s, ack {run.get('ack_ms')}")
    for run in report.get("cycle_timing", []):
        lines.append(f"cycle (status+upload+status, window {run['window']}): {run.get('result')} in {run['seconds']} s")
    for limit in report.get("size_limits", []):
        start = limit["start"]
        lines.append(f"START {limit['size']} B: result {start.get('result', start.get('error'))} data {start.get('data', '')}")
    soak = report.get("soak", [])
    if soak:
        ok = [c for c in soak if c["ok"]]
        times = [c["s"] for c in ok]
        lines.append(f"soak {len(ok)}/{len(soak)} ok" + (f", {min(times)}-{max(times)} s" if times else ""))
    return "\n".join(lines)
