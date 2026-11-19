
'REPAIR phase.\n\nProject rule: repair only with EVIDENCE. Each step first measures/reads, then decides whether to make a change, and\nrecords its findings (``ctx.issue``) in the report. If there is no issue, it says so.\nNo ``fsck`` on a mounted disk, no GRUB reinstall "just in case", and no enabling\na service just because it is disabled.\n'
from __future__ import annotations

import os
import re
from typing import List

from .. import diagnostics as dg
from .. import packages as packages
from ..environment import ENV_APT
from ..constants import MIN_BOOT_MB, REINSTALL_LIMIT, CORE_PACKAGES, LIBRARY_SEGFAULT_PATTERN
from ..context import PARTIAL, REVERSIBLE, Step, warning, error, ok, skipped

_DPKG_VERIFY_PATTERN = re.compile(r"^.{9} [^c] /(usr/)?(s?bin|lib(64|32)?)/")


def _is_binary_mismatch(line: str) -> bool:
    'A dpkg --verify line counts only for a missing file, an executable or an ELF object (never data/config files).'
    if not _DPKG_VERIFY_PATTERN.match(line):
        return False
    if line.startswith("missing"):
        return True
    path = line[12:]
    try:
        if os.access(path, os.X_OK):
            return True
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return True


def _env():
    return dict(ENV_APT)



def step_dpkg_diagnostics(ctx):
    broken, detail = packages.diagnose_packages(ctx)
    if broken:
        ctx.issue("packages", detail, "remains", key="packages")
        ctx.log.write("dpkg --audit: " + detail)
        return ok('pending items found')
    return ok("consistent")


def step_dpkg_configuration(ctx):
    r = ctx.read_only_command(["dpkg", "--audit"], root=ctx.has_root and not ctx.simulate, log_output=False)
    has_pending = bool(r.out.strip()) and not (r.return_code != 0 and r.out.strip().startswith("dpkg: error"))
    if not has_pending and not _pending_dpkg_updates():
        return skipped("nothing pending")
    c = ctx.cmd(["dpkg", "--configure", "-a", "--force-confdef", "--force-confold"], root=True,
                protected=True, env=_env())
    if not c.ok:
        ctx.error("dpkg --configure -a failed")
        return error("dpkg --configure failed")
    ctx.dpkg_error_pending = False
    ctx.recover_upgrade()
    ctx.issue("packages", "", "fixed", key="packages")
    return ok('completed')


def _pending_dpkg_updates() -> bool:
    try:
        return any(os.scandir(("/var/lib/dpkg/updates")))
    except OSError:
        return False


def step_fix_broken_dependencies(ctx):
    if packages.apt_check(ctx)[0]:
        return skipped('no broken dependencies')
    
    simulation, blocked = packages.fix_broken_blocker(ctx)
    if blocked:
        reason, message = blocked
        ctx.warning(message)
        ctx.issue("packages", f"broken dependencies: repair not run ({reason})", "remains", key="dependencies")
        for n in simulation.remove:
            ctx.not_updated(n, 'the dependency repair would require package removals — not done automatically')
        return warning(reason)
    c = ctx.apt(["--fix-broken", "install"])
    if not c.ok:
        ctx.error("apt --fix-broken failed")
        ctx.issue("packages", 'broken dependencies', "remains", key="dependencies")
        return error("apt --fix-broken failed")
    ctx.recover_upgrade()
    ctx.issue("packages", 'broken dependencies', "fixed", key="dependencies")
    return ok("fixed" + (f" ({len(simulation.remove)} simulated removal(s), allowed)" if simulation.remove else ""))


