from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import Optional, Set

from . import environment as environment, planner as planner, report as report, security as security
from .constants import APP_NAME, APP_VERSION
from .context import Context
from .executor import StopSignal, SudoKeepAlive, authenticate_sudo
from .logger import RunMarker, LogRecorder, state_directory
from .ui.base import SimpleUI

_lock_fd = None   


def _usage() -> int:
    print(f"{APP_NAME} does not accept options or parameters. Type only:  {APP_NAME}\n"
          "Help is available in the menu (press ? or F1).")
    return 2


def _reexec_as_user(user: str) -> Optional[int]:
    "Called with 'sudo update-mint': drops back to the user (the program does not run entirely as root)."
    if not environment.find_executable("sudo"):
        return None
    target = os.path.realpath(sys.argv[0])
    env = ["env", f"TERM={os.environ.get('TERM', 'xterm')}", f"LANG={os.environ.get('LANG', 'C.UTF-8')}"]
    print(f"You called it with sudo; switching back to user '{user}' (sudo is requested only when necessary)…")
    try:
        os.execvp("sudo", ["sudo", "-u", user, "-H"] + env + [target])
    except OSError:
        return None
    return None   


def _inhibit_sleep(env_data) -> Optional[subprocess.Popen]:
    'Prevents sleep/shutdown during maintenance (interrupting dpkg mid-operation is a problem).'
    if not (env_data.has_tool("systemd-inhibit") and env_data.has_tool("tail")):
        return None
    try:
        part = subprocess.Popen(
            ["systemd-inhibit", "--what=sleep:shutdown", f"--who={APP_NAME}",
             '--why=System maintenance in progress', "--mode=block",
             "tail", f"--pid={os.getpid()}", "-f", "/dev/null"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=environment.child_environment(), start_new_session=True, close_fds=True)
    except OSError:
        return None
    time.sleep(0.4)
    return part if part.poll() is None else None


def _print_report(lines, utf8: bool) -> None:
    try:
        width = max(60, min(110, os.get_terminal_size().columns - 1))
    except OSError:
        width = 100
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    ansi = {"ok": "32", "warning": "33", "error": "31", "dim": "2", "title": "1;36", "sec": "1;36", "sec2": "1"}
    for level, text in report.wrap(lines, width, utf8):
        if color and level in ansi:
            print(f"\x1b[{ansi[level]}m{text}\x1b[0m")
        else:
            print(text)


def main(argv=None) -> int:
    global _lock_fd
    argv = sys.argv if argv is None else argv
    if len(argv) > 1:
        return _usage()

    env_data = environment.detect()
    if env_data.is_root and env_data.sudo_user and env_data.sudo_user != "root":
        _reexec_as_user(env_data.sudo_user)          
    simulate = bool(os.environ.get("UPDATE_MINT_DRY_RUN"))
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        print(f"{APP_NAME} requires an interactive terminal (it is a menu program).", file=sys.stderr)
        return 2

    _lock_fd = security.single_instance()
    if _lock_fd is None:
        print(f"Another instance of {APP_NAME} is already running. Wait for it to finish.", file=sys.stderr)
        return 1
    if _lock_fd == security.LOCK_UNAVAILABLE:
        print(f"{APP_NAME} could not create a lock file (neither /run/lock nor /tmp is writable), so it cannot "
              "guarantee that no other instance is running. Nothing was done.", file=sys.stderr)
        _lock_fd = None
        return 1

    stop_signal = StopSignal()
    ui = None
    if interactive:
        try:
            from .ui.tui import TUI
            ui = TUI(env_data.utf8, stop_signal)
            ui.start()
        except Exception:  
            try:
                import curses
                curses.endwin()
            except Exception:  
                pass
            ui = None
    if ui is None:
        ui = SimpleUI(env_data.utf8, stop_signal)
    ui.header = f"{env_data.description} · kernel {env_data.kernel} · user {env_data.user}"

    def on_signal(signum, _frame):
        stop_signal.request()

    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(s, on_signal)
        except (OSError, ValueError):
            pass

    try:
        if not env_data.mint:
            r = ui.dialog('This system does not appear to be Linux Mint',
                           f"Detected: {env_data.os_name}. {APP_NAME} was designed for Linux Mint "
                           "and makes use of its specific features. Continue anyway?",
                           ["Quit", "Continue anyway"], 0)
            if r != 1:
                ui.close()
                print("Nothing was changed.")
                return 0

        selection: Optional[Set[str]]
        try:
            selection = ui.select({"U", "R", "C"})
        except Exception as e:
            if e.__class__.__name__ == "TerminalDisconnected":
                ui.close()
                return 1
            raise
        if not selection:
            ui.close()
            print('No operation selected. Nothing was changed.')
            return 0

        
        has_root = env_data.is_root
        if not has_root and not simulate:
            ui.suspend()
            print('\nThe program runs as you; sudo will be used only for commands that require an administrator.')
            has_root = authenticate_sudo(env_data)
            ui.resume()
            if not has_root:
                stop_signal.n = 0
                r = ui.dialog('No administrative privileges',
                               'Could not authenticate with sudo. I can continue with tasks that do not require an administrator (personal cache, user Flatpak apps, etc.); the rest will be skipped.',
                               ["Quit", 'Continue without privileges'], 0)
                if r != 1:
                    ui.close()
                    print("Nothing was changed.")
                    return 1
        sudo_keepalive = None
        if has_root and not env_data.is_root and not simulate:
            sudo_keepalive = SudoKeepAlive(env_data)
            sudo_keepalive.start()

        directory_state = state_directory(env_data.home)
        try:
            log = LogRecorder(directory_state)
        except OSError as e:
            if sudo_keepalive:
                sudo_keepalive.stop()
            ui.close()
            print(f"Could not create the log folder {directory_state}: {e.strerror}.\n"
                  "Check that you can write to that folder (and to the one above it), then run again. Nothing was changed.",
                  file=sys.stderr)
            return 1
        marker = RunMarker(directory_state)
        previous = marker.previous()
        marker.write("starting")
        ctx = Context(env_data, selection, ui, log, simulate=simulate, has_root=has_root, stop_signal=stop_signal)
        ctx.marker = marker
        log.write(f"{APP_NAME} {APP_VERSION} — {env_data.description}; kernel {env_data.kernel}; user {env_data.user}; "
                     f"selected: {''.join(sorted(selection))}; dry-run: {simulate}; root: {has_root}")
        if previous:
            ctx.note(f"the previous run ended unexpectedly (power loss or terminal closed) — "
                     f"last record: {previous}. The dpkg state was checked before continuing")
        inhibitor = _inhibit_sleep(env_data) if not simulate else None
        if inhibitor is None and not simulate and env_data.has_tool("systemd-inhibit"):
            ctx.note('could not prevent the computer from sleeping during maintenance (no logind/session?)')

        thread = threading.Thread(target=planner.execute, args=(ctx,), name="engine")
        terminal_active = True
        try:
            ui.run_engine(thread)
        except Exception:  
            import traceback
            terminal_active = False
            log.write('interface failure (terminal closed?):\n' + traceback.format_exc())
            if not stop_signal.requested:
                stop_signal.request()
            if thread.is_alive():
                thread.join()
        if sudo_keepalive:
            sudo_keepalive.stop()
        if inhibitor is not None:
            inhibitor.terminate()

        lines = report.build(ctx)
        file = log.save_report(report.as_text(lines, 100, True))
        if terminal_active:
            try:
                ui.show_report(lines)
            except Exception:  
                terminal_active = False
        ui.close()
        if terminal_active:
            try:
                _print_report(lines, env_data.utf8)
                print(f"\nReport saved to: {file or '(could not be saved)'}\nComplete log:       {log.path}\n"
                      "Journal entry: journalctl -t update-mint")
            except OSError:
                pass
        log.write("finished")
        log.close()
        marker.complete()
        if stop_signal.requested:
            return 130
        return 1 if ctx.errors else 0
    finally:
        try:
            ui.close()
        except Exception:  
            pass


if __name__ == "__main__":  
    sys.exit(main())
