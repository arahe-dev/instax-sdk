"""Client logic against the fake printer. The hardware tests in tests/hardware check the same on the real one."""

import asyncio
import io
from dataclasses import replace

import pytest
from fake_printer import FakeConfig, FakePrinter
from PIL import Image

from instax import protocol as p
from instax.client import InstaxPrinter, NotConnected, Outcome, ReplyTimeout
from instax.imaging import orientation_pattern, prepare_jpeg
from instax.profiles import MINI_LINK_3

FAST = replace(MINI_LINK_3, packet_gap_s=0.0, pre_print_delay_s=0.0)
JPEG = b"\xff\xd8" + bytes(range(256)) * 20 + b"\xff\xd9"  # 5124 bytes: 6 frames of 900


def cfg(**changes) -> FakeConfig:
    return FakeConfig(end_time_s=0, print_seconds=0.05, **changes)


def run(config: FakeConfig | None = None, *, execute: bool = True, jpeg: bytes = JPEG):
    fake = FakePrinter(config or cfg())

    async def go():
        async with InstaxPrinter(fake, profile=FAST, reply_timeout=0.3) as printer:
            return await printer.print_jpeg(jpeg, execute=execute, completion_timeout=1.0, poll_interval=0.05)

    return asyncio.run(go()), fake


def connected(config: FakeConfig | None = None, **kwargs):
    fake = FakePrinter(config or cfg())

    async def go():
        async with InstaxPrinter(fake, **kwargs) as printer:
            return printer

    return asyncio.run(go()), fake


def test_connect_reads_identity_and_limits_from_the_printer():
    printer, _ = connected()
    info = printer.info
    assert (info.manufacturer, info.model, info.serial, info.firmware) == ("FUJIFILM", "FI033", "70000000", "0102")
    assert (info.width, info.height, info.reported_max_bytes) == (600, 800, 153_600)
    assert printer.profile.model == "FI033"
    assert printer.max_jpeg_bytes == 55_000  # the working cap is below what the printer reports


def test_the_printers_own_limit_wins_when_it_is_smaller():
    printer, _ = connected(cfg(reported_max_bytes=40_000))
    assert printer.max_jpeg_bytes == 40_000


def test_unknown_model_gets_the_generic_profile():
    printer, _ = connected(cfg(model="FI032"))
    assert printer.profile.model == "mini"


def test_connect_fails_and_closes_if_the_printer_cannot_identify_itself():
    fake = FakePrinter(cfg(silent_ops={p.Op.DEVICE_INFO}))

    async def go():
        await InstaxPrinter(fake, reply_timeout=0.2).connect()

    with pytest.raises(ReplyTimeout):
        asyncio.run(go())
    assert not fake.is_open


def test_print_needs_a_connected_printer():
    async def go():
        await InstaxPrinter(FakePrinter(cfg())).print_jpeg(JPEG)

    with pytest.raises(NotConnected):
        asyncio.run(go())


def test_trace_records_every_packet():
    events = []
    fake = FakePrinter(cfg())

    async def go():
        async with InstaxPrinter(fake, trace=events.append) as printer:
            await printer.status()

    asyncio.run(go())
    tx = [e for e in events if e["kind"] == "tx"]
    rx = [e for e in events if e["kind"] == "rx"]
    assert len(tx) == len(rx) == len(fake.requests)
    assert tx[-1]["op"] == "FUNCTION_INFO" and rx[-1]["hex"].startswith("61 42")


def test_print_is_confirmed_by_the_film_count():
    outcome, fake = run()
    assert outcome.outcome is Outcome.PRINTED
    assert (outcome.film_before, outcome.film_after) == (10, 9)
    assert fake.prints == 1
    assert fake.count(p.Op.UPLOAD_DATA) == 6


def test_dry_run_never_prints():
    outcome, fake = run(execute=False)
    assert outcome.outcome is Outcome.DRY_RUN
    assert fake.count(p.Op.PRINT) == 0
    assert fake.count(p.Op.UPLOAD_CANCEL) == 0  # the printer refuses cancel after END anyway
    assert fake.prints == 0


def test_windowed_upload_prints_the_same():
    fake = FakePrinter(cfg())

    async def go():
        async with InstaxPrinter(fake, profile=replace(FAST, upload_window=4), reply_timeout=0.3) as printer:
            return await printer.print_jpeg(JPEG, completion_timeout=1.0, poll_interval=0.05)

    outcome = asyncio.run(go())
    assert outcome.outcome is Outcome.PRINTED
    assert fake.count(p.Op.UPLOAD_DATA) == 6


def test_windowed_upload_accepts_acknowledgements_in_any_order():
    fake = FakePrinter(cfg())
    real_reply = fake._reply
    held: list[bytes] = []

    def shuffled(packet: bytes) -> None:  # deliver frame acks in reverse pairs
        if packet[4:6] == bytes([0x10, 0x01]):
            held.append(packet)
            if len(held) == 2:
                for pkt in reversed(held):
                    real_reply(pkt)
                held.clear()
            return
        real_reply(packet)

    fake._reply = shuffled

    async def go():
        async with InstaxPrinter(fake, profile=FAST, reply_timeout=0.3) as printer:
            return await printer.upload(bytes(4 * 900), window=4)

    assert asyncio.run(go()) == 4


def test_prepare_then_print_sends_print_once():
    fake = FakePrinter(cfg())

    async def go():
        async with InstaxPrinter(fake, profile=FAST, reply_timeout=0.3) as printer:
            assert not printer.prepared
            assert await printer.prepare(JPEG) is None
            assert printer.prepared
            first = await printer.print_prepared(completion_timeout=1.0, poll_interval=0.05)
            again = await printer.print_prepared()
            return first, again

    first, again = asyncio.run(go())
    assert first.outcome is Outcome.PRINTED
    assert (again.outcome, again.reason) == (Outcome.REFUSED, "not_prepared")
    assert fake.count(p.Op.PRINT) == 1