def step_apt_indexes(ctx):
    'Repository indexes: read-only. Abandoned partial downloads are safe to remove.'
    base = ("/var/lib/apt/lists")
    listing = ctx.read_only_command(["find", os.path.join(base, "partial"), "-mindepth", "1", "-maxdepth", "1"],
                                    root=True, log_output=False)
    partial_downloads = listing.lines() if listing.ok and not packages.lock_holder(ctx) else []
    empty_files = []
    try:
        empty_files = [n for n in os.listdir(base) if os.path.isfile(os.path.join(base, n))
                  and os.path.getsize(os.path.join(base, n)) == 0 and n != "lock"]
    except OSError:
        pass
    found_items = []
    if partial_downloads:
        c = ctx.cmd(["find", "/var/lib/apt/lists/partial", "-mindepth", "1", "-delete"], root=True)
        found_items.append(f"{len(partial_downloads)} abandoned partial download(s) removed")
        ctx.issue("apt", f"{len(partial_downloads)} partial index download(s)", "fixed" if c.ok else "remains")
    if empty_files:
        ctx.warning(f"{len(empty_files)} empty index file(s) in /var/lib/apt/lists: run 'Update' to recreate them")
        ctx.issue("apt", f"{len(empty_files)} empty index(es)", "remains")
        found_items.append('empty indexes')
    timestamp = ("/var/lib/apt/periodic/update-success-stamp")
    try:
        import time
        age_days = (time.time() - os.path.getmtime(timestamp)) / 86400
        if age_days > 14:
            ctx.note(f"apt indexes were last updated {int(age_days)} days ago — run 'Update'")
    except OSError:
        pass
    return ok("; ".join(found_items) if found_items else 'indexes are healthy')



def _library_segfaults(ctx) -> int:
    if not ctx.has_tool("journalctl"):
        return 0
    r = ctx.read_only_command(["journalctl", "-k", "--since", "7 days ago", "--no-pager", "-q"],
                    root=ctx.has_root and not ctx.simulate, timeout=40, log_output=False)
    return sum(1 for l in r.out.splitlines() if LIBRARY_SEGFAULT_PATTERN.search(l))


def step_essential_files(ctx):
    'dpkg --verify on core packages (v1.3); expands to all packages ONLY with evidence of corruption.'
    arch = ctx.read_only_command(["dpkg", "--print-architecture"], log_output=False).out.strip() or "amd64"
    st = ctx.state
    if st and st.storage_status in (dg.ATTENTION, dg.CRITICAL):
        return skipped('disk warning: avoided heavy reads (dpkg --verify)')
    
    
    evidence = _target_segfaults(ctx)
    mismatches: List[str] = []
    n = 0
    if evidence:
        ctx.note('evidence of software corruption (segfault inside a system library): checked ALL packages, not only core packages')
        r = ctx.read_only_command(["dpkg", "--verify"], root=True, timeout=1500, log_output=False)
        mismatches_found = [l for l in r.out.splitlines() if _is_binary_mismatch(l)]
        if len(mismatches_found) > 200:
            ctx.log.write('dpkg --verify (first 200 mismatches):\n' + "\n".join(mismatches_found[:200]))
            ctx.warning(f"{len(mismatches_found)} binary/library files differ from package records — an unusual number (damaged disk "
                      "or manually modified system). Nothing was reinstalled automatically; see the log for the list")
            ctx.issue("files", f"{len(mismatches_found)} binary/library files differ from their packages", "remains",
                         key="files")
            return warning('too many mismatches to fix automatically')
        by_package = {}
        for l in mismatches_found:
            by_package.setdefault(_owner(ctx, l.split()[-1]), []).append(l)
        for pkg, ls in by_package.items():
            if pkg:
                mismatches.append(pkg)
                ctx.log.write(f"dpkg --verify {pkg}:\n" + "\n".join(ls))
        n = -1
    else:
        for part in CORE_PACKAGES:
            if packages.package_state(ctx, f"{part}:{arch}") != "ii":
                continue
            n += 1
            r = ctx.read_only_command(["dpkg", "--verify", f"{part}:{arch}"], root=True, log_output=False)
            output = [l for l in r.out.splitlines() if _is_binary_mismatch(l)]
            if output:
                mismatches.append(f"{part}:{arch}")
                ctx.log.write(f"dpkg --verify {part}:{arch}:\n" + "\n".join(output))
    if not mismatches:
        return ok('packages are intact' if n < 0 else f"{n} intact package(s)")
    ctx.warning('files with mismatched content in: ' + " ".join(mismatches))
    ctx.issue("files", 'binaries/libraries that differ from the package: ' + " ".join(mismatches), "remains", key="files")
    if len(mismatches) <= REINSTALL_LIMIT and not ctx.offline:
        c = ctx.apt(["install", "--reinstall", *mismatches])
        if c.ok:
            still = [] if ctx.simulate else [pkg for pkg in mismatches if any(
                _is_binary_mismatch(l) for l in ctx.read_only_command(["dpkg", "--verify", pkg], root=True, log_output=False).out.splitlines())]
            if still:
                ctx.warning('after reinstalling, files still differ from the package in: ' + " ".join(still)
                            + ' — something on this system changes them again; see the log')
                return warning('mismatch persists after reinstallation')
            ctx.note("Reinstalled " + " ".join(mismatches) + ' to restore the original files')
            ctx.issue("files", "", "fixed", key="files")
            return ok("reinstalled")
        ctx.error('could not reinstall ' + " ".join(mismatches))
        return error('reinstallation failed')
    return warning('many packages have mismatched files: did not reinstall them automatically')


