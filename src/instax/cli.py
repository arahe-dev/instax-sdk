"""Command line. Every command talks to the real printer; there is no offline mode."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time

from . import extract
from .client import InstaxPrinter, Outcome, PrinterError
from .transport import Transport, TransportError


def _transport(args, on_gatt_notify=None) -> Transport:
    if args.transport == "rfcomm":
        if not args.address:
            sys.exit("the Classic link needs the printer's (EDR) address: pass --address or set INSTAX_ADDRESS. "
                     "`instax --transport ble classic-address` reads it from the printer.")
        from .rfcomm import RfcommTransport

        return RfcommTransport(args.address, args.channel)
    from .ble import DEFAULT_WRITE_SIZE, BleTransport

    return BleTransport(args.address, on_gatt_notify=on_gatt_notify, write_size=args.write_size or DEFAULT_WRITE_SIZE)


def _printer(args, **kwargs) -> InstaxPrinter:
    """A client for the real printer, recording packets to --capture if given."""
    sink = None
    if args.capture:
        fh = open(args.capture, "a", encoding="utf-8")

        def sink(event: dict) -> None:
            fh.write(json.dumps(event) + "\n")
            fh.flush()

    return InstaxPrinter(_transport(args), trace=sink, **kwargs)


async def cmd_scan(args) -> int:
    from .ble import scan

    found = await scan(args.timeout)
    for f in found:
        print(f"{f.name:28} {f.address}  rssi {f.rssi}")
    if not found:
        print("no instax printers advertising over BLE; is the printer on and not connected to a phone?")
    return 0 if found else 1


async def cmd_info(args) -> int:
    """Connect, print what the printer reports about itself and its state, disconnect."""
    async with _printer(args) as printer:
        status, battery, history = await printer.status(), await printer.battery(), await printer.history()
        info = printer.info
        print(f"{info.manufacturer} {info.model}  serial {info.serial}  firmware {info.firmware}")
        print(f"image {info.width}x{info.height}, printer limit {info.reported_max_bytes} bytes, "
              f"SDK limit {printer.max_jpeg_bytes} bytes (profile {printer.profile.model}, "
              f"{'verified' if printer.profile.verified else 'unverified'})")
        print(f"battery {battery.percent}%  charging {status.charging}  film {status.film_left}  "
              f"print state {status.print_state}  prints {history.total_prints}")
    return 0


async def cmd_classic_address(args) -> int:
    """Read the printer's Bluetooth Classic address over BLE (Link 3 characteristic FFD2)."""
    from .ble import CLASSIC_ADDRESS_CHAR, BleTransport

    transport = BleTransport(None if args.transport == "rfcomm" else args.address)
    await transport.open()
    try:
        value = bytes(await transport.client.read_gatt_char(CLASSIC_ADDRESS_CHAR))
    finally:
        await transport.close()
    address = ":".join(f"{b:02X}" for b in value[:6])
    print(address)
    print(f"set it for the Classic link with:  setx INSTAX_ADDRESS {address}", file=sys.stderr)
    return 0


async def cmd_extract(args) -> int:
    out = args.out or os.path.join("captures", time.strftime("%Y%m%d-%H%M%S"))
    windows = tuple(int(w) for w in args.windows.split(","))
    report = await extract.run(lambda notify: _transport(args, notify), out, soak_cycles=args.soak, windows=windows)
    print(extract.summary(report))
    print(f"report and packet trace in {out}")
    return 1 if "error" in report else 0


async def cmd_rfcomm_scan(args) -> int:
    if not args.address:
        sys.exit("rfcomm-scan needs --address of the (EDR) device")
    first, _, last = args.channels.partition("-")
    results = await extract.rfcomm_scan(args.address, range(int(first), int(last or first) + 1))
    for r in results:
        print(f"channel {r['channel']:2}: {r['result']} ({r['s']} s)")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=1)
    return 0


async def cmd_soak(args) -> int:
    """Connect, read status, disconnect; repeat. Prints nothing on the printer."""
    ok, durations = 0, []
    for i in range(args.cycles):
        printer = _printer(args)
        started = time.perf_counter()
        try:
            await printer.connect()
            status = await printer.status()
            elapsed = time.perf_counter() - started
            ok += 1
            durations.append(elapsed)
            print(f"{i + 1:3}: ok   {elapsed:5.1f}s  film {status.film_left}")
        except (TransportError, PrinterError, ValueError) as exc:
            print(f"{i + 1:3}: FAIL {exc}")
        finally:
            await printer.close()
        await asyncio.sleep(args.gap)
    print(f"{ok}/{args.cycles} ok" + (f"; {min(durations):.1f}-{max(durations):.1f}s" if durations else ""))
    return 0 if ok == args.cycles else 1


