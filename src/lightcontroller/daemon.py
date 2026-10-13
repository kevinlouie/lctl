"""Long-running light controller: `lctl daemon`.

One process owns the BLE connection and decides what the light should do:

- mode "on"/"off" pins the light; no call detection runs;
- mode "auto" turns it on while a call is detected and off once nothing has
  been detected for `off_delay_seconds`.

Mic streams are event driven (`pactl subscribe` wakes the loop as soon as an
app opens or closes a capture stream); camera use and meeting processes have
no change events, so they are re-checked every `poll_seconds`.

Settings come from `config.json` and are re-read on SIGHUP (`lctl set` sends
it). Executables named `light-on` / `light-off` in the `hooks` directory next
to it run (detached) after the light switches. What the daemon is doing is published to `state.json`, rewritten only
when something changes, for front ends to watch.
"""

import asyncio
import logging
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .ble_controller import BLELight
from .call_detector import DEFAULT_MIC_APPS, DetectionConfig, detect_calls
from .config import Settings, config_path, load_settings, state_path, write_json_atomic
from .meeting_detector import DetectionResult

logger = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 60.0
# A light that stopped answering can stall a BLE write for a long time; don't
# let turning it off hold up shutdown.
SHUTDOWN_OFF_TIMEOUT_SECONDS = 8.0
PACTL_RESTART_SECONDS = 5.0


def describe_source(result: DetectionResult) -> dict:
    """Split a detection into {kind, name} for display ("mic", "slack")."""
    kind, sep, rest = result.source.partition(": ")
    if not sep:
        # Process signatures are named after the meeting app ("zoom", "meet").
        return {"kind": "app", "name": result.source}
    name = rest.split(" (", 1)[0]
    if kind == "camera" and name.startswith("PipeWire "):
        name = ""
    return {"kind": kind, "name": name}


