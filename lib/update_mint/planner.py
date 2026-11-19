
'Execution engine: analyzes the system, chooses an order, and runs in rounds.\n\nFlow (not a fixed command list):\n  1. INITIAL analysis (read-only): space, dpkg/apt, updates, services, disks (SMART/FS),\n     battery, firmware, Timeshift, inventories.\n  2. SAFEGUARDS: if continuing is unsafe (failing disk, read-only file system, critical battery),\n     affected operations do NOT start and the reason is added to the report.\n  3. ADAPTIVE ORDER: broken dpkg → repair first · low space → clean before downloading ·\n     otherwise update → repair → cleanup.\n  4. ROUNDS (up to MAX_ROUNDS): after each round, reassesses using fresh measurements and repeats only\n     what is still justified.\n  5. WRAP-UP (TRIM and RAM, if Cleanup) and FINAL CHECK (packages, inventory, reboot, space).\n'
from __future__ import annotations

import time
import traceback
from typing import List, Tuple

from . import diagnostics as dg
from . import packages as packages, reboot as reboot, security as security
from .storage import MB, format_bytes, parse_du
from .constants import CRITICAL_BATTERY, MAX_ROUNDS, JOURNAL_MAX_MB
from .context import WARNING, ERROR, OK, SKIPPED, Context, Step, StepRecord, warning, error, ok
from .steps import update as update, clean as clean, repair as repair
from .steps import apt_pending

INTERRUPTED = "interrupted"
PHASE_NAME = {"U": 'Update', "R": 'Repair', "C": 'Cleanup'}
_MODULES = {"U": update, "R": repair, "C": clean}


def join_phases(order: List[str]) -> str:
    return " → ".join(PHASE_NAME[f] for f in order)



def analyze(ctx: Context) -> None:
    "Read-only probes. None raises an exception: anything that fails is logged as 'unavailable'."
    st = dg.HealthState()
    ctx.state = st
    selection = ctx.selection
    changes_packages = bool(selection & {"U", "R", "C"})
    needs_health_check = bool(selection & {"U", "R"})

    def probe(text, fn):
        ctx.progress(None, text)
        try:
            fn()
        except Exception:  
            ctx.log.write(f"probe '{text}' failed:\n" + traceback.format_exc())

    def space():
        ctx.space_before = ctx.meter.snapshot()
        st.space = ctx.space_before
        st.free_root_mb = ctx.free_mb("/")

    def power():
        st.battery_percent, st.on_battery, st.plugged_in = security.battery()

    def dpkg():
        if ctx.has_tool("dpkg") and ctx.has_tool("apt-get"):
            st.dpkg_broken = packages.diagnose_packages(ctx)[0]

    def apt_state():
        st.holds = packages.held_packages(ctx)
        st.ignored = packages.read_ignored(ctx)

    def updates():
        st.simulation = packages.simulate(ctx, "full-upgrade")
        st.estimate = packages.estimate(ctx, "full-upgrade")

    def services():
        st.systemd_failures = dg.systemd_failures(ctx)
        st.active_before = dg.active_units(ctx)

    def disks():
        st.disks = dg.read_smart(ctx)
        st.filesystem_alerts = dg.check_filesystems(ctx)
        st.io_errors = dg.recent_io_errors(ctx)
        st.fs_read_only = security.read_only("/")
        levels = [d.state for d in st.disks]
        if any(a.severity == dg.CRITICAL for a in st.filesystem_alerts) or st.fs_read_only:
            levels.append(dg.CRITICAL)
        elif st.filesystem_alerts or st.io_errors:
            levels.append(dg.ATTENTION)
        st.storage_status = dg.worst_state(levels) if levels else dg.UNAVAILABLE

    def apps():
        ctx.inventory_before["flatpak"] = packages.flatpak_inventory(ctx)
        ctx.inventory_before["snap"] = packages.snap_inventory(ctx)
        st.flatpak_items = len(ctx.inventory_before["flatpak"])

    def inventory():
        ctx.inventory_before["dpkg"] = packages.dpkg_inventory(ctx)

    def connection():
        online, _ = update.check_connection(ctx)
        ctx.offline = not online

    probe('disk space', space)
    probe('battery', power)
    if changes_packages:
        probe("dpkg/apt status", dpkg)
        probe('package inventory', inventory)
    if "U" in selection:
        probe('held and ignored updates', apt_state)
        probe('available updates', updates)
    if needs_health_check or "C" in selection:
        probe('services', services)
    probe('disk and file-system health', disks)
    probe("Flatpak and Snap", apps)
    if "U" in selection:
        probe('connectivity', connection)
        probe("Timeshift", lambda: dg.probe_timeshift(ctx))
        if not ctx.offline and not st.on_battery:
            probe("firmware (fwupd)", lambda: dg.probe_firmware(ctx))
        elif st.on_battery:
            st.firmware_missing_reason = 'computer on battery power (firmware requires AC power)'
    update_estimates(ctx)


