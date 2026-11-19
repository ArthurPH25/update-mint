#!/usr/bin/python3 -I


"Privileged helper for update-mint.\n\nIt is called only by the main program, through ``sudo -n /usr/bin/python3 -I root_helper.py <action> ...``.\nIt performs only three kinds of work, each with its own safeguards, and responds in JSON:\n\n  libraries                 lists processes using system libraries already replaced on disk\n  tmp <dir>:<days>...       cleans /tmp and /var/tmp (only those two), respecting files in use\n  debconf                   removes '*-old' files from /var/cache/debconf\n\nNo action accepts an arbitrary path to delete.\n"
import json
import os
import re
import sys

LIB = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, LIB)

from update_mint import fs  
from update_mint.constants import DUMP_FILE_PATTERNS, PRESERVED_TEMP_PREFIXES  

ALLOWED_TEMP_DIRS = ("/tmp", "/var/tmp")


def _pids():
    for n in os.listdir("/proc"):
        if n.isdigit():
            yield int(n)


def _comm(pid):
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return "?"


def open_under(prefixes):
    'Files under the prefixes that a process keeps open (fd) or memory-mapped (mmap).'
    found_items = set()
    prefixes = tuple(part.rstrip("/") + "/" for part in prefixes)
    for pid in _pids():
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    target = os.readlink(f"/proc/{pid}/fd/{fd}")
                except OSError:
                    continue
                if target.startswith(prefixes):
                    found_items.add(target)
        except OSError:
            pass
        try:
            with open(f"/proc/{pid}/maps", encoding="utf-8", errors="replace") as f:
                for line in f:
                    part = line.rstrip("\n").split(None, 5)
                    if len(part) == 6 and part[5].startswith(prefixes):
                        found_items.add(part[5].replace(" (deleted)", ""))
        except OSError:
            pass
    return found_items


def old_libraries():
    'Processes using libraries from /usr, /lib, or /opt that were deleted/replaced on disk.'
    output = {}
    pattern = re.compile(r"\.so(\.[0-9A-Za-z_.-]+)?$")
    for pid in _pids():
        try:
            with open(f"/proc/{pid}/maps", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.rstrip().endswith("(deleted)"):
                        continue
                    part = line.rstrip("\n").split(None, 5)
                    if len(part) != 6:
                        continue
                    path = part[5][: -len(" (deleted)")]
                    if path.startswith(("/usr/", "/lib", "/opt/")) and pattern.search(path):
                        output.setdefault(pid, path)
        except OSError:
            continue
    return [{"pid": pid, "name": _comm(pid), "library": lib} for pid, lib in sorted(output.items())]


def clean_temp(specs):
    open_files = open_under(ALLOWED_TEMP_DIRS)
    result = {}
    for spec in specs:
        d, _, days = spec.partition(":")
        if d not in ALLOWED_TEMP_DIRS or not days.replace(".", "", 1).isdigit():
            result[d] = {"error": 'invalid argument'}
            continue

        def exclude(path, is_dir, root_path=d):
            return os.path.dirname(path) == root_path and os.path.basename(path).startswith(PRESERVED_TEMP_PREFIXES)

        try:
            r = fs.clean_tree(d, age_days=float(days), exclude=exclude, preserved_names=DUMP_FILE_PATTERNS,
                              open_files=open_files, regular_files_only=True, allow_top_level=True)
        except fs.ForbiddenPath as e:
            result[d] = {"error": f"refused: {e}"}
            continue
        result[d] = {"files": r.files, "bytes": r.bytes, "in_use": r.skipped_in_use, "errors": r.errors[:5],
                     "error_count": len(r.errors)}
    return result


def clean_debconf():
    total = 0
    n = 0
    base = "/var/cache/debconf"
    try:
        for name in os.listdir(base):
            if name.endswith("-old"):
                part = os.path.join(base, name)
                if os.path.isfile(part) and not os.path.islink(part):
                    total += os.path.getsize(part)
                    os.unlink(part)
                    n += 1
    except OSError:
        pass
    return {"files": n, "bytes": total}


def main(argv):
    if os.geteuid() != 0:
        print(json.dumps({"error": "must be run as root"}))
        return 1
    if len(argv) < 2:
        print(json.dumps({"error": 'missing action'}))
        return 2
    action, args = argv[1], argv[2:]
    if action == "libraries":
        print(json.dumps(old_libraries()))
    elif action == "tmp":
        print(json.dumps(clean_temp(args)))
    elif action == "debconf":
        print(json.dumps(clean_debconf()))
    else:
        print(json.dumps({"error": f"unknown action: {action}"}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
