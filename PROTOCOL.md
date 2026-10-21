# instax Link protocol notes

What the SDK relies on, where each fact comes from, and how sure we are.

**Sources.**
- **S1:** [paorin/InstaxLink](https://github.com/paorin/InstaxLink) @ `b66f40f` (GPL-3.0). Field names look decoded from the official app. Only the facts are used; no code is copied.
- **S2:** [javl/InstaxBLE](https://github.com/javl/InstaxBLE) @ `804dca8` (MIT).
- **S3:** [dgwilson/ESP32-Instax-Bridge](https://github.com/dgwilson/ESP32-Instax-Bridge) @ `f068c8f`, `INSTAX_PROTOCOL.md`. It is useful, but several of its hand-typed examples have wrong checksums, and it misreads some reply fields.
- **S4:** the Fujifilm instax mini Link 3 owner's manual.
- **W:** what Windows showed for our printer on 2026-10-19.

**Status.**
- **ours:** checked on our printer.
- **agreed:** the sources agree, or a captured packet decodes cleanly.
- **unverified:** one source only, or the sources conflict.

Rows marked **ours** were checked by the read-only `instax probe` on 2026-10-19.

## Our printer (W, probe)

| | |
|---|---|
| Identity | FUJIFILM `FI033`, serial withheld, firmware `0102`, hardware `0000`, software `0003` |
| History | 0 prints, 2 film-cover ejects |
| Link | connected from Windows with no pairing; MTU 247; writes of 182 bytes |
| BLE device | `INSTAX-7XXXXXXX(BLE)` (the digits are the serial), `FA:AB:BC:XX:XX:XX` (random static) |
| Classic device | `INSTAX-7XXXXXXX(EDR)`, `88:B4:36:XX:XX:XX`; Windows created a "Standard Serial over Bluetooth link" COM port for it, so it offers the serial port profile (SPP) |
| Radio chip | Realtek (Bluetooth vendor ID 0x005D in both device IDs) |

S1 describes the same split on older models, with names ending `(IOS)` (BLE) and `(ANDROID)` (Classic, RFCOMM socket). The RFCOMM channel for the Link 3 is unknown. S1 hard-codes channel 6 for the Link WIDE.

## Measured on our printer, 2026-10-20 (ours)

The `instax extract` reports and packet traces behind these numbers stay local: they contain device identifiers and image data.

### The links

| | Classic RFCOMM, channel 6 | BLE |
|---|---|---|
| Found by | `rfcomm-scan`: only channel 6 speaks the protocol (3 and 8 connect but don't answer) | scan for `INSTAX-…(BLE)` |
| Connect + identity (9 queries) | 0.5–2.4 s | 5–9 s |
| Reply / frame acknowledgement | 15–31 ms | about 780 ms per 900-byte frame (244-byte writes didn't help) |
| 54 KB upload, 1 frame in flight, no gap | 1.69 s | about 50 s |
| Reliability | 10/10 connect cycles | 5/5 cycles; once dropped mid-upload |

Running the scan made Windows add the `(EDR)` device, with no prompt.

### Uploading over Classic (54 KB image, 61 frames)

| Gap / frames in flight | Time |
|---|---|
| 75 ms / 1 | 4.76 s |
| 0 / 1 | 1.69 s |
| 0 / 2 | 0.96 s |
| 0 / 4 | 0.82 s |
| 0 / 8 | 0.80 s |
| 0 / 16 | overran the printer (see below) |

None of these is verified for printing yet: these uploads were never printed.

**Re-run at 16:46, after a full power-down:**
- **Repeatable:** 4 in flight took 0.822 s and 8 took 0.83 s, so 8 gains nothing.
- **Pre-print sequence:** status + 54 KB upload + status took 0.84–0.86 s, three times in a row.
- **Reliability:** 10/10 connect cycles (0.67–2.26 s).
- **Hardware tests:** 3 passed; the print test was skipped (`--allow-print` not given).

**Battery and charging:**
- The battery reply reads `01 03 5f 00 10` (95 %) unplugged and `01 0b 64 03 06` (100 %, charger type 3, state 6) on USB power.
- The status film byte's bit 7 is set while charging (`0x30` becomes `0xb0`).
- The printer read 95 % hours before its battery ran out with auto power-off disabled, so the percentage may be coarse.
- `ADDITIONAL_INFO` kind 0 rose to `cd c0 01 15` while charging, consistent with voltage and temperature (27.7 °C).

### Upload rules

- **Frame size:** `UPLOAD_START` answers frame size 900.
- **Size limit:** a size of 153 600 bytes is accepted; 153 601 and above get 129.
- **Cancel:** `UPLOAD_CANCEL` works only while an upload is in progress. After `UPLOAD_END` it gets 130, and the finished image stays until the next `UPLOAD_START` replaces it.
- **Out-of-sequence commands:**
  - `UPLOAD_END` before every frame has arrived gets 130.
  - `UPLOAD_END` or `UPLOAD_CANCEL` with nothing uploaded gets 130.
- **The printer does not police frame order.** `UPLOAD_DATA` is accepted without `UPLOAD_START`, and frame 1 is accepted before frame 0. The SDK enforces the order itself.
- **Short frames:** a frame shorter than 900 bytes gets no reply. The printer waits for the rest, so frames must always be padded.

### First real print, 2026-10-20 17:45 (ours)

The sheet was checked by eye: it printed as expected, the right way up.

- **Image:** a logo (5760² PNG) became a 600 × 800 JPEG of 16.7 KB (19 frames), **not flipped**.
- **Timing (fast settings):**
  - status 16 ms
  - upload 0.30 s (no gap, 4 frames in flight)
  - status re-check and 1 s pause
  - `PRINT` reply in 16 ms: `61 42 00 09 10 80 00 0e`, which is OK with an estimate of **14 s**
- **Completion:** the first status check 14.1 s later read print state 0 with the film byte `0x2a`, which became `0x29`, so film went from 10 to 9.
- **Settled by this print:**
  - The film count is the low 4 bits of the status film byte. The earlier 0 was because no pack was loaded.
  - The Link 3 needs **no vertical flip**. S3's flip came from an iOS app whose drawing context draws images upside down.
  - The `PRINT` reply's byte is the estimated print time in seconds.
  - Waiting that long and then checking for "idle and one sheet fewer" detects completion.
- **Status byte 4** read 0x0b with nothing uploaded, 0x0e with this image uploaded (matching the 14 s estimate), and 0x0b after printing. It is probably the estimated print time for the image in the buffer.
- **Still not printed:** JPEGs between 16.7 KB and the 55 KB cap, and pauses shorter than 1 s before `PRINT`.

### Failure modes and recovery

- **Link dropped mid-upload (seen over BLE):**
  - Afterwards every upload command (`10 00`–`10 03`) answered **133** on both links, while status queries still worked.
  - Switching the printer off and on cleared it.
  - The SDK reports this as `printer_stuck`.
- **Overrun (16 frames in flight):**
  - The printer stopped answering anything, even on a new connection.
  - Sending 256–1024 zero bytes (which never form a command) got answers again, but from then on each reply came **one message late**: the printer processed a message only when more bytes arrived.
  - The SDK's `resync()` and its one retry of read-only queries cover the first symptom. A power cycle is needed for the second.
  - The default stays at 4 frames in flight.

### Extra queries

- `DEVICE_INFO` kinds 6–8: answered with empty strings.
- `FUNCTION_INFO` kinds 4–5 (camera info): 129.
- `ADDITIONAL_INFO` (`30 10`):
  - kind 0 is 2 bytes, possibly battery voltage (`0xC4CC` falling to `0xC426`), then 2 bytes that look like temperature in 0.1 °C (232 rising to 238 during a run).
  - kind 1 is `00000000 01 02 01 …` (S1: print attempts, battery type, colour, film flag; unverified).
- **History:** the second counter was 2 on 19 Oct and 16 on 20 Oct, and didn't change during any of our runs. Its meaning is unknown.
- **Link 3 GATT extras:** no notifications on `6487`; every readable value stayed the same across runs.

## GATT (BLE)

| What | UUID | Status |
|---|---|---|
| Print service | `70954782-2d83-473d-9e5f-81e1d02d5273` | ours |
| Write (write without response, and write) | `70954783-…` | ours |
| Notify | `70954784-…` | ours |
| Device information | `180a`: the strings above, each ending in a NUL byte; PnP ID vendor 0x005D | ours |
| Link 3 info service | `0000d0ff-3c17-d293-8e48-14fe2e4da212`. Its characteristics use **16-bit** UUIDs, not the custom base S3 gives: `ffd1` (write without response), `ffd2` (reads the Classic address `88 b4 36 xx xx xx`), `ffd3`/`ffd4` (listed, but reading gives "attribute not found"), `fff1`, `ffe0`, `ffe1`, `fff3`, `fff4`, `fff5` | ours |
| Link 3 status service | `00006287-3c17-d293-8e48-14fe2e4da212`: `6387` (write without response), `6487` (write, notify) | ours |
| FFF1 etc. | `fff1`, `ffe0`, `ffe1`, `fff3`, `fff4` and `fff5` read **byte-for-byte the same** as S3's capture from a different printer. They look fixed for this firmware, so FFF1 byte 0 is not a live film count, although S3 says it is | ours (meaning unresolved) |

Writes are split at 182 bytes, the size the phone apps use (S1, S2). Replies come back as notifications.

## Framing (ours)

```
client → printer:  41 62 | length (2, BE) | function | operation | payload … | checksum
printer → client:  61 42 | length (2, BE) | function | operation | result | data … | checksum
```

- **length** is the whole packet: 7 bytes plus the payload, or plus the result byte and data for replies.
- **checksum** makes all the bytes of the packet sum to `0xFF` (mod 256).
- **result** is `0` for OK. This byte is what explains the "odd success codes" in S3. For example, its "Link 3 success code 16" is really the PRINT reply saying `OK, about 16 s`.

## Commands

| Op | Name | Payload | Reply data |
|---|---|---|---|
| `00 00` | VERSION_INFO | none | flags (bit 0 = can print), 7 version bytes |
| `00 01` | DEVICE_INFO | kind: 0 manufacturer, 1 model, 2 serial, 3 HW, 4 FW, 5 SW | kind, length, ASCII |
| `00 02` | FUNCTION_INFO | kind: 0 image, 1 battery, 2 status, 3 history | see below |
| `10 00` | UPLOAD_START | type `02` (JPEG), option, `00 00`, size (4, BE) | frame size (4, BE), 900 in a capture |
| `10 01` | UPLOAD_DATA | index (4, BE), frame zero-padded to the frame size | index (4, BE) |
| `10 02` | UPLOAD_END | none | none |
| `10 03` | UPLOAD_CANCEL | none | none |
| `10 80` | PRINT | none | estimated seconds (1 byte) |

Rules:
- **Nothing prints until `10 80`.** Ending an upload does not print (S3).
- **The probe** only sends `00 00`, `00 01` and `00 02`.

Also listed by the sources, not used by the SDK: `01 00` shut down, `01 01` reset, `01 02` auto-sleep (the layouts conflict between S1 and S3), `01 03` connection parameters, `10 81` eject the film cover, `30 01` colour table / print mode, `30 10` additional info.

The three read-only commands (`00 00`, `00 01`, `00 02`) all answered on our printer, in the layouts below (ours).

### FUNCTION_INFO replies

- **image (0), ours:** width (2), height (2), picture type, picture option, then the maximum size (4) at offset 7, as S1 says, followed by 4 zero bytes.
  - Ours: 600 × 800, JPEG, option `0x7b`, maximum **153 600 bytes** (150 KB).
  - S3 says about 55 KB is the real limit, so the SDK keeps 55 000 bytes until a print above that is tested.
- **battery (1), ours:** level code, percent, charger type, charger state. Ours: `03 5f 00 10` = level 3, 95 %, state `0x10`.
- **status (2):**
  - a film byte: film left (bits 0–3, **unresolved**: see below), battery level (bits 4–6), charging (bit 7)
  - a status byte (bits for cover / operating / error; **unverified**, so not used for decisions)
  - a print state: 0 idle, 127 processing, 1 cover open, 2 no film, 240 error
  - wait seconds
  - an error detail (4 bytes)

  Ours read `30 00 00 0b 00000000`: film nibble 0, battery level 3, idle, wait 11 s. FFF1 byte 0 read 6 at the same moment.
- **history (3), ours:** total prints (4), total ejects (4). Ours: 0 and 2.

## Print sequence

1. **Status:** film left above 0, print state 0.
2. **UPLOAD_START.** Use the frame size from the reply.
3. **UPLOAD_DATA** for each frame. Wait for each acknowledgement, then pause 75 ms. The 75 ms is **unverified**: S3 says 50 ms in one place and 75 ms in another, and that sending too fast gives a yellow blinking light.
4. **UPLOAD_END.**
5. **Status again.** The official app re-checks the film count here (S3).
6. **Wait 1 s** (S3; GitHub issue #21 there mentions 300 ms as the minimum).
7. **PRINT.** The reply gives the estimated seconds; the manual says about 15 s (S4).
8. **Confirm.** Wait the estimated time, then poll status every 2 s until the print state is 0 and film left has dropped by one. S3 reports that some models crash if polled mid-print.

## mini Link 3 specifics (unverified until the first real print)

- **Image size:** 600 × 800 pixels (S4: "800 × 600 dots").
- **File size:** at most about 55 KB of JPEG (S3). The SDK uses 55 000 bytes.
- **Orientation:** S3 says the image must be flipped vertically. **This is wrong for our printer:** it printed correctly unflipped (ours, 2026-10-20).
- **Formats:** the manual lists JPEG, PNG, HEIF and DNG for the app. Over Bluetooth only JPEG is known to work.
- **Power:** auto power-off is 5 minutes or Off, set in the INSTAX app (S4). The printer prints while charging (S4).

## Result codes

Names are from S1. S3 labels 178 "no film", 179 "cover open", 180 "low battery" and 181 "busy".

| Code | S1 name | Status |
|---|---|---|
| 1 | printer busy | unverified |
| 128 / 129 / 130 | unsupported / parameter error / sequence error | unverified |
| 176 | cover open | unverified |
| 177 | no film | unverified |
| 178 | no paper; answered to PRINT with no film loaded (S3 capture) | agreed: no film |
| 179–181 | image error / jammed / print fault | conflict |
| 194 | now printing | unverified |

The SDK treats any non-zero result as "refused". It maps 177 and 178 to `no_film`, and 1 and 194 to `busy`.

## Open questions

**Still open after the probe:**
- **Film count:** where is it? It's settled by comparing the status reply with the real pack (empty, a new pack of 10, or a known count).
- **Classic:** does an RFCOMM channel answer the same protocol?

**For the first print:**
- Is the vertical flip needed?
- Is a 50 ms or 75 ms gap safe?
- Is the 1 s wait before PRINT needed?
- Is the status check between END and PRINT required?
- What do the status bytes look like during a print?
