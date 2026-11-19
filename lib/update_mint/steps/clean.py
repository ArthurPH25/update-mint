
'DEEP CLEANUP phase.\n\nPrinciples:\n  * each file type is cleaned using its OWN mechanism (apt, journalctl, flatpak, snap,\n    systemd-tmpfiles...). Where no mechanism exists, files are removed in Python using ``fs`` safeguards;\n  * user files are handled AS the user (the program already runs as that user);\n  * never delete: personal files, Trash, ML models, browser profiles, crash dumps\n    (/var/crash, pstore, core), rotated logs, Timeshift snapshots, conda/Docker environments;\n  * the npm cache is deliberately NOT cleaned (npm repairs itself; deleting it only makes it slower).\n'
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
from typing import List, Optional, Set, Tuple

from .. import fs, packages as packages
from ..environment import helper_path
from ..storage import MB, format_bytes, parse_du
from ..constants import (PROTECTED_CACHES, REGENERABLE_CACHES, CACHE_DAYS, JOURNAL_DAYS, TEMP_DAYS,
                          AUTOREMOVE_LIMIT, XSESSION_ERROR_LIMIT, JOURNAL_MAX_MB, BROWSERS)
from ..context import DIFFICULT, REVERSIBLE, Step, warning, error, ok, skipped
from ..diagnostics import ATTENTION, CRITICAL
from ..security import valid_orphan_path, is_running_kernel, trusted_library, protected_in



def _package_rc(ctx) -> List[str]:
    r = ctx.read_only_command(["dpkg-query", "-W", "-f=${db:Status-Abbrev} ${binary:Package}\n"], log_output=False)
    return [l.split(None, 1)[1].strip() for l in r.out.splitlines() if l.startswith("rc ")]


def _modified_config_files(ctx, pkgs: List[str]) -> Set[str]:
    "'rc' packages whose configuration files you edited are never purged (preserves your configuration)."
    modified_packages: Set[str] = set()
    for i in range(0, len(pkgs), 100):
        batch = pkgs[i:i + 100]
        r = ctx.read_only_command(["dpkg-query", "-W", "-f=PKG ${binary:Package}\n${Conffiles}\n", *batch], log_output=False)
        current = ""
        for l in r.out.splitlines():
            if l.startswith("PKG "):
                current = l[4:].strip()
                continue
            m = re.match(r"^\s+(\S+)\s+([0-9a-f]{32})(\s+obsolete)?\s*$", l)
            if not m or not current or m.group(3):
                continue
            try:
                with open((m.group(1)), "rb") as f:
                    h = hashlib.md5(f.read()).hexdigest()
            except FileNotFoundError:
                continue
            except OSError:
                modified_packages.add(current)      
                continue
            if h != m.group(2):
                modified_packages.add(current)
    return modified_packages


def step_apt_cleanup(ctx):
    if ctx.apt_locked:
        return skipped("apt is in use by another program")
    who = packages.wait_for_apt(ctx, 60)
    if who:
        ctx.warning(f"apt/dpkg is in use by {who}: package cleanup was deferred")
        return warning("apt busy")
    return_code = False
    simulation = packages.simulate(ctx, "autoremove")
    removal_candidates = simulation.remove
    n_auto = len(removal_candidates)
    protected_items = sorted(set(protected_in(removal_candidates)) | set(packages.essential_packages(ctx, removal_candidates)))
    in_use = [n for n in removal_candidates if is_running_kernel(n, ctx.env_data.kernel)]
    if removal_candidates:
        ctx.log.write("autoremove would remove: " + " ".join(removal_candidates))
    skip = False
    if not simulation.ok:
        skip = True
        ctx.autoremove_rejected = True
        ctx.warning('could not simulate autoremove (apt-get -s autoremove failed) — not run; review with: apt autoremove -s')
    elif protected_items or in_use or n_auto >= AUTOREMOVE_LIMIT:
        skip = True
        ctx.autoremove_rejected = True
        why = (f", including {' '.join(protected_items)}" if protected_items else "") + \
              (f", including the RUNNING kernel ({' '.join(in_use)})" if in_use else "")
        ctx.warning(f"autoremove would remove {n_auto} package(s){why} — not run; review with: apt autoremove -s")
        for n in removal_candidates[:20]:
            ctx.not_updated(n, 'orphan not removed (autoremove safety safeguard)')
    elif n_auto:
        
        r = ctx.apt(["autoremove", "--purge"])
        if not r.ok:
            return_code = True
        else:
            ctx.cleanup_counts['orphaned packages removed'] = n_auto
    residual_config_packages = _package_rc(ctx)
    preserved: List[str] = []
    if residual_config_packages:
        edited = _modified_config_files(ctx, residual_config_packages)
        preserved = sorted(edited)
        target = [part for part in residual_config_packages if part not in edited]
        if preserved:
            ctx.note('did not purge the configuration of ' + ", ".join(preserved[:8])
                     + (" …" if len(preserved) > 8 else "") + ': you edited their configuration files')
        if target:
            r = ctx.apt(["purge", *target])
            if not r.ok:
                return_code = True
            else:
                ctx.cleanup_counts['residual configurations purged'] = len(target)
        residual_config_packages = target
        ctx.preserved_rc = set(preserved)
    if not ctx.cmd(["apt-get", "clean"], root=True).ok:
        return_code = True
    if return_code:
        ctx.error('failed to clean apt cache (see the log)')
        return error('apt failed')
    return ok(f"{0 if skip else n_auto} orphans · {len(residual_config_packages)} configs")



