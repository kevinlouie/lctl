#!/usr/bin/env python3
"""
Test script to find optimal BLE delay timings.

Run with different delay values to find the minimum reliable settings.
Requires --execute to actually test with the light.
"""

import argparse
import asyncio
import logging
import time
from dataclasses import dataclass

from lightcontroller.ble_controller import (
    BleakClient,
    DEFAULT_CONTROL_CHARACTERISTIC,
    DEFAULT_NOTIFY_CHARACTERISTIC,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class DelayConfig:
    post_subscribe: float = 0.5  # After notification subscription
    between_commands: float = 0.1  # Between sequential writes


class TestableLight:
    """Light controller with configurable delays for testing."""

    def __init__(self, address: str, delays: DelayConfig, dry_run: bool = True):
        self.address = address
        self.delays = delays
        self.dry_run = dry_run
        self._client = None

    def _notification_handler(self, sender: int, data: bytes) -> None:
        logger.debug("Notification from %s: %s", sender, data.hex())

    async def connect(self) -> None:
        if self.dry_run:
            logger.info("[dry-run] Would connect to %s", self.address)
            return
        self._client = BleakClient(self.address)

        t0 = time.perf_counter()
        await self._client.connect()
        connect_time = time.perf_counter() - t0
        logger.info("Connected in %.3fs", connect_time)

        t0 = time.perf_counter()
        await self._client.start_notify(DEFAULT_NOTIFY_CHARACTERISTIC, self._notification_handler)
        subscribe_time = time.perf_counter() - t0
        logger.info("Subscribed in %.3fs", subscribe_time)

        logger.info("Waiting %.3fs after subscribe (configurable)", self.delays.post_subscribe)
        await asyncio.sleep(self.delays.post_subscribe)

    async def disconnect(self) -> None:
        if self._client and self._client.is_connected:
            try:
                await self._client.stop_notify(DEFAULT_NOTIFY_CHARACTERISTIC)
            except Exception:
                pass
            await self._client.disconnect()
            logger.info("Disconnected")

    async def _write(self, payload: bytes, action: str) -> float:
        """Write a command and return the time taken."""
        if self.dry_run:
            logger.info("[dry-run] Would send %s: %s", action, payload.hex())
            return 0.0

        t0 = time.perf_counter()
        await self._client.write_gatt_char(DEFAULT_CONTROL_CHARACTERISTIC, payload, response=False)
        write_time = time.perf_counter() - t0
        logger.info("Wrote %s in %.3fs", action, write_time)

        logger.debug("Waiting %.3fs between commands", self.delays.between_commands)
        await asyncio.sleep(self.delays.between_commands)
        return write_time

    async def power_on(self) -> float:
        payload = bytes([0xFF, 0xA1, 0x01, 0x00, 0x00, 0xAA])
        return await self._write(payload, "power_on")

    async def power_off(self) -> float:
        """Power off requires two commands."""
        payload_init = bytes([0xFF, 0xA1, 0x01, 0x30, 0x30, 0xAA])
        t1 = await self._write(payload_init, "power_init")
        payload_off = bytes([0xFF, 0xA1, 0x01, 0x01, 0x01, 0xAA])
        t2 = await self._write(payload_off, "power_off")
        return t1 + t2

    async def set_brightness(self, level: int) -> float:
        level = max(0, min(100, level))
        payload = bytes([0xFF, 0xA2, 0x01, level, level, 0xAA])
        return await self._write(payload, f"brightness={level}")


async def run_test(address: str, delays: DelayConfig, dry_run: bool) -> dict:
    """Run a sequence of commands and measure total time."""
    results = {
        "post_subscribe_delay": delays.post_subscribe,
        "between_commands_delay": delays.between_commands,
        "success": False,
        "total_time": 0.0,
        "operations": [],
    }

    light = TestableLight(address, delays, dry_run)

    try:
        total_start = time.perf_counter()

        await light.connect()

        # Test sequence: on -> brightness -> off
        ops = []

        t = await light.power_on()
        ops.append(("power_on", t))

        t = await light.set_brightness(50)
        ops.append(("brightness_50", t))

        t = await light.set_brightness(80)
        ops.append(("brightness_80", t))

        t = await light.power_off()
        ops.append(("power_off", t))

        await light.disconnect()

        total_time = time.perf_counter() - total_start
        results["success"] = True
        results["total_time"] = total_time
        results["operations"] = ops

        logger.info("=" * 50)
        logger.info("Test completed successfully!")
        logger.info("Total time: %.3fs", total_time)
        logger.info("Delays: post_subscribe=%.3fs, between_commands=%.3fs",
                   delays.post_subscribe, delays.between_commands)

    except Exception as e:
        logger.error("Test failed: %s", e)
        results["error"] = str(e)

    return results


async def sweep_delays(address: str, dry_run: bool):
    """Test multiple delay configurations to find optimal values."""

    # Test configurations: (post_subscribe, between_commands)
    configs = [
        # Current baseline
        (0.5, 0.1),
        # Reduce post-subscribe
        (0.3, 0.1),
        (0.2, 0.1),
        (0.1, 0.1),
        (0.05, 0.1),
        # Reduce between-commands
        (0.2, 0.05),
        (0.2, 0.02),
        (0.1, 0.05),
        (0.1, 0.02),
        # Aggressive
        (0.05, 0.02),
        (0.02, 0.02),
    ]

    results = []
    for post_sub, between in configs:
        logger.info("\n" + "=" * 60)
        logger.info("Testing: post_subscribe=%.3fs, between_commands=%.3fs", post_sub, between)
        logger.info("=" * 60)

        delays = DelayConfig(post_subscribe=post_sub, between_commands=between)
        result = await run_test(address, delays, dry_run)
        results.append(result)

        if not dry_run:
            # Wait between tests to let the light settle
            await asyncio.sleep(1.0)

    # Summary
    print("\n" + "=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)
    print(f"{'Post-Sub':>10} {'Between':>10} {'Total':>10} {'Status':>10}")
    print("-" * 70)
    for r in results:
        status = "OK" if r["success"] else f"FAIL: {r.get('error', 'unknown')[:20]}"
        print(f"{r['post_subscribe_delay']:>10.3f} {r['between_commands_delay']:>10.3f} "
              f"{r['total_time']:>10.3f} {status:>10}")


def main():
    parser = argparse.ArgumentParser(description="Test BLE delay timings")
    parser.add_argument("--address", "-a", required=True, help="Light BLE address")
    parser.add_argument("--execute", action="store_true", help="Actually send BLE commands")
    parser.add_argument("--post-subscribe", type=float, default=0.5,
                       help="Delay after subscribing (default: 0.5)")
    parser.add_argument("--between-commands", type=float, default=0.1,
                       help="Delay between commands (default: 0.1)")
    parser.add_argument("--sweep", action="store_true",
                       help="Test multiple delay configurations")

    args = parser.parse_args()
    dry_run = not args.execute

    if dry_run:
        logger.info("DRY-RUN mode (add --execute to send real commands)")

    if args.sweep:
        asyncio.run(sweep_delays(args.address, dry_run))
    else:
        delays = DelayConfig(
            post_subscribe=args.post_subscribe,
            between_commands=args.between_commands,
        )
        asyncio.run(run_test(args.address, delays, dry_run))


if __name__ == "__main__":
    main()
