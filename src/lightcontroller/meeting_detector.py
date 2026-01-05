import logging
import subprocess
from dataclasses import dataclass
from typing import Iterable, List

logger = logging.getLogger(__name__)


DEFAULT_SIGNATURES = {
    "Zoom": ["zoom", "zoom.us"],
    "Google Meet": ["meet.google.com", "Google Meet"],
}


@dataclass
class DetectionResult:
    source: str
    process_line: str


def _read_process_table() -> List[str]:
    try:
        completed = subprocess.run(
            ["ps", "-A", "-o", "comm=", "-o", "args="],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.splitlines()
    except subprocess.SubprocessError as exc:
        logger.warning("Failed to read process table: %s", exc)
        return []


def find_meeting_processes(
    signatures: dict[str, Iterable[str]] | None = None,
) -> List[DetectionResult]:
    signatures = signatures or DEFAULT_SIGNATURES
    processes = _read_process_table()
    matches: List[DetectionResult] = []
    for line in processes:
        for source, tokens in signatures.items():
            if any(token.lower() in line.lower() for token in tokens):
                matches.append(DetectionResult(source=source, process_line=line.strip()))
                break
    return matches


def is_meeting_active() -> bool:
    return len(find_meeting_processes()) > 0
