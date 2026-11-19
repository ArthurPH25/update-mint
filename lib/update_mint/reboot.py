
'Reboot requirement — three levels, with reasons.\n\n  2  REQUIRED     the system requested it (/run/reboot-required), a new kernel was installed, firmware is scheduled\n                  for the next boot, or modules for the running kernel disappeared from disk.\n  1  RECOMMENDED  a newer kernel was already installed, core packages were updated, or processes/services\n                  are using libraries already replaced on disk (needrestart or our own scan).\n  0  NOT REQUIRED none of these apply.\n\nThe program NEVER reboots on its own: it only reports. The user decides when.\n'
from __future__ import annotations

import os
import re
from typing import List, Tuple

from . import packages as packages
from .environment import helper_path
from .constants import CORE_PACKAGE_PATTERN
from .security import trusted_library

LEVELS = {0: 'NOT REQUIRED', 1: "RECOMMENDED", 2: 'REQUIRED'}


def parse_needrestart(text: str) -> Tuple[int, List[str], List[str]]:
    '(KSTA, services, sessions) from ``needrestart -b`` output. KSTA: 1 ok, 2 ABI-compatible, 3 new version.'
    kernel_status = 0
    m = re.search(r"^NEEDRESTART-KSTA:\s*(\d)", text, re.MULTILINE)
    if m:
        kernel_status = int(m.group(1))
    services = re.findall(r"^NEEDRESTART-SVC:\s*(\S+)", text, re.MULTILINE)
    sessions = re.findall(r"^NEEDRESTART-SESS:\s*(.+)$", text, re.MULTILINE)
    return kernel_status, services, sessions


def _list(folder: str) -> List[str]:
    try:
        return os.listdir(folder)
    except OSError:
        return []


def _raise_reboot_level(ctx, level: int, reason: str) -> None:
    r = ctx.reboot
    r["level"] = max(r["level"], level)
    r["reasons"].append(reason)


def evaluate(ctx) -> None:
    'Fills ``ctx.reboot`` (level, reasons). Read-only; never changes anything.'
    ctx.reboot = {"level": 0, "reasons": []}
    file_path = ("/run/reboot-required")
    if os.path.exists(file_path):
        reboot_packages = ""
        try:
            with open(file_path + ".pkgs", encoding="utf-8", errors="replace") as f:
                reboot_packages = " ".join(sorted({l.strip() for l in f if l.strip()}))
        except OSError:
            pass
        _raise_reboot_level(ctx, 2, 'the system requested a reboot' + (f" ({reboot_packages})" if reboot_packages else ""))
    running = ctx.env_data.kernel
    kernels = packages.installed_kernels()
    new = kernels[-1] if kernels else ""
    if new and new != running and packages.version_key(new) > packages.version_key(running):
        if ctx.kernel_updated:
            _raise_reboot_level(ctx, 2, f"new kernel installed: {running} → {new}")
        else:
            _raise_reboot_level(ctx, 1, f"a newer kernel is already installed ({new}); currently running: {running}")
    if running and not os.path.isdir(f"/lib/modules/{running}"):
        others = [d for d in _list(("/lib/modules")) if d != running]
        if others:      
            _raise_reboot_level(ctx, 2, f"modules for the running kernel ({running}) no longer exist on disk: new drivers will not load")
    
    if ctx.inventory_before.get("dpkg") and ctx.inventory_after.get("dpkg"):
        previous, current = ctx.inventory_before["dpkg"], ctx.inventory_after["dpkg"]
        core_packages = sorted(n for n, v in current.items() if n in previous and previous[n] != v and CORE_PACKAGE_PATTERN.match(n))
        if core_packages:
            _raise_reboot_level(ctx, 1, "core packages updated: " + " ".join(core_packages[:8]))
    if ctx.firmware.get("requires_reboot"):
        _raise_reboot_level(ctx, 2, 'installed firmware takes effect only on the next boot: ' + ", ".join(ctx.firmware.get("installed", [])))
    
    if ctx.has_tool("needrestart") and not ctx.simulate:
        r = ctx.read_only_command(["needrestart", "-b"], root=ctx.has_root, timeout=60, log_output=False)
        kernel_status, services, sessions = parse_needrestart(r.out)
        if kernel_status in (2, 3) and not any("kernel" in m for m in ctx.reboot["reasons"]):
            _raise_reboot_level(ctx, 2 if kernel_status == 3 else 1, "needrestart reports a pending kernel update")
        if services:
            _raise_reboot_level(ctx, 1, f"{len(services)} service(s) using old libraries (needrestart): " + " ".join(services[:6]))
        if sessions:
            _raise_reboot_level(ctx, 1, 'user session(s) using old libraries: log out and back in')
    elif ctx.has_root and not ctx.simulate:
        h = helper_path()
        if os.path.exists(h) and trusted_library(os.path.dirname(os.path.dirname(h))):
            import json
            r = ctx.read_only_command(["/usr/bin/python3", "-I", h, "libraries"], root=True, timeout=60, log_output=False)
            try:
                procs = json.loads(r.out.strip().splitlines()[-1]) if r.out.strip() else []
            except (ValueError, IndexError):
                procs = []
            if not isinstance(procs, list):          
                procs = []
            procs = [part for part in procs if isinstance(part, dict) and "name" in part]
            if procs:
                names = sorted({part["name"] for part in procs})
                _raise_reboot_level(ctx, 1, f"{len(procs)} process(es) using libraries already replaced on disk "
                                f"(ex.: {', '.join(names[:5])}) — restart them or log out and back in")
