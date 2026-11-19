
'UPDATE phase.\n\nGolden rule (a project requirement): NEVER install updates that have not yet been released for this machine.\nTherefore: use only already-configured repositories; apt decides the rollout pace for "phased updates";\npackages marked with ``apt-mark hold`` and items "ignored" in Mint Update Manager are respected;\nno new PPAs and no Mint release upgrades.\n'
from __future__ import annotations

import os
import re
import socket
import time
from typing import List

from .. import packages as packages
from ..storage import MB, GB
from ..constants import APT_LOCK_WAIT_SECONDS, CRITICAL_SPACE_MB, FIRMWARE_BATTERY_THRESHOLD, UPGRADE_REMOVAL_LIMIT
from ..context import (DIFFICULT, PARTIAL, REVERSIBLE, Step, warning, error, ok, skipped)
from . import apt_line_callback, apt_pending

_LOCK_PATTERN = r"Could not get lock|Unable to acquire the dpkg|is another process using it"
_DPKG_ERROR_PATTERN = r"dpkg: error processing|Sub-process /usr/bin/dpkg returned|dpkg was interrupted"
_CORRUPTED_INDEX_PATTERN = r"Hash Sum mismatch|File has unexpected size|Bad header line|Encountered a section with no Package"
_SENSITIVE_PACKAGES = re.compile(r"^(libc6|systemd|udev|xserver-xorg-core|nvidia-.*|mesa-.*|libgl1-mesa-dri|cinnamon|"
                                r"linux-firmware|grub-.*|initramfs-tools|network-manager|mint-meta-.*)$")



