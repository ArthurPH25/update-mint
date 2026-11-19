
'Final report.\n\n``build(ctx)`` returns a list of logical lines ``(level, indent, text)`` — without colors or wrapping.\nThe display layer (curses screen, simple terminal, or file) chooses colors and width. The SAME\ncontent is sent to the screen, terminal, and saved file.\n\nLevels: title | section | ok | warning | error | dim | text\n'
from __future__ import annotations

import textwrap
import time
from typing import List, Tuple

from . import packages as packages
from .storage import MB, format_bytes, format_mb_gb, format_duration
from .constants import APP_NAME, APP_VERSION
from .planner import INTERRUPTED, PHASE_NAME
from .reboot import LEVELS

Line = Tuple[str, int, str]

_OPERATION_NAMES = {"U": 'Update', "R": 'Repair', "C": "Deep cleanup"}


def _short_list(items: List[str], maximum: int = 12) -> str:
    if len(items) <= maximum:
        return ", ".join(items)
    return ", ".join(items[:maximum]) + f" … (+{len(items) - maximum})"


def _operation_status(ctx, f: str) -> Tuple[str, str]:
    if f in ctx.ignored_selection:
        return "ignored", ctx.ignored_selection[f]
    regs = [r for r in ctx.results if r.phase == f]
    if not regs:
        return ("interrupted" if (ctx.stop_signal.requested or ctx.aborted) else "no steps"), \
               (ctx.aborted or "interrupted at your request" if (ctx.stop_signal.requested or ctx.aborted) else "")
    if any(r.status == INTERRUPTED for r in regs):
        return "interrupted", ctx.aborted or "interrupted at your request"
    if any(r.status == "error" for r in regs):
        return 'completed with errors', ""
    if any(r.status == "warning" for r in regs):
        return 'completed with warnings', ""
    return 'completed', ""


