import logging
import subprocess
from dataclasses import dataclass
from typing import List
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MeetingSignature:
    # Executable names (basename of `comm`) of a native meeting app.
    process_names: tuple[str, ...] = ()
    # Hosts of meeting URLs passed on a browser's command line, e.g.
    # `chromium --app=https://meet.google.com/abc-defg-hij`.
    url_hosts: tuple[str, ...] = ()


# Matching is exact on process names (not a substring of the whole command
# line), so `vim zoom-notes.md` or `grep meet.google.com` don't count as a
# meeting. The tradeoff: a browser only shows up here when a meeting URL is on
# its command line (app/PWA window or opened from a link while the browser was
# not running); a Meet tab in an already-running browser is invisible to `ps`.
DEFAULT_SIGNATURES = {
    "Zoom": MeetingSignature(process_names=("zoom", "zoom.us"), url_hosts=("zoom.us",)),
    "Google Meet": MeetingSignature(process_names=("Google Meet",), url_hosts=("meet.google.com",)),
}

BROWSER_PROCESS_NAMES = (
    "chrome",
    "google-chrome",
    "google chrome",
    "chromium",
    "chromium-browser",
    "brave",
    "brave browser",
    "msedge",
    "microsoft edge",
    "vivaldi-bin",
    "firefox",
    "firefox-bin",
)

# Linux truncates `comm` to 15 characters.
_COMM_LEN = 15


@dataclass
class ProcessInfo:
    name: str
    args: str


@dataclass
class DetectionResult:
    source: str
    process_line: str


def _ps_column(field: str) -> dict[str, str]:
    completed = subprocess.run(
        ["ps", "-A", "-ww", "-o", "pid=", "-o", f"{field}="],
        check=True,
        capture_output=True,
        text=True,
    )
    column = {}
    for line in completed.stdout.splitlines():
        pid, _, value = line.strip().partition(" ")
        column[pid] = value.strip()
    return column


def _read_process_table() -> List[ProcessInfo]:
    # comm and args are read separately (keyed by pid) because either can
    # contain spaces, which makes a combined `comm= args=` line ambiguous.
    try:
        names = _ps_column("comm")
        args = _ps_column("args")
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Failed to read process table: %s", exc)
        return []
    return [
        # macOS reports the full executable path in comm; keep the basename.
        ProcessInfo(name=comm.rsplit("/", 1)[-1], args=args.get(pid, ""))
        for pid, comm in names.items()
    ]


def _name_matches(name: str, candidates: tuple[str, ...]) -> bool:
    name = name.lower()[:_COMM_LEN]
    return any(name == candidate.lower()[:_COMM_LEN] for candidate in candidates)


def _has_meeting_url(args: str, hosts: tuple[str, ...]) -> bool:
    for arg in args.split():
        if arg.startswith("-"):
            arg = arg.partition("=")[2]  # --app=https://...
        try:
            url = urlsplit(arg)
        except ValueError:
            continue
        if url.scheme not in ("http", "https") or not url.hostname:
            continue
        if any(url.hostname == host or url.hostname.endswith("." + host) for host in hosts):
            return True
    return False


def _matches(signature: MeetingSignature, process: ProcessInfo) -> bool:
    if _name_matches(process.name, signature.process_names):
        return True
    if not signature.url_hosts or not _name_matches(process.name, BROWSER_PROCESS_NAMES):
        return False
    return _has_meeting_url(process.args, signature.url_hosts)


def find_meeting_processes(
    signatures: dict[str, MeetingSignature] | None = None,
) -> List[DetectionResult]:
    signatures = signatures or DEFAULT_SIGNATURES
    processes = _read_process_table()
    matches: List[DetectionResult] = []
    for process in processes:
        for source, signature in signatures.items():
            if _matches(signature, process):
                matches.append(DetectionResult(source=source, process_line=process.args or process.name))
                break
    return matches


def is_meeting_active() -> bool:
    return len(find_meeting_processes()) > 0