def check_connection(ctx):
    '(online, reason) — does not log anything (used by initial analysis and the step).'
    proxy_env = any(os.environ.get(k) for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"))
    n_proxy = 0
    if ctx.has_tool("apt-config"):
        r = ctx.read_only_command(["apt-config", "dump"], log_output=False)
        n_proxy = len(re.findall(r"(?im)^Acquire::https?::Proxy", r.out))
    if proxy_env or n_proxy:
        return True, "via proxy"
    for host in ("packages.linuxmint.com", "archive.ubuntu.com", "1.1.1.1"):
        try:
            with socket.create_connection((host, 443), timeout=6):
                return True, "online"
        except OSError:
            continue
    return False, 'no internet'


def step_connection(ctx):
    online, reason = check_connection(ctx)
    ctx.offline = not online
    if online:
        return ok(reason)
    ctx.warning('no internet connection — steps requiring downloads were skipped')
    return warning(reason)



def step_minimal_repair(ctx):
    "Only when dpkg is broken and 'Repair' was not selected: the minimum needed to update."
    return_code = 0
    cmd1 = ctx.cmd(["dpkg", "--configure", "-a", "--force-confdef", "--force-confold"], root=True,
                   protected=True, env=_env_apt())
    if not cmd1.ok:
        return_code = 1
    simulation, blocked = packages.fix_broken_blocker(ctx)
    if blocked:
        reason, message = blocked
        ctx.warning(message)
        ctx.issue("packages", f"dpkg/apt has pending issues that prevent updating: repair not run ({reason})", "remains")
        for n in simulation.remove:
            ctx.not_updated(n, 'the dependency repair would require package removals — not done automatically')
        return warning(reason)
    cmd2 = ctx.apt(["--fix-broken", "install"])
    if not cmd2.ok:
        return_code = 1
    if return_code:
        ctx.error('could not get dpkg/apt into a consistent state before updating')
        ctx.issue("packages", 'dpkg/apt has pending issues that prevent updating', "remains")
        return error('minimal repair failed')
    ctx.dpkg_error_pending = False
    ctx.recover_upgrade()
    ctx.issue("packages", 'dpkg/apt has pending issues before updating', "fixed")
    return ok("dpkg/apt are consistent")


def _env_apt():
    from ..environment import ENV_APT
    return dict(ENV_APT)


def step_emergency_space(ctx):
    "Low disk space and 'Cleanup' was not selected: only the safest cleanup (already-downloaded .deb cache) to make room for the download."
    before = ctx.free_mb("/")
    r = ctx.cmd(["apt-get", "clean"], root=True)
    after = ctx.free_mb("/")
    if not r.ok:
        return warning("apt-get clean failed")
    return ok(f"free space: {before} MB → {after} MB")



def step_apt_update(ctx):
    if ctx.offline:
        return skipped('no internet')
    who = packages.wait_for_apt(ctx, APT_LOCK_WAIT_SECONDS)
    if who:
        ctx.apt_locked = True
        ctx.error(f"apt/dpkg is in use by {who} (is Update Manager open?) — close it and run again")
        return error("apt is locked")
    args = ["-o", "Acquire::http::Timeout=20", "-o", "Acquire::https::Timeout=20", "update"]
    r = ctx.apt(args, on_line=apt_line_callback(ctx, download_only=True))
    if r.contains(_LOCK_PATTERN):
        ctx.apt_locked = True
        ctx.error('apt/dpkg is in use by another program (is Update Manager open?) — close it and run again')
        return error("apt is locked")
    
    if r.contains(_CORRUPTED_INDEX_PATTERN):
        ctx.warning('apt indexes are corrupted (Hash Sum mismatch/unexpected size): cleared partial downloads and retried')
        ctx.cmd(["find", "/var/lib/apt/lists/partial", "-mindepth", "1", "-delete"], root=True)
        r = ctx.apt(args, on_line=apt_line_callback(ctx, download_only=True))
        still_corrupted = r.contains(_CORRUPTED_INDEX_PATTERN)
        ctx.issue("apt", 'package indexes are corrupted', "remains" if still_corrupted else "fixed")
    
    of, tp = packages.classify_failures(r.out)
    if of:
        ctx.apt_index_broken = True
        ctx.error(f"OFFICIAL repository is unresponsive ({' '.join(of)}): nothing was installed to avoid using an outdated package "
                 "index — run the program again")
    if tp:
        ctx.warning(f"third-party repository/repositories unresponsive ({' '.join(tp)}): the remaining updates were installed; "
                  "these packages still have the old index")
    if r.contains(r"NO_PUBKEY|EXPKEYSIG|KEYEXPIRED") and not (of or tp):
        ctx.warning('repository has a missing or expired GPG key (see the log)')
    if not r.ok and not (of or tp) and r.interrupted:
        ctx.warning(f"apt update ended with code {r.return_code}")
    elif not r.ok and not (of or tp):
        ctx.apt_index_broken = True
        ctx.error(f"apt update failed (exit code {r.return_code}) for a reason that could not be tied to a specific "
                  "repository (see the log): nothing was installed to avoid using an incomplete package index — "
                  "run the program again")
    if ctx.apt_index_broken:
        return error('official repository is unresponsive')
    return ok()



def _relevant_update(simulation) -> str:
    "Reason a snapshot is worthwhile; '' if the update is small/routine."
    if simulation.has_kernel:
        return "includes a kernel"
    sensitive = [part.name for part in simulation.install_file if _SENSITIVE_PACKAGES.match(part.name)]
    if sensitive:
        return "includes core components (" + ", ".join(sensitive[:4]) + ")"
    if simulation.remove:
        return "removes packages"
    if simulation.n >= 150:
        return f"large update ({simulation.n} packages)"
    return ""


def _timeshift_info(text: str):
    dates = re.findall(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})", text)
    free = 0
    m = re.search(r"([\d.,]+)\s*([KMGT])B? free", text)
    if m:
        unit = {"K": 1024, "M": MB, "G": GB, "T": 1024 * GB}[m.group(2)]
        free = int(float(m.group(1).replace(",", ".")) * unit)
    return (max(dates) if dates else ""), free, len(dates)


