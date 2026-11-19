
'Package management: everything that reads or interprets APT/dpkg state.\n\nOnly read and text-parsing functions live here (parsers execute nothing\nand are tested against real apt output). The operation steps are responsible for changing the system.\n'
from __future__ import annotations

import ast
import fnmatch
import glob
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .constants import UPGRADE_REMOVAL_LIMIT
from .security import is_running_kernel, protected_in
from .storage import parse_apt_size


_RE_INST = re.compile(r"^Inst (\S+)(?: \[([^\]]+)\])? \((\S+) (.*?)\)(?: \[.*\])?\s*$")
_RE_REMV = re.compile(r"^Remv (\S+)(?: \[([^\]]+)\])?")
_KEPT_BACK_HEADER = "The following packages have been kept back:"
_PHASED_UPDATES_HEADER = "The following upgrades have been deferred due to phasing:"


@dataclass
class InstalledPackage:
    name: str
    old_version: Optional[str]
    new_version: str
    origin: str = ""

    @property
    def new(self) -> bool:
        return self.old_version is None


@dataclass
class AptSimulation:
    install_file: List[InstalledPackage] = field(default_factory=list)
    remove: List[str] = field(default_factory=list)
    held: List[str] = field(default_factory=list)        
    deferred_phases: List[str] = field(default_factory=list)   
    ok: bool = True

    @property
    def n(self) -> int:
        return len(self.install_file)

    @property
    def upgrades(self) -> List[InstalledPackage]:
        return [part for part in self.install_file if not part.new]

    @property
    def has_kernel(self) -> bool:
        return any(re.match(r"^linux-(image|modules)-[0-9]", part.name) for part in self.install_file)


def parse_simulation(text: str) -> AptSimulation:
    'Parses the output of ``apt-get -s <command>`` (C locale).'
    simulation = AptSimulation()
    mode: Optional[str] = None
    for line in text.splitlines():
        m = _RE_INST.match(line)
        if m:
            simulation.install_file.append(InstalledPackage(m.group(1), m.group(2), m.group(3), m.group(4)))
            mode = None
            continue
        m = _RE_REMV.match(line)
        if m:
            simulation.remove.append(m.group(1))
            mode = None
            continue
        if line.startswith(_KEPT_BACK_HEADER):
            mode = "held"
            continue
        if line.startswith(_PHASED_UPDATES_HEADER):
            mode = "phased"
            continue
        if line.startswith("  ") and mode:
            target = simulation.held if mode == "held" else simulation.deferred_phases
            target.extend(line.split())
            continue
        if line and not line.startswith(" "):
            mode = None
    if re.search(r"^E: ", text, re.MULTILINE):
        simulation.ok = False
    return simulation


@dataclass
class Estimate:
    download_bytes: int = 0          
    space: int = 0          


def parse_estimate(text: str) -> Estimate:
    estimate = Estimate()
    m = re.search(r"Need to get ([\d.,]+ ?[kKMGT]?B)(?:/[\d.,]+ ?[kKMGT]?B)? of archives", text)
    if m:
        estimate.download_bytes = parse_apt_size(m.group(1))
    m = re.search(r"After this operation, ([\d.,]+ ?[kKMGT]?B) of additional disk space will be used", text)
    if m:
        estimate.space = parse_apt_size(m.group(1))
    m = re.search(r"After this operation, ([\d.,]+ ?[kKMGT]?B) disk space will be freed", text)
    if m:
        estimate.space = -parse_apt_size(m.group(1))
    return estimate


_APT_OPTIONS_WITHOUT_LOCK = ["-o", "Debug::NoLocking=1"]


def simulate(ctx, *args: str) -> AptSimulation:
    'Runs ``apt-get -s ...`` as an ordinary user (does not need root and changes nothing).\n\n    The result is cached until an apt/dpkg command CHANGES the system (``Context.cmd`` clears the cache).'
    cache = getattr(ctx, "_simulation_cache", None)
    key = ("simulation",) + args
    if cache is not None and key in cache:
        return cache[key]
    r = ctx.read_only_command(["apt-get", "-s", *_APT_OPTIONS_WITHOUT_LOCK, *args], log_output=False)
    simulation = parse_simulation(r.out)
    if r.return_code != 0:
        simulation.ok = False
    if cache is not None:
        cache[key] = simulation
    return simulation