def build(ctx) -> List[Line]:
    L: List[Line] = []
    add = lambda n, r, t="": L.append((n, r, t))  
    dur = (ctx.end_time or time.time()) - ctx.t0
    env_data = ctx.env_data
    selected_operations = [f for f in "URC" if f in (ctx.selection | set(ctx.ignored_selection))]
    add("title", 0, f"REPORT — {APP_NAME} {APP_VERSION}   ({time.strftime('%Y-%m-%d %H:%M:%S')})")
    add("dim", 0, f"{env_data.description} · kernel {env_data.kernel} · base {env_data.base} {env_data.base_codename}".strip())
    if ctx.simulate:
        add("warning", 0, 'DRY-RUN MODE: no changes were made to the system; repeated rounds depend on real results')

    
    add("sec", 0, "GENERAL SUMMARY")
    add("txt", 1, 'Selected operations: ' + (", ".join(_OPERATION_NAMES[f] for f in selected_operations) or 'none'))
    for f in selected_operations:
        estimate, reason = _operation_status(ctx, f)
        level = {'completed': "ok", 'completed with warnings': "warning", 'completed with errors': "error",
                 "interrupted": "error", "ignored": "warning"}.get(estimate, "txt")
        add(level, 2, f"{_OPERATION_NAMES[f]}: {estimate}" + (f" — {reason}" if reason else ""))
    if ctx.order:
        add("txt", 1, "Chosen order: " + " → ".join(PHASE_NAME[f] for f in ctx.order)
            + (f"  (reason: {ctx.ordering_reasons[0]})" if ctx.ordering_reasons else ""))
    n = {s: len([r for r in ctx.results if r.status == s]) for s in ("ok", "warning", "error", "skipped", INTERRUPTED)}
    add("txt", 1, f"Steps: {n['ok']} completed · {n['warning']} with warnings · {n['error']} with errors · "
                  f"{n['skipped']} skipped (nothing to do) · {n[INTERRUPTED]} interrupted · {ctx.completed_rounds} round(s)")
    add("txt", 1, f"Total duration: {format_duration(dur)}")
    add("error" if ctx.errors else "ok", 1, f"Failures: {len(ctx.errors)} error(s), {len(ctx.warnings)} warning(s)")
    reboot_level = ctx.reboot["level"]
    add({0: "ok", 1: "warning", 2: "error"}[reboot_level], 1, f"Reboot: {LEVELS[reboot_level]}")
    if ctx.aborted:
        add("error", 1, 'Execution stopped by a safety safeguard: ' + ctx.aborted)

    
    add("sec", 0, 'UPDATES')
    before, after = ctx.inventory_before.get("dpkg", {}), ctx.inventory_after.get("dpkg", {})
    if before and after:
        up, new_items, rem = packages.difference(before, after)
        add("txt", 1, f"APT packages: {len(up)} updated · {len(new_items)} installed · {len(rem)} removed")
        if up:
            add("dim", 2, "updated: " + _short_list([f"{n} {a}→{d}" for n, a, d in up], 8))
        if new_items:
            add("dim", 2, "installed: " + _short_list([n for n, _ in new_items]))
        if rem:
            add("warning", 2, "removed: " + _short_list([n for n, _ in rem]))
        new_kernels = [n for n, _ in new_items if n.startswith(("linux-image-", "linux-modules-"))]
        kern_up = [n for n, _, _ in up if n.startswith(("linux-image-", "linux-generic", "linux-headers-generic"))]
        if new_kernels or kern_up:
            add("txt", 1, "Kernel: update installed — " + _short_list(new_kernels + kern_up, 5)
                + f" (currently running: {env_data.kernel})")
        else:
            add("dim", 1, 'Kernel: no update in this run')
        if ctx.dkms_alerts:
            add("warning", 1, 'DKMS drivers not built for the new kernel: ' + ", ".join(ctx.dkms_alerts))
    else:
        add("dim", 1, 'APT packages: could not compare before/after (see warnings)')
    if ctx.simulate and ctx.state is not None and ctx.state.simulation is not None:
        add("txt", 1, f"In dry-run mode: apt would update {ctx.state.simulation.n} packages now")
    for name, key in (("Flatpak", "flatpak"), ("Snap", "snap")):
        a, d = ctx.inventory_before.get(key, {}), ctx.inventory_after.get(key, {})
        if a or d:
            up, new_items, rem = packages.difference(a, d)
            add("txt", 1, f"{name}: {len(up)} updated · {len(new_items)} installed · {len(rem)} removed")
    fw = ctx.firmware
    if fw.get("installed"):
        add("ok", 1, "Installed firmware: " + ", ".join(fw["installed"]))
    elif ctx.state is not None and ctx.state.firmware:
        add("warning", 1, 'Firmware available but NOT installed: ' + ", ".join(d.name for d in ctx.state.firmware))
    elif ctx.state is not None and "U" in ctx.selection:
        add("dim", 1, "Firmware: " + (ctx.state.firmware_missing_reason or "nothing to install"))
    if ctx.timeshift:
        add("ok" if ctx.timeshift.get("created") else "dim", 1,
            "Timeshift snapshot: " + ('created before the update (' + ctx.timeshift.get("reason", "") + ")"
                                     if ctx.timeshift.get("created") else 'not created — ' + str(ctx.timeshift.get("reason", "")))
            )
    if ctx.not_updated_items:
        add("sec2", 1, 'Updates NOT performed and reasons:')
        for item, reason in ctx.not_updated_items[:15]:
            add("warning", 2, f"{item}: {reason}")
        if len(ctx.not_updated_items) > 15:
            add("dim", 2, f"… {len(ctx.not_updated_items) - 15} more")

    
    add("sec", 0, 'REPAIRS')
    rel = ctx.issues
    if not rel:
        add("ok", 1, 'No detectable issues.' if "R" in ctx.selection else 'Repair was not selected.')
    else:
        add("txt", 1, f"Identified issues: {len(rel)}")
        for state, label, level in (("fixed", "fixed", "ok"), ("remains", "REMAINS", "error"),
                                      ("incomplete", 'diagnosis incomplete', "warning")):
            for part in [x for x in rel if x.state == state]:
                add(level, 2, f"[{label}] {part.category}: {part.description}")
    if ctx.procedures:
        add("sec2", 1, "Procedures that WOULD be performed (dry run — no changes were made):" if ctx.simulate
            else 'Procedures performed that changed the system:')
        for part in ctx.procedures[:14]:
            add("dim", 2, part)
        if len(ctx.procedures) > 14:
            add("dim", 2, f"… {len(ctx.procedures) - 14} more (all listed in the log)")

    
    add("sec", 0, 'CLEANUP')
    if "C" not in ctx.selection and "C" not in ctx.ignored_selection:
        add("dim", 1, 'Cleanup was not selected.')
    else:
        regs = [r for r in ctx.results if r.category == "cleanup" and r.phase in ("C", "F", "U")]
        found = False
        for r in regs:
            parts = []
            if r.delta >= 1024 * 1024:
                parts.append(f"freed {format_bytes(r.delta)}")
            if r.declared >= 1024 * 1024 and r.declared != r.delta:
                parts.append(f"the program deleted {format_bytes(r.declared)}")
            if parts:
                found = True
                add("ok", 1, f"{r.title}: " + " · ".join(parts))
        for label, v in sorted(ctx.cleanup_counts.items()):
            found = True
            add("txt", 2, f"{label}: {v}")
        for label, v in sorted(ctx.cleanup_details.items()):
            found = True
            add("txt", 2, f"{label}: {format_bytes(v)}")
        if not found:
            add("dim", 1, 'Nothing relevant to clean (or it was already clean).')
        if ctx.ram:
            r = ctx.ram
            add("dim", 1, f"RAM: cache {format_bytes(r['cache_before'])} → {format_bytes(r['cache_after'])} · "
                          f"free {format_bytes(r['ram_before'])} → {format_bytes(r['ram_after'])} (this is not disk space)")
        if ctx.opportunities:
            add("sec2", 1, 'Where large files remain (the program does NOT delete them):')
            for b, path, obs in ctx.opportunities:
                add("dim", 2, f"{format_bytes(b)}  {path} — {obs}")

    
    add("sec", 0, 'STORAGE (sum of file systems for /, /boot, /var, and the home directory)')
    start, end = ctx.space_before, ctx.space_after
    if start and end:
        add("txt", 1, f"Initial free space:    {format_mb_gb(start.free)}")
        add("txt", 1, f"Initial used space:    {format_mb_gb(start.used)}")
        add("txt", 1, f"Final free space:      {format_mb_gb(end.free)}")
        add("txt", 1, f"Final used space:      {format_mb_gb(end.used)}")
        net = end.free - start.free
        add("ok" if net > -MB else "warning", 1,
            ('Net recovered:           ' if net >= 0 else 'Net consumed:            ') + format_mb_gb(abs(net)))
        dc = ctx.category_delta
        add("txt", 1, 'By step type (measured by df before/after each step):')
        add("txt", 2, f"cleanup      {'freed' if dc['cleanup'] >= 0 else 'used'} {format_mb_gb(abs(dc['cleanup']))}")
        add("txt", 2, f"updates      {'freed' if dc['update'] >= 0 else 'used'} {format_mb_gb(abs(dc['update']))}")
        add("txt", 2, f"repairs      {'freed' if dc['repair'] >= 0 else 'used'} {format_mb_gb(abs(dc['repair']))}")
        add("dim", 1, 'Values come from df (space actually recovered). They may differ from the size of deleted files (open files, snapshots, compression) and include writes made by other programs during the run. MB/GB use base 1024, as in df -h.')
    else:
        add("warning", 1, 'Could not measure space before/after.')

    
    add("sec", 0, 'REBOOT')
    add({0: "ok", 1: "warning", 2: "error"}[reboot_level], 1, f"{LEVELS[reboot_level]}")
    for m in ctx.reboot["reasons"]:
        add("txt", 2, m)
    if reboot_level:
        add("dim", 1, 'The program does not reboot on its own: reboot whenever it is convenient for you.')

    
    if ctx.errors:
        add("sec", 0, "ERRORS")
        for e in ctx.errors:
            add("error", 1, e)
    if ctx.warnings:
        add("sec", 0, "WARNINGS")
        for a in ctx.warnings:
            add("warning", 1, a)
    if ctx.notes:
        add("sec", 0, "NOTES")
        for t in ctx.notes:
            add("dim", 1, t)
    return L


def wrap(lines: List[Line], width: int = 100, utf8: bool = True) -> List[Tuple[str, str]]:
    'Logical lines -> visual ``(level, text)`` lines already wrapped to width (with indentation and markers).'
    checked_mark = {"ok": "✔ " if utf8 else "OK ", "warning": "⚠ " if utf8 else "!! ", "error": "✖ " if utf8 else "XX "}
    reg = "─" if utf8 else "-"
    dup = "═" if utf8 else "="
    output: List[Tuple[str, str]] = []
    for level, indent, text in lines:
        if level == "sec":
            output += [("txt", ""), ("sec", text), ("dim", reg * min(len(text), width))]
            continue
        if level == "title":
            output += [("title", text), ("dim", dup * min(len(text), width))]
            continue
        pref = "  " * indent + checked_mark.get(level, "")
        cont = " " * len(pref)
        parts = textwrap.wrap(text, width=max(20, width), initial_indent=pref, subsequent_indent=cont) or [pref.rstrip()]
        output += [(level, part) for part in parts]
    return output


def as_text(lines: List[Line], width: int = 100, utf8: bool = True) -> str:
    'Plain-text report (for the file and for terminals without colors).'
    return "\n".join(t for _, t in wrap(lines, width, utf8)) + "\n"