def step_snapshot(ctx):
    'Creates a Timeshift snapshot before a significant update — only if Timeshift is configured and there is enough space.\n    Never deletes old snapshots.'
    if ctx.offline or ctx.apt_locked or ctx.apt_index_broken:
        return skipped('will not update')
    simulation = packages.simulate(ctx, "full-upgrade")
    if simulation.n == 0:
        return skipped('nothing to update')
    reason = _relevant_update(simulation)
    if not reason:
        return skipped('routine update: snapshot not needed')
    st = ctx.state
    if not st.timeshift_ok:
        ctx.note(f"the update {reason}, but no snapshot was created ({st.timeshift_reason or 'Timeshift unavailable'}). "
                 "Recommended: configure Timeshift so you can roll back if something goes wrong")
        ctx.timeshift = {"created": False, "reason": st.timeshift_reason}
        return skipped(st.timeshift_reason or 'Timeshift is not configured')
    snapshot_list_result = ctx.read_only_command(["timeshift", "--list"], root=True, timeout=120, log_output=False)
    latest_snapshot, free, snapshot_count = _timeshift_info(snapshot_list_result.out)
    if latest_snapshot:
        try:
            snapshot_time = time.mktime(time.strptime(latest_snapshot, "%Y-%m-%d_%H-%M-%S"))
            if time.time() - snapshot_time < 24 * 3600:
                ctx.timeshift = {"created": False, "reason": f"an existing snapshot is {latest_snapshot}"}
                return skipped(f"a snapshot less than 24 hours old already exists ({latest_snapshot})")
        except ValueError:
            pass
    spec = ctx.state.space
    required_space_bytes = 5 * GB if snapshot_count else int((spec.used if spec else 0) * 1.1)
    if free and free < required_space_bytes:
        ctx.note("Timeshift snapshot not created: insufficient space on the snapshot device "
                 f"({free // MB} MB free, approximately {required_space_bytes // MB} MB). No old snapshots were deleted")
        ctx.timeshift = {"created": False, "reason": 'not enough disk space'}
        return skipped('not enough space for a snapshot')
    r = ctx.cmd(["timeshift", "--create", "--comments", f"update-mint: before updating ({reason})",
                 "--tags", "O", "--scripted"], root=True, timeout=3600, protected=True)
    if not r.ok:
        ctx.warning('Timeshift could not create the snapshot — the update will continue WITHOUT a recent restore point')
        ctx.timeshift = {"created": False, "reason": f"failed (code {r.return_code})"}
        return warning("snapshot failed")
    ctx.timeshift = {"created": True, "reason": reason}
    return ok(f"snapshot created ({reason})")



