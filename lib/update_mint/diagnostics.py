
'Diagnostics: read-only probes that feed the planner decisions.\n\nEach probe is independent, never raises an exception to its caller, and returns\nsimple data. The ``evaluate_*``/``parse_*`` functions are pure (they execute nothing) and are\ntested with real or sample command output.\n'
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

from . import packages as packages
from .storage import DiskSpace
from .constants import (LOW_SPACE_MB, KERNEL_ERROR_PATTERN,
                         CRITICAL_FIRMWARE_PLUGINS, CRITICAL_FIRMWARE_PATTERN, FWUPD_TIMEOUT_SECONDS)


OK_STATUS, ATTENTION, CRITICAL, UNAVAILABLE = "ok", "attention", "critical", "unavailable"
_SEVERITY = {UNAVAILABLE: 0, OK_STATUS: 1, ATTENTION: 2, CRITICAL: 3}


@dataclass
class Health:
    device: str
    model: str = ""
    state: str = UNAVAILABLE
    details: List[str] = field(default_factory=list)


@dataclass
class FilesystemAlert:
    mount_point: str
    type: str
    detail: str
    severity: str = ATTENTION


@dataclass
class FirmwareDevice:
    id: str
    name: str
    version: str = ""
    new_version: str = ""
    plugin: str = ""
    flags: List[str] = field(default_factory=list)
    critical: bool = False
    critical_reason: str = ""
    requires_reboot: bool = False


@dataclass
class HealthState:
    space: Optional[DiskSpace] = None
    free_root_mb: int = 0
    battery_percent: Optional[int] = None
    on_battery: bool = False
    plugged_in: bool = True
    dpkg_broken: bool = False
    simulation: Optional[packages.AptSimulation] = None
    estimate: Optional[packages.Estimate] = None
    holds: List[str] = field(default_factory=list)
    ignored: List[str] = field(default_factory=list)
    systemd_failures: Set[str] = field(default_factory=set)
    active_before: Set[str] = field(default_factory=set)
    disks: List[Health] = field(default_factory=list)
    storage_status: str = UNAVAILABLE          
    filesystem_alerts: List[FilesystemAlert] = field(default_factory=list)
    io_errors: List[str] = field(default_factory=list)
    fs_read_only: bool = False
    flatpak_items: int = 0
    firmware: List[FirmwareDevice] = field(default_factory=list)
    firmware_missing_reason: str = ""
    firmware_probed: bool = False
    timeshift_ok: bool = False
    timeshift_reason: str = ""
    estimated_to_free: int = 0

    @property
    def low_space(self) -> bool:
        return self.free_root_mb < LOW_SPACE_MB



def _get_attribute(table: List[dict], ids: Tuple[int, ...]) -> Optional[dict]:
    for a in table:
        if a.get("id") in ids:
            return a
    return None