def _matching_packages(ctx, pattern: str) -> List[str]:
    r = ctx.read_only_command(["dpkg-query", "-W", "-f=${db:Status-Abbrev}\t${Package}\n", pattern], log_output=False)
    return [l for l in r.out.splitlines() if "\t" in l]


def step_orphaned_kernels(ctx):
    if ctx.read_only_command(["findmnt", "--fstab", "/boot"], log_output=False).ok and \
            ctx.read_only_command(["mountpoint", "-q", "/boot"], log_output=False).return_code != 0:
        ctx.warning('/boot is in fstab but is not mounted: without seeing it, orphaned files cannot be identified — left unchanged')
        return warning('/boot is not mounted')
    running = ctx.env_data.kernel
    n = 0
    candidates: List[str] = []
    for d in sorted(_list(("/lib/modules"))):
        if not re.match(r"^[0-9]+\.[0-9]+", d) or d == running:
            continue
        if os.path.exists((f"/boot/vmlinuz-{d}")) or os.path.exists((f"/boot/initrd.img-{d}")):
            continue
        if _matching_packages(ctx, f"linux-*{d}*"):
            continue
        candidates.append(f"/lib/modules/{d}")
    for d in sorted(_list(("/usr/src"))):
        if not d.startswith("linux-headers-"):
            continue
        v = d[len("linux-headers-"):]
        if not re.match(r"^[0-9]+\.[0-9]+", v) or v == running or os.path.exists((f"/boot/vmlinuz-{v}")):
            continue
        if _matching_packages(ctx, f"linux-headers-{v}"):
            continue
        candidates.append(f"/usr/src/{d}")
    for path in candidates:
        if not valid_orphan_path(path):
            ctx.warning(f"{path} does not match the expected kernel-leftover format — left unchanged")
            continue
        ctx.log.write(f"orphan: {path}")
        if ctx.cmd(["rm", "-rf", "--one-file-system", "--", path], root=True).ok:
            n += 1
    old_items = []
    try:
        old_items = [f for f in os.listdir(("/boot")) if fnmatch.fnmatch(f, "*.old-dkms")]
    except OSError:
        pass
    if old_items:
        ctx.cmd(["find", "/boot", "-maxdepth", "1", "-type", "f", "-name", "*.old-dkms", "-delete"], root=True)
    if n == 0 and not old_items:
        return skipped('no orphans')
    return ok(f"{n} directory/directories · {len(old_items)} initrd.old-dkms")


def _list(folder: str) -> List[str]:
    try:
        return os.listdir(folder)
    except OSError:
        return []