def estimate_cleanup(ctx: Context) -> int:
    'Estimate (in bytes) of what Cleanup should free. It is an estimate; the final report shows the measured value.'
    total = 0
    r = ctx.read_only_command(["du", "-sb", "-x", "/var/cache/apt/archives"], log_output=False, timeout=30)
    total += parse_du(r.out)
    if ctx.has_tool("apt-get"):
        e = packages.estimate(ctx, "autoremove")
        if e.space < 0:
            total += -e.space
    if ctx.has_tool("journalctl"):
        j = clean._journal_bytes(ctx)
        total += max(0, j - JOURNAL_MAX_MB * MB)
    from . import fs
    from .constants import REGENERABLE_CACHES
    import os
    c = os.path.join(ctx.env_data.home, ".cache")
    for d in REGENERABLE_CACHES:
        part = os.path.join(c, d)
        if os.path.isdir(part):
            total += fs.tree_size(part)
    return total


def update_estimates(ctx: Context) -> None:
    st = ctx.state
    parts = []
    if "U" in ctx.selection and st.estimate is not None and st.simulation is not None and st.simulation.n:
        if st.estimate.space > 0:
            parts.append(f"updates will use ~{format_bytes(st.estimate.space)} (download of {format_bytes(st.estimate.download_bytes)})")
        else:
            parts.append(f"updates: download of {format_bytes(st.estimate.download_bytes)}, without using additional space")
    if "C" in ctx.selection:
        try:
            st.estimated_to_free = estimate_cleanup(ctx)
        except Exception:  
            st.estimated_to_free = 0
        if st.estimated_to_free:
            parts.append(f"cleanup should free ~{format_bytes(st.estimated_to_free)}")
    text = "Estimate: " + " · ".join(parts) if parts else 'Estimate: no space changes expected'
    ctx.ui.estimate(text + ' (estimate — the final report shows the measured value)')



def apply_safeguards(ctx: Context) -> None:
    'Conditions that make continuing UNSAFE. Affected operations do not start.'
    st = ctx.state
    if st.storage_status == dg.CRITICAL or st.fs_read_only:
        reason = ('file system mounted read-only' if st.fs_read_only
                  else 'critical disk failure (SMART/file system)')
        ctx.aborted = (f"{reason}: no changes were made. BACK UP now and resolve this before updating, "
                        "repairing, or cleaning")
        
        try:
            repair.step_smart_disk(ctx)
            repair.step_filesystems(ctx)
        except Exception:  
            ctx.log.write(traceback.format_exc())
        ctx.error(ctx.aborted)
        return
    if st.battery_percent is not None and st.on_battery and st.battery_percent < CRITICAL_BATTERY:
        for f in ("U", "R"):
            if f in ctx.selection:
                ctx.ignored_selection[f] = f"battery at {st.battery_percent}% without a charger (minimum {CRITICAL_BATTERY}%): connect AC power"
        ctx.selection -= {"U", "R"}
        ctx.warning(f"Battery at {st.battery_percent}% and unplugged: Update/Repair were not run to avoid interruption mid-operation.")
        if not ctx.selection:
            ctx.aborted = 'critical battery with no charger: nothing was changed'
    if st.storage_status == dg.ATTENTION:
        ctx.note('storage health warning: avoided optional heavy disk operations (full verification, TRIM). Back up your data')



