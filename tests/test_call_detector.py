import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lightcontroller import call_detector
from lightcontroller.call_detector import (
    DEFAULT_MIC_APPS,
    DetectionConfig,
    detect_calls,
    find_camera_fds,
    find_mic_streams,
    parse_mic_streams,
    parse_running_cameras,
)
from lightcontroller.meeting_detector import DetectionResult

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


class ParseMicStreamsTest(unittest.TestCase):
    def setUp(self):
        self.outputs = _load("pactl-source-outputs.json")

    def test_matches_meeting_apps_even_when_muted_or_corked(self):
        results = parse_mic_streams(self.outputs, DEFAULT_MIC_APPS)
        self.assertEqual(
            [(r.source, r.process_line) for r in results],
            [
                ("mic: slack", "Slack (pid 29545)"),
                ("mic: chromium", "Chromium input (pid 4242)"),
            ],
        )

    def test_skips_monitor_peak_detect_and_internal_streams(self):
        # Even when the app list names them explicitly.
        results = parse_mic_streams(self.outputs, ("chrome", "pavucontrol", "slack"))
        self.assertEqual([r.source for r in results], ["mic: slack"])

    def test_extra_apps_match_binary_or_application_name(self):
        self.assertEqual([r.source for r in parse_mic_streams(self.outputs, ("pacat",))], ["mic: pacat"])
        self.assertEqual([r.source for r in parse_mic_streams(self.outputs, ("PARECORD",))], ["mic: pacat"])

    def test_tolerates_unexpected_shapes(self):
        self.assertEqual(parse_mic_streams(None, DEFAULT_MIC_APPS), [])
        self.assertEqual(parse_mic_streams({"not": "a list"}, DEFAULT_MIC_APPS), [])
        self.assertEqual(parse_mic_streams([None, 1, {"properties": None}], DEFAULT_MIC_APPS), [])


class ParseRunningCamerasTest(unittest.TestCase):
    def test_reports_only_running_video_sources(self):
        results = parse_running_cameras(_load("pw-dump.json"))
        self.assertEqual(
            [(r.source, r.process_line) for r in results],
            [
                (
                    "camera: PipeWire v4l2_input.pci-0000_c3_00.0-usb-0_3_1.0",
                    "Laptop Webcam Module (2nd Gen) (V4L2)",
                )
            ],
        )

    def test_idle_camera_is_not_a_call(self):
        dump = copy.deepcopy(_load("pw-dump.json"))
        for obj in dump:
            if obj.get("id") == 141:
                obj["info"]["state"] = "suspended"
        self.assertEqual(parse_running_cameras(dump), [])

    def test_tolerates_unexpected_shapes(self):
        self.assertEqual(parse_running_cameras(None), [])
        self.assertEqual(parse_running_cameras([None, {"info": None}, {"info": {"props": None}}]), [])


class FindCameraFdsTest(unittest.TestCase):
    def setUp(self):
        self.proc = tempfile.TemporaryDirectory()
        self.addCleanup(self.proc.cleanup)

    def _process(self, pid, comm, fds, cmdline=None):
        root = Path(self.proc.name, str(pid))
        (root / "fd").mkdir(parents=True)
        (root / "comm").write_text(comm + "\n")
        (root / "cmdline").write_bytes(((cmdline or comm) + "\0").replace(" ", "\0").encode())
        for fd, target in enumerate(fds):
            os.symlink(target, root / "fd" / str(fd))

    def test_finds_processes_holding_a_video_device(self):
        self._process(100, "chrome", ["/dev/null", "/dev/video0"], cmdline="/opt/chrome/chrome --type=utility")
        self._process(200, "bash", ["/dev/pts/1"])
        self._process(300, "pipewire", ["/dev/video0"])
        self._process(301, "wireplumber", ["/dev/video1"])
        Path(self.proc.name, "self").mkdir()  # non-pid entries are ignored

        results = find_camera_fds(self.proc.name)

        self.assertEqual(
            [(r.source, r.process_line) for r in results],
            [("camera: chrome (/dev/video0)", "/opt/chrome/chrome --type=utility")],
        )

    def test_unreadable_or_vanished_processes_are_skipped(self):
        Path(self.proc.name, "400").mkdir()  # no fd dir, like a pid that just exited
        self.assertEqual(find_camera_fds(self.proc.name), [])

    def test_missing_proc_is_not_an_error(self):
        self.assertEqual(find_camera_fds(os.path.join(self.proc.name, "missing")), [])


class CommandFailureTest(unittest.TestCase):
    def test_missing_or_failing_tools_disable_the_signal(self):
        errors = [
            FileNotFoundError("pactl"),
            subprocess.TimeoutExpired("pactl", 2),
            subprocess.CalledProcessError(1, "pactl"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(call_detector.subprocess, "run", side_effect=error):
                    self.assertEqual(find_mic_streams(), [])

    def test_invalid_json_disables_the_signal(self):
        completed = subprocess.CompletedProcess([], 0, stdout="not json", stderr="")
        with mock.patch.object(call_detector.subprocess, "run", return_value=completed):
            self.assertEqual(call_detector.find_running_pipewire_cameras(), [])


class DetectCallsTest(unittest.TestCase):
    def _patch(self, **results):
        calls = []
        names = ["find_meeting_processes", "find_mic_streams", "find_camera_fds", "find_running_pipewire_cameras"]
        for name in names:
            def fake(*args, _name=name, **kwargs):
                calls.append(_name)
                return results.get(_name, [])
            patcher = mock.patch.object(call_detector, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        return calls

    def test_short_circuits_on_first_hit(self):
        calls = self._patch(find_mic_streams=[DetectionResult("mic: slack", "Slack")])
        results = detect_calls(DetectionConfig())
        self.assertEqual([r.source for r in results], ["mic: slack"])
        self.assertEqual(calls, ["find_meeting_processes", "find_mic_streams"])

    def test_find_all_runs_every_enabled_signal(self):
        calls = self._patch(find_camera_fds=[DetectionResult("camera: chrome (/dev/video0)", "chrome")])
        results = detect_calls(DetectionConfig(), find_all=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(calls), 4)

    def test_disabled_signals_are_not_run(self):
        calls = self._patch()
        self.assertEqual(detect_calls(DetectionConfig(camera=False, mic=False), find_all=True), [])
        self.assertEqual(calls, ["find_meeting_processes"])

    def test_extra_mic_apps_are_passed_through(self):
        with mock.patch.object(call_detector, "find_mic_streams", return_value=[]) as find_mic:
            with mock.patch.object(call_detector, "find_meeting_processes", return_value=[]):
                detect_calls(DetectionConfig(camera=False, mic_apps=("lark",)))
        find_mic.assert_called_once_with(("lark",))


if __name__ == "__main__":
    unittest.main()