def test_a_dropped_link_forgets_the_prepared_image():
    fake = FakePrinter(cfg())

    async def go():
        async with InstaxPrinter(fake, profile=FAST, reply_timeout=0.3) as printer:
            await printer.prepare(JPEG)
            fake._drop()
            return await printer.print_prepared()

    outcome = asyncio.run(go())
    assert (outcome.outcome, outcome.reason) == (Outcome.REFUSED, "not_prepared")
    assert fake.count(p.Op.PRINT) == 0


def test_a_query_that_times_out_is_retried_once_after_resync():
    fake = FakePrinter(cfg())

    async def go():
        async with InstaxPrinter(fake, profile=FAST, reply_timeout=0.2) as printer:
            fake.config.silent_ops = {p.Op.FUNCTION_INFO}
            real_send = fake.send

            async def send(packet: bytes) -> None:
                if packet == bytes(1024):  # the flush: the printer answers again afterwards
                    fake.config.silent_ops = set()
                    return
                await real_send(packet)

            fake.send = send
            return await printer.status()

    status = asyncio.run(go())
    assert status.film_left == 10
    status_queries = [r for r in fake.requests if r == (p.Op.FUNCTION_INFO, bytes([p.FunctionInfo.PRINTER_STATUS]))]
    assert len(status_queries) == 2  # the one that timed out, and the retry after the flush


def test_print_is_never_resent_after_a_timeout():
    outcome, fake = run(cfg(silent_ops={p.Op.PRINT}))
    assert outcome.outcome is Outcome.UNCERTAIN
    assert fake.count(p.Op.PRINT) == 1


def test_a_stuck_printer_is_reported_as_stuck():
    outcome, fake = run(cfg(stuck=True))
    assert (outcome.outcome, outcome.reason, outcome.result_code) == (Outcome.REFUSED, "printer_stuck", 133)
    assert fake.count(p.Op.PRINT) == 0


def test_no_film_is_refused_before_upload():
    outcome, fake = run(cfg(film_left=0))
    assert (outcome.outcome, outcome.reason, outcome.stage) == (Outcome.REFUSED, "no_film", "precheck")
    assert fake.count(p.Op.UPLOAD_START) == 0


def test_printer_refusing_print_is_refused():
    outcome, fake = run(cfg(refuse_print_code=178))
    assert (outcome.outcome, outcome.reason, outcome.result_code) == (Outcome.REFUSED, "no_film", 178)
    assert fake.prints == 0


def test_failed_frame_is_refused_and_cancelled():
    outcome, fake = run(cfg(fail_frame=3))
    assert (outcome.outcome, outcome.stage, outcome.result_code) == (Outcome.REFUSED, "upload", 129)
    assert fake.count(p.Op.PRINT) == 0
    assert fake.count(p.Op.UPLOAD_CANCEL) == 1


def test_missing_frame_size_is_refused():
    outcome, fake = run(cfg(no_frame_size=True))
    assert (outcome.outcome, outcome.reason, outcome.stage) == (Outcome.REFUSED, "rejected", "upload")
    assert fake.count(p.Op.UPLOAD_DATA) == 0


def test_disconnect_after_print_is_uncertain_and_never_resent():
    outcome, fake = run(cfg(drop_after_print=True))
    assert (outcome.outcome, outcome.reason, outcome.stage) == (Outcome.UNCERTAIN, "disconnected", "print")
    assert fake.prints == 1  # the printer printed even though the reply was lost
    assert fake.count(p.Op.PRINT) == 1


def test_lost_print_reply_is_uncertain():
    outcome, fake = run(cfg(silent_ops={p.Op.PRINT}))
    assert (outcome.outcome, outcome.reason) == (Outcome.UNCERTAIN, "no_reply")
    assert fake.count(p.Op.PRINT) == 1


def test_finish_without_film_drop_is_uncertain():
    outcome, fake = run(cfg(print_without_film_drop=True))
    assert (outcome.outcome, outcome.reason, outcome.stage) == (Outcome.UNCERTAIN, "not_confirmed", "completion")
    assert fake.count(p.Op.PRINT) == 1


def test_oversized_image_is_rejected_before_upload():
    fake = FakePrinter(cfg())

    async def go():
        async with InstaxPrinter(fake, profile=FAST) as printer:
            with pytest.raises(ValueError):
                await printer.print_jpeg(bytes(printer.max_jpeg_bytes + 1))

    asyncio.run(go())
    assert fake.count(p.Op.UPLOAD_START) == 0


def test_prepare_jpeg_fits_size_and_limit():
    jpeg = prepare_jpeg(orientation_pattern(), 600, 800, 55_000)
    img = Image.open(io.BytesIO(jpeg))
    assert len(jpeg) <= 55_000
    assert img.size == (600, 800)
    assert "exif" not in img.info


def test_flip_puts_the_bottom_on_top():
    pattern = orientation_pattern()
    plain = Image.open(io.BytesIO(prepare_jpeg(pattern, 600, 800, 55_000)))
    flipped = Image.open(io.BytesIO(prepare_jpeg(pattern, 600, 800, 55_000, flip_vertical=True)))
    assert plain.getpixel((10, 10))[2] > 200  # the gradient starts blue at the top
    assert flipped.getpixel((10, 10))[0] > 200  # and ends orange at the bottom
