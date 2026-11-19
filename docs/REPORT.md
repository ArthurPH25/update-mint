# How to read the final report

The report appears on screen when the run finishes (scroll with ↑↓/PgUp/PgDn; **q** or **Enter** closes it), is printed in the terminal (and remains in scrollback), and is saved to `~/.local/state/update-mint/report_YYYY-MM-DD_HH-MM-SS.txt`. The complete log for the same run is next to it (`update-mint_….log`).

Symbols: **✔** ok · **⚠** attention · **✖** error/remaining issue. In terminals without UTF-8: `OK`, `!!`, `XX`.

## Sections

**GENERAL SUMMARY**
- *Selected operations* and each operation's status: **completed**, **completed with warnings**, **completed with errors**, **interrupted** (by you or by a safety safeguard), or **ignored** (with the reason, e.g. low battery).
- *Chosen order* and **why** (broken dpkg → repaired first; low space → cleaned first…).
- *Steps*: number completed, with warnings, with errors, **skipped** (nothing to do or required tool missing), and interrupted; number of rounds.
- *Total duration*, *Failures* (errors/warnings), and *Reboot*.

**UPDATES** — based on **before/after inventory comparison** (not apt output), so it is reliable:
APT packages updated/installed/removed (with versions), kernel, DKMS drivers not built, Flatpak/Snap, firmware (installed or available but **not** installed), Timeshift snapshot (created or reason it was not), and **"Updates NOT performed and reasons"**: *phased*, *held*, *ignored in Update Manager*, *would require package removals*.

**REPAIRS**
- *Identified issues*, each with an outcome: `[fixed]`, `[REMAINS]`, or `[diagnosis incomplete]` (for example, SMART unavailable because `smartmontools` is not installed — **that does not mean the disk is healthy**).
- *Procedures performed*: commands that **changed** the system, with their exit codes. When no issues are found: "No detectable issues" (the tool does not invent faults).

**CLEANUP**
- Per step: how much disk space was **freed** (measured) and, when the program itself deletes files (personal cache, `/tmp`), how much it **counted as deleted**. These may differ; the measured value is authoritative.
- Counts and amounts (orphaned packages removed, residual configurations purged, journal reduced…).
- *RAM*: free/cache RAM before and after — **not** disk space.
- **"Where large files remain (the program does NOT delete them)"**: Trash, `~/.npm`, `/var/crash`, AI models… for you to decide what to do.

**STORAGE** — sum of **distinct** file systems for `/`, `/boot`, `/var`, and the home directory:
initial and final free/used space, the **net** result (recovered or consumed), and below that what each type of step did (cleanup × updates × repairs), always in **MB and GB** (base 1024, like `df -h`). Updates **consume** space and cleanup **frees** it, so the net result may be negative in a run that only performs Update. The number comes from actual `df` measurements — not an estimate based on deleted-file sizes — and includes writes made by other programs during the run.

**REBOOT**
- **REQUIRED**: the system requested it (`/run/reboot-required`), a new kernel was installed, firmware was scheduled for the next boot, or modules for the running kernel disappeared.
- **RECOMMENDED**: a newer kernel was already installed, core packages (libc, systemd, Xorg, NVIDIA…) were updated, or processes/services are using replaced libraries.
- **NOT REQUIRED**: nothing is pending.
The program **never** reboots on its own.

**ERRORS / WARNINGS / NOTES** — the complete text of everything reported. An upgrade error later resolved by a repair appears as a warning ("recovered…").

## Exit code

`0` finished without errors · `1` errors occurred · `2` incorrect usage (the command accepts no arguments) · `130` interrupted by you.

## Example (sample dry-run report)

```
REPORT — update-mint 2.0.0   (2026-10-31 10:55:01)
⚠ DRY-RUN MODE: no changes were made to the system…

GENERAL SUMMARY
  Selected operations: Update, Repair, Deep cleanup
    ✔ Update: completed
    ⚠ Repair: completed with warnings
    ✔ Deep cleanup: completed
  Chosen order: Update → Repair → Cleanup  (reason: default order — update before repair)
  Total duration: 48s
  Reboot: NOT REQUIRED
…
STORAGE
  Initial free space:      10,192.0 MB (9.95 GB)
  Final free space:        10,192.0 MB (9.95 GB)
  Net recovered:                0.0 MB (0.00 GB)
```
