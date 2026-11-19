
'curses interface (Python standard library — no extra dependencies).\n\nScreens: selection (keyboard and mouse), execution panel (overall and step progress, current command,\nestimate, step list, and recent output), dialogs (engine prompts), and text viewer\n(help and report). If curses cannot start, ``main`` falls back to the simple UI.\n'
from __future__ import annotations

import curses
import locale
import os
import textwrap
import threading
import time
from typing import List, Optional, Set, Tuple

from ..constants import APP_NAME, APP_VERSION
from .help import HELP_TEXT
from .base import OPERATIONS, FINISHED_STATUSES, BaseUI

class TerminalDisconnected(Exception):
    'The terminal was closed (getch starts returning an error immediately, without waiting).'


ENTER_KEY = (10, 13, curses.KEY_ENTER)
MIN_SIZE = (60, 16)     


class TUI(BaseUI):
    def __init__(self, utf8, stop_signal=None):
        super().__init__(utf8, stop_signal)
        self.scr = None
        self.colors = False
        self._spinner_index = 0
        self._fast_polls = 0

    def _getch(self) -> int:
        'getch detects a dead terminal: without it, the keyboard loop spins at 100% CPU.'
        t0 = time.monotonic()
        ch = self.scr.getch()
        if ch == -1:
            if time.monotonic() - t0 < 0.02:
                self._fast_polls += 1
                if self._fast_polls > 30:
                    raise TerminalDisconnected()
                time.sleep(0.01)
            else:
                self._fast_polls = 0
        else:
            self._fast_polls = 0
        return ch

    
    def start(self) -> None:
        try:
            locale.setlocale(locale.LC_ALL, "")
        except locale.Error:
            pass
        os.environ.setdefault("ESCDELAY", "25")
        self.scr = curses.initscr()
        curses.noecho()
        curses.cbreak()
        self.scr.keypad(True)
        self.scr.idlok(False)                    
        self.scr.idcok(False)                    
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors() and not os.environ.get("NO_COLOR"):
            try:
                curses.start_color()
                curses.use_default_colors()
                for i, color in enumerate((curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED,
                                         curses.COLOR_CYAN, curses.COLOR_MAGENTA), 1):
                    curses.init_pair(i, color, -1)
                self.colors = True
            except curses.error:
                self.colors = False
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
            curses.mouseinterval(0)
        except curses.error:
            pass

    def close(self) -> None:
        if self.scr is not None:
            try:
                curses.endwin()
            except curses.error:
                pass
            self.scr = None

    def suspend(self) -> None:
        'Restores the terminal to normal mode (so sudo can request the password).'
        curses.def_prog_mode()
        curses.endwin()

    def resume(self) -> None:
        curses.reset_prog_mode()
        self.scr.refresh()
        self.scr.clear()

    
    def _get_attribute(self, name: str) -> int:
        c = (lambda n: curses.color_pair(n)) if self.colors else (lambda n: 0)
        return {
            "ok": c(1), "warning": c(2), "error": c(3), "accent": c(4) | curses.A_BOLD, "dim": curses.A_DIM,
            "title": c(4) | curses.A_BOLD, "sec": curses.A_BOLD | c(4), "sec2": curses.A_BOLD,
            "txt": 0, "focus": curses.A_REVERSE, "bold": curses.A_BOLD, "magenta": c(5),
        }.get(name, 0)

    def _put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w or x < 0:
            return
        text = text[: max(0, w - x - 1)]
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def _draw_horizontal_line(self, y: int, x: int, width: int, attr: int = 0) -> None:
        self._put(y, x, ("─" if self.utf8 else "-") * width, attr | curses.A_DIM)

    @staticmethod
    def _wrap_text(text: str, width: int) -> List[str]:
        return textwrap.wrap(text, width=max(10, width)) or [""]

    def _is_small(self) -> bool:
        h, w = self.scr.getmaxyx()
        return w < MIN_SIZE[0] or h < MIN_SIZE[1]

    def _small_warning(self) -> None:
        h, w = self.scr.getmaxyx()
        self.scr.erase()
        self._put(0, 0, f"Window too small ({w}x{h}).")
        self._put(1, 0, f"Enlarge to {MIN_SIZE[0]}x{MIN_SIZE[1]} or larger.")
        self.scr.refresh()

    
    def select(self, selected: Set[str]) -> Optional[Set[str]]:
        selected = set(selected)
        focus = 0
        msg = ""
        self.scr.timeout(250)
        while True:
            if self.stop_signal.requested:           
                return None
            hit_regions = self._draw_selection(selected, focus, msg)
            ch = self._getch()
            if ch == -1:
                continue
            msg = ""                              
            if ch == curses.KEY_RESIZE:
                self.scr.erase()
                continue
            if ch in (curses.KEY_UP, ord("k")):
                focus = (focus - 1) % len(OPERATIONS)
            elif ch in (curses.KEY_DOWN, ord("j"), 9):
                focus = (focus + 1) % len(OPERATIONS)
            elif ch == ord(" "):
                selected ^= {OPERATIONS[focus][0]}
            elif ch in (ord("1"), ord("2"), ord("3")):
                focus = ch - ord("1")
                selected ^= {OPERATIONS[focus][0]}
            elif ch in (ord("a"), ord("A")):
                selected = {"U", "R", "C"} if len(selected) < 3 else set()
            elif ch in ENTER_KEY:
                if selected:
                    return selected
                msg = 'Select at least one operation (Space) before starting.'
            elif ch in (ord("?"), curses.KEY_F1, ord("h"), ord("H")):
                self.show_help()
            elif ch in (ord("q"), ord("Q"), 27):
                return None
            elif ch == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, b = curses.getmouse()
                except curses.error:
                    continue
                if not (b & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED)):
                    continue
                for (y0, y1, x0, x1, action) in hit_regions:
                    if y0 <= my <= y1 and x0 <= mx <= x1:
                        if action[0] == "opc":
                            focus = action[1]
                            selected ^= {OPERATIONS[focus][0]}
                        elif action[1] == "start":
                            if selected:
                                return selected
                            msg = 'Select at least one operation before starting.'
                        elif action[1] == 'help':
                            self.show_help()
                        elif action[1] == "quit":
                            return None
                        break

    def _draw_selection(self, selected: Set[str], focus: int, msg: str):
        self.scr.erase()
        if self._is_small():
            self._small_warning()
            return []
        h, w = self.scr.getmaxyx()
        width = min(w - 4, 94)
        x0 = (w - width) // 2
        compact = h < 26
        hit_regions: List[Tuple[int, int, int, int, tuple]] = []
        y = 0
        self._put(y, x0, f" {APP_NAME} {APP_VERSION} ", self._get_attribute("title") | curses.A_REVERSE)
        y += 1
        if self.header:
            self._put(y, x0, self.header[: width], self._get_attribute("dim"))
        y += 2
        self._put(y, x0, 'Choose what to do (Space selects/deselects; Enter starts):', self._get_attribute("bold"))
        y += 2
        for i, (k, name, desc) in enumerate(OPERATIONS):
            active = k in selected
            checkbox = (self.symbols.checked_mark if active else self.symbols.unchecked_mark)
            label = f" {checkbox} {i + 1}  {name} "
            attr = self._get_attribute("focus") if i == focus else (self._get_attribute("ok") | curses.A_BOLD if active else 0)
            line_descriptions = self._wrap_text(desc, width - 10)[: 2 if compact else 3]
            y_ini = y
            self._put(y, x0, label.ljust(min(width, len(label) + 2)), attr)
            if len(label) + 14 <= width:       
                self._put(y, x0 + len(label) + 3, "[sudo required]", self._get_attribute("warning"))
            for l in line_descriptions:
                y += 1
                self._put(y, x0 + 7, l, self._get_attribute("dim"))
            hit_regions.append((y_ini, y, x0, x0 + width, ("opc", i)))
            y += 1 if compact else 2
        buttons = [("start", "[ START ]", "ok"), ('help', '[ HELP ]', "accent"), ("quit", "[ QUIT ]", "dim")]
        x = x0
        for name, txt, color in buttons:
            self._put(y, x, txt, self._get_attribute(color) | curses.A_BOLD)
            hit_regions.append((y, y, x, x + len(txt), ("btn", name)))
            x += len(txt) + 3
        y += 2
        if selected:
            names = " + ".join({"U": 'Update', "R": 'Repair', "C": 'Cleanup'}[k] for k in "URC" if k in selected)
            self._put(y, x0, f"Selected: {names}", self._get_attribute("ok"))
        else:
            self._put(y, x0, "Nothing selected.", self._get_attribute("warning"))
        if msg:
            self._put(y + 1, x0, msg, self._get_attribute("warning") | curses.A_BOLD)
        footer = '↑↓ move · Space select · Enter START · a all · ? or F1 = HELP · q quit'
        self._put(h - 2, x0, footer if self.utf8 else footer.replace("↑↓", "arrows"), self._get_attribute("dim"))
        self._put(h - 1, x0, 'The sudo password is requested once, after your selection. The program runs as you.',
                  self._get_attribute("dim"))
        self.scr.refresh()
        return hit_regions

    
    def dialog(self, title: str, text: str, options: List[str], pattern: int = 0) -> int:
        choice = pattern
        self.scr.timeout(200)
        while True:
            if self.stop_signal.requested:           
                return pattern
            hit_regions = self._draw_dialog(title, text, options, choice)
            ch = self._getch()
            if ch == -1:
                continue
            if ch == curses.KEY_RESIZE:
                self.scr.erase()
            elif ch in (curses.KEY_UP, ord("k")):
                choice = (choice - 1) % len(options)
            elif ch in (curses.KEY_DOWN, ord("j"), 9):
                choice = (choice + 1) % len(options)
            elif ch in ENTER_KEY:
                return choice
            elif ch == 27:
                return pattern
            elif ch >= ord("1") and ch < ord("1") + len(options):
                return ch - ord("1")
            elif ch == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, b = curses.getmouse()
                except curses.error:
                    continue
                if b & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                    for (y0, y1, x0, x1, i) in hit_regions:
                        if y0 <= my <= y1 and x0 <= mx <= x1:
                            return i

    def _draw_dialog(self, title: str, text: str, options: List[str], choice: int):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        width = max(40, min(w - 6, 88))
        x0 = (w - width) // 2
        body: List[str] = []
        for par in text.split("\n"):
            body += self._wrap_text(par, width - 4) if par.strip() else [""]
        body = body[: max(3, h - len(options) - 8)]
        height = len(body) + len(options) + 5
        y0 = max(0, (h - height) // 2)
        for yy in range(y0, y0 + height):
            self._put(yy, x0 - 1, " " * (width + 2), curses.A_REVERSE if yy == y0 else 0)
        self._put(y0, x0, f" {title} ", self._get_attribute("title") | curses.A_REVERSE)
        y = y0 + 2
        for l in body:
            self._put(y, x0 + 1, l, 0)
            y += 1
        y += 1
        hit_regions = []
        for i, o in enumerate(options):
            checked_mark = self.symbols.arrow if i == choice else " "
            self._put(y, x0 + 1, f"{checked_mark} {i + 1}. {o}", self._get_attribute("focus") if i == choice else 0)
            hit_regions.append((y, y, x0, x0 + width, i))
            y += 1
        self._put(y + 1, x0 + 1, '↑↓ choose · Enter confirm · Esc = default option', self._get_attribute("dim"))
        self.scr.refresh()
        return hit_regions

    
    def _viewer(self, title: str, lines: List[Tuple[str, str]], wrap_at=None) -> None:
        '``lines``: [(level, text)] already wrapped OR, if ``wrap_at`` is supplied, it is called with the width.'
        pos = 0
        self.scr.timeout(250)
        while True:
            self.scr.erase()
            if self._is_small():
                self._small_warning()
                ch = self._getch()
                if ch in (ord("q"), 27):
                    return
                continue
            h, w = self.scr.getmaxyx()
            vis = wrap_at(w - 3) if wrap_at else lines
            body = h - 3
            maximum = max(0, len(vis) - body)
            pos = max(0, min(pos, maximum))
            self._put(0, 0, f" {title} ".ljust(w - 1), self._get_attribute("title") | curses.A_REVERSE)
            for i in range(body):
                j = pos + i
                if j >= len(vis):
                    break
                level, text = vis[j]
                self._put(i + 1, 1, text, self._get_attribute(level))
            pct = 100 if maximum == 0 else int(100 * pos / maximum)
            self._put(h - 1, 1, f"↑↓ PgUp/PgDn Home/End scroll · q/Enter/Esc close · {pct}%"
                      if self.utf8 else f"arrows PgUp/PgDn scroll - q closes - {pct}%", self._get_attribute("dim"))
            self.scr.refresh()
            ch = self._getch()
            if ch == -1:
                continue
            if ch in (ord("q"), ord("Q"), 27, 10, 13, curses.KEY_ENTER):
                return
            if ch in (curses.KEY_DOWN, ord("j")):
                pos += 1
            elif ch in (curses.KEY_UP, ord("k")):
                pos -= 1
            elif ch in (curses.KEY_NPAGE, ord(" ")):
                pos += body - 1
            elif ch == curses.KEY_PPAGE:
                pos -= body - 1
            elif ch == curses.KEY_HOME:
                pos = 0
            elif ch == curses.KEY_END:
                pos = maximum
            elif ch == curses.KEY_MOUSE:
                try:
                    _, _, _, _, b = curses.getmouse()
                except curses.error:
                    continue
                if b & getattr(curses, "BUTTON4_PRESSED", 0):
                    pos -= 3
                elif b & getattr(curses, "BUTTON5_PRESSED", 0):
                    pos += 3

    def show_help(self) -> None:
        def q(width):
            output: List[Tuple[str, str]] = []
            for l in HELP_TEXT:
                if not l.strip():
                    output.append(("txt", ""))
                    continue
                if l.startswith(APP_NAME):
                    output.append(("title", l))
                    continue
                if not l.startswith(" ") and l.split(" (")[0].replace(" ", "").isupper():
                    output.append(("sec", l))
                    continue
                ind = len(l) - len(l.lstrip(" "))
                for part in textwrap.wrap(l.strip(), width=max(20, width), initial_indent=" " * ind,
                                       subsequent_indent=" " * (ind + 2)) or [""]:
                    output.append(("txt", part))
            return output
        self._viewer('Help — ' + APP_NAME, [], wrap_at=q)

    def show_report(self, lines) -> None:
        from .. import report
        self._viewer('Final report', [], wrap_at=lambda larg: report.wrap(lines, larg, self.utf8))

    
    def run_engine(self, thread: threading.Thread) -> None:
        thread.start()
        self.scr.timeout(120)
        while True:
            with self.lock:
                part = self.pending_question
            if part is not None and part.response is None and not part.event.is_set():
                part.response = self.dialog(part.title, part.text, part.options, part.pattern)
                part.event.set()
                with self.lock:
                    self.pending_question = None
                self.scr.timeout(120)
            end = self.finished_event.is_set() and not thread.is_alive()
            self._draw_panel(end)
            ch = self._getch()
            if ch == curses.KEY_RESIZE:
                self.scr.erase()
            elif end and ch in (10, 13, curses.KEY_ENTER, ord("q"), ord(" "), 27):
                break
        thread.join()

    def _draw_panel(self, end: bool) -> None:
        self.scr.erase()
        if self._is_small():
            self._small_warning()
            return
        h, w = self.scr.getmaxyx()
        self._spinner_index += 1
        with self.lock:
            items = list(self.items)
            current = self.current
            output_tail = list(self.output_tail)
            command, step_text = self.command_text, self.step_text
            step_fraction = self.step_fraction
            estim, reason = self.estimate_text, self.reason
        frac = self.overall_fraction()
        completed = len([i for i in items if i.status in FINISHED_STATUSES])
        self._put(0, 0, f" {APP_NAME} {APP_VERSION} ".ljust(w - 1), self._get_attribute("title") | curses.A_REVERSE)
        self._put(0, max(0, w - 14), f" {self.elapsed()} ", self._get_attribute("title") | curses.A_REVERSE)
        if self.header:
            self._put(1, 1, self.header, self._get_attribute("dim"))
        bw = max(10, min(40, w - 30))
        self._put(3, 1, "Overall  ", self._get_attribute("bold"))
        self._put(3, 8, self.progress_bar(frac, bw), self._get_attribute("ok"))
        self._put(3, 9 + bw, f"{int(frac * 100):3d}%  ({completed}/{len(items)} steps)")
        self._put(4, 1, 'Step  ', self._get_attribute("bold"))
        self._put(4, 8, self.progress_bar(step_fraction if current else (1.0 if end else 0.0), bw, self._spinner_index // 2), self._get_attribute("accent"))
        if current is not None and step_fraction is not None:
            self._put(4, 9 + bw, f"{int(step_fraction * 100):3d}%")
        y = 5
        if end:
            self._put(y, 1, 'Completed. Press Enter to view the report.', self._get_attribute("ok") | curses.A_BOLD)
        elif current is not None:
            spinner = self.symbols.spinner[self._spinner_index % len(self.symbols.spinner)]
            self._put(y, 1, f"{spinner} {current.title}", self._get_attribute("bold"))
            if step_text:
                self._put(y, min(w - 2, 4 + len(current.title) + 2), "· " + step_text, self._get_attribute("dim"))
            self._put(y + 1, 1, ("$ " + command) if command else "", self._get_attribute("dim"))
        else:
            self._put(y, 1, "…", self._get_attribute("dim"))
        y = 7
        compact = h < 24
        if estim:
            for l in self._wrap_text(estim, w - 4)[: 1 if compact else 2]:
                self._put(y, 1, l, self._get_attribute("accent"))
                y += 1
        if reason and not compact:
            for l in self._wrap_text("Plan: " + reason, w - 4)[:2]:
                self._put(y, 1, l, self._get_attribute("dim"))
                y += 1
        y += 1
        
        display_height = h - 2 - y
        if display_height >= 8:
            step_count = max(3, int((display_height - 2) * 0.55))
            output_lines = max(1, display_height - 2 - step_count)
        else:
            step_count, output_lines = max(1, display_height - 1), 0
        self._put(y, 1, "Steps", self._get_attribute("sec"))
        self._draw_horizontal_line(y, 8, max(0, w - 10))
        y += 1
        idx_ref = next((i for i, it in enumerate(items) if it.status == "running"), None)
        if idx_ref is None:
            idx_ref = max([i for i, it in enumerate(items) if it.status in FINISHED_STATUSES] or [0])
        start = max(0, min(idx_ref - step_count // 2, max(0, len(items) - step_count)))
        for it in items[start: start + step_count]:
            if it.status == "running":
                ic, color = self.symbols.arrow, "bold"
            elif it.status == "pending":
                ic, color = self.symbols.pending, "dim"
            else:
                ic, color = self.symbols.symbol_for_status(it.status), {"ok": "ok", "warning": "warning", "error": "error"}.get(it.status, "dim")
            extra = (f" — {it.reason}" if it.reason else "") + (f"  ({it.duration:.0f}s)" if it.duration >= 2 else "")
            self._put(y, 1, f"{ic} ", self._get_attribute(color))
            self._put(y, 3, (it.title + extra)[: w - 5],
                      self._get_attribute("bold") if it.status == "running"
                      else self._get_attribute(color if it.status in ("warning", "error") else "txt"))
            y += 1
        if output_lines:
            y_title = h - 2 - output_lines
            self._put(y_title, 1, 'Output', self._get_attribute("sec"))
            self._draw_horizontal_line(y_title, 8, max(0, w - 10))
            for k, l in enumerate(output_tail[-output_lines:]):
                self._put(y_title + 1 + k, 2, l[: w - 4], self._get_attribute("dim"))
        if self.stop_signal.requested and not end:
            self._put(h - 1, 1, ("⏸ " if self.utf8 else "") + 'Stop requested: safely finishing the current step (Ctrl+C again forces termination).', self._get_attribute("warning") | curses.A_BOLD)
        elif not end:
            self._put(h - 1, 1, 'Ctrl+C: safely stop after the current step', self._get_attribute("dim"))
        self.scr.refresh()
