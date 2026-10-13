import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from lightcontroller.config import Settings, load_settings, save_settings
from lightcontroller.daemon import LightDaemon, describe_source
from lightcontroller.meeting_detector import DetectionResult


class FakeLight:
    def __init__(self, address, dry_run=False):
        self.address = address
        self.calls = []
        self.fail = False

    async def set_power(self, on):
        if self.fail:
            raise RuntimeError("unreachable")
        self.calls.append(("power", on))

    async def set_brightness(self, value):
        self.calls.append(("brightness", value))

    async def set_color_temperature(self, kelvin):
        self.calls.append(("color_temp", kelvin))

    async def disconnect(self):
        self.calls.append(("disconnect",))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


SLACK_MIC = DetectionResult(source="mic: slack", process_line="Slack (pid 1)")


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_file = Path(self.tmp.name) / "config.json"
        self.state_file = Path(self.tmp.name) / "state.json"
        self.detections = []
        self.clock = Clock()

    def make_daemon(self, **settings):
        save_settings(Settings(address="AA:BB:CC:DD:EE:FF", brightness=40, color_temp=3400, **settings), self.config_file)
        daemon = LightDaemon(
            config_file=self.config_file,
            state_file=self.state_file,
            light_factory=FakeLight,
            detect=lambda config: list(self.detections),
            clock=self.clock,
        )
        daemon._reload_settings()
        return daemon

    def step(self, daemon):
        asyncio.run(daemon.step())

    def test_call_turns_light_on_with_saved_look(self):
        daemon = self.make_daemon()
        self.detections = [SLACK_MIC]
        self.step(daemon)
        self.assertEqual(
            daemon.light.calls,
            [("power", True), ("color_temp", 3400), ("brightness", 40)],
        )
        self.assertTrue(daemon.snapshot()["inCall"])
        self.assertEqual(daemon.snapshot()["sources"], [{"kind": "mic", "name": "slack"}])

    def test_light_stays_on_through_off_delay(self):
        daemon = self.make_daemon(off_delay_seconds=8)
        self.detections = [SLACK_MIC]
        self.step(daemon)
        daemon.light.calls.clear()

        self.detections = []
        self.clock.now = 5
        self.step(daemon)
        self.assertEqual(daemon.light.calls, [])
        self.assertTrue(daemon.snapshot()["inCall"])

        self.clock.now = 10
        self.step(daemon)
        self.assertEqual(daemon.light.calls, [("power", False)])
        self.assertFalse(daemon.snapshot()["inCall"])
        self.assertEqual(daemon.snapshot()["sources"], [])

    def test_unknown_state_is_resent(self):
        daemon = self.make_daemon()
        self.step(daemon)
        self.step(daemon)
        # Unknown at start -> one "off"; known afterwards -> nothing more.
        self.assertEqual(daemon.light.calls, [("power", False)])

    def test_manual_modes_skip_detection(self):
        daemon = self.make_daemon(mode="on")
        daemon._detect = lambda config: self.fail("detection should not run in manual mode")
        self.step(daemon)
        self.assertEqual(daemon.light.calls[0], ("power", True))

    def test_look_change_is_applied_while_on(self):
        daemon = self.make_daemon(mode="on")
        self.step(daemon)
        daemon.light.calls.clear()
        save_settings(Settings(address="AA:BB:CC:DD:EE:FF", mode="on", brightness=70, color_temp=3400), self.config_file)
        daemon._reload_settings()
        self.step(daemon)
        self.assertEqual(daemon.light.calls, [("color_temp", 3400), ("brightness", 70)])

    def test_failed_write_leaves_state_unknown(self):
        daemon = self.make_daemon(mode="on")
        daemon.light.fail = True
        with self.assertRaises(RuntimeError):
            self.step(daemon)
        self.assertIsNone(daemon.snapshot()["lightOn"])
        daemon.light.fail = False
        self.step(daemon)
        self.assertTrue(daemon.snapshot()["lightOn"])

    def test_hooks_run_on_switch(self):
        hooks = self.config_file.parent / "hooks"
        hooks.mkdir()
        marker = Path(self.tmp.name) / "ran"
        for name in ("light-on", "light-off"):
            hook = hooks / name
            hook.write_text(f"#!/bin/sh\necho {name} >> {marker}\n")
            hook.chmod(0o755)
        daemon = self.make_daemon()
        self.detections = [SLACK_MIC]
        self.step(daemon)
        self.step(daemon)  # still on: no second hook
        self.detections = []
        self.clock.now = 100
        self.step(daemon)
        for _ in range(50):
            if marker.exists() and len(marker.read_text().split()) == 2:
                break
            time.sleep(0.02)
        self.assertEqual(marker.read_text().split(), ["light-on", "light-off"])

    def test_no_light_selected(self):
        save_settings(Settings(), self.config_file)
        daemon = LightDaemon(config_file=self.config_file, state_file=self.state_file, light_factory=FakeLight)
        daemon._reload_settings()
        self.step(daemon)
        self.assertEqual(daemon.snapshot()["error"], "No light selected")

    def test_bad_config_keeps_last_good_settings(self):
        daemon = self.make_daemon(mode="on")
        self.config_file.write_text(json.dumps({"mode": "sideways"}))
        daemon._reload_settings()
        self.assertEqual(daemon.settings.mode, "on")
        self.assertIn("Bad config", daemon.snapshot()["error"])

    def test_publish_writes_only_on_change(self):
        daemon = self.make_daemon()
        daemon.publish()
        first = self.state_file.stat().st_mtime_ns
        daemon.publish()
        self.assertEqual(self.state_file.stat().st_mtime_ns, first)
        state = json.loads(self.state_file.read_text())
        self.assertTrue(state["running"])
        self.assertEqual(state["address"], "AA:BB:CC:DD:EE:FF")


class ConfigTest(unittest.TestCase):
    def test_missing_file_gives_defaults(self):
        self.assertEqual(load_settings(Path("/nonexistent/lctl.json")), Settings())

    def test_unknown_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"mode": "off", "future": 1}))
            self.assertEqual(load_settings(path).mode, "off")

    def test_validation(self):
        with self.assertRaises(ValueError):
            Settings(brightness=101).validate()
        with self.assertRaises(ValueError):
            Settings(color_temp=2000).validate()


class DescribeSourceTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(describe_source(SLACK_MIC), {"kind": "mic", "name": "slack"})
        self.assertEqual(
            describe_source(DetectionResult(source="camera: slack (/dev/video2)", process_line="")),
            {"kind": "camera", "name": "slack"},
        )
        self.assertEqual(
            describe_source(DetectionResult(source="camera: PipeWire v4l2_input.cam", process_line="")),
            {"kind": "camera", "name": ""},
        )
        self.assertEqual(describe_source(DetectionResult(source="zoom", process_line="")), {"kind": "app", "name": "zoom"})


if __name__ == "__main__":
    unittest.main()
