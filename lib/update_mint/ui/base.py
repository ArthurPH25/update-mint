
'Interface base.\n\nThe engine runs in a thread and communicates with the interface through short methods (``step_start``,\n``progress``, ``ask``, etc.). The interface stores state under a lock and the main thread\nrenders it. There are two interfaces: :class:`SimpleUI` (plain text, used when curses is unavailable) and the\ncurses ``TUI`` (ui/tui.py), with mouse support.\n'
from __future__ import annotations

import os
import select
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Set

from ..storage import format_duration
from ..constants import APP_NAME, APP_VERSION
from ..executor import StopSignal

OPERATIONS = (
    ("U", 'FULLY UPDATE THE SYSTEM AND COMPUTER',
     'Packages (apt), kernel, drivers, Flatpak/Snap, and firmware (only with your confirmation). Respects phased updates not yet released by apt, held packages, and items ignored in Update Manager.'),
    ("R", 'FULLY REPAIR THE SYSTEM AND COMPUTER',
     'Diagnoses and fixes only what has evidence: dpkg/apt, system files, boot (initramfs/GRUB), services, disks (SMART), and file systems. If everything is fine, it says so.'),
    ("C", 'PERFORM A DEEP CLEANUP OF THE SYSTEM AND COMPUTER',
     'apt cache, orphaned packages, journal, temporary files, app and browser caches. Never touches personal files, the Trash, crash dumps, or AI models.'),
)
FINISHED_STATUSES = ("ok", "warning", "error", "skipped", "interrupted")


class Symbols:
    def __init__(self, utf8: bool):
        if utf8:
            self.ok, self.warning, self.error, self.skipped, self.inter = "✔", "⚠", "✖", "–", "■"
            self.pending, self.full, self.empty = "·", "█", "░"
            self.spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            self.checked_mark, self.unchecked_mark, self.arrow = "[x]", "[ ]", "▶"
        else:
            self.ok, self.warning, self.error, self.skipped, self.inter = "OK", "!!", "XX", "--", "##"
            self.pending, self.full, self.empty = ".", "#", "-"
            self.spinner = "|/-\\"
            self.checked_mark, self.unchecked_mark, self.arrow = "[x]", "[ ]", ">"

    def symbol_for_status(self, status: str) -> str:
        return {"ok": self.ok, "warning": self.warning, "error": self.error, "skipped": self.skipped,
                "interrupted": self.inter}.get(status, self.pending)


@dataclass
class Item:
    step: object
    title: str
    weight: int
    round: int
    status: str = "pending"
    reason: str = ""
    duration: float = 0.0


@dataclass
class Question:
    title: str
    text: str
    options: List[str]
    pattern: int = 0
    response: Optional[int] = None
    event: threading.Event = field(default_factory=threading.Event)