async def cmd_job(args) -> int:
    """dry-run uploads and cancels; print sends PRINT once."""
    from .imaging import orientation_pattern, prepare_jpeg

    execute = args.command == "print"
    async with _printer(args) as printer:
        info = printer.info
        flip = printer.profile.flip_vertical if args.flip is None else args.flip
        if args.image:
            with open(args.image, "rb") as fh:
                source = fh.read()
        else:
            source = orientation_pattern(info.width, info.height)
        jpeg = prepare_jpeg(source, info.width, info.height, printer.max_jpeg_bytes, flip_vertical=flip)
        print(f"{info.model}: {len(jpeg)} byte JPEG, flip={flip}")
        outcomes = []
        for i in range(1 if execute else args.cycles):
            started = time.perf_counter()
            outcome = await printer.print_jpeg(jpeg, execute=execute)
            outcomes.append(outcome)
            print(f"{i + 1:3}: {outcome.outcome.value} ({outcome.reason}, stage {outcome.stage}) "
                  f"{time.perf_counter() - started:.1f}s  {outcome}")
    good = {Outcome.PRINTED, Outcome.DRY_RUN}
    return 0 if all(o.outcome in good for o in outcomes) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="instax", description="instax Link printer tool (needs the printer)")
    parser.add_argument("--transport", choices=["rfcomm", "ble"], default=os.environ.get("INSTAX_TRANSPORT", "rfcomm"),
                        help="rfcomm (Bluetooth Classic, default, about 25x faster) or ble")
    parser.add_argument("--address", default=os.environ.get("INSTAX_ADDRESS"),
                        help="the (EDR) device's address for rfcomm (default $INSTAX_ADDRESS), or the (BLE) one for ble")
    parser.add_argument("--channel", type=int, default=6, help="RFCOMM channel (default 6, found by rfcomm-scan)")
    parser.add_argument("--write-size", type=int, help="bytes per BLE write (ble only; default 182)")
    parser.add_argument("--capture", help="append every packet to this JSONL file")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="list instax printers advertising over BLE")
    s.add_argument("--timeout", type=float, default=8.0)
    s.set_defaults(run=cmd_scan)

    s = sub.add_parser("info", help="what the printer reports: identity, limits, battery, film, state")
    s.set_defaults(run=cmd_info)

    s = sub.add_parser("classic-address", help="read the printer's Bluetooth Classic address over BLE (one-time setup)")
    s.set_defaults(run=cmd_classic_address)

    s = sub.add_parser("extract", help="run every non-printing experiment and save a report and packet trace")
    s.add_argument("--out", help="output folder (default captures/<time>)")
    s.add_argument("--soak", type=int, default=10, help="connect/disconnect cycles at the end")
    s.add_argument("--windows", default="1,2,4,8", help="frames in flight to time (16 overran our printer)")
    s.set_defaults(run=cmd_extract)

    s = sub.add_parser("rfcomm-scan", help="look for the instax protocol on the Classic (EDR) device's channels")
    s.add_argument("--channels", default="1-12")
    s.add_argument("--out", help="write the results as JSON")
    s.set_defaults(run=cmd_rfcomm_scan)

    s = sub.add_parser("soak", help="connect, read status and disconnect, repeatedly")
    s.add_argument("--cycles", type=int, default=10)
    s.add_argument("--gap", type=float, default=1.0, help="seconds between cycles")
    s.set_defaults(run=cmd_soak)

    for name, text in [
        ("dry-run", "upload an image without printing it; never sends PRINT"),
        ("print", "print one image (uses one sheet of film)"),
    ]:
        s = sub.add_parser(name, help=text)
        s.add_argument("image", nargs="?", help="image file; default is an orientation test pattern")
        s.add_argument("--flip", action=argparse.BooleanOptionalAction, default=None,
                       help="flip vertically before sending (default: per model)")
        if name == "dry-run":
            s.add_argument("--cycles", type=int, default=1)
        else:
            s.add_argument("--confirm-print", action="store_true", help="required: really print")
        s.set_defaults(run=cmd_job)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.command == "print" and not args.confirm_print:
        parser.error("print uses a sheet of film; add --confirm-print to really print")
    try:
        return asyncio.run(args.run(args))
    except TransportError as exc:
        print(f"printer not reachable: {exc}", file=sys.stderr)
        return 2
