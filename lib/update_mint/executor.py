
"External command execution.\n\nPrinciples (all derived from the project's security rules):\n  * The program runs as the USER. Only commands that need privileges use\n    ``sudo -n`` (the password is requested ONCE beforehand by :func:`authenticate_sudo`).\n  * Every command is recorded in the log with its output and exit code.\n  * Each child runs in its own process group: terminal Ctrl+C does not kill it mid-operation (which\n    would break dpkg). This module, through :class:`StopSignal`, decides when to stop it.\n  * Commands that CHANGE the system (``action=True``) are not executed in dry-run mode.\n  * Arguments are always passed as a list (never ``shell=True``): no command injection.\n"
from __future__ import annotations

import os
import re
import select
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .environment import Environment, child_environment

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]")
_STATUS_PREFIXES = ("dlstatus:", "pmstatus:", "pmerror:", "pmconffile:", "media-change:")

if sys.version_info >= (3, 11):
    _OWN_PROCESS_GROUP = {"process_group": 0}
else:
    _OWN_PROCESS_GROUP = {"preexec_fn": os.setpgrp}


class StopSignal:
    'User stop requests: first = safely finish the current step; second and later = force stop.'

    def __init__(self) -> None:
        self.n = 0

    def request(self) -> None:
        self.n += 1

    @property
    def requested(self) -> bool:
        return self.n >= 1

    @property
    def force(self) -> bool:
        return self.n >= 2


@dataclass
class Cmd:
    argv: List[str]
    return_code: int
    out: str
    dur: float
    simulated: bool = False
    timed_out: bool = False
    interrupted: bool = False
    denied: bool = False

    @property
    def ok(self) -> bool:
        return self.return_code == 0

    def contains(self, *patterns: str) -> bool:
        return any(re.search(part, self.out) for part in patterns)

    def lines(self) -> List[str]:
        return [l for l in self.out.splitlines() if l.strip()]