def decisions(ctx: Context) -> None:
    'The only user prompts concern firmware (writes to BIOS/UEFI/controllers and cannot be undone).'
    st = ctx.state
    ctx.firmware["decision"] = "none"
    if "U" in ctx.selection and st.firmware_probed and not ctx.simulate and not st.on_battery and not ctx.aborted:
        ctx.progress(None, 'refreshing firmware metadata (fwupd)')
        try:
            dg.probe_firmware(ctx, refresh=True)
        except Exception:
            ctx.log.write("firmware metadata refresh failed:\n" + traceback.format_exc())
    if "U" not in ctx.selection or not st.firmware or ctx.simulate:
        return
    critical_items = [d for d in st.firmware if d.critical]
    common_items = [d for d in st.firmware if not d.critical]
    lines = []
    for d in st.firmware:
        lines.append(f"• {d.name}: {d.version} → {d.new_version}" + (f"  [CRITICAL: {d.critical_reason}]" if d.critical else ""))
    power = ("PLUGGED IN" if st.plugged_in else 'ON BATTERY') + (f", battery {st.battery_percent}%" if st.battery_percent is not None else "")
    text = ('A firmware update is available (through LVFS/fwupd):\n\n' + "\n".join(lines) +
             f"\n\nRisk: firmware is written to the device and CANNOT be rolled back by the operating system. "
             f"Interrupting the process (power loss or shutdown) can make the device unusable. The computer is {power}. "
             "Recommended: connect AC power, make a backup, and close other applications. "
             + ('Critical items (BIOS/UEFI, etc.) are installed only if you choose the last option.' if critical_items else ""))
    options = ['Do not install now (recommended)']
    if common_items and critical_items:
        options.append('Install peripherals only (no BIOS/UEFI)')
    options.append("Install all" + (" (includes BIOS/UEFI)" if critical_items else ""))
    if st.on_battery:
        ctx.warning('new firmware is available, but the computer is on battery power: installation was not offered')
        return
    i = ctx.ui.ask('Firmware available', text, options, 0)
    choice = options[i] if 0 <= i < len(options) else options[0]
    if choice.startswith('Install peripherals only'):
        ctx.firmware["decision"] = "peripherals"
    elif choice.startswith("Install all"):
        ctx.firmware["decision"] = "all"
    ctx.log.write(f"firmware decision: {ctx.firmware['decision']}")



def plan(ctx: Context) -> Tuple[List[str], List[str]]:
    st = ctx.state
    order: List[str] = []
    reasons: List[str] = []
    selection = ctx.selection
    broken, low = st.dpkg_broken, st.low_space
    if "R" in selection and broken:
        order.append("R")
        reasons.append('dpkg/apt has pending issues → repair before any update')
    if "C" in selection and "U" in selection and low:
        order.append("C")
        reasons.append(f"only {format_bytes(st.free_root_mb * MB)} free → clean first to make room for downloads")
    for f in "URC":
        if f in selection and f not in order:
            order.append(f)
    if "U" in selection and "R" not in selection and broken:
        reasons.append('dpkg/apt has pending issues → minimal repair before updating')
    if "U" in selection and "C" not in selection and low:
        reasons.append(f"low disk space ({format_bytes(st.free_root_mb * MB)} free) → free the minimum amount (the .deb cache) before downloading")
    if not reasons:
        if len(order) == 1:
            reasons.append(f"only {PHASE_NAME[order[0]]} was selected")
        elif "C" in order:
            reasons.append('default order — Cleanup runs last to remove what the other steps leave behind')
        else:
            reasons.append('default order — update before repair')
    return order, reasons


def needs_repair(ctx: Context) -> bool:
    return ctx.dpkg_error_pending or packages.diagnose_packages(ctx)[0]


