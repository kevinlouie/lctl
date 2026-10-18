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
to it run (detached) after the light switches. What the daemon is doing is
published to `state.json`, rewritten only when something changes, for front
ends to watch.
"""

import asyncio
import logging
import os
import signal
import subprocess
import threading
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


def backoff_seconds(base: float, failures: int, cap: float = MAX_BACKOFF_SECONDS) -> float:
    """Delay before retry number `failures`: doubling from `base`, capped."""
    # Clamp the exponent: 2 ** 1024 overflows a float after a long outage.
    return min(base * 2 ** min(failures, 32), cap)


def describe_error(exc: BaseException) -> str:
    # Timeouts and some BlueZ errors stringify to "".
    return str(exc) or type(exc).__name__


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
        self._step_task: asyncio.Task | None = None
        # Fire-and-forget work (retiring a deselected light) kept referenced
        # so it isn't garbage collected mid-flight.
        self._background: set[asyncio.Task] = set()

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
                # A task so stop() can cancel it: a BLE connect to a missing
                # light retries for well over a minute.
                self._step_task = asyncio.create_task(self.step())
                try:
                    await self._step_task
                    failures = 0
                    timeout = self.settings.poll_seconds if self.settings.mode == "auto" else None
                except asyncio.CancelledError:
                    if not self._stopping:
                        raise
                    break
                except Exception as exc:
                    # Light unplugged, out of range, BlueZ busy, ... - keep going.
                    failures += 1
                    timeout = backoff_seconds(self.settings.poll_seconds, failures)
                    self._error = f"Light unreachable: {describe_error(exc)}"
                    logger.error("Light update failed (%s); retrying in %.1fs", describe_error(exc), timeout)
                    logger.debug("Failure details", exc_info=True)
                    if self.light:
                        await self.light.disconnect()
                finally:
                    self._step_task = None
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
        if self._step_task:
            self._step_task.cancel()

    async def _sleep(self, timeout: float | None) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except asyncio.TimeoutError:
            # Not woken: leave the event alone so a wake-up that lands right
            # after the timeout isn't cleared unseen.
            return
        self._wake.clear()

    async def _shutdown(self) -> None:
        if self.light:
            if await self._retire(self.light, turn_off=self._light_on is not False, why="on exit"):
                self._light_on = False
        if self._background:
            await asyncio.wait(self._background, timeout=SHUTDOWN_OFF_TIMEOUT_SECONDS)
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
            old, old_on = self.light, self._light_on
            self.light = (
                self._light_factory(address=settings.address, dry_run=self.dry_run) if settings.address else None
            )
            self._light_on = None
            self._applied = None
            if old:
                # Not awaited inline: reload runs between steps, and a stuck
                # light shouldn't hold up the new one.
                self._spawn(self._retire(old, turn_off=old_on is not False, why="after switching lights"))
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

    async def _retire(self, light: BLELight, turn_off: bool, why: str) -> bool:
        """Best-effort, time-bounded power-off (if asked) and disconnect.

        Returns whether the light was turned off.
        """
        off = False
        if turn_off:
            try:
                await asyncio.wait_for(light.set_power(False), SHUTDOWN_OFF_TIMEOUT_SECONDS)
                off = True
            except Exception as exc:
                logger.warning("Could not turn the light off %s: %s", why, describe_error(exc))
        try:
            await asyncio.wait_for(light.disconnect(), SHUTDOWN_OFF_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("Could not disconnect %s: %s", why, describe_error(exc))
        return off

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

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
            proc = subprocess.Popen(
                [str(hook)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            logger.warning("Could not run %s: %s", hook, exc)
            return
        # Reap it when it exits so it doesn't linger as a zombie.
        threading.Thread(target=proc.wait, name=f"hook-{name}", daemon=True).start()

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
