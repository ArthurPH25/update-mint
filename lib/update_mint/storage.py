
'Disk-space monitoring and size formatting.\n\nMB/GB here use base 1024 (the same values shown by ``df -h`` and ``du -h``).\napt values ("287 MB") use base 1000; :func:`parse_apt_size` converts them to bytes.\n'
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Tuple

MB = 1024 * 1024
GB = 1024 * MB


def _format_number(value: float, decimal_places: int) -> str:
    return f"{value:,.{decimal_places}f}"


def format_bytes(b: float) -> str:
    signal = "-" if b < 0 else ""
    b = abs(b)
    if b < 1024:
        return f"{signal}{int(b)} B"
    v = float(b)
    for unit in ("KB", "MB", "GB", "TB"):
        v /= 1024.0
        if v < 1024 or unit == "TB":
            decimal_places = 0 if v >= 100 else 1
            return f"{signal}{_format_number(v, decimal_places)} {unit}"
    return f"{signal}{int(b)} B"


def format_mb_gb(b: float) -> str:
    "Report format, always in MB and GB: '4,231.5 MB (4.13 GB)'."
    signal = "-" if b < 0 else ""
    b = abs(b)
    return f"{signal}{_format_number(b / MB, 1)} MB ({signal}{_format_number(b / GB, 2)} GB)"


_APT_UNITS = {"B": 1, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4}


def parse_apt_size(txt: str) -> int:

    m = re.match(r"^\s*([\d.,]+)\s*([kKMGT]?B)\s*$", txt)
    if not m:
        return 0
    num = m.group(1).replace(",", "")
    try:
        return int(float(num) * _APT_UNITS[m.group(2).upper()])
    except (ValueError, KeyError):
        return 0


def parse_du(text: str) -> int:
    "First number of the last ``du`` result line. Permission errors, which du prints before the total, are ignored."
    for line in reversed(text.splitlines()):
        m = re.match(r"^(\d+)\s", line)
        if m:
            return int(m.group(1))
    return 0


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


@dataclass
class DfLine:
    source: str
    mount_point: str
    size: int
    used: int
    free: int
    inodes: int
    free_inodes: int


def parse_df(text: str) -> List[DfLine]:
    output: List[DfLine] = []
    for line in text.splitlines()[1:]:
        parts = line.rsplit(None, 5)
        if len(parts) != 6:
            continue
        start, *nums = parts
        try:
            n = [int(x) for x in nums]
        except ValueError:
            continue
        file_pair = start.split(None, 1)
        if len(file_pair) != 2:
            continue
        output.append(DfLine(file_pair[0], file_pair[1], *n))
    return output


@dataclass
class DiskSpace:
    'Space snapshot, summing DISTINCT file systems for /, /boot, /var, and ~.'
    free: int = 0
    used: int = 0
    size: int = 0
    by_filesystem: Dict[str, DfLine] = field(default_factory=dict)


class SpaceMeter:
    'Measures space using ``df`` (read-only). ``run(argv) -> (return_code, text)``.'

    def __init__(self, run: Callable[[List[str]], Tuple[int, str]], home: str):
        self._run = run
        self.mount_points = ["/", "/boot", "/var", home]

    def snapshot(self) -> DiskSpace:
        mount_points = [part for part in dict.fromkeys(self.mount_points) if os.path.isdir(part)] or ["/"]
        return_code, txt = self._run(["df", "-B1", "--output=source,target,size,used,avail,itotal,iavail", *mount_points])
        spec = DiskSpace()
        for l in parse_df(txt):
            if l.source in spec.by_filesystem:
                continue
            spec.by_filesystem[l.source] = l
            spec.free += l.free
            spec.used += l.used
            spec.size += l.size
        return spec

    def free_mb(self, mount_point: str = "/") -> int:
        return_code, txt = self._run(["df", "-B1", "--output=source,target,size,used,avail,itotal,iavail", mount_point])
        lines = parse_df(txt)
        return lines[0].free // MB if lines else 0