def _target_segfaults(ctx) -> bool:
    return _library_segfaults(ctx) > 0


def _owner(ctx, path: str) -> str:
    r = ctx.read_only_command(["dpkg", "-S", path], log_output=False)
    m = re.match(r"^([^:]+(?::[a-z0-9]+)?):", r.out)
    return m.group(1) if m else ""



def step_kernel_initramfs(ctx):
    'An installed kernel without initramfs will not boot: regenerate it only when MISSING (v1.3).'
    kernels = [v for v in packages.installed_kernels() if os.path.isdir((f"/lib/modules/{v}"))]
    if not kernels:
        return skipped('no kernels in /boot')
    acted = False
    for v in kernels:
        if os.path.exists((f"/boot/initrd.img-{v}")):
            continue
        if ctx.free_mb("/boot") < MIN_BOOT_MB:
            ctx.error(f"initramfs missing for kernel {v} and /boot has less than {MIN_BOOT_MB} MB free: cannot regenerate it")
            ctx.issue("boot", f"initramfs missing ({v}); not enough space in /boot", "remains")
            continue
        c = ctx.cmd(["update-initramfs", "-c", "-k", v], root=True, protected=True)
        if c.ok:
            acted = True
            ctx.warning(f"initramfs missing for kernel {v} — regenerated")
            ctx.issue("boot", f"initramfs missing for kernel {v}", "fixed")
        else:
            ctx.error(f"initramfs missing for kernel {v} and could not regenerate (update-initramfs -c -k {v})")
            ctx.issue("boot", f"initramfs missing for kernel {v}", "remains")
    if acted:
        ctx.cmd(["update-grub"], root=True, protected=True)
    return ok(f"{len(kernels)} kernel(s) checked")


def _grub_config(ctx) -> str:
    path = ("/boot/grub/grub.cfg")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except PermissionError:
        return ctx.read_only_command(["cat", "/boot/grub/grub.cfg"], root=True, log_output=False).out
    except OSError:
        return ""