class LightDaemon:
    def __init__(
        self,
        config_file: Path | None = None,
        state_file: Path | None = None,
        dry_run: bool = False,
        light_factory: Callable[..., BLELight] = BLELight,
        detect: Callable[[DetectionConfig], list[DetectionResult]] = detect_calls,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config_file = config_file or config_path()
        self.state_file = state_file or state_path()
        self.dry_run = dry_run
        self._light_factory = light_factory
        self._detect = detect
        self._clock = clock

        self.settings = Settings()
        self.light: BLELight | None = None
        # None means unknown: never set, or a write failed part-way.
        self._light_on: bool | None = None
        # (brightness, color_temp) last written while on.
        self._applied: tuple[int, int] | None = None
        self._last_detected: float | None = None
        self._sources: list[dict] = []
        self._in_call = False
        self._error: str | None = None
        self._published: dict | None = None

        self._wake = asyncio.Event()
        self._reload = True
        self._stopping = False

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGHUP, self.request_reload)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop)
        mic_watcher = asyncio.create_task(self._watch_mic_streams())
        failures = 0
        try:
            while not self._stopping:
                if self._reload:
                    self._reload = False
                    self._reload_settings()
                try:
                    await self.step()
                    failures = 0
                    timeout = self.settings.poll_seconds if self.settings.mode == "auto" else None
                except Exception as exc:
                    # Light unplugged, out of range, BlueZ busy, ... - keep going.
                    failures += 1
                    timeout = min(self.settings.poll_seconds * 2 ** failures, MAX_BACKOFF_SECONDS)
                    self._error = f"Light unreachable: {exc}"
                    logger.error("Light update failed (%s); retrying in %.1fs", exc, timeout)
                    logger.debug("Failure details", exc_info=True)
                    if self.light:
                        await self.light.disconnect()
                self.publish()
                await self._sleep(timeout)
        finally:
            mic_watcher.cancel()
            await self._shutdown()

    def request_reload(self) -> None:
        self._reload = True
        self._wake.set()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    async def _sleep(self, timeout: float | None) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def _shutdown(self) -> None:
        if self.light and self._light_on is not False:
            try:
                await asyncio.wait_for(self.light.set_power(False), SHUTDOWN_OFF_TIMEOUT_SECONDS)
                self._light_on = False
            except Exception as exc:
                logger.warning("Could not turn the light off on exit: %s", exc)
        if self.light:
            await self.light.disconnect()
        self._sources = []
        self._in_call = False
        self.publish(running=False)

    def _reload_settings(self) -> None:
        try:
            settings = load_settings(self.config_file)
        except (OSError, ValueError, TypeError) as exc:
            # Keep running on the last good settings rather than dying on a typo.
            self._error = f"Bad config {self.config_file}: {exc}"
            logger.error("%s", self._error)
            return
        if settings.address != self.settings.address or self.light is None:
            old = self.light
            self.light = (
                self._light_factory(address=settings.address, dry_run=self.dry_run) if settings.address else None
            )
            self._light_on = None
            self._applied = None
            if old:
                # Not awaited inline: reload runs between steps, and a stuck
                # disconnect shouldn't hold up the new light.
                asyncio.ensure_future(old.disconnect())
        if settings.mode != "auto":
            self._last_detected = None
            self._sources = []
            self._in_call = False
        self.settings = settings
        self._error = None
        logger.info(
            "Settings: address=%s mode=%s brightness=%s color_temp=%sK",
            settings.address,
            settings.mode,
            settings.brightness,
            settings.color_temp,
        )

    # ------------------------------------------------------------- decisions

    async def step(self) -> None:
        settings = self.settings
        if settings.mode == "auto":
            detections = await asyncio.to_thread(self._detect, self._detection_config())
            now = self._clock()
            if detections:
                self._last_detected = now
                self._sources = [describe_source(d) for d in detections]
            elif self._last_detected is None or now - self._last_detected >= settings.off_delay_seconds:
                self._last_detected = None
                self._sources = []
            self._in_call = self._last_detected is not None
            want_on = self._in_call
        else:
            want_on = settings.mode == "on"

        if self.light is None:
            self._error = "No light selected"
            return
        await self._apply(want_on)
        self._error = None

    def _detection_config(self) -> DetectionConfig:
        return DetectionConfig(
            camera=self.settings.camera,
            mic=self.settings.mic,
            mic_apps=DEFAULT_MIC_APPS + tuple(self.settings.mic_apps),
        )

    async def _apply(self, want_on: bool) -> None:
        light = self.light
        settings = self.settings
        if want_on:
            look = (settings.brightness, settings.color_temp)
            switched = self._light_on is not True
            if switched:
                logger.info("Turning light on (%s)", self._sources or settings.mode)
                self._light_on = None
                self._applied = None
                await light.set_power(True)
                self._light_on = True
            if self._applied != look:
                await light.set_color_temperature(settings.color_temp)
                await light.set_brightness(settings.brightness)
                self._applied = look
            if switched:
                self._run_hook("light-on")
        elif self._light_on is not False:
            logger.info("Turning light off")
            self._light_on = None
            await light.set_power(False)
            self._light_on = False
            self._applied = None
            self._run_hook("light-off")

    def _run_hook(self, name: str) -> None:
        hook = self.config_file.parent / "hooks" / name
        if not os.access(hook, os.X_OK):
            return
        logger.info("Running %s", hook)
        try:
            # Fire and forget: a slow hook (e.g. camera setup) must not hold up the light.
            subprocess.Popen(
                [str(hook)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            logger.warning("Could not run %s: %s", hook, exc)

    # ------------------------------------------------------------- mic events

    async def _watch_mic_streams(self) -> None:
        """Wake the loop whenever a capture stream opens, closes or changes."""
        while True:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "pactl",
                    "subscribe",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    stdin=asyncio.subprocess.DEVNULL,
                )
            except OSError as exc:
                logger.warning("pactl unavailable (%s); mic changes are only seen when polling", exc)
                return
            try:
                async for line in proc.stdout:
                    if b"source-output" in line and self.settings.mode == "auto" and self.settings.mic:
                        self._wake.set()
                await proc.wait()
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            # The sound server restarted; resubscribe once it is back.
            logger.info("pactl subscribe exited (%s); restarting", proc.returncode)
            await asyncio.sleep(PACTL_RESTART_SECONDS)

    # ------------------------------------------------------------- state

    def snapshot(self, running: bool = True) -> dict:
        settings = self.settings
        return {
            "running": running,
            "pid": os.getpid(),
            "dryRun": self.dry_run,
            "mode": settings.mode,
            "address": settings.address,
            "brightness": settings.brightness,
            "colorTemp": settings.color_temp,
            "lightOn": self._light_on,
            "inCall": self._in_call,
            "sources": self._sources,
            "error": self._error,
        }

    def publish(self, running: bool = True) -> None:
        state = self.snapshot(running)
        if state == self._published:
            return
        self._published = state
        try:
            write_json_atomic(self.state_file, {**state, "updated": datetime.now(timezone.utc).isoformat()})
        except OSError as exc:
            logger.warning("Could not write %s: %s", self.state_file, exc)
