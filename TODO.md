# To-do guide

What's left before instax-sdk is dependable beyond the one printer it was built on, in priority order. Each item says why it matters, what to do, and how to tell it's done.

## How to work on an item

1. **Ground every fact in hardware.**
   - The SDK has no offline mode on purpose.
   - Anything learned about the protocol goes into [PROTOCOL.md](PROTOCOL.md), with its source and status: `ours` (seen on a real printer), `agreed` (the sources agree) or `unverified`.
2. **Measure before and after.**
   - `instax extract` runs every non-printing experiment and writes `captures/<time>/report.json` and `trace.jsonl`.
   - Attach the relevant numbers to the change. Never attach the raw files: they contain the printer's serial and addresses, and the trace contains image data.
3. **Test on hardware.**
   - A behaviour isn't done until a hardware test in `tests/hardware` covers it.
   - Tests that print are marked `@pytest.mark.prints` and only run with `--allow-print`.
4. **Count the film.** A test that prints costs a sheet, so say in the change how many sheets it used.

**Status key:** ☐ open · ◐ started · ☑ done

## P0: needed for unattended use (a kiosk, a photo booth)

### ☐ 1. Print the whole size range
**Why:**
- One print has been made so far, a 16.7 KB JPEG.
- The SDK caps uploads at 55 000 bytes, a figure from community notes, while the printer reports 153 600.

**Do:** print JPEGs of about 30, 45 and 55 KB, then 80 and 150 KB. Look for corrupted or partial prints.

**Done when:**
- `MINI_LINK_3.max_jpeg_bytes` is set from evidence.
- A hardware test prints at the cap.

### ☐ 2. Shorten the pause before PRINT
**Why:** the SDK waits 1 s after `UPLOAD_END` (from community notes; one note says 300 ms). That pause is the largest part of a 1.7 s print.

**Do:** print with 0.5, 0.3 and 0 s. Watch for the printer refusing or for a warning light.

**Done when:** `pre_print_delay_s` is the smallest value that printed reliably 5 times in a row.

### ☐ 3. Disable auto power-off from the SDK
**Why:**
- The printer switches itself off after 5 minutes without use, and protocol traffic doesn't count as use.
- Today this has to be turned off in the official app.

**Do:**
- Decode `AUTO_SLEEP` (`01 02`). The two community sources disagree on its layout.
- One source describes a read-only "get current setting" mode. Try that first and compare the result with the app's setting.
- Only then add a setter, behind an explicit flag.

**Done when:**
- `instax settings auto-off` can read the setting and turn it off.
- The setting survives a restart.
- A hardware test checks it.

### ☐ 4. Recover a "stuck" printer without a power cycle
**Why:** after a link dropped mid-upload, every upload command answered `133` until the printer was switched off and on. The SDK reports this as `printer_stuck`.

**Do:**
1. Find out whether `UPLOAD_CANCEL`, a fresh `UPLOAD_START` after a delay, or `RESET` (`01 01`, meaning undocumented) clears it.
2. Document what `RESET` also resets before using it.

**Done when:** the SDK recovers by itself, or the docs say a power cycle is the only fix.

### ☐ 5. Recover from an overrun
**Why:**
- 16 frames in flight overran the printer, which then stopped answering.
- Zero bytes (`resync()`) brought replies back, but they arrived one message late until a power cycle.

**Do:**
1. Try padding each command with trailing zero bytes while the printer is in that state.
2. Find the in-flight limit precisely (8 works, 16 fails).

**Done when:** the SDK either avoids the state for certain or recovers from it, and a hardware test shows which.

### ☐ 6. Keep a long-lived connection healthy
**Why:** a kiosk keeps one connection open for hours, and the drops it will meet are untested.

**Do:**
1. Add an optional keep-alive (a status query every N seconds) that notices a dropped link.
2. Reconnect with backoff.
3. Test while walking out of range, switching the printer off and on, and sleeping the PC.

**Done when:** a 2-hour soak with induced drops ends connected and able to print.

### ☐ 7. Prove an interrupted print never prints twice
**Why:**
- The SDK never resends PRINT after an `uncertain` outcome, but the printer side is untested.
- If the link drops just after PRINT, does the job still come out, and only once?

**Do:** drop the link at points after PRINT and record what comes out. This needs film.

**Done when:** PROTOCOL.md records the behaviour, and the `uncertain` advice is confirmed or changed.

## P1: quality and portability

### ☐ 8. Battery and charging readings
- The percentage read 95 % shortly before the battery ran out, and 100 % while charging.
- `ADDITIONAL_INFO` kind 0 looks like voltage and temperature.

**Do:** log both over a full discharge, then decide which to expose as "battery".

### ☐ 9. Confirm the result codes
**Why:** the community sources disagree on the codes from 176 to 181.

**Do:**
- Provoke what can be provoked safely: cover open with no film loaded, PRINT with no film, PRINT while busy.
- Record the codes.
- Never open the film door with a partly used pack: it exposes the film.

### ☐ 10. Status fields
- Byte 4 of the status reply follows the estimated print time of the image in the buffer.
- The second history counter (2, then 16, then 18) is unexplained.

**Do:** confirm or rule out both, then name the fields.

### ☐ 11. Faster BLE
**Why:** BLE takes about 780 ms per 900-byte frame against 15–31 ms over Classic.

**Do:**
- Try writes with response.
- Ask Windows for a shorter connection interval.
- Change the write size.

BLE matters on platforms without RFCOMM sockets.

### ☐ 12. Linux and macOS
- **Linux:** Python's RFCOMM sockets work on BlueZ, and bleak works. Run the hardware tests there.
- **macOS:** Python has no RFCOMM sockets, so it's BLE only, or IOBluetooth through PyObjC. Decide and document.

### ☐ 13. Lint and types in CI
Add `ruff` and `mypy --strict` for `src/` to the workflow, and fix what they find.

## P2: wider scope

### ☐ 14. Other Link models
**Why:** mini Link 1/2, SQUARE Link and Link WIDE use the same framing according to the community sources, but none has been tested.

**Do:** owners of those printers can run `instax extract` and share the summary, then add a profile per model.

### ☐ 15. Print modes
The official app sends a colour table (`30 01`) before each print, which picks Rich or Natural. Capture the tables and decide whether to support them.

### ☐ 16. Packaging
- Check the PyPI name.
- Add a changelog.
- Publish 0.1.0 once items 1–3 are done.

## Sharing a hardware report

1. Run `instax extract`, and `instax rfcomm-scan` if Classic is new on that model.
2. From `report.json`, keep the model, firmware, timings and result codes. Remove the serial, the Bluetooth addresses and the device name digits.
3. Open an issue with that summary, your OS and Bluetooth adapter, and how many sheets you used, if any.

**Never share `trace.jsonl` publicly.** Its upload packets contain the image.
