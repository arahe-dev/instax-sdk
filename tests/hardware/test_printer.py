"""Hardware tests: these run against the real printer and are part of every default test run.

They fail, rather than skip, when the printer is off: the SDK is only done when
they pass. Run the offline tests alone with `pytest -m "not printer"`.
"""

import asyncio
import math

import pytest

from instax import protocol as p
from instax.ble import BleTransport
from instax.client import InstaxPrinter, Outcome
from instax.imaging import orientation_pattern, prepare_jpeg
from instax.rfcomm import RfcommTransport

pytestmark = pytest.mark.printer


@pytest.fixture
def on_printer(printer_link):
    transport, address = printer_link

    def make():
        return BleTransport(address) if transport == "ble" else RfcommTransport(address, 6)

    def run(body):
        async def go():
            async with InstaxPrinter(make()) as printer:
                return await body(printer)

        return asyncio.run(go())

    return run


def test_identity_comes_from_the_printer(on_printer):
    async def body(printer):
        return printer.info

    info = on_printer(body)
    assert (info.manufacturer, info.model) == ("FUJIFILM", "FI033")
    assert (info.width, info.height) == (600, 800)
    assert info.can_print and info.reported_max_bytes


def test_status_battery_and_history_parse(on_printer):
    async def body(printer):
        return await printer.status(), await printer.battery(), await printer.history()

    status, battery, history = on_printer(body)
    assert status.print_state == p.PRINT_STATE_IDLE
    assert 0 <= battery.percent <= 100
    assert history.total_prints >= 0


def test_upload_is_acknowledged_frame_by_frame_and_cancelled(on_printer):
    async def body(printer):
        jpeg = prepare_jpeg(orientation_pattern(), printer.info.width, printer.info.height, printer.max_jpeg_bytes)
        before = await printer.history()
        sizes = []
        frames = await printer.upload(jpeg, on_frame=lambda i, s: sizes.append(i))
        await printer.cancel_upload()
        status, after = await printer.status(), await printer.history()
        return len(jpeg), frames, sizes, status, before, after

    size, frames, acked, status, before, after = on_printer(body)
    assert frames == len(acked) == math.ceil(size / 900)
    assert status.print_state == p.PRINT_STATE_IDLE
    assert after.total_prints == before.total_prints  # nothing printed


@pytest.mark.prints
def test_print_one_sheet(on_printer):
    async def body(printer):
        jpeg = prepare_jpeg(orientation_pattern(), printer.info.width, printer.info.height, printer.max_jpeg_bytes,
                            flip_vertical=printer.profile.flip_vertical)
        return await printer.print_jpeg(jpeg)

    outcome = on_printer(body)
    assert outcome.outcome is Outcome.PRINTED, outcome