def step_bootloader(ctx):
    'GRUB, EFI, boot time, and evidence of previous failures. GRUB is changed only with evidence.'
    found_items: List[str] = []
    if not os.path.isdir(("/boot/grub")):
        return skipped('no GRUB in /boot/grub (another boot manager is in use)')
    cfg = _grub_config(ctx)
    kernels = [v for v in packages.installed_kernels() if os.path.isdir((f"/lib/modules/{v}"))]
    reason = ""
    if not cfg.strip():
        reason = "grub.cfg is missing or empty"
    else:
        missing_kernels = [v for v in kernels if f"vmlinuz-{v}" not in cfg]
        missing_targets = []
        own_entries = re.sub(r"### BEGIN /etc/grub\.d/30_os-prober ###.*?### END /etc/grub\.d/30_os-prober ###", "", cfg, flags=re.S)
        for m in re.finditer(r"^\s*(?:linux|initrd)\s+(\S+)", own_entries, re.MULTILINE):
            target = m.group(1)
            if "$" in target or not target.startswith("/"):
                continue
            if not (os.path.exists((target)) or os.path.exists(("/boot" + target))):
                missing_targets.append(target)
        if missing_kernels:
            reason = "installed kernel(s) missing from the GRUB menu: " + ", ".join(missing_kernels)
        elif missing_targets:
            reason = 'GRUB menu points to files that do not exist: ' + ", ".join(sorted(set(missing_targets))[:3])
    if reason:
        ctx.warning(f"{reason} — regenerated the menu (update-grub)")
        c = ctx.cmd(["update-grub"], root=True, protected=True)
        ctx.issue("boot", reason, "fixed" if c.ok else "remains")
        if not c.ok:
            ctx.error("update-grub failed")
        found_items.append("GRUB menu regenerated")
    
    if os.path.isdir(("/sys/firmware/efi")) and _in_fstab("/boot/efi"):
        if ctx.read_only_command(["findmnt", "-n", "/boot/efi"], log_output=False).return_code != 0:
            ctx.warning('/boot/efi is in fstab but is not mounted: GRUB/shim updates may fail')
            ctx.issue("boot", '/boot/efi is not mounted', "remains")
    
    if ctx.has_tool("systemd-analyze"):
        t = ctx.read_only_command(["systemd-analyze", "time"], log_output=False).out
        m = re.search(r"=\s*(?:(\d+)min\s*)?([\d.]+)s", t)
        if m:
            total = int(m.group(1) or 0) * 60 + float(m.group(2))
            if total > 90:
                b = ctx.read_only_command(["systemd-analyze", "blame"], log_output=False).out.splitlines()[:4]
                ctx.note(f"slow boot ({int(total)} s). Longest-running: " + " | ".join(x.strip() for x in b))
    dumps = _failure_evidence()
    if dumps:
        ctx.note(dumps + ' — preserved (cleanup never deletes them)')
    if ctx.has_tool("systemctl") and os.path.isdir(("/sys/module/apparmor")):
        a = ctx.read_only_command(["systemctl", "is-active", "apparmor"], log_output=False).out.strip()
        if a and a != "active":
            ctx.note(f"AppArmor (security mechanism) is not active ({a}) — no changes made")
    return ok("; ".join(found_items) if found_items else 'no boot problems')


def _in_fstab(mount_point: str) -> bool:
    try:
        with open(("/etc/fstab"), encoding="utf-8", errors="replace") as f:
            return any(len(l.split()) > 1 and l.split()[1] == mount_point for l in f if not l.lstrip().startswith("#"))
    except OSError:
        return False


def _failure_evidence() -> str:
    parts = []
    try:
        n = len(os.listdir(("/sys/fs/pstore")))
        if n:
            parts.append(f"{n} entry/entries in /sys/fs/pstore")
    except OSError:
        pass
    try:
        n = len([x for x in os.listdir(("/var/crash")) if x.endswith(".crash")])
        if n:
            parts.append(f"{n} report(s) in /var/crash")
    except OSError:
        pass
    return ('evidence of previous failures: ' + ", ".join(parts)) if parts else ""



_LDCONFIG_WARNINGS = re.compile(r"is not a symbolic link|is empty, not checked|not an ELF|Invalid")


def _newer_than(ctx, folder: str, reference: str, name: str) -> bool:
    if not os.path.exists((reference)):
        return False
    r = ctx.read_only_command(["find", folder, "-name", name, "-newer", reference, "-print", "-quit"], log_output=False)
    return bool(r.out.strip())