def evaluate_smart(data: dict, device: str) -> Health:
    'Parses the JSON from ``smartctl -a --json``. Never invents a diagnosis without data.'
    s = Health(device=device, model=str(data.get("model_name") or data.get("device", {}).get("name", "")))
    status = data.get("smart_status")
    msgs = " ".join(m.get("string", "") for m in data.get("smartctl", {}).get("messages", []))
    if status is None:
        s.state = UNAVAILABLE
        s.details.append('SMART is not supported by this device (virtual disk, USB, or controller)'
                          + (f": {msgs[:90]}" if msgs else ""))
        return s
    s.state = OK_STATUS
    crit: List[str] = []
    attention_details: List[str] = []
    if status.get("passed") is False:
        crit.append('SMART self-assessment: FAILING')
    temp = data.get("temperature", {}).get("current")
    if isinstance(temp, int):
        if temp >= 70:
            attention_details.append(f"high temperature ({temp} °C)")
    table = data.get("ata_smart_attributes", {}).get("table", [])
    if table:
        for a in table:
            if a.get("when_failed") == "now":
                crit.append(f"attribute {a.get('name')} is below the threshold")
        for ids, label in (((5,), "reallocated sectors"), ((197,), "pending sectors"),
                            ((198,), 'uncorrectable sectors'), ((187,), 'uncorrectable errors reported'),
                            ((199,), "CRC errors (cable/controller)")):
            a = _get_attribute(table, ids)
            raw = a.get("raw", {}).get("value", 0) if a else 0
            if isinstance(raw, int) and raw > 0:
                attention_details.append(f"{label}: {raw}")
        power_on_hours = _get_attribute(table, (9,))
        if power_on_hours and isinstance(power_on_hours.get("raw", {}).get("value"), int):
            s.details.append(f"{power_on_hours['raw']['value']} power-on hours")
    nv = data.get("nvme_smart_health_information_log")
    if nv:
        if nv.get("critical_warning", 0):
            crit.append(f"critical NVMe warning (0x{nv['critical_warning']:02x})")
        if nv.get("available_spare") is not None and nv.get("available_spare_threshold") is not None \
                and nv["available_spare"] < nv["available_spare_threshold"]:
            crit.append("NVMe spare capacity is below the threshold")
        if nv.get("percentage_used", 0) >= 90:
            attention_details.append(f"NVMe wear at {nv['percentage_used']}%")
        if nv.get("media_errors", 0) > 0:
            attention_details.append(f"media errors: {nv['media_errors']}")
        if nv.get("num_err_log_entries", 0) > 0:
            s.details.append(f"{nv['num_err_log_entries']} NVMe error log entr(y/ies) (informational)")
    log = data.get("ata_smart_error_log", {}).get("summary", {})
    if log.get("count", 0) > 0:
        attention_details.append(f"{log['count']} SMART log error(s)")
    s.details = crit + attention_details + s.details
    if crit:
        s.state = CRITICAL
    elif attention_details:
        s.state = ATTENTION
    return s


def worst_state(states: List[str]) -> str:
    states = [e for e in states if e]
    return max(states, key=lambda e: _SEVERITY.get(e, 0)) if states else UNAVAILABLE


def list_disks(ctx) -> List[str]:
    r = ctx.read_only_command(["lsblk", "-J", "-d", "-o", "NAME,TYPE"], log_output=False)
    try:
        data = json.loads(r.out)
    except ValueError:
        return []
    names = []
    for d in data.get("blockdevices", []):
        n = d.get("name", "")
        if d.get("type") == "disk" and not re.match(r"^(zram|loop|ram|sr|fd|nbd)", n):
            names.append(n)
    return names


def read_smart(ctx) -> List[Health]:
    'SMART data for each physical disk. Without smartctl or privileges: empty list (and the reason is recorded).'
    if not ctx.has_tool("smartctl"):
        ctx.note('smartmontools is not installed: disk SMART health was not checked (to enable: sudo apt install smartmontools)')
        return []
    if not (ctx.has_root or ctx.simulate):
        return []
    output: List[Health] = []
    for name in list_disks(ctx):
        r = ctx.read_only_command(["smartctl", "-a", "--json=c", "-n", "standby", f"/dev/{name}"], root=True,
                        timeout=40, log_output=False)
        try:
            data = json.loads(r.out)
        except ValueError:
            output.append(Health(f"/dev/{name}", state=UNAVAILABLE, details=['smartctl did not return readable data']))
            continue
        if r.return_code == 2 and "standby" in r.out.lower():
            output.append(Health(f"/dev/{name}", state=UNAVAILABLE, details=['disk is in standby (not woken up for testing)']))
            continue
        output.append(evaluate_smart(data, f"/dev/{name}"))
    return output



def parse_tune2fs(text: str) -> Tuple[str, int]:
    state = re.search(r"^Filesystem state:\s*(.+)$", text, re.MULTILINE)
    errors = re.search(r"^FS Error count:\s*(\d+)", text, re.MULTILINE)
    return (state.group(1).strip() if state else "", int(errors.group(1)) if errors else 0)


