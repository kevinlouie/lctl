import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Optional

try:
    # bleak is the de facto cross-platform BLE client for Python.
    from bleak import BleakClient, BleakScanner
except Exception:  # pragma: no cover - optional dependency
    BleakClient = None  # type: ignore[misc]
    BleakScanner = None  # type: ignore[misc]

logger = logging.getLogger(__name__)


class LightEffect(Enum):
    """Available light effects."""
    FLASH = 0x04
    TV_SCREEN = 0x05
    CANDLE = 0x06
    STROBE_1 = 0x0C
    STROBE_2 = 0x0D
    STROBE_3 = 0x0E


# Discovered from the device:
# - Service 0xFFF0 exposes a write-without-response characteristic 0xFFF2
#   and a read/notify characteristic 0xFFF1. 0xFFF2 is the control path.
# - The device requires subscribing to 0xFFF1 notifications for 2-way communication
# - Command protocol:
#   * Power ON:  FF A1 01 00 00 AA (single command)
#   * Power OFF requires TWO commands in sequence:
#     1. Initialization: FF A1 01 30 30 AA (device responds with notification)
#     2. Power off:      FF A1 01 01 01 AA (actually turns light off)
#   * Brightness: FF A2 01 <level> <level> AA where level is 0-100 decimal
#   * Color Temperature: FF A3 02 <high> <low> <checksum> AA where temp is Kelvin (2700-6500)
#   * FX Effects: FF A1 01 <fx> <fx> AA where fx is effect code (0x04-0x0E)
DEFAULT_CONTROL_CHARACTERISTIC = "0000fff2-0000-1000-8000-00805f9b34fb"
DEFAULT_NOTIFY_CHARACTERISTIC = "0000fff1-0000-1000-8000-00805f9b34fb"
ULANZI_SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
ULANZI_NAME_PREFIX = "Ulanzi"


@dataclass
class DiscoveredLight:
    """Information about a discovered Ulanzi light."""
    address: str
    name: str
    rssi: int


async def scan_for_lights(timeout: float = 10.0) -> list[DiscoveredLight]:
    """
    Scan for Ulanzi lights on the BLE network.

    Args:
        timeout: How long to scan in seconds (default 10s)

    Returns:
        List of discovered Ulanzi lights
    """
    if BleakScanner is None:
        raise RuntimeError("bleak is required for scanning. Install requirements.txt.")

    logger.info("Scanning for Ulanzi lights for %.1f seconds...", timeout)

    discovered = []
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)

    for device, adv_data in devices.values():
        # Check if device name matches Ulanzi pattern
        name = device.name or adv_data.local_name or ""
        if not name.startswith(ULANZI_NAME_PREFIX):
            continue

        # Additional check: verify it has the expected service UUID
        service_uuids = adv_data.service_uuids or []
        has_ulanzi_service = any(
            ULANZI_SERVICE_UUID.lower() in uuid.lower()
            for uuid in service_uuids
        )

        if has_ulanzi_service or name.startswith(ULANZI_NAME_PREFIX):
            rssi = adv_data.rssi if adv_data.rssi is not None else -100
            discovered.append(DiscoveredLight(
                address=device.address,
                name=name,
                rssi=rssi,
            ))
            logger.debug("Found Ulanzi light: %s (%s) RSSI: %d", name, device.address, rssi)

    # Sort by signal strength (strongest first)
    discovered.sort(key=lambda d: d.rssi, reverse=True)

    logger.info("Found %d Ulanzi light(s)", len(discovered))
    return discovered


@dataclass
class LightState:
    is_on: bool
    brightness: int