def step_flatpak_snap_cleanup(ctx):
    acted = False
    return_code = False
    if ctx.has_tool("flatpak") and ctx.state and ctx.state.flatpak_items:
        acted = True
        if not ctx.cmd(["flatpak", "uninstall", "--system", "--unused", "-y", "--noninteractive"], root=True).ok:
            return_code = True
        if not ctx.cmd(["flatpak", "uninstall", "--user", "--unused", "-y", "--noninteractive"], user_path=True).ok:
            return_code = True
        ctx.cmd(["find", "/var/tmp", "-maxdepth", "1", "-name", "flatpak-cache-*", "-exec", "rm", "-rf",
                 "--one-file-system", "{}", "+"], root=True, tolerate=True)
    if ctx.has_tool("snap"):
        acted = True
        r = ctx.read_only_command(["snap", "list", "--all"], log_output=False)
        n_snap = 0
        for l in r.out.splitlines():
            if "disabled" in l:
                part = l.split()
                if len(part) >= 3:
                    
                    ctx.log.write(f"snap: removing old revision {part[0]} (rev {part[2]})")
                    if ctx.cmd(["snap", "remove", part[0], f"--revision={part[2]}"], root=True).ok:
                        n_snap += 1
        ctx.cmd(["find", "/var/lib/snapd/cache", "-type", "f", "-delete"], root=True, tolerate=True)
        if n_snap:
            ctx.cleanup_counts['old Snap revisions (no rollback)'] = n_snap
    if not acted:
        return skipped('neither Flatpak nor Snap is installed')
    if return_code:
        ctx.warning('failed to remove unused Flatpak items')
        return warning('with errors')
    return ok()



def _journal_bytes(ctx) -> int:
    r = ctx.read_only_command(["journalctl", "--disk-usage"], log_output=False)
    m = re.search(r"take up ([\d.,]+)([KMGT]?)B?", r.out)
    if not m:
        return 0
    un = {"": 1, "K": 1024, "M": MB, "G": 1024 * MB, "T": 1024 * 1024 * MB}[m.group(2)]
    return int(float(m.group(1).replace(",", ".")) * un)


def step_journal(ctx):
    if not ctx.has_tool("journalctl") or not os.path.isdir(("/run/systemd/system")):
        return skipped('systemd-journald is unavailable')
    before = _journal_bytes(ctx)
    ctx.cmd(["journalctl", "--rotate"], root=True)
    ctx.cmd(["journalctl", f"--vacuum-time={JOURNAL_DAYS}d", f"--vacuum-size={JOURNAL_MAX_MB}M"], root=True)
    after = _journal_bytes(ctx)
    if before and after < before:
        ctx.cleanup_details["journal reduced"] = before - after
    return ok(f"keeps {JOURNAL_DAYS}d / {JOURNAL_MAX_MB}M" + (f" · {format_bytes(before)} → {format_bytes(after)}" if before else ""))



def _parse_json_output(text: str) -> Optional[dict]:
    for l in reversed(text.splitlines()):
        l = l.strip()
        if l.startswith("{"):
            try:
                return json.loads(l)
            except ValueError:
                return None
    return None


def _run_helper(ctx, *args: str):
    'Runs the root helper (if the tool directory is trusted). Returns (Cmd, json|None).'
    h = helper_path()
    if not trusted_library(os.path.dirname(os.path.dirname(h))):
        ctx.warning('the tool directory is not owned by root: for safety, the privileged helper was not run')
        return None, None
    c = ctx.cmd(["/usr/bin/python3", "-I", h, *args], root=True, show=False)
    return c, _parse_json_output(c.out)


def _helper_problems(c, data, key: str = "") -> List[str]:
    if c is None or c.simulated or c.denied or c.interrupted:
        return []
    if data is None:
        return [f"the privileged helper returned no result (exit code {c.return_code})"]
    if data.get("error"):
        return [f"the privileged helper reported: {data['error']}"]
    entry = data.get(key) if key else None
    if not isinstance(entry, dict):
        return []
    problems: List[str] = []
    if entry.get("error"):
        problems.append(f"{key}: {entry['error']}")
    errs = entry.get("errors") or []
    if errs:
        count = int(entry.get("error_count", len(errs)))
        problems.append(f"{key}: {count} item(s) could not be removed (e.g. {errs[0]})")
    return problems