def estimate(ctx, *args: str) -> Estimate:
    'Download size and additional disk-space estimate, without root: ``--assume-no`` displays the summary and aborts.'
    cache = getattr(ctx, "_simulation_cache", None)
    key = ("estimate",) + args
    if cache is not None and key in cache:
        return cache[key]
    r = ctx.read_only_command(["apt-get", "--assume-no", *_APT_OPTIONS_WITHOUT_LOCK, *args], log_output=False)
    estimate = parse_estimate(r.out)
    if cache is not None:
        cache[key] = estimate
    return estimate



def diagnose_packages(ctx) -> Tuple[bool, str]:
    '(broken, details): partially installed packages, broken dependencies, or interrupted dpkg.\n\n    ``dpkg --audit`` runs with privileges when available (as an ordinary user dpkg may refuse to check\n    the lock and fail for a reason unrelated to package health — this is logged, but does NOT count as broken).\n    '
    cache = getattr(ctx, "_simulation_cache", None)
    if cache is not None and ("diag",) in cache:
        return cache[("diag",)]
    issues: List[str] = []
    r = ctx.read_only_command(["dpkg", "--audit"], root=ctx.has_root and not ctx.simulate, log_output=True)
    text = r.out.strip()
    if r.return_code != 0 and text.startswith("dpkg: error"):
        ctx.log.write('dpkg --audit could not check (this does NOT count as a broken package): ' + text[:200])
    elif text:
        issues.append("packages in an inconsistent state: " + " ".join(text.split()[:12]))
    check_ok, check_summary = apt_check(ctx)
    if not check_ok:
        issues.append('broken dependencies: ' + check_summary)
    try:
        pending = [n for n in os.listdir(("/var/lib/dpkg/updates")) if n.isdigit() or n.startswith("tmp")]
    except OSError:
        pending = []
    if pending:
        issues.append('dpkg operation interrupted before completion (/var/lib/dpkg/updates)')
    result = (bool(issues), "; ".join(issues))
    if cache is not None:
        cache[("diag",)] = result
    return result


def apt_check(ctx) -> Tuple[bool, str]:
    '(ok, summary) from ``apt-get check`` (cached until an apt/dpkg command changes the system).'
    cache = getattr(ctx, "_simulation_cache", None)
    if cache is not None and ("check",) in cache:
        return cache[("check",)]
    r = ctx.read_only_command(["apt-get", *_APT_OPTIONS_WITHOUT_LOCK, "check"], log_output=False)
    summary = ""
    if r.return_code != 0:
        lines = [l for l in r.out.splitlines() if l.startswith(("E:", " ")) or "broken" in l.lower()]
        summary = " ".join(lines[:4]) or f"apt-get check code {r.return_code}"
    result = (r.return_code == 0, summary)
    if cache is not None:
        cache[("check",)] = result
    return result


def held_packages(ctx) -> List[str]:
    r = ctx.read_only_command(["apt-mark", "showhold"], log_output=False)
    return [l.strip() for l in r.out.splitlines() if l.strip() and not l.startswith(("E:", "W:"))]



LOCK_FILES = ("/var/lib/dpkg/lock-frontend", "/var/lib/dpkg/lock", "/var/lib/apt/lists/lock",
                 "/var/cache/apt/archives/lock")


def lock_holder(ctx) -> str:
    "Name(s) of processes holding the apt/dpkg lock ('' if free or unknown)."
    pids: Set[str] = set()
    existing = [(a) for a in LOCK_FILES if os.path.exists((a))]
    if not existing:
        return ""
    if ctx.has_tool("fuser"):
        r = ctx.read_only_command(["fuser", *existing], root=True, log_output=False)
        pids.update(re.findall(r"\b(\d+)\b", r.out))
    elif ctx.has_tool("lsof"):
        r = ctx.read_only_command(["lsof", "-t", *existing], root=True, log_output=False)
        pids.update(part for part in r.out.split() if part.isdigit())
    names: List[str] = []
    for pid in sorted(pids, key=int):
        if int(pid) == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as f:
                names.append(f"{f.read().strip()} (PID {pid})")
        except OSError:
            names.append(f"PID {pid}")
    return ", ".join(names)


def wait_for_apt(ctx, max_wait_seconds: int) -> str:
    t0 = time.monotonic()
    who = lock_holder(ctx)
    while who and (time.monotonic() - t0) < max_wait_seconds and not ctx.stopped:
        ctx.progress(None, f"waiting for {who} to release apt/dpkg…")
        time.sleep(3)
        who = lock_holder(ctx)
    return who



