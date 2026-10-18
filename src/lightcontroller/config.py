"""Persistent settings and runtime state for the `lctl daemon`.

The daemon reads its settings from a JSON file (`lctl set` edits it and pokes
the daemon with SIGHUP) and publishes what it is doing to a JSON state file
that front ends such as the Omarchy bar plugin watch for changes.
"""

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

MODES = ("auto", "on", "off")
MIN_BRIGHTNESS = 0
MAX_BRIGHTNESS = 100
MIN_COLOR_TEMP = 2700
MAX_COLOR_TEMP = 6500
ADDRESS_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$", re.IGNORECASE)


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "lctl" / "config.json"


def state_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(tempfile.gettempdir(), f"lctl-{os.getuid()}")
    return Path(base) / "lctl" / "state.json"


@dataclass
class Settings:
    address: str | None = None
    mode: str = "auto"
    brightness: int = 80
    color_temp: int = 4000
    camera: bool = True
    mic: bool = True
    mic_apps: list[str] = field(default_factory=list)
    # Camera use and meeting processes have no change events, so they are
    # polled; mic streams are event driven (`pactl subscribe`).
    poll_seconds: float = 5.0
    # How long a call must stay undetected before the light goes off.
    off_delay_seconds: float = 8.0

    def validate(self) -> None:
        # Hand-edited JSON can hold anything; check types before ranges.
        if self.address is not None and not (isinstance(self.address, str) and ADDRESS_RE.match(self.address)):
            raise ValueError(f"address must be a BLE address like AA:BB:CC:DD:EE:FF, not {self.address!r}")
        for name in ("camera", "mic"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be true or false, not {getattr(self, name)!r}")
        for name in ("brightness", "color_temp"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be a whole number, not {value!r}")
        for name in ("poll_seconds", "off_delay_seconds"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{name} must be a number, not {value!r}")
        if not isinstance(self.mic_apps, list) or not all(isinstance(app, str) for app in self.mic_apps):
            raise ValueError(f"mic_apps must be a list of app names, not {self.mic_apps!r}")
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}, not {self.mode!r}")
        if not MIN_BRIGHTNESS <= self.brightness <= MAX_BRIGHTNESS:
            raise ValueError(f"brightness must be {MIN_BRIGHTNESS}-{MAX_BRIGHTNESS}, not {self.brightness}")
        if not MIN_COLOR_TEMP <= self.color_temp <= MAX_COLOR_TEMP:
            raise ValueError(f"color_temp must be {MIN_COLOR_TEMP}-{MAX_COLOR_TEMP}K, not {self.color_temp}")
        if not self.poll_seconds >= 0.5:
            raise ValueError(f"poll_seconds must be at least 0.5, not {self.poll_seconds}")
        if not self.off_delay_seconds >= 0:
            raise ValueError(f"off_delay_seconds must be non-negative, not {self.off_delay_seconds}")


def load_settings(path: Path | None = None) -> Settings:
    """Read settings, falling back to defaults for a missing file or keys."""
    path = path or config_path()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return Settings()
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a JSON object")
    known = {f.name for f in fields(Settings)}
    settings = Settings(**{key: value for key, value in raw.items() if key in known})
    settings.validate()
    return settings


def save_settings(settings: Settings, path: Path | None = None) -> None:
    settings.validate()
    write_json_atomic(path or config_path(), asdict(settings))


def write_json_atomic(path: Path, data: object) -> None:
    """Replace `path` in one rename so watchers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_state(path: Path | None = None) -> dict | None:
    try:
        state = json.loads((path or state_path()).read_text())
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def daemon_pid(state: dict | None, proc_root: str = "/proc") -> int | None:
    """The pid of the daemon that wrote `state`, if it is still running.

    A daemon killed without a chance to clean up (SIGKILL, OOM, power loss)
    leaves `running: true` behind, and its pid may since have been reused.
    """
    pid = state.get("pid") if state and state.get("running") else None
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    try:
        with open(f"{proc_root}/{pid}/cmdline", "rb") as f:
            argv = f.read().split(b"\0")
    except OSError:
        return None
    # Interpreter flags (pipx's shebang adds -E) shift where the script lands;
    # `python -m lightcontroller.cli daemon` has no lctl at all.
    launcher = any(arg == b"lctl" or arg.endswith(b"/lctl") or arg.startswith(b"lightcontroller") for arg in argv)
    return pid if launcher and b"daemon" in argv else None