def step_apt_upgrade(ctx):
    if ctx.offline or ctx.apt_locked:
        return skipped("skipped")
    if ctx.apt_index_broken:
        return skipped('official index is incomplete')
    if ctx.aborted:
        return skipped('stopped by a safety safeguard')
    st = ctx.state
    if st and st.free_root_mb is not None and ctx.free_mb("/") < CRITICAL_SPACE_MB:
        ctx.error(f'less than {CRITICAL_SPACE_MB} MB free on /: nothing was installed to avoid running out of space during the update')
        return error('not enough disk space')
    simulation = packages.simulate(ctx, "full-upgrade")
    if not simulation.ok:
        ctx.warning('could not simulate the update (apt-get -s full-upgrade failed) — see the log')
        return warning('simulation failed')
    
    if simulation.deferred_phases:
        ctx.note('apt deferred phased updates: ' + " ".join(simulation.deferred_phases)
                 + ' — they will arrive when Ubuntu releases them for this machine')
        for n in simulation.deferred_phases:
            ctx.not_updated(n, 'phased update: not yet released for this computer')
    for n in (st.holds if st else []):
        ctx.not_updated(n, 'held (apt-mark hold)')
    if simulation.n == 0:
        ctx.pending_after_update = 0
        return skipped('everything is already up to date')

    cmd = ["full-upgrade"]
    
    if simulation.remove:
        names = " ".join(simulation.remove[:12])
        essential_packages, running_kernel = packages.removal_hazards(ctx, simulation.remove)
        if len(simulation.remove) >= UPGRADE_REMOVAL_LIMIT or essential_packages or running_kernel:
            cmd = ["upgrade"]
            ctx.warning(f"full-upgrade would remove {len(simulation.remove)} package(s)"
                      + (f", including the RUNNING kernel ({' '.join(running_kernel)})" if running_kernel else "")
                      + (f", including essential package(s): {' '.join(essential_packages)}" if essential_packages else "")
                      + f": {names} — ran 'upgrade' only (nothing removed). If you agree: sudo apt full-upgrade (review first with -s)")
            for n in simulation.remove:
                ctx.not_updated(n, 'the update would require package removals — not done automatically')
        else:
            ctx.note(f"the update removes {len(simulation.remove)} package(s) (none essential): {names}")

    target_count = simulation.n
    if cmd == ["upgrade"]:
        target_count = packages.simulate(ctx, "upgrade").n
        for n in packages.simulate(ctx, "upgrade").held:
            ctx.not_updated(n, "kept back by apt: would require installing/removing other packages")

    
    final_command = cmd
    ignored_packages = st.ignored if st else []
    if ignored_packages and target_count:
        base = packages.simulate(ctx, *cmd) if cmd != ["full-upgrade"] else simulation
        source_map = packages.package_sources(ctx, [part.name for part in base.upgrades])
        ignored_updates = [part for part in base.upgrades if packages.matches_ignored(source_map.get(part.name, part.name), part.new_version or "", ignored_packages)]
        if ignored_updates:
            allowed_packages = [part.name for part in base.upgrades if part not in ignored_updates]
            for part in ignored_updates:
                ctx.not_updated(part.name, 'ignored by you in Mint Update Manager')
            ctx.note(f"respected {len(ignored_updates)} update(s) you asked Mint Update Manager to ignore")
            if not allowed_packages:
                ctx.pending_after_update = simulation.n
                return ok('only ignored updates were available')
            final_command = ["install", "--only-upgrade", "--no-remove", *allowed_packages]
            filtered_simulation = packages.simulate(ctx, *final_command)
            if not filtered_simulation.ok:
                ctx.warning('the remaining updates could not be installed without touching ignored updates (dependencies) — nothing installed')
                return warning('conflict with ignored updates')
            ignored_names = {part.name for part in ignored_updates}
            dragged = [part.name for part in filtered_simulation.upgrades if part.name in ignored_names]
            if dragged:
                ctx.warning(f"the remaining updates depend on updates you ignored ({', '.join(dragged)}) — nothing installed")
                return warning('conflict with ignored updates')
            target_count = filtered_simulation.n
    if target_count == 0:
        ctx.pending_after_update = simulation.n
        return ok('only updates requiring removals were available')

    ctx.progress(0.0, "starting")
    r = ctx.apt(final_command, on_line=apt_line_callback(ctx, total=target_count))
    ctx.pending_after_update = apt_pending(ctx)
    if not r.ok:
        if r.contains(_LOCK_PATTERN):
            ctx.apt_locked = True
            ctx.error("apt/dpkg is in use by another program — close it and run again")
        else:
            if r.contains(_DPKG_ERROR_PATTERN):
                ctx.dpkg_error_pending = True
                ctx.upgrade_error_index = len(ctx.errors)
            ctx.error(f"apt-get {final_command[0]} ended with an error (exit code {r.return_code})")
        return error("apt-get failed")
    if simulation.has_kernel:
        ctx.kernel_updated = True
    return ok(f"{target_count} package(s)")



_RE_DKMS = re.compile(r"^(?P<mod>[^/,\s]+)/(?P<ver>[^,\s]+),\s*(?P<kver>[^,\s]+)(?:,\s*[^:]+)?:\s*(?P<state>\w+)")


def parse_dkms(text: str):
    output = []
    for l in text.splitlines():
        m = _RE_DKMS.match(l.strip())
        if m:
            output.append((m.group("mod"), m.group("kver"), m.group("state")))
    return output