class BaseUI:
    def __init__(self, utf8: bool, stop_signal: Optional[StopSignal] = None):
        self.utf8 = utf8
        self.symbols = Symbols(utf8)
        self.stop_signal = stop_signal or StopSignal()
        self.lock = threading.RLock()
        self.items: List[Item] = []
        self.total_weight = 0
        self.completed_weight = 0
        self.current: Optional[Item] = None
        self.step_fraction: Optional[float] = None
        self.step_text = ""
        self.command_text = ""
        self.estimate_text = ""
        self.round = 0
        self.reason = ""
        self.output_tail: Deque[str] = deque(maxlen=400)
        self.pending_question: Optional[Question] = None
        self.finished_event = threading.Event()
        self.t0 = time.time()
        self.header = ""

    
    def command(self, text: str) -> None:
        with self.lock:
            self.command_text = text

    def output(self, line: str) -> None:
        with self.lock:
            self.output_tail.append(line)

    def progress(self, frac: Optional[float], text: str = "") -> None:
        with self.lock:
            self.step_fraction = None if frac is None else max(0.0, min(1.0, frac))
            if text:
                self.step_text = text

    def estimate(self, text: str) -> None:
        with self.lock:
            self.estimate_text = text

    def set_plan(self, steps, round: int, reason: str) -> None:
        with self.lock:
            self.round = round
            self.reason = reason
            for part in steps:
                self.items.append(Item(part, part.title, part.weight, round))
                self.total_weight += part.weight

    def step_start(self, step) -> None:
        with self.lock:
            target = next((i for i in self.items if i.status == "pending" and i.step is step), None) \
                or next((i for i in self.items if i.status == "pending" and getattr(i.step, "id", None) == step.id), None)
            if target is None:                                 
                target = Item(step, step.title, step.weight, self.round)
                self.items.append(target)
                self.total_weight += step.weight
            target.status = "running"
            self.current = target
            self.step_fraction = None
            self.step_text = ""
            self.command_text = ""

    def step_end(self, reg) -> None:
        with self.lock:
            item = self.current
            if item is None or getattr(item.step, "id", None) != reg.id:
                item = next((i for i in self.items if i.status in ("pending", "running")
                             and getattr(i.step, "id", None) == reg.id), None)
            if item is not None:
                item.status = reg.status
                item.reason = reg.reason
                item.duration = reg.duration
                self.completed_weight += item.weight
            self.current = None
            self.step_fraction = None
            self.step_text = ""
            self.command_text = ""

    def ask(self, title: str, text: str, options: List[str], pattern: int = 0) -> int:
        'Blocks the ENGINE until the user responds (the main thread displays the dialog).'
        part = Question(title, text, options, pattern)
        with self.lock:
            self.pending_question = part
        while not part.event.wait(0.2):
            if self.finished_event.is_set() or self.stop_signal.requested:
                break
        return part.pattern if part.response is None else part.response

    def end(self) -> None:
        self.finished_event.set()

    
    def overall_fraction(self) -> float:
        with self.lock:
            if self.total_weight <= 0:
                return 0.0
            part = 0.0
            if self.current is not None:
                part = (self.step_fraction or 0.0) * self.current.weight
            return min(1.0, (self.completed_weight + part) / self.total_weight)

    def elapsed(self) -> str:
        return format_duration(time.time() - self.t0)

    def progress_bar(self, frac: Optional[float], width: int, spinner: int = 0) -> str:
        if width < 3:
            return ""
        if frac is None:  
            pos = spinner % max(1, width - 4)
            cells = [self.symbols.empty] * width
            for i in range(pos, min(width, pos + 4)):
                cells[i] = self.symbols.full
            return "".join(cells)
        n = int(round(frac * width))
        return self.symbols.full * n + self.symbols.empty * (width - n)