def _descendants(pid: int) -> List[int]:
    children: Dict[int, List[int]] = {}
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", "rb") as f:
                    data = f.read().decode("utf-8", "replace")
                ppid = int(data[data.rindex(")") + 2:].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            children.setdefault(ppid, []).append(int(name))
    except OSError:
        pass
    order = [pid]
    i = 0
    while i < len(order):
        order.extend(children.get(order[i], []))
        i += 1
    return order


def terminate_process_tree(pid: int, signal: int, via_sudo: bool) -> None:
    denied: List[int] = []
    for part in _descendants(pid):
        try:
            os.kill(part, signal)
        except ProcessLookupError:
            pass
        except PermissionError:
            denied.append(part)
    if denied and via_sudo:
        try:
            subprocess.run(["sudo", "-n", "kill", "-s", str(int(signal))] + [str(part) for part in denied],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15, env=child_environment(), close_fds=True)
        except (OSError, subprocess.SubprocessError):
            pass


def short_label(argv: List[str]) -> str:
    'Human-readable command label for the screen and warnings (without env/VAR=/timeout/apt options).'
    a = list(argv)
    while a and (a[0] in ("sudo", "env", "timeout", "nice", "ionice", "-n") or "=" in a[0]
                 or re.fullmatch(r"-?[0-9]+", a[0]) or a[0] in ("-c", "-n")):
        if a[0] == "sudo" and len(a) > 1 and a[1] == "-n":
            a = a[2:]
        else:
            a = a[1:]
    if not a:
        return " ".join(argv[:3])
    if a[0] in ("apt-get", "apt"):
        sub, targets, skip = "", [], False
        for t in a[1:]:
            if skip:
                skip = False
                continue
            if t == "-o":
                skip = True
                continue
            if t.startswith("-"):
                continue
            if not sub:
                sub = t
            else:
                targets.append(t)
        more = " …" if len(targets) > 3 else ""
        return " ".join([a[0], sub] + targets[:3]) + more
    return " ".join(a[:4]) + (" …" if len(a) > 4 else "")


class Executor:
    def __init__(self, env_data: Environment, log, *, simulate: bool, has_root: bool, stop_signal: StopSignal,
                 on_command: Optional[Callable[[str], None]] = None,
                 on_output: Optional[Callable[[str], None]] = None,
                 on_failure: Optional[Callable[[str], None]] = None):
        self.env_data = env_data
        self.log = log
        self.simulate = simulate
        self.has_root = has_root
        self.stop_signal = stop_signal
        self.on_command = on_command or (lambda _t: None)
        self.on_output = on_output or (lambda _l: None)
        self.on_failure = on_failure or (lambda _d: None)

    
    def _build(self, argv: List[str], root: bool, env_extra: Dict[str, str]) -> List[str]:
        if not root:
            return list(argv)
        
        assign = [f"{k}={v}" for k, v in sorted(env_extra.items())]
        assign = ["LC_ALL=C.UTF-8", "LANGUAGE=C"] + assign
        if self.env_data.is_root:
            return ["env"] + assign + list(argv)
        if self.simulate:
            return list(argv)
        return ["sudo", "-n", "env"] + assign + list(argv)

    
    def run(self, argv: List[str], *, root: bool = False, action: bool = False, tolerate: bool = False,
              timeout: Optional[float] = None, env: Optional[Dict[str, str]] = None,
              user_path: bool = False, on_line: Optional[Callable[[str], None]] = None,
              protected: bool = False, log_output: bool = True, silent: bool = False,
              show: Optional[bool] = None) -> Cmd:
        'Runs a command.\n\n        root       requires privileges (uses sudo -n).\n        action       CHANGES the system: skipped in dry-run; failures become warnings (unless ``tolerate``).\n        protected  package manager: the first stop request does NOT interrupt it, and it never receives SIGKILL.\n        silent internal measurement (df): no log, label, or screen output.\n        show    displays command output in the panel (default: only commands that CHANGE something).\n        '
        show = action if show is None else show
        env_extra = dict(env or {})
        label = short_label(argv)
        prefix = "[root] " if root and not self.env_data.is_root else ""
        if not silent:
            self.log.write(f"$ {prefix}{' '.join(shlex.quote(x) for x in argv)}")
        if action and self.simulate:
            self.log.raw_write('  (dry-run: not executed)')
            return Cmd(list(argv), 0, "", 0.0, simulated=True)
        if root and not self.has_root and not self.simulate:
            self.log.raw_write('  (no administrative privileges: not executed)')
            return Cmd(list(argv), 126, "", 0.0, denied=True)

        complete = self._build(argv, root, env_extra)
        if root and not (self.env_data.is_root or self.simulate):
            env_proc = child_environment()                 
        else:
            env_proc = child_environment(extra=env_extra, user_path=self.env_data.user_path if user_path else None)
        if not silent:
            self.on_command(("sudo " if prefix else "") + label)
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(complete, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=env_proc, close_fds=True, **_OWN_PROCESS_GROUP)
        except FileNotFoundError:
            self.log.raw_write(f"  (command not found: {argv[0]})")
            return Cmd(list(argv), 127, f"command not found: {argv[0]}", 0.0)
        except OSError as e:
            self.log.raw_write(f"  (failed to start: {e})")
            return Cmd(list(argv), 126, str(e), 0.0)

        lines: List[str] = []
        total = 0
        fd = proc.stdout.fileno()
        buf = b""
        timed_out = interrupted = False
        term_requested = kill_requested = False
        t_term = 0.0

        def handle(raw: bytes) -> None:
            nonlocal total
            txt = _ANSI.sub("", raw.decode("utf-8", "replace")).rstrip()
            if not txt:
                return
            if txt.startswith(_STATUS_PREFIXES):
                if on_line:
                    on_line(txt)
                return
            if total < 8_000_000:
                lines.append(txt)
                total += len(txt)
            if log_output and not silent:
                self.log.raw_write(txt)
            if show and not silent:
                self.on_output(txt)
            if on_line:
                on_line(txt)

        eof = False
        while not eof:
            try:
                ready, _, _ = select.select([fd], [], [], 0.2)
            except (OSError, ValueError):
                break
            if ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    eof = True
                else:
                    buf += data
                    parts = re.split(rb"[\r\n]", buf)
                    buf = parts.pop()
                    for part in parts:
                        handle(part)
            elif proc.poll() is not None:
                
                time.sleep(0.05)
                break

            now = time.monotonic()
            
            if not term_requested:
                if self.stop_signal.force or (self.stop_signal.requested and not action and not protected):
                    interrupted = term_requested = True
                    t_term = now
                    terminate_process_tree(proc.pid, signal.SIGTERM, via_sudo=not self.env_data.is_root)
            
            if timeout and not protected and not term_requested and (now - t0) > timeout:
                timed_out = term_requested = True
                t_term = now
                terminate_process_tree(proc.pid, signal.SIGTERM, via_sudo=not self.env_data.is_root)
            if term_requested and not kill_requested and not protected and (now - t_term) > 8 and proc.poll() is None:
                kill_requested = True
                terminate_process_tree(proc.pid, signal.SIGKILL, via_sudo=not self.env_data.is_root)

        if buf:
            handle(buf)
        try:
            return_code = proc.wait(timeout=2 if ((term_requested or timed_out) and not protected) else None)
        except subprocess.TimeoutExpired:
            terminate_process_tree(proc.pid, signal.SIGKILL, via_sudo=not self.env_data.is_root)
            return_code = proc.wait()
        try:
            proc.stdout.close()
        except OSError:
            pass
        if timed_out:
            return_code = 124
        dur = time.monotonic() - t0
        if return_code != 0 and not silent:
            self.log.raw_write(f"  (exit code: {return_code}{' — timed out' if timed_out else ''}"
                           f"{' — interrupted' if interrupted else ''})")
            if action and not tolerate and not interrupted:
                self.on_failure(f"{label} (code {return_code})")
        return Cmd(list(argv), return_code, "\n".join(lines), dur, timed_out=timed_out, interrupted=interrupted)



def sudo_active(env_data: Environment) -> bool:
    'True if sudo is already authenticated (without requesting a password).'
    try:
        return subprocess.run(["sudo", "-n", "-v"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=15, env=child_environment(),
                              close_fds=True).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def authenticate_sudo(env_data: Environment) -> bool:
    'Requests the password in the terminal (inherits the tty). Returns True if sudo is authenticated.'
    if env_data.is_root:
        return True
    if not env_data.has_tool("sudo"):
        return False
    try:
        return subprocess.run(["sudo", "-v"], env=child_environment(), close_fds=True).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class SudoKeepAlive(threading.Thread):
    'Refreshes the sudo timestamp during execution (otherwise, long updates may request the password again).'

    def __init__(self, env_data: Environment):
        super().__init__(daemon=True)
        self.env_data = env_data
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(45):
            sudo_active(self.env_data)

    def stop(self) -> None:
        self._stop_event.set()
