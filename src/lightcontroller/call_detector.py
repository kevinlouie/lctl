"""Detect calls from camera and microphone usage (Linux).

Complements the process/URL heuristic in `meeting_detector`, which can't see a
Slack huddle (Slack is always running) or a Meet tab in an already-open
browser. Signals, each best-effort - a missing tool or /proc just disables it:

- camera: any process holding a /dev/video* fd, or a PipeWire video source node
  in the "running" state;
- mic: a meeting app (see DEFAULT_MIC_APPS) with a capture stream open on the
  PulseAudio/PipeWire server, whether or not it is muted.
"""

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Callable, List

from .meeting_detector import BROWSER_PROCESS_NAMES, DetectionResult, _name_matches, find_meeting_processes

logger = logging.getLogger(__name__)

COMMAND_TIMEOUT_SECONDS = 2.0

# Apps whose open capture stream means "in a call". Matched exactly (case
# insensitive) against the stream's application.process.binary or
# application.name. Browsers are included so a Meet/Zoom/Teams tab counts; the
# cost is that any page using the mic (e.g. voice typing) does too.
DEFAULT_MIC_APPS = (
    "slack",
    "zoom",
    "zoom.us",
    "teams",
    "teams-for-linux",
) + BROWSER_PROCESS_NAMES

# PipeWire daemons keep the camera open on behalf of their clients; the
# PipeWire node state covers those, so their fds don't count by themselves.
CAMERA_FD_IGNORED_PROCESSES = ("pipewire", "wireplumber", "pipewire-pulse")

# pavucontrol's level meters open capture streams of their own.
_PEAK_DETECT_APP_IDS = ("org.PulseAudio.pavucontrol",)

_logged_once: set[str] = set()


@dataclass
class DetectionConfig:
    processes: bool = True
    camera: bool = True
    mic: bool = True
    mic_apps: tuple[str, ...] = DEFAULT_MIC_APPS


def _debug_once(key: str, message: str, *args: object) -> None:
    if key not in _logged_once:
        _logged_once.add(key)
        logger.debug(message, *args)


def _run_json(argv: List[str]) -> object | None:
    try:
        completed = subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        return json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        _debug_once(argv[0], "%s unavailable, skipping that signal: %s", argv[0], exc)
        return None


def _is_true(value: object) -> bool:
    # pactl reports every property as a string.
    return value is True or str(value).lower() == "true"


def parse_mic_streams(source_outputs: object, apps: tuple[str, ...]) -> List[DetectionResult]:
    """Meeting-app capture streams in `pactl -f json list source-outputs` output."""
    if not isinstance(source_outputs, list):
        return []
    matches: List[DetectionResult] = []
    for output in source_outputs:
        props = output.get("properties") if isinstance(output, dict) else None
        if not isinstance(props, dict):
            continue
        binary = props.get("application.process.binary")
        # Loopbacks, monitors and other server-internal streams have no client binary.
        if not binary:
            continue
        if _is_true(props.get("stream.monitor")) or props.get("application.id") in _PEAK_DETECT_APP_IDS:
            continue
        name = props.get("application.name") or ""
        if not (_name_matches(binary, apps) or (name and _name_matches(name, apps))):
            continue
        pid = props.get("application.process.id")
        matches.append(
            DetectionResult(
                source=f"mic: {binary}",
                process_line=f"{name or binary} (pid {pid})" if pid else (name or binary),
            )
        )
    return matches


def find_mic_streams(apps: tuple[str, ...] = DEFAULT_MIC_APPS) -> List[DetectionResult]:
    return parse_mic_streams(_run_json(["pactl", "-f", "json", "list", "source-outputs"]), apps)


def parse_running_cameras(pw_dump: object) -> List[DetectionResult]:
    """PipeWire video sources in the "running" state in `pw-dump` output."""
    if not isinstance(pw_dump, list):
        return []
    matches: List[DetectionResult] = []
    for obj in pw_dump:
        info = obj.get("info") if isinstance(obj, dict) else None
        if not isinstance(info, dict):
            continue
        props = info.get("props") or {}
        if props.get("media.class") != "Video/Source" or info.get("state") != "running":
            continue
        node = props.get("node.name") or f"node {obj.get('id')}"
        matches.append(
            DetectionResult(
                source=f"camera: PipeWire {node}",
                process_line=props.get("node.description") or node,
            )
        )
    return matches


def find_running_pipewire_cameras() -> List[DetectionResult]:
    return parse_running_cameras(_run_json(["pw-dump"]))


def _read_proc_text(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def find_camera_fds(proc_root: str = "/proc") -> List[DetectionResult]:
    """Processes (readable by this user) with a /dev/video* device open."""
    try:
        pids = [entry for entry in os.listdir(proc_root) if entry.isdigit()]
    except OSError as exc:
        _debug_once(proc_root, "%s unavailable, skipping camera fd check: %s", proc_root, exc)
        return []
    matches: List[DetectionResult] = []
    for pid in pids:
        fd_dir = os.path.join(proc_root, pid, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue  # Another user's process, or it exited.
        device = None
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target.startswith("/dev/video"):
                device = target
                break
        if device is None:
            continue
        name = _read_proc_text(os.path.join(proc_root, pid, "comm"))
        if name in CAMERA_FD_IGNORED_PROCESSES:
            continue
        matches.append(
            DetectionResult(
                source=f"camera: {name or 'pid ' + pid} ({device})",
                process_line=_read_proc_text(os.path.join(proc_root, pid, "cmdline")) or name,
            )
        )
    return matches


def detect_calls(config: DetectionConfig, find_all: bool = False) -> List[DetectionResult]:
    """Run the enabled signals, cheapest first.

    Stops at the first signal that finds something unless `find_all` is set.
    """
    checks: List[Callable[[], List[DetectionResult]]] = []
    if config.processes:
        checks.append(find_meeting_processes)
    if config.mic:
        checks.append(lambda: find_mic_streams(config.mic_apps))
    if config.camera:
        checks.append(find_camera_fds)
        checks.append(find_running_pipewire_cameras)
    results: List[DetectionResult] = []
    for check in checks:
        results.extend(check())
        if results and not find_all:
            break
    return results