def parse_findmnt(text: str) -> List[Tuple[str, str, str, str]]:
    output = []
    for l in text.splitlines():
        part = l.split()
        if len(part) >= 4:
            output.append((part[0], part[1], part[2], part[3]))    
    return output


def check_filesystems(ctx) -> List[FilesystemAlert]:
    'Reads the state of MOUNTED file systems without changing anything (never runs fsck on a mounted file system).'
    alerts: List[FilesystemAlert] = []
    r = ctx.read_only_command(["findmnt", "-rn", "-o", "SOURCE,FSTYPE,TARGET,OPTIONS"], log_output=False)
    for source, type, mount_point, options in parse_findmnt(r.out):
        if not source.startswith("/dev/") or type in ("squashfs", "iso9660", "udf", "vfat", "tmpfs"):
            if type == "vfat" and "ro" in options.split(",") and mount_point.startswith("/boot"):
                alerts.append(FilesystemAlert(mount_point, "mount", 'boot partition mounted read-only'))
            continue
        if "ro" in options.split(",") and mount_point in ("/", "/home", "/var", "/boot"):
            alerts.append(FilesystemAlert(mount_point, "mount",
                                    'mounted READ-ONLY (the kernel does this when it detects a severe error)', CRITICAL))
        if not (ctx.has_root or ctx.simulate):
            continue
        if type in ("ext2", "ext3", "ext4") and ctx.has_tool("tune2fs"):
            t = ctx.read_only_command(["tune2fs", "-l", source], root=True, timeout=20, log_output=False)
            state, error_count = parse_tune2fs(t.out)
            if error_count > 0 or "error" in state.lower():
                alerts.append(FilesystemAlert(mount_point, type, f"status '{state}', {error_count} error(s) recorded in the superblock"))
        elif type == "btrfs" and ctx.has_tool("btrfs"):
            b = ctx.read_only_command(["btrfs", "device", "stats", mount_point], root=True, timeout=20, log_output=False)
            mismatches = [l for l in b.out.splitlines() if re.search(r"\s[1-9][0-9]*$", l)]
            if mismatches:
                alerts.append(FilesystemAlert(mount_point, "btrfs", 'non-zero error counters: ' + "; ".join(mismatches[:3])))
    return alerts


def recent_io_errors(ctx, days: int = 7) -> List[str]:
    'Kernel messages indicating disk/file-system failures in recent days.'
    if not ctx.has_tool("journalctl"):
        return []
    r = ctx.read_only_command(["journalctl", "-k", "--since", f"{days} days ago", "-p", "warning", "--no-pager", "-q",
                     "-o", "short-iso"], root=ctx.has_root and not ctx.simulate, timeout=40, log_output=False)
    return [l for l in r.out.splitlines() if KERNEL_ERROR_PATTERN.search(l)]



def systemd_failures(ctx) -> Set[str]:
    if not ctx.has_tool("systemctl"):
        return set()
    r = ctx.read_only_command(["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"], log_output=False)
    if not r.ok:
        return set()
    return {l.split()[0] for l in r.out.splitlines() if l.split() and not l.startswith(("Failed", "Warning"))}


def active_units(ctx) -> Set[str]:
    if not ctx.has_tool("systemctl"):
        return set()
    r = ctx.read_only_command(["systemctl", "list-units", "--type=service", "--state=active", "--no-legend",
                     "--plain", "--no-pager"], log_output=False)
    if not r.ok:
        return set()
    return {l.split()[0] for l in r.out.splitlines() if l.split()}