def step_system_caches(ctx):
    'Each routine runs only if there is evidence that it is outdated (the prototype always ran all of them).'
    completed: List[str] = []
    failures: List[str] = []

    def attempt(label, argv, **kw):
        c = ctx.cmd(argv, root=True, **kw)
        (completed if c.ok else failures).append(label)

    if ctx.has_tool("ldconfig"):
        r = ctx.read_only_command(["ldconfig", "-N", "-X", "-v"], log_output=False)
        mismatches = [l for l in r.out.splitlines() if _LDCONFIG_WARNINGS.search(l)]
        if mismatches:
            ctx.issue("libraries", "ldconfig reports: " + "; ".join(mismatches[:2]), "remains", key="ldconfig")
            attempt("ldconfig", ["ldconfig"])
            ctx.issue("libraries", "", "fixed" if "ldconfig" in completed else "remains", key="ldconfig")
    mods = (f"/lib/modules/{ctx.env_data.kernel}")
    if os.path.isdir(mods) and ctx.has_tool("depmod"):
        dep = os.path.join(mods, "modules.dep")
        if not os.path.exists(dep) or os.path.getsize(dep) == 0:
            ctx.issue('modules', f"modules.dep is missing for the running kernel ({ctx.env_data.kernel})", "remains", key="depmod")
            attempt("depmod", ["depmod", "-a"])
            ctx.issue('modules', "", "fixed" if "depmod" in completed else "remains", key="depmod")
    if ctx.has_tool("fc-cache") and ctx.has_tool("fc-list"):
        if not ctx.read_only_command(["fc-list", ":", "family"], log_output=False).out.strip():
            attempt("fontconfig", ["fc-cache", "-f"])
    if ctx.has_tool("glib-compile-schemas") and os.path.isdir(("/usr/share/glib-2.0/schemas")):
        comp = "/usr/share/glib-2.0/schemas/gschemas.compiled"
        if not os.path.exists((comp)) or _newer_than(ctx, "/usr/share/glib-2.0/schemas", comp, "*.gschema.xml"):
            attempt("glib-schemas", ["glib-compile-schemas", "/usr/share/glib-2.0/schemas"])
    if ctx.has_tool("update-desktop-database") and _newer_than(ctx, "/usr/share/applications", "/usr/share/applications/mimeinfo.cache", "*.desktop"):
        attempt("desktop-database", ["update-desktop-database", "-q"])
    if ctx.has_tool("update-mime-database") and _newer_than(ctx, "/usr/share/mime/packages", "/usr/share/mime/mime.cache", "*.xml"):
        attempt("mime-database", ["update-mime-database", "/usr/share/mime"])
    if ctx.has_tool("gtk-update-icon-cache"):
        base = ("/usr/share/icons")
        try:
            themes = [d for d in sorted(os.listdir(base)) if os.path.exists(os.path.join(base, d, "icon-theme.cache"))]
        except OSError:
            themes = []
        for t in themes:
            if _newer_than(ctx, f"/usr/share/icons/{t}", f"/usr/share/icons/{t}/icon-theme.cache", "*.png") \
                    or _newer_than(ctx, f"/usr/share/icons/{t}", f"/usr/share/icons/{t}/icon-theme.cache", "*.svg"):
                attempt(f"icons:{t}", ["gtk-update-icon-cache", "-q", "-f", f"/usr/share/icons/{t}"], tolerate=True)
    if ctx.has_tool("systemctl") and ctx.state and ctx.state.systemd_failures:
        for u in sorted(ctx.state.systemd_failures)[:10]:
            v = ctx.read_only_command(["systemctl", "show", u, "-p", "NeedDaemonReload", "--value"], log_output=False).out.strip()
            if v == "yes":
                attempt("daemon-reload", ["systemctl", "daemon-reload"])
                break
    if failures:
        ctx.warning("Failed: " + ", ".join(failures) + " (see the log)")
        return warning('routines failed')
    if not completed:
        return ok('no outdated system caches')
    return ok("Rebuilt: " + ", ".join(completed))



