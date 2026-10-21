# instax-sdk

A Python client for **instax Link** printers over Bluetooth, written from the protocol up and tested against a real **instax mini Link 3**. On Windows, over the printer's Bluetooth Classic link, a 600 × 800 photo goes from "print" to the printer accepting the job in about **1.7 s**.

> Not affiliated with or endorsed by FUJIFILM. "instax" is a trademark of FUJIFILM Corporation. This is an independent implementation of the printer's Bluetooth protocol.

## Status

The project is early (0.x) but works on real hardware. One mini Link 3 (model `FI033`, firmware `0102`) has been probed, soak-tested and has printed with the default settings.

- **Every protocol fact** the code relies on is in [PROTOCOL.md](PROTOCOL.md), with its source and whether it was confirmed on a real printer.
- **What's left to do** is in [TODO.md](TODO.md).

| | Confirmed on a real printer |
|---|---|
| mini Link 3 over Bluetooth Classic (RFCOMM channel 6), Windows 11 | Yes: identity, status, upload, print, completion |
| mini Link 3 over BLE, Windows 11 | Queries and upload work, but it is about 25 times slower than Classic |
| Other Link models (mini Link 1/2, SQUARE Link, Link WIDE) | No. They fall back to a generic profile |
| Linux, macOS | Not tested yet (see TODO.md) |

## Design

- **The printer is the source of truth.** `InstaxPrinter.connect()` reads the model, firmware, image size and size limit from the printer every time, and fails rather than guessing. There is no simulator in the package.
- **Clear print outcomes.** `print_jpeg()` returns `printed`, `refused` or `uncertain`:
  - **`refused`:** nothing can have printed, so it is safe to retry.
  - **`uncertain`:** the print command was sent but not confirmed. The job may still print, so the SDK never sends it again.
- **Images stay in memory.** Nothing touches the disk.
- **Only behaviour the printer doesn't report about itself lives in `profiles.py`:** the gap between chunks, how many chunks are in flight, the pause before printing, the flip and a working size cap. Each value says whether a real print has confirmed it.

## Install

```powershell
git clone https://github.com/arahe-dev/instax-sdk
cd instax-sdk
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[imaging]"
```

**Requirements:**
- **Python 3.11+.**
- **[bleak](https://github.com/hbldh/bleak)** (MIT) for BLE.
- **[Pillow](https://python-pillow.org)** (MIT-CMU), optional, for image preparation.
- **The Python standard library**, for Bluetooth Classic.

## Quick start

The Classic link needs the printer's Classic (EDR) address. Read it once over BLE:

```powershell
instax --transport ble classic-address     # prints e.g. 88:B4:36:XX:XX:XX
setx INSTAX_ADDRESS 88:B4:36:XX:XX:XX      # or pass --address every time
```

Then:

```powershell
instax info                     # identity, size limit, battery, film left, state
instax soak --cycles 20         # connect, read status, disconnect, repeatedly
instax dry-run                  # upload a test image without printing
instax print photo.jpg --confirm-print
```

Turn the printer's auto power-off to "Off" in the official app if it must stay on. Our commands don't count as use, so the printer switches itself off after 5 minutes otherwise.

## Library

```python
import asyncio
from instax.client import InstaxPrinter, Outcome
from instax.imaging import prepare_jpeg
from instax.rfcomm import RfcommTransport

async def main(image_bytes: bytes) -> None:
    async with InstaxPrinter(RfcommTransport("88:B4:36:XX:XX:XX")) as printer:
        info = printer.info  # read from the printer on connect
        jpeg = prepare_jpeg(image_bytes, info.width, info.height, printer.max_jpeg_bytes,
                            flip_vertical=printer.profile.flip_vertical)
        outcome = await printer.print_jpeg(jpeg)
        print(outcome.outcome, outcome.reason)

asyncio.run(main(open("photo.jpg", "rb").read()))
```

**For the lowest latency:**
- **Keep the printer connected.** Call `ensure_connected()` before each job.
- **Upload early.** Call `prepare(jpeg)` as soon as the image is final, then `print_prepared()` when it should print. If the upload finished at least `pre_print_delay_s` (1 s) earlier, the print command goes out within about 50 ms.

| Outcome | Meaning | Safe to retry? |
|---|---|---|
| `printed` | The printer finished, and its film count dropped by one | n/a |
| `refused` | Failed before the print command, or the printer answered it with an error | Yes |
| `uncertain` | The print command was sent, then the link dropped, the reply was lost, or the finish was never confirmed | **No**: the job may still print |
| `dry_run` | Uploaded but not printed (`execute=False`) | n/a |

`reason` gives the detail. It is one of `no_film`, `busy`, `printer_error`, `printer_stuck` (the printer needs switching off and on), `disconnected`, `timeout`, `no_reply`, `rejected`, `not_prepared` or `not_confirmed`.

## Speed (mini Link 3, Windows 11, Bluetooth Classic)

| Step | Time |
|---|---|
| Connect and read identity | 0.5–2.5 s (once, if you keep the connection) |
| Status check | about 16 ms |
| Upload a 54 KB JPEG, 4 chunks in flight, no gap | 0.82 s |
| Pause before the print command | 1 s |
| Print command accepted | about 16 ms |
| Printing (exposure and eject) | the printer's own about 14 s, which it reports |

BLE takes about 780 ms per 900-byte chunk, so the same upload takes about 50 s. That's why Classic is the default.

## Safety rules the code keeps

- **`info`, `soak` and `extract` never print.**
  - `extract` uses a client that blocks the print command and every settings command (shut down, reset, auto-sleep, connection parameters, film-cover eject).
- **Printing always needs an explicit request:**
  - `print` refuses to run without `--confirm-print`.
  - The hardware print test runs only with `--allow-print`.
- **The print command is sent at most once per upload, and never retried.**
- **Community code was not copied.**
  - **InstaxLink (GPL-3.0):** read for protocol facts only.
  - **InstaxBLE:** not used, because it depends on simplepyble (BUSL-1.1).
  - See PROTOCOL.md for sources.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q -m "not printer"   # unit tests (no hardware)
.\.venv\Scripts\python.exe -m pytest -q                    # plus hardware tests: the printer must be on
.\.venv\Scripts\python.exe -m pytest -q --allow-print      # plus one real print (uses a sheet of film)
```

**How the tests split:**
- **Hardware tests** (`tests/hardware`) fail, rather than skip, when the printer is off.
- **Classic address:** over Classic they need `INSTAX_ADDRESS` or `--printer-address`.
- **Unit tests** use `tests/fake_printer.py`, whose read-only replies are bytes recorded from a real printer, with the serial number replaced.

## Layout

| Path | What |
|---|---|
| `src/instax/protocol.py` | framing, checksums, commands and reply layouts |
| `src/instax/client.py` | `InstaxPrinter`: identity, status, upload, `prepare` / `print_prepared` / `print_jpeg`, resync |
| `src/instax/rfcomm.py` | Bluetooth Classic transport (the default) |
| `src/instax/ble.py` | BLE transport, scan, GATT snapshot, Classic-address lookup |
| `src/instax/profiles.py` | per-model behaviour (timing, chunks in flight, flip, size cap) |
| `src/instax/imaging.py` | in-memory JPEG preparation and an orientation test pattern |
| `src/instax/extract.py` | the non-printing experiments behind `instax extract` and `rfcomm-scan` |
| `src/instax/cli.py` | the `instax` command |
| `tests/` | unit tests, the fake printer, hardware tests |

## Licence

MIT. See [LICENSE](LICENSE).