def parse_fwupd_json(text: str) -> List[FirmwareDevice]:
    'Parses ``fwupdmgr get-updates --json``. A critical device writes to BIOS/UEFI/a controller.'
    try:
        data = json.loads(text)
    except ValueError:
        return []
    output: List[FirmwareDevice] = []
    for d in data.get("Devices", []):
        flags = d.get("Flags") or d.get("DeviceFlags") or []
        if isinstance(flags, str):
            flags = [f.strip() for f in flags.split(",")]
        rel = (d.get("Releases") or [{}])[0]
        dev = FirmwareDevice(id=str(d.get("DeviceId", "")), name=str(d.get("Name", "?")),
                            version=str(d.get("Version", "")), new_version=str(rel.get("Version", "")),
                            plugin=str(d.get("Plugin", "")), flags=list(flags))
        reason = ""
        if "is-system-firmware" in flags:
            reason = 'system firmware (BIOS/UEFI)'
        elif dev.plugin in CRITICAL_FIRMWARE_PLUGINS:
            reason = f"writes to a critical component (plugin {dev.plugin})"
        elif CRITICAL_FIRMWARE_PATTERN.search(dev.name):
            reason = 'critical component by name'
        dev.critical = bool(reason)
        dev.critical_reason = reason
        dev.requires_reboot = "needs-reboot" in flags or "uefi" in dev.plugin
        output.append(dev)
    return output


def probe_firmware(ctx, refresh: bool = False) -> None:
    '''Looks for new firmware through fwupd/LVFS WITHOUT installing anything. Respects the user configuration.

    By default it is READ-ONLY: it uses the firmware metadata already stored on this computer. Only with
    ``refresh=True`` does it run ``fwupdmgr refresh`` (which downloads and stores new LVFS metadata); the planner
    does that only after the safety safeguards, never during the initial analysis.
    '''
    st = ctx.state
    if not ctx.has_tool("fwupdmgr"):
        st.firmware_missing_reason = 'fwupd is not installed'
        return
    if ctx.offline:
        st.firmware_missing_reason = 'no internet'
        return
    r = ctx.read_only_command(["fwupdmgr", "get-remotes", "--json"], timeout=FWUPD_TIMEOUT_SECONDS, log_output=False)
    try:
        remotes = json.loads(r.out).get("Remotes", [])
        lvfs = [x for x in remotes if x.get("Id") == "lvfs"]
        if lvfs and lvfs[0].get("Enabled") is False:
            st.firmware_missing_reason = 'the LVFS remote is disabled in your configuration (left unchanged)'
            return
    except ValueError:
        pass
    st.firmware_probed = True
    if refresh:
        ctx.cmd(["fwupdmgr", "refresh"], root=True, tolerate=True, timeout=FWUPD_TIMEOUT_SECONDS)
    r = ctx.read_only_command(["fwupdmgr", "get-updates", "--json"], root=ctx.has_root and not ctx.simulate,
                    timeout=FWUPD_TIMEOUT_SECONDS, log_output=False)
    if r.return_code == 2 or ("no updatable" in r.out.lower() and "{" not in r.out):
        st.firmware_missing_reason = 'no device with firmware available through LVFS'
        return
    st.firmware = parse_fwupd_json(r.out)
    if not st.firmware:
        if r.return_code not in (0, 2):
            st.firmware_missing_reason = f"fwupd could not check for updates (code {r.return_code}; see the log)"
        elif r.out.strip() and "{" not in r.out:
            st.firmware_missing_reason = 'fwupd returned data that could not be interpreted (see the log)'
        else:
            st.firmware_missing_reason = 'no new firmware available'



def probe_timeshift(ctx) -> None:
    st = ctx.state
    if not ctx.has_tool("timeshift"):
        st.timeshift_reason = 'Timeshift is not installed'
        return
    cfg = ("/etc/timeshift/timeshift.json")
    try:
        with open(cfg, encoding="utf-8", errors="replace") as f:
            conf = json.load(f)
    except (OSError, ValueError):
        st.timeshift_reason = 'Timeshift is installed but not configured yet'
        return
    if not conf.get("backup_device_uuid"):
        st.timeshift_reason = 'Timeshift has no snapshot device configured'
        return
    st.timeshift_ok = True