def step_home_ownership(ctx):
    'Restores user ownership for items sudo left owned by root in ~/.cache ~/.config ~/.local (v1.3).'
    env_data = ctx.env_data
    if env_data.is_root:
        return skipped("running as root")
    uid, cap, total, needs_review = env_data.uid, 200, 0, False
    for target in (".cache", ".config", ".local", ".ICEauthority", ".Xauthority"):
        path = os.path.join(env_data.home, target)
        if not (os.path.exists(path) or os.path.islink(path)):
            continue
        if os.path.islink(path):
            ctx.warning(f"{path}  is a symbolic link — skipped")
            continue
        owner = os.stat(path).st_uid
        if owner not in (uid, 0):
            ctx.warning(f"{path} belongs to another user (uid {owner}) — ignored")
            continue
        r = ctx.read_only_command(["find", path, "-xdev", "-user", "root", "-printf", "%u:%g %m %p\n"], root=True, log_output=False)
        items = r.lines()
        if not items:
            continue
        ctx.log.write(f"root-owned items in {path} ({len(items)}):")
        for l in items[:cap]:
            ctx.log.raw_write("  " + l)
        if len(items) > cap:
            ctx.warning(f"{len(items)} root-owned items in {path} (more than {cap}): nothing changed — the list is in the log; review before fixing")
            ctx.issue('permissions', f"{len(items)} root-owned items in {path}", "remains")
            needs_review = True
            continue
        total += len(items)
        ctx.issue('permissions', f"{len(items)} root-owned item(s) in {path}", "remains", key=f"ownership:{path}")
        ctx.cmd(["find", path, "-xdev", "-user", "root", "-group", "root", "-execdir", "chown", "-h", "-v",
                 "--from=root:root", f"{env_data.user}:{env_data.group}", "--", "{}", "+"], root=True)
        ctx.cmd(["find", path, "-xdev", "-user", "root", "!", "-group", "root", "-execdir", "chown", "-h", "-v",
                 "--from=root", env_data.user, "--", "{}", "+"], root=True)
        ctx.issue('permissions', "", "fixed", key=f"ownership:{path}")
    if total == 0:
        return warning('root-owned items to review') if needs_review else skipped("all user-owned files")
    return ok(f"{total} item(s) returned to their owner (see log)")



_PROTECTED_SERVICES = re.compile(r"^(systemd-.*|dbus.*|NetworkManager.*|lightdm.*|display-manager.*|gdm.*|getty@.*|"
                                  r"user@.*|accounts-daemon.*|polkit.*|udisks2.*|apparmor.*|ufw.*)$")
_CONFIGURATION_ERROR_PATTERN = re.compile(r"No such file|Permission denied|not found|Invalid argument|Failed to parse|"
                             r"Failed to determine user|Failed at step|Unknown (lvalue|section)|unable to open", re.I)


def _unit_properties(ctx, unit: str):
    r = ctx.read_only_command(["systemctl", "show", unit, "-p", "Result,ExecMainStatus,NRestarts,UnitFileState,ActiveState"],
                    log_output=False)
    return dict(l.split("=", 1) for l in r.out.splitlines() if "=" in l)


