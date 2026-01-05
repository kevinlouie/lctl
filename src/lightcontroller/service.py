import asyncio
import logging
from dataclasses import dataclass

from .ble_controller import BLELight
from .meeting_detector import is_meeting_active

logger = logging.getLogger(__name__)


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
        self._light_on = False

    async def run_forever(self) -> None:
        logger.info(
            "Starting meeting watcher (poll=%.1fs, brightness=%s, dry_run=%s)",
            self.config.poll_seconds,
            self.config.brightness,
            self.config.dry_run,
        )
        try:
            while True:
                await self._tick()
                await asyncio.sleep(self.config.poll_seconds)
        finally:
            await self.light.disconnect()

    async def _tick(self) -> None:
        active = is_meeting_active()
        if active and not self._light_on:
            logger.info("Meeting detected; turning light on")
            await self.light.set_state(on=True, brightness=self.config.brightness)
            self._light_on = True
        elif not active and self._light_on:
            logger.info("No meeting detected; turning light off")
            await self.light.set_power(False)
            self._light_on = False