def step_temporary_files(ctx):
    if ctx.has_tool("systemd-tmpfiles"):
        
        ctx.cmd(["systemd-tmpfiles", "--clean", "--prefix=/tmp", "--prefix=/var/tmp"], root=True, tolerate=True)
    in_temp = in_debconf = 0
    problems: List[str] = []
    c, data = _run_helper(ctx, "tmp", f"/tmp:{TEMP_DAYS}")
    problems += _helper_problems(c, data, "/tmp")
    if data and "/tmp" in data:
        d = data["/tmp"]
        in_temp = int(d.get("bytes", 0))
        if d.get("in_use"):
            ctx.note(f"{d['in_use']} old file(s) in /tmp were left because a program is still using them")
        if in_temp:
            ctx.cleanup_details['old temporary files (/tmp)'] = in_temp
    c2, dc = _run_helper(ctx, "debconf") if c is not None else (None, None)
    problems += _helper_problems(c2, dc)
    if dc:
        in_debconf = int(dc.get("bytes", 0))
        if in_debconf:
            ctx.cleanup_details['old debconf copies'] = in_debconf
    ctx.declared = in_temp + in_debconf
    parts = []
    if in_temp:
        parts.append(f"/tmp: {format_bytes(in_temp)}")
    if in_debconf:
        parts.append(f"debconf: {format_bytes(in_debconf)}")
    summary = " · ".join(parts) if parts else 'no old unused items'
    problems = list(dict.fromkeys(problems))
    if problems:
        ctx.log.write("temporary-file cleanup problems: " + "; ".join(problems))
        ctx.warning("temporary-file cleanup finished with problems: " + "; ".join(problems[:3])
                    + (" …" if len(problems) > 3 else "") + " — see the log")
        return warning(f"{summary} · {len(problems)} problem(s)" if parts else f"{len(problems)} problem(s)")
    if c is None:
        return warning('privileged helper not run')
    return ok(summary)



def _user_processes(uid: int) -> Set[str]:
    names: Set[str] = set()
    try:
        for n in os.listdir("/proc"):
            if not n.isdigit():
                continue
            try:
                if os.stat(f"/proc/{n}").st_uid != uid:
                    continue
                with open(f"/proc/{n}/comm", encoding="utf-8", errors="replace") as f:
                    names.add(f.read().strip())
            except OSError:
                continue
    except OSError:
        pass
    return names


def step_user_cache(ctx):
    "Everything runs AS the user: these are the user's files (the program already runs as that user; no ownership changes)."
    home = ctx.env_data.home
    c = os.path.join(home, ".cache")
    if not (os.path.isdir(c) or os.path.exists(os.path.join(home, ".thumbnails"))):
        return skipped('nothing to clean')
    simulation = ctx.simulate
    total = 0
    errors: List[str] = []

    def tally(label: str, action, *args, **kw) -> None:
        "Runs an ``fs`` removal. A cache folder that is a symbolic link is skipped (never followed or deleted)."
        nonlocal total
        try:
            r = action(*args, **kw)
        except fs.ForbiddenPath as e:
            ctx.log.write(f"skipped for safety: {e}")
            return
        total += r.bytes
        errors.extend(r.errors)
        if r.bytes:
            ctx.cleanup_details[label] = ctx.cleanup_details.get(label, 0) + r.bytes

    
    for d in REGENERABLE_CACHES:
        part = os.path.join(c, d)
        if os.path.lexists(part):
            tally('regenerable caches (thumbnails, pip, shaders...)', fs.delete_directory_contents, part, home=home, simulate=simulation)
    for rel in (".thumbnails", os.path.join(".nv", "GLCache")):
        part = os.path.join(home, rel)
        if os.path.lexists(part):
            tally('regenerable caches (thumbnails, pip, shaders...)', fs.delete_directory_contents, part, home=home, simulate=simulation)

    
    running = _user_processes(ctx.env_data.uid)
    skipped_items: List[str] = []
    for procs, folder in BROWSERS:
        part = os.path.join(c, folder)
        if not os.path.isdir(part):
            continue
        if any(x in running for x in procs):
            skipped_items.append(folder)
        else:
            tally("browser caches", fs.delete_directory_contents, part, home=home, simulate=simulation)

    
    if os.path.isdir(c):
        preserved_items = set(PROTECTED_CACHES) | set(skipped_items)

        def exclude(path: str, is_dir: bool) -> bool:
            rel = os.path.relpath(path, c).split(os.sep)[0]
            return rel in preserved_items

        tally('app caches unused for %d days' % CACHE_DAYS, fs.clean_tree, c, age_days=CACHE_DAYS, exclude=exclude,
              home=home, simulate=simulation, regular_files_only=True)

    
    base = os.path.join(home, ".var", "app")
    for app in _list(base):
        cc = os.path.join(base, app, "cache")
        if os.path.isdir(cc) and not os.path.islink(cc):
            tally('Flatpak app caches unused for %d days' % CACHE_DAYS, fs.clean_tree, cc, age_days=CACHE_DAYS, home=home,
                  simulate=simulation, regular_files_only=True)

    
    xs = os.path.join(home, ".xsession-errors")
    try:
        if os.path.isfile(xs) and not os.path.islink(xs) and os.path.getsize(xs) > XSESSION_ERROR_LIMIT:
            total += os.path.getsize(xs)
            if not simulation:
                open(xs, "w").close()
    except OSError as e:
        errors.append(f"{xs}: {e.strerror}")

    ctx.declared = total
    if skipped_items:
        ctx.note(f"cache for {', '.join(skipped_items)} was not cleaned because the browser was open")
    reason = f"{format_bytes(total)} deleted" + (f" · browser open: {', '.join(skipped_items)}" if skipped_items else "")
    if errors:
        ctx.log.write('errors while cleaning personal cache: ' + "; ".join(errors))
        ctx.warning(f"{len(errors)} item(s) in your personal cache could not be removed (e.g. "
                    + "; ".join(errors[:3]) + (" …" if len(errors) > 3 else "") + ") — see the log")
        return warning(f"{reason} · {len(errors)} error(s)")
    return ok(reason)


