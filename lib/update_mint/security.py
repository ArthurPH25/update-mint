
'Reusable security rules (each one is small and independently testable).'
from __future__ import annotations

import fcntl
import glob
import os
import re
import stat
from typing import Iterable, List, Optional, Tuple

from .constants import PROTECTED_PACKAGE_PATTERN


def without_architecture(name: str) -> str:
    return name.split(":", 1)[0]


def protected(name: str) -> bool:
    'True for packages that must never be removed automatically (v1.3).'
    return bool(PROTECTED_PACKAGE_PATTERN.match(without_architecture(name)))


def protected_in(names: Iterable[str]) -> List[str]:
    return [n for n in names if protected(n)]


def is_running_kernel(name: str, running_kernel: str) -> bool:
    'True if the package belongs to the currently running kernel (image, modules, headers, tools).'
    if not running_kernel:
        return False
    n = without_architecture(name)
    versions = {running_kernel, re.sub(r"-[a-z][a-z0-9]*$", "", running_kernel)}   
    prefix = r"^linux-(image|image-unsigned|modules|modules-extra|headers|tools|cloud-tools|buildinfo)-"
    return any(re.match(prefix + re.escape(v) + r"$", n) for v in versions)



def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def battery() -> Tuple[Optional[int], bool, bool]:
    '(percentage, on_battery, plugged_in). Without a battery: (None, False, True).'
    base = ("/sys/class/power_supply")
    pct: Optional[int] = None
    discharging = False
    plugged_in = False
    has_battery = False
    for d in sorted(glob.glob(os.path.join(base, "*"))):
        if _read(os.path.join(d, "scope")).lower() == "device":
            continue
        type = _read(os.path.join(d, "type")).lower()
        if type == "battery":
            has_battery = True
            cap = _read(os.path.join(d, "capacity"))
            if cap.isdigit():
                v = int(cap)
                pct = v if pct is None else min(pct, v)
            if _read(os.path.join(d, "status")).lower() == "discharging":
                discharging = True
        elif type in ("mains", "usb", "ups"):
            if _read(os.path.join(d, "online")) == "1":
                plugged_in = True
    if not has_battery:
        return None, False, True
    return pct, (discharging and not plugged_in), plugged_in



def read_only(mount_point: str = "/") -> bool:
    'True if ``mount_point`` is mounted read-only (a classic sign of a serious disk error).'
    options: Optional[str] = None
    try:
        with open(("/proc/mounts"), encoding="utf-8", errors="replace") as f:
            for line in f:
                part = line.split()
                if len(part) >= 4 and part[1] == mount_point:
                    options = part[3]          
    except OSError:
        return False
    return options is not None and "ro" in options.split(",")



LOCK_UNAVAILABLE = -1


def single_instance() -> Optional[int]:
    '''Lock (flock) that prevents simultaneous runs.

    Returns the file descriptor, ``None`` if another run exists, or ``LOCK_UNAVAILABLE`` if no lock file could be
    created at all (the caller must then NOT continue: nothing would prevent two simultaneous runs).
    The lock is a kernel ``flock`` on an empty file (no PID file): it is released automatically when the process
    ends, however it ends, so a crash can never leave an inconsistent lock behind. The file is deliberately never
    deleted (deleting a flock file opens a race between two instances). The descriptor is not inherited by children.
    '''
    candidates = ["/run/lock/update-mint.lock", f"/tmp/update-mint-{os.getuid()}.lock"]
    for path in candidates:
        try:
            old = os.umask(0)
            try:
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o666)
            finally:
                os.umask(old)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            os.close(fd)
            return None
        except OSError:
            os.close(fd)
            continue
    return LOCK_UNAVAILABLE



_KERNEL_VERSION_RE = r"[0-9]+\.[0-9]+[A-Za-z0-9.+~_-]*"
_ORPHAN_RE = re.compile(r"^/(usr/)?lib/modules/" + _KERNEL_VERSION_RE + r"$|^/usr/src/linux-headers-" + _KERNEL_VERSION_RE + r"$")


def valid_orphan_path(path: str) -> bool:
    "Only these two formats can be removed as 'kernel leftovers': /lib/modules/<version> and\n    /usr/src/linux-headers-<version>. No symbolic links or '..' components."
    if ".." in path or not _ORPHAN_RE.match(path):
        return False
    return not os.path.islink(path)


def _root_owned_not_writable(st: os.stat_result) -> bool:
    return st.st_uid == 0 and not (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


def trusted_library(directory: str) -> bool:
    try:
        d = os.path.realpath(directory)
        top = d
        while True:
            if not _root_owned_not_writable(os.stat(d)):
                return False
            if d == "/":
                break
            d = os.path.dirname(d)
        pending = [top]
        while pending:
            current = pending.pop()
            with os.scandir(current) as it:
                for entry in it:
                    st = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(st.st_mode) or not _root_owned_not_writable(st):
                        return False
                    if stat.S_ISDIR(st.st_mode):
                        pending.append(entry.path)
        return True
    except OSError:
        return False
