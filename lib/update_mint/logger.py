
'Operation logging.\n\nThe complete log (each command, its output, and exit code) is stored in\n``~/.local/state/update-mint/`` (XDG_STATE_HOME). The program runs as the user,\nso these files belong to the user and no root process writes inside their directory.\nThe most recent ``KEEP_LOGS`` files (logs and reports) are retained.\n'
from __future__ import annotations

import glob
import os
import threading
import time
from typing import Optional

from .constants import KEEP_LOGS, APP_NAME


def state_directory(home: str) -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
    return os.path.join(base, APP_NAME)


class LogRecorder:
    def __init__(self, directory: str, keep: int = KEEP_LOGS):
        self.directory = directory
        self.keep = keep
        self._lock = threading.Lock()
        self.timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.path = os.path.join(directory, f"{APP_NAME}_{self.timestamp}.log")
        self.report_path = os.path.join(directory, f"report_{self.timestamp}.txt")
        self._f = open(self.path, "a", encoding="utf-8", errors="replace", buffering=1)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._rotate(f"{APP_NAME}_*.log", self.keep)
        self._rotate("report_*.txt", self.keep - 1)

    def _rotate(self, pattern: str, keep: int) -> None:
        files = sorted(glob.glob(os.path.join(self.directory, pattern)))
        for old in files[: max(0, len(files) - keep)]:
            try:
                os.unlink(old)
            except OSError:
                pass

    def write(self, text: str) -> None:
        'Timestamped line.'
        with self._lock:
            self._f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), text))

    def raw_write(self, text: str) -> None:
        'Command output, without a timestamp.'
        with self._lock:
            self._f.write(text if text.endswith("\n") else text + "\n")

    def save_report(self, text: str) -> Optional[str]:
        try:
            with open(self.report_path, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(self.report_path, 0o600)
            return self.report_path
        except OSError:
            return None

    def close(self) -> None:
        with self._lock:
            try:
                self._f.close()
            except OSError:
                pass


class RunMarker:
    'File that exists ONLY while a run is in progress. If it still exists on the next run,\n    the previous execution ended unexpectedly (power loss, dead terminal, kill -9).'

    def __init__(self, directory: str):
        self.path = os.path.join(directory, "in-progress")

    def previous(self) -> str:
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                return f.read().strip()
        except OSError:
            return ""

    def write(self, text: str) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} — {text}\n")
        except OSError:
            pass

    def complete(self) -> None:
        try:
            os.unlink(self.path)
        except OSError:
            pass
