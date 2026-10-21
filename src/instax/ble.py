"""Bluetooth Low Energy transport (the printer's "(BLE)" device), using bleak."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .protocol import IN_HEADER, FrameReader
from .transport import Transport, TransportError

log = logging.getLogger(__name__)

PRINT_SERVICE = "70954782-2d83-473d-9e5f-81e1d02d5273"
WRITE_CHAR = "70954783-2d83-473d-9e5f-81e1d02d5273"
NOTIFY_CHAR = "70954784-2d83-473d-9e5f-81e1d02d5273"
LINK3_INFO_SERVICE = "0000d0ff-3c17-d293-8e48-14fe2e4da212"
LINK3_STATUS_SERVICE = "00006287-3c17-d293-8e48-14fe2e4da212"
# The Link 3 services use custom UUIDs, but their characteristics use 16-bit ones (seen on our printer).
LINK3_STATUS_CHAR = "0000fff1-0000-1000-8000-00805f9b34fb"  # fixed per firmware on ours, not live
CLASSIC_ADDRESS_CHAR = "0000ffd2-0000-1000-8000-00805f9b34fb"  # first 6 bytes: the (EDR) device's address
SERVICE_CHANGED_CHAR = "00002a05-0000-1000-8000-00805f9b34fb"
DEVICE_INFO_SERVICE = "0000180a-0000-1000-8000-00805f9b34fb"
DEVICE_INFO_CHARS = {
    "00002a29-0000-1000-8000-00805f9b34fb": "manufacturer",
    "00002a24-0000-1000-8000-00805f9b34fb": "model",
    "00002a25-0000-1000-8000-00805f9b34fb": "serial",
    "00002a27-0000-1000-8000-00805f9b34fb": "hardware",
    "00002a26-0000-1000-8000-00805f9b34fb": "firmware",
    "00002a28-0000-1000-8000-00805f9b34fb": "software",
}
NAME_PREFIX = "INSTAX-"
# The write size the phone apps use; lowered if the link reports less room.
DEFAULT_WRITE_SIZE = 182


@dataclass(frozen=True)
class Found:
    name: str
    address: str
    rssi: int | None


def looks_like_instax(device: BLEDevice, adv: AdvertisementData) -> bool:
    name = device.name or adv.local_name or ""
    return name.startswith(NAME_PREFIX) or PRINT_SERVICE in (u.lower() for u in adv.service_uuids)


async def scan(timeout: float = 8.0) -> list[Found]:
    """instax printers currently advertising, strongest signal first."""
    seen = await BleakScanner.discover(timeout=timeout, return_adv=True)
    found = [
        Found(device.name or adv.local_name or "", device.address, adv.rssi)
        for device, adv in seen.values()
        if looks_like_instax(device, adv)
    ]
    return sorted(found, key=lambda f: f.rssi if f.rssi is not None else -999, reverse=True)


class BleTransport(Transport):
    def __init__(
        self,
        address: str | None = None,
        *,
        scan_timeout: float = 10.0,
        connect_timeout: float = 20.0,
        write_size: int = DEFAULT_WRITE_SIZE,
        use_cached_services: bool = False,
        on_gatt_notify: Callable[[str, bytes], None] | None = None,
        connect_attempts: int = 3,
    ) -> None:
        """on_gatt_notify, if given, also subscribes to every other notifying characteristic
        (such as the Link 3 status one) and reports what they send as (uuid, value)."""
        super().__init__()
        self._on_gatt_notify = on_gatt_notify
        self._connect_attempts = connect_attempts
        self._address = address
        self._scan_timeout = scan_timeout
        self._connect_timeout = connect_timeout
        self._max_write_size = write_size
        self._write_size = write_size
        self._use_cached_services = use_cached_services
        self._client: BleakClient | None = None
        self._reader = FrameReader(IN_HEADER)
        self._closing = False

    @property
    def is_open(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def client(self) -> BleakClient:
        if self._client is None:
            raise TransportError("not connected")
        return self._client

    async def open(self) -> None:
        """Connect, retrying a few times: on Windows the link can drop during service discovery."""
        for attempt in range(1, self._connect_attempts + 1):
            try:
                await self._open_once()
                return
            except TransportError as exc:
                if attempt == self._connect_attempts:
                    raise
                log.warning("BLE connect attempt %d failed: %s", attempt, exc)
                await asyncio.sleep(1.0)

    async def _open_once(self) -> None:
        device = await self._find()
        self._closing = False
        self._reader.reset()
        client = BleakClient(
            device,
            disconnected_callback=self._on_ble_disconnect,
            timeout=self._connect_timeout,
            winrt={"use_cached_services": self._use_cached_services},
        )
        try:
            await client.connect()
            write = client.services.get_characteristic(WRITE_CHAR)
            if write is None or client.services.get_characteristic(NOTIFY_CHAR) is None:
                raise TransportError("device does not expose the instax print service")
            self._write_size = max(20, min(self._max_write_size, write.max_write_without_response_size))
            await client.start_notify(NOTIFY_CHAR, self._on_notify)
            if self._on_gatt_notify:
                await self._subscribe_others(client)
        except (Exception, asyncio.CancelledError) as exc:
            task = asyncio.current_task()
            if isinstance(exc, asyncio.CancelledError) and task is not None and task.cancelling():
                raise  # we are really being cancelled
            # bleak raises CancelledError when the link drops during service discovery.
            self._closing = True
            try:
                await client.disconnect()
            except Exception:
                pass
            if isinstance(exc, TransportError):
                raise
            reason = "link dropped during service discovery" if isinstance(exc, asyncio.CancelledError) else exc
            raise TransportError(f"BLE connect to {device.address} failed: {reason}") from exc
        self._client = client
        log.info("connected to %s (%s), write size %d", device.name, device.address, self._write_size)

    async def _find(self) -> BLEDevice:
        try:
            if self._address:
                device = await BleakScanner.find_device_by_address(self._address, timeout=self._scan_timeout)
            else:
                device = await BleakScanner.find_device_by_filter(looks_like_instax, timeout=self._scan_timeout)
        except Exception as exc:
            raise TransportError(f"BLE scan failed: {exc}") from exc
        if device is None:
            raise TransportError("no instax printer found; is it on and not connected to a phone?")
        return device

    async def _subscribe_others(self, client: BleakClient) -> None:
        for service in client.services:
            for char in service.characteristics:
                uuid = char.uuid.lower()
                if uuid in (NOTIFY_CHAR, SERVICE_CHANGED_CHAR):
                    continue
                if "notify" in char.properties or "indicate" in char.properties:
                    try:
                        await client.start_notify(char, self._on_other_notify)
                        log.info("subscribed to %s", uuid)
                    except Exception as exc:
                        log.warning("could not subscribe to %s: %s", uuid, exc)

    def _on_other_notify(self, char, data: bytearray) -> None:
        if self._on_gatt_notify:
            self._on_gatt_notify(char.uuid.lower(), bytes(data))

    def _on_notify(self, _char: object, data: bytearray) -> None:
        for packet in self._reader.feed(bytes(data)):
            self._deliver(packet)

    def _on_ble_disconnect(self, _client: BleakClient) -> None:
        if not self._closing:
            log.warning("printer disconnected")
            self._lost()

    async def send(self, packet: bytes) -> None:
        client = self._client
        if client is None or not client.is_connected:
            raise TransportError("not connected")
        try:
            for i in range(0, len(packet), self._write_size):
                await client.write_gatt_char(WRITE_CHAR, packet[i:i + self._write_size], response=False)
        except Exception as exc:
            raise TransportError(f"BLE write failed: {exc}") from exc

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        self._closing = True
        try:
            if client.is_connected:
                await client.disconnect()
        except Exception as exc:
            log.debug("disconnect: %s", exc)

    async def snapshot(self) -> dict:
        """Read-only: the GATT table, plus every readable device-info and Link 3 value."""
        client = self.client
        services, reads = [], {}
        for service in client.services:
            chars = []
            for char in service.characteristics:
                chars.append({
                    "uuid": char.uuid,
                    "description": char.description,
                    "properties": list(char.properties),
                    "handle": char.handle,
                })
                wanted = service.uuid.lower() in (DEVICE_INFO_SERVICE, LINK3_INFO_SERVICE, LINK3_STATUS_SERVICE)
                if wanted and "read" in char.properties:
                    label = DEVICE_INFO_CHARS.get(char.uuid.lower(), char.uuid.lower())
                    try:
                        value = bytes(await client.read_gatt_char(char))
                        entry = {"uuid": char.uuid, "hex": value.hex(" ")}
                        if label in DEVICE_INFO_CHARS.values():
                            entry["text"] = value.decode("ascii", "replace").rstrip("\x00")
                    except Exception as exc:
                        entry = {"uuid": char.uuid, "error": str(exc)}
                    reads[label] = entry
            services.append({"uuid": service.uuid, "description": service.description, "characteristics": chars})
        return {"mtu": client.mtu_size if client.is_connected else None, "write_size": self._write_size,
                "services": services, "reads": reads}
