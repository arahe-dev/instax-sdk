import pytest

from instax import protocol as p


def h(text: str) -> bytes:
    return bytes.fromhex(text)


@pytest.mark.parametrize(
    "op, payload, expected",
    [
        (p.Op.VERSION_INFO, b"", "41 62 00 07 00 00 55"),
        # The next four match bytes captured from the official app.
        (p.Op.FUNCTION_INFO, b"\x01", "41 62 00 08 00 02 01 51"),
        (p.Op.FUNCTION_INFO, b"\x02", "41 62 00 08 00 02 02 50"),
        (p.Op.UPLOAD_END, b"", "41 62 00 07 10 02 43"),
        (p.Op.PRINT, b"", "41 62 00 07 10 80 c5"),
    ],
)
def test_encode_known_requests(op, payload, expected):
    assert p.encode(op, payload) == h(expected)


def test_upload_start_payload():
    assert p.encode(p.Op.UPLOAD_START, p.upload_start_payload(0x017B90)) == h(
        "41 62 00 0f 10 00 02 00 00 00 00 01 7b 90 2f"
    )


def test_print_reply_carries_estimated_seconds():
    reply = p.decode_reply(h("61 42 00 09 10 80 00 0c b7"))
    assert reply.ok and reply.op == p.Op.PRINT
    assert p.parse_print_reply(reply.data) == 12


def test_print_refused_for_no_film():
    reply = p.decode_reply(h("61 42 00 08 10 80 b2 12"))
    assert reply.result == 178 and not reply.ok


def test_printer_status_layout():
    reply = p.decode_reply(h("61 42 00 11 00 02 00 02 28 00 00 0c 00 00 00 00 13"))
    status = p.parse_printer_status(reply.data)
    assert status.film_left == 8
    assert status.battery_level == 2
    assert not status.charging
    assert status.print_state == p.PRINT_STATE_IDLE
    assert status.print_wait_s == 12


def test_upload_start_reply_gives_frame_size():
    reply = p.decode_reply(h("61 42 00 0c 10 00 00 00 00 03 84 b9"))
    assert p.parse_frame_size(reply.data) == 900


def test_device_info_reply():
    reply = p.decode_reply(h("61 42 00 0f 00 01 00 01 05 46 49 30 31 37 1f"))
    assert p.parse_device_info(reply.data) == (p.DeviceInfo.MODEL, "FI017")


def test_battery_reply():
    reply = p.decode_reply(h("61 42 00 0d 00 02 00 01 02 32 00 00 18"))
    assert p.parse_battery(reply.data).percent == 50


def test_version_reply():
    reply = p.decode_reply(h("61 42 00 10 00 00 00 01 00 02 00 00 00 00 00 49"))
    assert p.parse_version_info(reply.data).can_print


# Replies from our mini Link 3 (FI033, firmware 0102), read-only probe on 2026-10-19.

def test_our_image_support_reply():
    reply = p.decode_reply(h("61 42 00 17 00 02 00 00 02 58 03 20 02 7b 00 02 58 00 00 00 00 00 ef"))
    support = p.parse_image_support(reply.data)
    assert (support.width, support.height, support.picture_type) == (600, 800, p.JPEG)
    assert support.reported_max_bytes == 153_600


def test_our_device_info_reply():
    reply = p.decode_reply(h("61 42 00 0f 00 01 00 01 05 46 49 30 33 33 21"))
    assert p.parse_device_info(reply.data) == (p.DeviceInfo.MODEL, "FI033")


def test_our_battery_reply():
    battery = p.parse_battery(p.decode_reply(h("61 42 00 0d 00 02 00 01 03 5f 00 10 da")).data)
    assert (battery.level_code, battery.percent, battery.charger_state) == (3, 95, 0x10)


def test_our_status_reply():
    status = p.parse_printer_status(p.decode_reply(h("61 42 00 11 00 02 00 02 30 00 00 0b 00 00 00 00 0c")).data)
    assert (status.battery_level, status.print_state, status.print_wait_s) == (3, p.PRINT_STATE_IDLE, 11)


def test_our_history_reply():
    history = p.parse_history(p.decode_reply(h("61 42 00 11 00 02 00 03 00 00 00 00 00 00 00 02 44")).data)
    assert (history.total_prints, history.total_ejects) == (0, 2)


def test_bad_checksum_is_rejected():
    with pytest.raises(p.ProtocolError):
        p.decode_reply(h("61 42 00 09 10 80 00 0c b8"))


def test_length_mismatch_is_rejected():
    with pytest.raises(p.ProtocolError):
        p.decode_reply(h("61 42 00 0a 10 80 00 0c b7"))


def test_split_frames_pads_last_frame():
    frames = p.split_frames(bytes(range(10)), 4)
    assert frames == [bytes([0, 1, 2, 3]), bytes([4, 5, 6, 7]), bytes([8, 9, 0, 0])]


def test_frame_reader_handles_any_split():
    a = p.encode_reply(p.Op.FUNCTION_INFO, 0, bytes(9))
    b = p.encode_reply(p.Op.PRINT, 0, b"\x10")
    stream = b"\x00\xff" + a + b
    for cut in range(len(stream) + 1):
        reader = p.FrameReader()
        assert reader.feed(stream[:cut]) + reader.feed(stream[cut:]) == [a, b]
    reader, out = p.FrameReader(), []
    for i in range(len(stream)):
        out += reader.feed(stream[i:i + 1])
    assert out == [a, b]


def test_link3_status_characteristic():
    status = p.parse_link3_status(h("06 01 00 15 00 00 4f 00 c0 ff 0f 00"))
    assert (status.film_left, status.battery_percent, status.charging) == (6, 96, False)