class BLELight:
    """
    BLE controller for Ulanzi light devices.

    Controls power and brightness via BLE GATT characteristics.
    Requires notification subscription for proper device communication.
    """

    def __init__(
        self,
        address: str,
        control_characteristic: str | None = None,
        notify_characteristic: str | None = None,
        dry_run: bool = True,
    ) -> None:
        if BleakClient is None and not dry_run:
            raise RuntimeError(
                "bleak is required for real BLE control. Install requirements.txt or set dry_run=True."
            )
        self.address = address
        self.control_characteristic = control_characteristic or DEFAULT_CONTROL_CHARACTERISTIC
        self.notify_characteristic = notify_characteristic or DEFAULT_NOTIFY_CHARACTERISTIC
        self._client: Optional["BleakClient"] = None
        self.dry_run = dry_run

    async def __aenter__(self) -> "BLELight":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.disconnect()

    def _notification_handler(self, sender: int, data: bytes) -> None:
        """Handle notifications from the device."""
        logger.debug("Received notification from %s: %s", sender, data.hex())

    async def connect(self) -> None:
        if self.dry_run:
            logger.debug("dry-run enabled; skipping BLE connection to %s", self.address)
            return
        if BleakClient is None:
            raise RuntimeError("bleak is missing; cannot connect.")
        if self._client and self._client.is_connected:
            return
        self._client = BleakClient(self.address)
        await self._client.connect()
        logger.info("Connected to %s", self.address)
        # Subscribe to notifications to enable 2-way communication
        await self._client.start_notify(self.notify_characteristic, self._notification_handler)
        logger.debug("Subscribed to notifications on %s", self.notify_characteristic)
        # Wait after subscribing before sending commands
        await asyncio.sleep(0.05)

    async def disconnect(self) -> None:
        if self.dry_run:
            return
        if self._client:
            if self._client.is_connected:
                try:
                    await self._client.stop_notify(self.notify_characteristic)
                except Exception:
                    pass  # Best effort cleanup
            await self._client.disconnect()
            logger.info("Disconnected from %s", self.address)

    async def set_power(self, on: bool) -> None:
        if on:
            # Power on uses a single command with 0x00
            payload_on = bytes([0xFF, 0xA1, 0x01, 0x00, 0x00, 0xAA])
            await self._write(self.control_characteristic, payload_on, action="power_on")
        else:
            # Power off requires a two-step sequence:
            # 1. Initialization command (0x30) - device responds with notification
            # 2. Power off command (0x01) - actually turns the light off
            payload_init = bytes([0xFF, 0xA1, 0x01, 0x30, 0x30, 0xAA])
            await self._write(self.control_characteristic, payload_init, action="power_init")
            payload_off = bytes([0xFF, 0xA1, 0x01, 0x01, 0x01, 0xAA])
            await self._write(self.control_characteristic, payload_off, action="power_off")

    async def set_brightness(self, brightness: int) -> None:
        brightness = max(0, min(100, brightness))
        payload = self._build_brightness_payload(brightness)
        await self._write(
            self.control_characteristic,
            payload,
            action=f"brightness={brightness}",
        )

    async def set_color_temperature(self, kelvin: int) -> None:
        """Set color temperature in Kelvin (2700-6500)."""
        kelvin = max(2700, min(6500, kelvin))
        payload = self._build_color_temp_payload(kelvin)
        await self._write(
            self.control_characteristic,
            payload,
            action=f"color_temp={kelvin}K",
        )

    async def set_effect(self, effect: LightEffect) -> None:
        """Set a light effect (flash, candle, strobe, etc.)."""
        effect_code = effect.value
        payload = bytes([0xFF, 0xA1, 0x01, effect_code, effect_code, 0xAA])
        await self._write(
            self.control_characteristic,
            payload,
            action=f"effect={effect.name}",
        )

    async def set_state(self, on: bool, brightness: Optional[int] = None) -> None:
        await self.set_power(on)
        if brightness is not None:
            await self.set_brightness(brightness)

    async def _write(self, characteristic: str, payload: bytes, action: str) -> None:
        if self.dry_run:
            logger.info("[dry-run] Would send %s to %s (%s)", payload, characteristic, action)
            return
        if self._client is None or not self._client.is_connected:
            await self.connect()
        if self._client is None:
            raise RuntimeError("BLE client unavailable after connect.")
        # Characteristic 0xFFF2 is write-without-response; BlueZ rejects response=True.
        try:
            await self._client.write_gatt_char(characteristic, payload, response=False)
            logger.debug("Sent %s to %s", action, characteristic)
            await asyncio.sleep(0.02)  # Wait between commands
        except Exception as exc:
            logger.error("Failed to write %s to %s: %s", action, characteristic, exc)
            raise

    def _build_brightness_payload(self, brightness: int) -> bytes:
        # Brightness command: FF A2 01 <level> <level> AA (level 0-100 decimal).
        level = max(0, min(100, int(brightness)))
        return bytes([0xFF, 0xA2, 0x01, level, level, 0xAA])

    def _build_color_temp_payload(self, kelvin: int) -> bytes:
        # Color temperature: FF A3 02 <high> <low> <checksum> AA
        # Temperature is encoded as Kelvin value in big-endian 16-bit format
        high_byte = (kelvin >> 8) & 0xFF
        low_byte = kelvin & 0xFF
        # Checksum is sum of temperature bytes modulo 256
        checksum = (high_byte + low_byte) & 0xFF
        return bytes([0xFF, 0xA3, 0x02, high_byte, low_byte, checksum, 0xAA])


async def toggle_light(address: str, on: bool, brightness: Optional[int], dry_run: bool) -> None:
    async with BLELight(address=address, dry_run=dry_run) as light:
        await light.set_state(on=on, brightness=brightness)


def run_sync(coro: Awaitable[None]) -> None:
    asyncio.run(coro)