class SimpleUI(BaseUI):
    'Plain-text interface: used when curses is unavailable, in limited terminals.'

    def __init__(self, utf8: bool, stop_signal: Optional[StopSignal] = None, output=None, input=None):
        super().__init__(utf8, stop_signal)
        self.out = output or sys.stdout
        self.inp = input or sys.stdin
        self._tty = hasattr(self.out, "isatty") and self.out.isatty()
        self._last_line = ""

    
    def _read_line(self) -> Optional[str]:
        'Reads one line without blocking forever: Ctrl+C/closed terminal returns None. No buffering (os.read).'
        try:
            fd = self.inp.fileno()
        except (AttributeError, ValueError, OSError):
            r = self.inp.readline()
            return None if r == "" else r
        data = b""
        while True:
            if self.stop_signal.requested:
                return None
            ready, _, _ = select.select([fd], [], [], 0.3)
            if not ready:
                continue
            try:
                b = os.read(fd, 1)
            except OSError:
                return None
            if not b:
                return None if not data else data.decode("utf-8", "replace")
            if b == b"\n":
                return data.decode("utf-8", "replace")
            data += b

    def _print_line(self, txt: str = "") -> None:
        if self._tty and self._last_line:
            self.out.write("\r\x1b[2K")
        self._last_line = ""
        self.out.write(txt + "\n")
        self.out.flush()

    
    def select(self, selected: Set[str]) -> Optional[Set[str]]:
        selected = set(selected)
        while True:
            self._print_line(f"\n{APP_NAME} {APP_VERSION} — choose what to do\n")
            for i, (k, name, desc) in enumerate(OPERATIONS, 1):
                self._print_line(f"  {i}. {'[x]' if k in selected else '[ ]'} {name}")
                self._print_line(f"         {desc}")
            self._print_line('\nEnter numbers to select/deselect (e.g. 1 3), Enter to start, ? for help, q to quit.')
            r = self._read_line()
            if r is None:
                return None
            r = r.strip().lower()
            if r in ("q", "quit"):
                return None
            if r in ("?", 'help', "h"):
                from .help import HELP_TEXT
                self._print_line("\n".join(HELP_TEXT))
                continue
            if r == "":
                if selected:
                    return selected
                self._print_line("Select at least one operation.")
                continue
            for tok in r.replace(",", " ").split():
                if tok in ("1", "2", "3"):
                    k = OPERATIONS[int(tok) - 1][0]
                    selected ^= {k}
                elif tok == "a":
                    selected = {"U", "R", "C"} if len(selected) < 3 else set()

    
    def dialog(self, title: str, text: str, options: List[str], pattern: int = 0) -> int:
        self._print_line(f"\n== {title} ==\n{text}\n")
        for i, o in enumerate(options, 1):
            self._print_line(f"  {i}. {o}" + ('  (default)' if i - 1 == pattern else ""))
        self.out.write(f"Choose [1-{len(options)}] (Enter = default): ")
        self.out.flush()
        r = self._read_line()
        if r is None:
            return pattern
        r = r.strip()
        if r.isdigit() and 1 <= int(r) <= len(options):
            return int(r) - 1
        return pattern

    
    def _print_new_items(self, printed_items: set) -> None:
        with self.lock:
            items = list(self.items)
        for it in items:
            if it.status == "running" and id(it) not in printed_items:
                printed_items.add(id(it))
                self._print_line(f"{self.symbols.arrow} {it.title}")
            elif it.status in FINISHED_STATUSES and ("f", id(it)) not in printed_items:
                printed_items.add(("f", id(it)))
                self._print_line(f"  {self.symbols.symbol_for_status(it.status)} {it.title}" + (f" — {it.reason}" if it.reason else "")
                        + (f"  ({it.duration:.0f}s)" if it.duration >= 2 else ""))

    def run_engine(self, thread: threading.Thread) -> None:
        thread.start()
        spinner = 0
        self._print_line(f"\n{self.header}\n")
        printed_items: set = set()
        while not (self.finished_event.is_set() and not thread.is_alive()):
            with self.lock:
                part = self.pending_question
            if part is not None and part.response is None and not part.event.is_set():
                part.response = self.dialog(part.title, part.text, part.options, part.pattern)
                part.event.set()
                with self.lock:
                    self.pending_question = None
            self._print_new_items(printed_items)
            if self.stop_signal.requested and ("stop_signal",) not in printed_items:
                printed_items.add(("stop_signal",))
                self._print_line('  ⏸ stop requested: safely finishing the current step…' if self.utf8
                        else '  stop requested: safely finishing the current step...')
            with self.lock:
                current = self.current
            if self._tty and current is not None:
                spinner += 1
                with self.lock:
                    pct = int(self.overall_fraction() * 100)
                    txt = f"  {self.symbols.spinner[spinner % len(self.symbols.spinner)]} {pct:3d}%  {self.command_text or self.step_text}"[:78]
                self.out.write("\r\x1b[2K" + txt)
                self.out.flush()
                self._last_line = txt
            time.sleep(0.15)
        thread.join()
        self._print_new_items(printed_items)
        self._print_line()

    def show_report(self, lines) -> None:
        'No report screen here: main() prints the report to the terminal right after close().'

    def start(self) -> None:
        pass

    def suspend(self) -> None:
        pass

    def resume(self) -> None:
        pass

    def close(self) -> None:
        pass