def step_kernel_dkms(ctx):
    'After a new kernel: checks whether driver modules (DKMS) were built for it.'
    kernels = packages.installed_kernels()
    if not kernels:
        return skipped('no kernels in /boot')
    new, running = kernels[-1], ctx.env_data.kernel
    if not ctx.kernel_updated and packages.version_key(new) <= packages.version_key(running):
        return skipped('no new kernel')
    
    missing_items = []
    if not os.path.exists(f"/boot/initrd.img-{new}"):
        missing_items.append("initramfs")
    if not os.path.isdir(f"/lib/modules/{new}"):
        missing_items.append('modules')
    if missing_items:
        ctx.warning(f"kernel {new}: missing {' and '.join(missing_items)} — do NOT reboot into it before running 'Repair'")
    if not ctx.has_tool("dkms"):
        ctx.note(f"new kernel installed ({new}); DKMS is not installed, nothing to check for external drivers")
        return ok('new kernel, no DKMS') if not missing_items else warning("incomplete kernel")
    r = ctx.read_only_command(["dkms", "status"], log_output=False)
    lines = parse_dkms(r.out)
    running_modules = {m for m, k, e in lines if k == running and e == "installed"}
    new_ok = {m for m, k, e in lines if k == new and e == "installed"}
    mismatches = sorted({m for m, k, e in lines if k == new and e != "installed"} | (running_modules - new_ok))
    if mismatches:
        msg = (f"DKMS module(s) not built for the new kernel {new}: {', '.join(mismatches)} — if you reboot into it, "
               "video/Wi-Fi may fail. The previous kernel was kept: select it under GRUB 'Advanced options' if needed")
        ctx.warning(msg)
        ctx.dkms_alerts.extend(mismatches)
        return warning("DKMS pending")
    ctx.note(f"DKMS drivers checked for kernel {new}: " + (", ".join(sorted(new_ok)) or 'no DKMS modules installed'))
    return ok(f"kernel {new} checked") if not missing_items else warning("incomplete kernel")



def step_flatpak_update(ctx):
    if not ctx.has_tool("flatpak"):
        return skipped('Flatpak is not installed')
    if ctx.offline:
        return skipped('no internet')
    if ctx.state and ctx.state.flatpak_items == 0:
        return skipped('no Flatpak apps/runtimes are installed')
    return_code = 0
    r = ctx.cmd(["flatpak", "update", "--system", "-y", "--noninteractive"], root=True, protected=True)
    if not r.ok:
        return_code = 1
        ctx.warning('system Flatpak update ended with an error')
        if r.contains(r"(?i)not found in local repo|corrupt|fsck|object missing|invalid"):
            ctx.flatpak_needs_repair = True
    r = ctx.cmd(["flatpak", "update", "--user", "-y", "--noninteractive"], user_path=True, protected=True)
    if not r.ok:
        return_code = 1
        ctx.warning('user Flatpak update ended with an error')
        if r.contains(r"(?i)not found in local repo|corrupt|fsck|object missing|invalid"):
            ctx.flatpak_needs_repair = True
    return warning('with errors') if return_code else ok()


def step_snap_refresh(ctx):
    if not ctx.has_tool("snap"):
        return skipped('Snap is not installed')
    if ctx.offline:
        return skipped('no internet')
    r = ctx.cmd(["snap", "refresh"], root=True, protected=True)
    if not r.ok:
        ctx.warning('snap refresh ended with an error')
        return warning('with errors')
    return ok()



def parse_ubuntu_drivers(text: str):
    '[(model, [(package, recommended)])] from ``ubuntu-drivers devices``.'
    devices = []
    current = None
    for l in text.splitlines():
        m = re.match(r"^model\s*:\s*(.+)$", l)
        if m:
            current = (m.group(1).strip(), [])
            devices.append(current)
            continue
        m = re.match(r"^driver\s*:\s*(\S+)\s*-\s*(.*)$", l)
        if m and current is not None:
            current[1].append((m.group(1), "recommended" in m.group(2)))
    return devices


def step_drivers(ctx):
    'Information only: never replaces a working driver. Updates for the installed driver already come through apt.'
    if not ctx.has_tool("ubuntu-drivers"):
        return skipped('ubuntu-drivers (Driver Manager) is unavailable')
    r = ctx.read_only_command(["ubuntu-drivers", "devices"], timeout=60, log_output=False)
    devices = parse_ubuntu_drivers(r.out)
    if not devices:
        return skipped('no device with a proprietary driver available')
    for model, drivers in devices:
        rec = [d for d, recom in drivers if recom]
        if not rec:
            continue
        installed = [d for d, _ in drivers if packages.package_state(ctx, d) == "ii"]
        if rec[0] in installed:
            ctx.note(f"driver for {model}: the recommended driver ({rec[0]}) is already installed")
        elif installed:
            ctx.note(f"driver for {model}: {installed[0]} is installed; the recommended driver would be {rec[0]}. "
                     "No changes made (the current driver is working); switch it using Driver Manager if desired")
        else:
            ctx.note(f"driver for {model}: the recommended driver is {rec[0]}, but the open-source driver is in use. "
                     "The driver was not installed automatically; see Driver Manager")
    return ok(f"{len(devices)} device(s) analyzed")