def needs_update(ctx: Context) -> bool:
    if ctx.offline or ctx.apt_locked or ctx.apt_index_broken:
        return False
    n = apt_pending(ctx)
    return n > 0 and n != ctx.pending_after_update


def needs_cleanup(ctx: Context) -> bool:
    n_auto = 0 if ctx.autoremove_rejected else len(packages.simulate(ctx, "autoremove").remove)
    n_rc = len([part for part in clean._package_rc(ctx) if part not in ctx.preserved_rc])
    r = ctx.read_only_command(["du", "-sk", "/var/cache/apt/archives"], log_output=False)
    arch_kb = parse_du(r.out)
    return n_auto > 0 or n_rc > 0 or arch_kb > 51200



def execute_step(ctx: Context, part: Step) -> None:
    ui = ctx.ui
    ctx.step = part
    ctx.declared = 0
    ctx._step_failures.clear()
    n_av, n_er = len(ctx.warnings), len(ctx.errors)

    def record(status, reason, dur=0.0, delta=0):
        reg = StepRecord(part.id, part.title, part.phase, ctx.round, status, reason, dur, delta, part.category or "other",
                            part.reversibility, ctx.warnings[n_av:], ctx.errors[n_er:], ctx.declared)
        ctx.results.append(reg)
        ui.step_end(reg)

    if ctx.stop_signal.requested:
        ui.step_start(part)
        return record(INTERRUPTED, "interrupted at your request")
    if ctx.aborted:
        ui.step_start(part)
        return record(INTERRUPTED, 'stopped by a safety safeguard')
    if part.root and not ctx.has_root and not ctx.simulate:
        ui.step_start(part)
        return record(SKIPPED, 'requires administrative privileges (sudo unavailable)')
    ui.step_start(part)
    ctx.log.write(f"=== {part.title} (phase {part.phase}, round {ctx.round}) ===")
    if getattr(ctx, "marker", None):
        ctx.marker.write(f"in progress: {part.title}")
    free_before = ctx.meter.snapshot().free
    t0 = time.monotonic()
    try:
        r = part.fn(ctx) or ok()
    except Exception as e:  
        ctx.log.write('INTERNAL EXCEPTION:\n' + traceback.format_exc())
        ctx.error(f"internal program failure ({type(e).__name__}: {e}) — see the log")
        r = error('internal failure')
    dur = time.monotonic() - t0
    delta = ctx.meter.snapshot().free - free_before
    status = r.status
    if ctx.stop_signal.requested and status == OK and ctx._step_failures:
        status = INTERRUPTED
    
    if ctx._step_failures and status == OK and len(ctx.errors) == n_er and len(ctx.warnings) == n_av:
        ctx.warning('command(s) failed: ' + "; ".join(ctx._step_failures[:4]) + " (see the log)")
    if len(ctx.errors) > n_er:
        status = ERROR
    elif len(ctx.warnings) > n_av and status == OK:
        status = WARNING
    cat = part.category or "other"
    ctx.category_delta[cat] = ctx.category_delta.get(cat, 0) + delta
    record(status, r.reason, dur, delta)


def execute_plan(ctx: Context, order: List[str], reasons: str) -> None:
    max_rounds = 1 if ctx.simulate else MAX_ROUNDS
    for round in range(1, max_rounds + 1):
        if ctx.stopped:
            break
        ctx.round = round
        ctx.completed_rounds = round
        steps: List[Step] = []
        for f in order:
            steps += _MODULES[f].steps(ctx, round)
        ctx.ui.set_plan(steps, round, f"{join_phases(order)} — {reasons}")
        ctx.log.write(f"--- round {round}: {join_phases(order)} ({reasons}) ---")
        for part in steps:
            execute_step(ctx, part)
            if ctx.stopped:
                break
        if ctx.stopped:
            break
        
        ctx.ui.progress(None, "reassessing what remains…")
        order, rz = [], []
        if "R" in ctx.selection and needs_repair(ctx):
            order.append("R"); rz.append('dpkg/apt issues remain')
        if "U" in ctx.selection and needs_update(ctx):
            order.append("U"); rz.append('updates are still available for installation')
        if "C" in ctx.selection and needs_cleanup(ctx):
            order.append("C"); rz.append("unused items remain to be cleaned")
        reasons = "; ".join(rz)
        if not order:
            break
        if round == max_rounds:
            ctx.warning(f"Plan: work still remains ({reasons}) after {max_rounds} rounds. Run the tool again to continue.")


