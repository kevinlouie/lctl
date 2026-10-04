import asyncio
import logging
from dataclasses import dataclass

from .ble_controller import BLELight
from .meeting_detector import is_meeting_active

logger = logging.getLogger(__name__)

# Upper bound for the delay between retries after consecutive tick failures.
MAX_BACKOFF_SECONDS = 60.0


@dataclass
class ServiceConfig:
    address: str
    brightness: int = 80
    poll_seconds: float = 5.0
    dry_run: bool = True


class MeetingAwareLightService:
    def __init__(self, config: ServiceConfig) -> None:
        self.config = config
        self.light = BLELight(
            address=config.address,
            dry_run=config.dry_run,
        )
        # None means unknown (a power write failed part-way); the next tick
        # re-sends whichever state is wanted.
        self._light_on: bool | None = False

    async def run_forever(self) -> None:
        logger.info(
            "Starting meeting watcher (poll=%.1fs, brightness=%s, dry_run=%s)",
            self.config.poll_seconds,
            self.config.brightness,
            self.config.dry_run,
        )
        failures = 0
        try:
            while True:
                try:
                    await self._tick()
                    failures = 0
                    delay = self.config.poll_seconds
                except Exception as exc:
                    # Light unplugged, out of range, BlueZ busy, ... - keep watching.
                    # CancelledError/KeyboardInterrupt are BaseException and still stop the loop.
                    failures += 1
                    delay = min(
                        self.config.poll_seconds * (2 ** failures),
                        max(self.config.poll_seconds, MAX_BACKOFF_SECONDS),
                    )
                    logger.error("Light update failed (%s); retrying in %.1fs", exc, delay)
                    logger.debug("Tick failure details", exc_info=True)
                    # Drop the (possibly stale) connection so the next write reconnects.
                    await self.light.disconnect()
                await asyncio.sleep(delay)
        finally:
            await self.light.disconnect()

    async def _tick(self) -> None:
        active = is_meeting_active()
        if active and self._light_on is not True:
            logger.info("Meeting detected; turning light on")
            self._light_on = None
            await self.light.set_power(True)
            # Record power state before the brightness write so a failure there
            # can't leave the light on with the service believing it is off.
            self._light_on = True
            await self.light.set_brightness(self.config.brightness)
        elif not active and self._light_on is not False:
            logger.info("No meeting detected; turning light off")
            self._light_on = None
            await self.light.set_power(False)
            self._light_on = False