def official_urls() -> Set[str]:
    'Base URLs of OFFICIAL repositories (Mint/Ubuntu/Debian), without a trailing slash (v1.3).'
    files = [("/etc/apt/sources.list"),
                ("/etc/apt/sources.list.d/official-package-repositories.list"),
                ("/etc/apt/sources.list.d/official-package-repositories.sources"),
                ("/etc/apt/sources.list.d/ubuntu.sources"),
                ("/etc/apt/sources.list.d/debian.sources")]
    urls: Set[str] = set()
    for a in files:
        try:
            with open(a, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if re.match(r"^(deb(-src)? |URIs:)", line):
                        urls.update(u.rstrip("/") for u in re.findall(r"(?:https?|ftp)://[^\s]+", line))
        except OSError:
            continue
    return urls


def failure_sources(text: str) -> List[str]:
    'Base URLs of repositories that failed during ``apt-get update`` (v1.3).'
    raw_urls = re.findall(r"Failed to fetch (\S+)", text) + re.findall(r"GPG error: (\S+)", text)
    output: Set[str] = set()
    for u in raw_urls:
        u = re.sub(r"/dists/.*$", "", u)
        u = re.sub(r"/(In)?Release$", "", u)
        u = re.sub(r"/Packages(\.[a-z0-9]+)?$", "", u)
        output.add(u.rstrip("/"))
    return sorted(output)


def classify_failures(text: str, official_items: Optional[Set[str]] = None) -> Tuple[List[str], List[str]]:
    '(failed_official, failed_third_party), without the scheme (https://).'
    official_items = official_urls() if official_items is None else official_items
    of: List[str] = []
    tp: List[str] = []
    for base in failure_sources(text):
        host = base.split("://", 1)[-1]
        (of if base in official_items else tp).append(host)
    return of, tp



def dpkg_inventory(ctx) -> Dict[str, str]:
    r = ctx.read_only_command(["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\n"], log_output=False)
    inv: Dict[str, str] = {}
    for l in r.out.splitlines():
        if "\t" in l:
            n, v = l.split("\t", 1)
            inv[n.strip()] = v.strip()
    return inv


def flatpak_inventory(ctx) -> Dict[str, str]:
    if not ctx.has_tool("flatpak"):
        return {}
    r = ctx.read_only_command(["flatpak", "list", "--columns=ref,active,installation"], log_output=False)
    inv: Dict[str, str] = {}
    for l in r.out.splitlines():
        parts = re.split(r"\t+|\s{2,}", l.strip())
        if len(parts) >= 2 and "/" in parts[0]:
            inv[f"{parts[0]} ({parts[2] if len(parts) > 2 else 'system'})"] = parts[1]
    return inv


def snap_inventory(ctx) -> Dict[str, str]:
    if not ctx.has_tool("snap"):
        return {}
    r = ctx.read_only_command(["snap", "list"], log_output=False)
    inv: Dict[str, str] = {}
    for l in r.out.splitlines()[1:]:
        part = l.split()
        if len(part) >= 3:
            inv[part[0]] = f"{part[1]} (rev {part[2]})"
    return inv


def difference(before: Dict[str, str], after: Dict[str, str]) -> Tuple[List[Tuple[str, str, str]], List[Tuple[str, str]], List[Tuple[str, str]]]:
    '(updated[(name, before, after)], new[(name, version)], removed[(name, version)]).'
    updated = [(n, before[n], after[n]) for n in sorted(after) if n in before and before[n] != after[n]]
    new_items = [(n, after[n]) for n in sorted(after) if n not in before]
    removed = [(n, before[n]) for n in sorted(before) if n not in after]
    return updated, new_items, removed



def read_ignored(ctx) -> List[str]:
    'Updates the user asked to ignore in Mint Update Manager.\n\n    Two sources (the same ones used by mintupdate): ``/etc/mintupdate.blacklist`` (also used by\n    mintupdate-cli) and the user setting ``com.linuxmint.updates blacklisted-packages``.\n    Each item is a source-package pattern, optionally ``pattern=version``.\n    '
    items: List[str] = []
    try:
        with open(("/etc/mintupdate.blacklist"), encoding="utf-8", errors="replace") as f:
            items += [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
    except OSError:
        pass
    if ctx.has_tool("gsettings"):
        r = ctx.read_only_command(["gsettings", "get", "com.linuxmint.updates", "blacklisted-packages"], log_output=False)
        if r.return_code == 0:
            txt = re.sub(r"^@as\s*", "", r.out.strip())
            try:
                val = ast.literal_eval(txt)
                items += [str(x).strip() for x in val if str(x).strip()]
            except (ValueError, SyntaxError):
                pass
    return list(dict.fromkeys(items))


def package_sources(ctx, names: Iterable[str]) -> Dict[str, str]:
    'binary package -> source package, for installed packages (Mint ignores by source package).'
    names = list(names)
    source_map: Dict[str, str] = {}
    for i in range(0, len(names), 400):
        batch = names[i:i + 400]
        r = ctx.read_only_command(["dpkg-query", "-W", "-f=${binary:Package}\t${source:Package}\n", *batch], log_output=False)
        for l in r.out.splitlines():
            if "\t" in l:
                b, s = l.split("\t", 1)
                source_map[b.strip()] = s.strip()
                source_map[b.strip().split(":")[0]] = s.strip()
    return source_map


def matches_ignored(source: str, version: str, ignored: Iterable[str]) -> bool:
    for entry in ignored:
        name, _, ver = entry.partition("=")
        if fnmatch.fnmatch(source, name) and (not ver or ver == version):
            return True
    return False



def version_key(v: str) -> Tuple:
    'Sort key for kernel versions (6.8.0-110-generic < 6.8.0-147-generic).'
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in re.findall(r"\d+|[A-Za-z]+", v))


def installed_kernels() -> List[str]:
    'Versions with /boot/vmlinuz-<version>, from oldest to newest.'
    versions = [os.path.basename(part)[len("vmlinuz-"):] for part in glob.glob(("/boot/vmlinuz-*"))]
    return sorted((v for v in versions if v), key=version_key)


def package_state(ctx, pattern: str) -> str:
    "dpkg state ('ii', 'rc', etc.) of the first package matching the pattern; '' if none."
    r = ctx.read_only_command(["dpkg-query", "-W", "-f=${db:Status-Abbrev}\t${Package}\n", pattern], log_output=False)
    for l in r.out.splitlines():
        if "\t" in l:
            return l.split("\t", 1)[0].strip()
    return ""


def essential_packages(ctx, names: Iterable[str]) -> List[str]:
    names = [n.split(":")[0] for n in names]
    if not names:
        return []
    r = ctx.read_only_command(["dpkg-query", "-W", "-f=${binary:Package}\t${Essential}\t${Priority}\n", *names], log_output=False)
    output: List[str] = []
    for l in r.out.splitlines():
        part = l.split("\t")
        if len(part) >= 3 and (part[1].strip() == "yes" or part[2].strip() == "required"):
            output.append(part[0])
    return output


def removal_hazards(ctx, names: Iterable[str]) -> Tuple[List[str], List[str]]:
    names = list(names)
    essential = sorted(set(protected_in(names)) | set(essential_packages(ctx, names)))
    running = [n for n in names if is_running_kernel(n, ctx.env_data.kernel)]
    return essential, running


def fix_broken_blocker(ctx) -> Tuple[AptSimulation, Optional[Tuple[str, str]]]:
    review = "review with: apt-get -s --fix-broken install"
    simulation = simulate(ctx, "--fix-broken", "install")
    if not simulation.ok:
        return simulation, ("simulation failed",
                            f"could not simulate the dependency repair (apt-get -s --fix-broken install failed) — not run; {review}")
    essential, running = removal_hazards(ctx, simulation.remove)
    if running:
        return simulation, ("would remove the running kernel",
                            f"fixing dependencies would remove the RUNNING kernel ({' '.join(running)}) — not run; {review}")
    if essential:
        return simulation, ("would remove essential packages",
                            f"fixing dependencies would require removing essential package(s): {' '.join(essential)} — not run; {review}")
    if len(simulation.remove) >= UPGRADE_REMOVAL_LIMIT:
        names = " ".join(simulation.remove[:12]) + (" …" if len(simulation.remove) > 12 else "")
        return simulation, (f"would remove {len(simulation.remove)} packages",
                            f"fixing dependencies would remove {len(simulation.remove)} package(s) "
                            f"(automatic repair is blocked at {UPGRADE_REMOVAL_LIMIT} or more): {names} — not run; {review}")
    return simulation, None