def step_development_cache(ctx):
    'conda and yarn (as the user). npm is deliberately excluded: it repairs itself, and deleting its cache only makes it slower.'
    acted = False
    if ctx.env_data.conda:
        acted = True
        ctx.cmd([ctx.env_data.conda, "clean", "--all", "-y"], user_path=True)
    yarn = shutil.which("yarn", path=ctx.env_data.user_path)
    if yarn:
        acted = True
        ctx.cmd([yarn, "cache", "clean"], user_path=True)
    if not acted:
        return skipped("conda/yarn unavailable")
    return ok()



def _disk_usage(ctx, path: str) -> int:
    if not os.path.exists(path):
        return 0
    r = ctx.read_only_command(["du", "-sb", "-x", path], timeout=90, log_output=False)
    return parse_du(r.out)


def step_large_file_report(ctx):
    'Looks for large files the program does NOT delete on its own. Reports only.'
    home = ctx.env_data.home
    candidates: List[Tuple[str, str]] = [
        (os.path.join(home, ".local", "share", "Trash"), 'Trash (your files — empty it yourself, if you wish)'),
        (os.path.join(home, ".npm"), 'npm cache (deliberately preserved)'),
        ("/var/crash", 'crash reports (preserved as evidence)'),
        ("/var/lib/systemd/coredump", 'core dumps (preserved as evidence)'),
        ("/var/lib/snapd", "Snap data"),
        ("/var/lib/flatpak", "Flatpak apps and runtimes"),
        ("/var/log", 'system logs (rotated logs are not deleted)'),
    ]
    c = os.path.join(home, ".cache")
    for name in _list(c):
        if name in PROTECTED_CACHES:
            candidates.append((os.path.join(c, name), "cache preserved because it is expensive to download or stores state"))
    found_items: List[Tuple[int, str, str]] = []
    for path, obs in candidates:
        if ctx.stopped:
            break
        ctx.progress(None, f"measuring {path}")
        b = _disk_usage(ctx, path)
        if b >= 200 * MB:
            found_items.append((b, path, obs))
    ctx.opportunities = sorted(found_items, reverse=True)[:10]
    return ok(f"{len(ctx.opportunities)} large location(s)" if ctx.opportunities else "no large leftovers")



