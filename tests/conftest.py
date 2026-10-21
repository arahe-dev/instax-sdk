import os

import pytest

def pytest_addoption(parser):
    parser.addoption("--printer-transport", default=os.environ.get("INSTAX_TRANSPORT", "rfcomm"),
                     choices=["rfcomm", "ble"], help="link used by the hardware tests (default rfcomm)")
    parser.addoption("--printer-address", default=os.environ.get("INSTAX_ADDRESS"),
                     help="the printer's (EDR) address for rfcomm (default $INSTAX_ADDRESS); "
                          "for ble, the (BLE) address or none to use the first instax found")
    parser.addoption("--allow-print", action="store_true", help="let the hardware tests print one sheet of film")


@pytest.fixture
def printer_link(request) -> tuple[str, str | None]:
    """(transport, address) for the hardware tests; fails (does not skip) without an rfcomm address."""
    transport = request.config.getoption("--printer-transport")
    address = request.config.getoption("--printer-address")
    if transport == "rfcomm" and not address:
        pytest.fail("set INSTAX_ADDRESS or pass --printer-address: the printer's (EDR) address, "
                    "which `instax --transport ble classic-address` reads")
    return transport, address


def pytest_configure(config):
    config.addinivalue_line("markers", "printer: talks to the real printer, which must be on and free of phones")
    config.addinivalue_line("markers", "prints: uses a sheet of film; runs only with --allow-print")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--allow-print"):
        return
    skip = pytest.mark.skip(reason="uses film; pass --allow-print")
    for item in items:
        if "prints" in item.keywords:
            item.add_marker(skip)