def final_verification(ctx: Context):
    ctx.progress(None, 'checking final state')
    if ctx.has_tool("dpkg") and ctx.has_tool("apt-get"):
        broken, detail = packages.diagnose_packages(ctx)
        if broken:
            ctx.issue("packages", detail, "remains", key="packages")
            ctx.error('dpkg/apt still has pending issues at the end: ' + detail)
        else:
            for pr in ctx.issues:
                if pr.key in ("packages", "dependencies") and pr.state == "remains":
                    pr.state = "fixed"
        ctx.inventory_after["dpkg"] = packages.dpkg_inventory(ctx)
    ctx.inventory_after["flatpak"] = packages.flatpak_inventory(ctx)
    ctx.inventory_after["snap"] = packages.snap_inventory(ctx)
    
    before, after = ctx.inventory_before.get("dpkg", {}), ctx.inventory_after.get("dpkg", {})
    if any(n.startswith(("linux-image-", "linux-modules-")) and n not in before for n in after):
        ctx.kernel_updated = True
    reboot.evaluate(ctx)
    ctx.space_after = ctx.meter.snapshot()
    return ok()


def step_analysis(ctx: Context):
    analyze(ctx)
    apply_safeguards(ctx)
    return ok('completed') if not ctx.aborted else warning('safety safeguards triggered')


def execute(ctx: Context) -> None:
    ui = ctx.ui
    try:
        if ctx.has_tool("logger"):
            ctx.read_only_command(["logger", "-t", 'update-mint', "-p", "user.info",
                         'maintenance started (' + ", ".join(PHASE_NAME[f] for f in "URC" if f in ctx.selection) + ")"],
                        log_output=False)
        ui.set_plan([Step("analysis", 'Initial computer analysis', "X", step_analysis, 2, "other", root=False)],
                         0, 'initial analysis')
        execute_step(ctx, Step("analysis", 'Initial computer analysis', "X", step_analysis, 2, "other"))
        if not ctx.aborted:
            decisions(ctx)
            order, reasons = plan(ctx)
            ctx.order, ctx.ordering_reasons = "".join(order), reasons
            execute_plan(ctx, order, reasons[0] if reasons else "")
            if "C" in ctx.selection and not ctx.stopped:
                wrap_up_steps = clean.final_steps(ctx)
                ui.set_plan(wrap_up_steps, ctx.round, "wrap-up")
                for part in wrap_up_steps:
                    execute_step(ctx, part)
        
        ui.set_plan([Step("final", 'Final check', "V", final_verification, 1)], ctx.round, "final check")
        execute_final_step(ctx)
    except Exception:  
        ctx.log.write('INTERNAL ENGINE ERROR:\n' + traceback.format_exc())
        ctx.errors.append('Engine: internal program error — see the log')
    finally:
        if ctx.has_tool("logger"):
            try:
                ctx.read_only_command(["logger", "-t", 'update-mint', "-p", "user.info", 'maintenance finished'], log_output=False)
            except Exception:  
                pass
        ctx.end_time = time.time()
        ui.end()


def execute_final_step(ctx: Context) -> None:
    part = Step("final", 'Final check', "V", final_verification, 1, "other")
    
    stopped_before = ctx.stop_signal.n
    aborted = ctx.aborted
    ctx.stop_signal.n = 0
    ctx.aborted = ""
    try:
        execute_step(ctx, part)
    finally:
        ctx.stop_signal.n = stopped_before
        ctx.aborted = aborted