def step_firmware(ctx):
    'Firmware requires an explicit user decision (default: do not install). Never on battery power.'
    st = ctx.state
    if not st.firmware:
        return skipped(st.firmware_missing_reason or 'no new firmware')
    dec = ctx.firmware.get("decision", "none")
    names = ", ".join(f"{d.name} {d.version}→{d.new_version}" for d in st.firmware)
    if dec == "none":
        ctx.warning(f"new firmware is available ({names}) — not installed, as requested. When ready, while plugged in: sudo fwupdmgr update")
        return warning('firmware not installed')
    if st.on_battery or not st.plugged_in:
        ctx.warning('firmware not installed: the computer is on battery power — plug it in and run again')
        return warning('not plugged in')
    if st.battery_percent is not None and st.battery_percent < FIRMWARE_BATTERY_THRESHOLD:
        ctx.warning(f"firmware not installed: battery at {st.battery_percent}% (minimum {FIRMWARE_BATTERY_THRESHOLD}%)")
        return warning('low battery')
    ids = [d.id for d in st.firmware if dec == "all" or not d.critical]
    target = [d for d in st.firmware if d.id in ids]
    if not target:
        return skipped('only critical firmware was available and you chose not to install it')
    r = ctx.cmd(["fwupdmgr", "update", "-y", "--no-reboot-check", *ids], root=True, protected=True, timeout=None)
    if not r.ok:
        ctx.error(f"fwupdmgr update failed (exit code {r.return_code}) — see the log; nothing was forced")
        return error("fwupd failed")
    ctx.firmware["installed"] = [d.name for d in target]
    if any(d.requires_reboot for d in target):
        ctx.firmware["requires_reboot"] = True
    for d in st.firmware:
        if d.id not in ids:
            ctx.not_updated(f"firmware {d.name}", 'critical: you chose not to install')
    return ok(f"{len(target)} device(s)")



def steps(ctx, round: int) -> List[Step]:
    steps: List[Step] = []
    if round == 1:
        steps.append(Step("connection", 'Checking connectivity', "U", step_connection, 1, "update", REVERSIBLE))
        if "R" not in ctx.selection and ctx.state and ctx.state.dpkg_broken:
            steps.append(Step("minimal_repair", 'Minimal repair before updating', "U", step_minimal_repair, 3,
                           "repair", PARTIAL, root=True))
        if "C" not in ctx.selection and ctx.state and ctx.state.low_space:
            steps.append(Step("emergency_space", 'Free minimum space (.deb cache)', "U", step_emergency_space, 1,
                           "cleanup", REVERSIBLE, root=True))
        steps.append(Step("apt_update", "Package lists (apt update)", "U", step_apt_update, 2, "update", REVERSIBLE, root=True))
        steps.append(Step("snapshot", 'Safety snapshot (Timeshift)', "U", step_snapshot, 3, "update", PARTIAL, root=True))
    steps.append(Step("apt_upgrade", 'Updates (apt full-upgrade)', "U", step_apt_upgrade, 8, "update", PARTIAL, root=True))
    steps.append(Step("kernel_dkms", "New kernel and DKMS drivers", "U", step_kernel_dkms, 1, "update", REVERSIBLE))
    if round == 1:
        steps.append(Step("flatpak_update", "Flatpak apps (flatpak update)", "U", step_flatpak_update, 3, "update", PARTIAL, root=True))
        steps.append(Step("snap_refresh", "Snaps (snap refresh)", "U", step_snap_refresh, 2, "update", PARTIAL, root=True))
        steps.append(Step("drivers", 'Drivers (check)', "U", step_drivers, 1, "update", REVERSIBLE))
        steps.append(Step("firmware", "Firmware (fwupd)", "U", step_firmware, 2, "update", DIFFICULT, root=True))
    return steps