def _meminfo_kb(key: str) -> int:
    try:
        with open(("/proc/meminfo"), encoding="utf-8") as f:
            for l in f:
                if l.startswith(key + ":"):
                    return int(l.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _cache_kb() -> int:
    return _meminfo_kb("Cached") + _meminfo_kb("Buffers") + _meminfo_kb("SReclaimable")


def step_trim(ctx):
    if not ctx.has_tool("fstrim"):
        return skipped('fstrim is unavailable')
    if ctx.state and ctx.state.storage_status in (ATTENTION, CRITICAL):
        return skipped('disk warning: avoided an additional write operation')
    r = ctx.cmd(["fstrim", "-av"], root=True, tolerate=True)
    if r.ok:
        return ok(" ".join(r.lines()[:1]))
    if r.contains(r"(?i)not supported|discard"):
        return skipped('disk does not support TRIM (HDD?)')
    ctx.warning(f"fstrim finished with exit code {r.return_code}")
    return warning(f"fstrim code {r.return_code}")


def step_ram_cache(ctx):
    'Page/dentry/inode cache + recycles swap only when plenty of RAM is available (v1.3).'
    ram_before, cache_before = _meminfo_kb("MemAvailable") * 1024, _cache_kb() * 1024
    swap_before = (_meminfo_kb("SwapTotal") - _meminfo_kb("SwapFree")) * 1024
    ctx.cmd(["sync"])
    if not ctx.cmd(["sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"], root=True).ok:
        ctx.warning('could not clear the RAM cache (drop_caches)')
    swap_kb = _meminfo_kb("SwapTotal") - _meminfo_kb("SwapFree")
    avail_kb = _meminfo_kb("MemAvailable")
    if swap_kb > 51200:
        if avail_kb > swap_kb + 524288:
            r = ctx.read_only_command(["swapon", "--show=NAME,PRIO", "--noheadings", "--raw"], log_output=False)
            for l in r.lines():
                part = l.split()
                name, prio = part[0], (part[1] if len(part) > 1 else "-1")
                script = ('swapoff "$1" && { if [ "$2" -ge 0 ] 2>/dev/null; then swapon -p "$2" "$1"; else swapon "$1"; fi; }')
                if not ctx.cmd(["timeout", "900", "sh", "-c", script, "_", name, prio], root=True).ok:
                    ctx.warning(f"could not recycle swap {name}")
        else:
            ctx.note(f"swap in use ({format_bytes(swap_kb * 1024)}) but not enough free RAM to safely flush it")
    ram_after, cache_after = _meminfo_kb("MemAvailable") * 1024, _cache_kb() * 1024
    swap_after = (_meminfo_kb("SwapTotal") - _meminfo_kb("SwapFree")) * 1024
    ctx.ram = {"ram_before": ram_before, "ram_after": ram_after, "cache_before": cache_before, "cache_after": cache_after, "swap_before": swap_before, "swap_after": swap_after}
    return ok(f"cache {format_bytes(cache_before)} → {format_bytes(cache_after)}")


def steps(ctx, round: int) -> List[Step]:
    steps: List[Step] = [
        Step("apt_cleanup", 'Orphans, configs, and apt cache', "C", step_apt_cleanup, 3, "cleanup", DIFFICULT, root=True),
        Step("orphaned_kernels", "Old kernel leftovers", "C", step_orphaned_kernels, 1, "cleanup", DIFFICULT, root=True),
    ]
    if round == 1:
        steps += [
            Step("flatpak_snap_cleanup", 'Unused Flatpak/Snap', "C", step_flatpak_snap_cleanup, 2, "cleanup", DIFFICULT, root=True),
            Step("journal", "systemd journal (vacuum)", "C", step_journal, 1, "cleanup", DIFFICULT, root=True),
            Step("tmp", 'Temporary files (tmpfiles, /tmp)', "C", step_temporary_files, 1, "cleanup", DIFFICULT, root=True),
            Step("user_cache", "Personal cache (~/.cache, browsers)", "C", step_user_cache, 3, "cleanup", DIFFICULT),
            Step("development_cache", "Development caches (conda, yarn)", "C", step_development_cache, 2, "cleanup", DIFFICULT),
            Step("opportunities", 'Where large files remain (report only)', "C", step_large_file_report, 2, "", REVERSIBLE),
        ]
    return steps


def final_steps(ctx) -> List[Step]:
    return [
        Step("trim", "TRIM (fstrim)", "F", step_trim, 1, "cleanup", REVERSIBLE, root=True),
        Step("ram", "RAM and swap cache (drop_caches)", "F", step_ram_cache, 1, "", REVERSIBLE, root=True),
    ]