def step_services(ctx):
    'Investigates the cause of each failed unit BEFORE considering any change. Old failures are only reported.'
    if not ctx.has_tool("systemctl") or not os.path.isdir(("/run/systemd/system")):
        return skipped('systemd is unavailable')
    now = dg.systemd_failures(ctx)
    before = ctx.state.systemd_failures if ctx.state else set()
    active_before = ctx.state.active_before if ctx.state else set()
    if not now:
        return ok('no failed units')
    restarted = 0
    for u in sorted(now)[:12]:
        props = _unit_properties(ctx, u)
        j = ctx.read_only_command(["journalctl", "-u", u, "-n", "15", "--no-pager", "-o", "cat", "-q"],
                        root=ctx.has_root and not ctx.simulate, timeout=20, log_output=False)
        last_message = (j.lines() or ['(no journal messages)'])[-1][:140]
        try:
            exit_code = int(props.get("ExecMainStatus", "0"))
        except ValueError:
            exit_code = 0
        config = 200 <= exit_code <= 243 or bool(_CONFIGURATION_ERROR_PATTERN.search(j.out))
        cause = (f"{props.get('Result', '?')}" + (f", exit code {exit_code}" if exit_code else "")
                 + (", restarted %s time(s)" % props["NRestarts"] if props.get("NRestarts", "0") not in ("", "0") else "")
                 + f" — {last_message}")
        new_version = u not in before
        should_restart = (new_version and u.endswith(".service") and u in active_before and not config
                and not _PROTECTED_SERVICES.match(u) and props.get("UnitFileState", "") in ("enabled", "static", "alias")
                and props.get("Result", "") in ("exit-code", "signal", "timeout", "core-dump", "watchdog"))
        key = f"service:{u}"
        ctx.issue('service', f"{u}: {cause}", "remains", key=key)
        if should_restart:
            c = ctx.cmd(["systemctl", "restart", u], root=True, tolerate=True, timeout=90)
            active = ctx.read_only_command(["systemctl", "is-active", u], log_output=False).out.strip() == "active"
            if c.ok and active:
                restarted += 1
                ctx.issue('service', f"{u}: failed during this run and recovered on restart ({cause})", "fixed", key=key)
                continue
        negative_reason = ('failure predates this run (state preserved for investigation)' if not new_version
                      else 'cause appears to be a configuration issue' if config
                      else 'service is protected or there is no concrete reason to restart it')
        ctx.warning(f"{u} failed: {cause}. No changes made ({negative_reason}). See: systemctl status {u} · journalctl -u {u}")
    remaining = sorted(now)[12:]
    if remaining:
        ctx.warning(f"{len(remaining)} more failed unit(s) not detailed: {' '.join(remaining[:6])}…")
    return warning(f"{len(now)} failed unit(s)") if len(now) > restarted else ok(f"{restarted} restarted")



def step_smart_disk(ctx):
    st = ctx.state
    if not ctx.has_tool("smartctl"):
        ctx.issue("disk", 'SMART health not checked (smartmontools is missing)', "incomplete")
        return skipped('smartmontools is not installed')
    if not st or not st.disks:
        return skipped('no SMART data (no privileges or no physical disks)')
    worst_state = dg.OK_STATUS
    for d in st.disks:
        worst_state = dg.worst_state([worst_state, d.state])
        label = f"{d.device} ({d.model})" if d.model else d.device
        if d.state == dg.CRITICAL:
            ctx.error(f"SMART reports FAILURE on {label}: {'; '.join(d.details)} — back up now")
            ctx.issue("disk", f"{label}: {'; '.join(d.details)}", "remains")
        elif d.state == dg.ATTENTION:
            ctx.warning(f"{label} with SMART warning: {'; '.join(d.details)} — back up and monitor")
            ctx.issue("disk", f"{label}: {'; '.join(d.details)}", "remains")
        elif d.state == dg.UNAVAILABLE:
            ctx.issue("disk", f"{label}: {'; '.join(d.details) or 'SMART unavailable'}", "incomplete")
    if worst_state == dg.CRITICAL:
        return error('failing disk')
    if worst_state == dg.ATTENTION:
        return warning("SMART warnings")
    return ok(f"{len([d for d in st.disks if d.state == dg.OK_STATUS])} healthy disk(s)")


def step_filesystems(ctx):
    st = ctx.state
    if not st:
        return skipped('not analyzed')
    found = False
    for a in st.filesystem_alerts:
        found = True
        if a.severity == dg.CRITICAL:
            ctx.error(f"{a.mount_point}: {a.detail}")
        else:
            ctx.warning(f"{a.mount_point} ({a.type}): {a.detail} — check with the disk UNMOUNTED "
                      "(Live USB or recovery mode): sudo fsck -f <partition>")
        ctx.issue('file system', f"{a.mount_point}: {a.detail}", "remains")
    if st.io_errors:
        found = True
        ctx.warning(f"the kernel logged {len(st.io_errors)} serious I/O/FS/oops message(s) in the last 7 days: "
                  "journalctl -k -p err --since -7d")
        ctx.issue("kernel", f"{len(st.io_errors)} I/O/FS/oops message(s) in the last 7 days", "remains")
        for l in st.io_errors[:3]:
            ctx.log.write("  " + l[:200])
    spec = st.space
    if spec:
        for l in spec.by_filesystem.values():
            if l.inodes and l.free_inodes / l.inodes < 0.10:
                found = True
                ctx.warning(f"{l.mount_point}: only {100 * l.free_inodes // l.inodes}% of inodes free")
                ctx.issue('file system', f"{l.mount_point}: inodes nearly exhausted", "remains")
    serious = any(a.severity == dg.CRITICAL for a in st.filesystem_alerts)
    if serious:
        return error('file system has a serious problem')
    return warning("warnings") if found else ok('no detectable errors')


def step_flatpak_repair(ctx):
    if not ctx.has_tool("flatpak"):
        return skipped('Flatpak is not installed')
    if not ctx.flatpak_needs_repair:
        return skipped('no sign of a Flatpak problem')
    return_code = 0
    if not ctx.cmd(["flatpak", "repair", "--system"], root=True).ok:
        return_code = 1
    if not ctx.cmd(["flatpak", "repair", "--user"], user_path=True).ok:
        return_code = 1
    ctx.issue("flatpak", 'inconsistent Flatpak installation (object/fsck error during update)',
                 "remains" if return_code else "fixed")
    if return_code:
        ctx.warning('flatpak repair ended with an error (see the log)')
        return warning('with errors')
    return ok("repaired")


def steps(ctx, round: int) -> List[Step]:
    L: List[Step] = [
        Step("dpkg_diag", "dpkg/apt integrity", "R", step_dpkg_diagnostics, 1, "repair", REVERSIBLE),
        Step("dpkg_configure", 'Pending configuration (dpkg --configure -a)', "R", step_dpkg_configuration, 2, "repair", PARTIAL, root=True),
        Step("fix_broken", 'Dependencies (apt --fix-broken)', "R", step_fix_broken_dependencies, 2, "repair", PARTIAL, root=True),
    ]
    if round == 1:
        L += [
            Step("apt_indexes", 'Repository indexes', "R", step_apt_indexes, 1, "repair", REVERSIBLE, root=True),
            Step("essential_files", "Essential files (dpkg --verify)", "R", step_essential_files, 3, "repair", PARTIAL, root=True),
            Step("kernels_initramfs", "Kernels and initramfs", "R", step_kernel_initramfs, 1, "repair", PARTIAL, root=True),
            Step("bootloader", 'Boot (GRUB, EFI, startup)', "R", step_bootloader, 1, "repair", PARTIAL, root=True),
            Step("system_caches", 'System caches (only if outdated)', "R", step_system_caches, 1, "repair", REVERSIBLE, root=True),
            Step("home_ownership", "File ownership under ~ (chown)", "R", step_home_ownership, 1, "repair", PARTIAL, root=True),
            Step("services", 'Failed services (investigation)', "R", step_services, 1, "repair", PARTIAL),
            Step("disk_smart", 'Disk health (SMART)', "R", step_smart_disk, 1, "repair", REVERSIBLE),
            Step("filesystems", "File systems and I/O errors", "R", step_filesystems, 1, "repair", REVERSIBLE),
            Step("flatpak_repair", 'Flatpak (only if an error occurred)', "R", step_flatpak_repair, 1, "repair", PARTIAL, root=True),
        ]
    return L
