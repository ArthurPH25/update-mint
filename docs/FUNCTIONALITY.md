# How update-mint works

## 1. Overview

```
update-mint
   │
   ├─ menu (curses: keyboard + mouse)  ──►  operation selection (U, R, C)
   ├─ sudo -v (once, only now)          ──►  SudoKeepAlive refreshes the timestamp during execution
   └─ engine (thread) + panel (main thread)
        ├─ 1. INITIAL ANALYSIS (read-only)       → State
        ├─ 2. SECURITY SAFEGUARDS                 → aborts/skips anything unsafe
        ├─ 3. DECISIONS (firmware only)            → asks; default = no
        ├─ 4. ADAPTIVE ORDER                      → R→U→C according to system state
        ├─ 5. ROUNDS (up to 3): steps → reassess → repeat only what is justified
        ├─ 6. WRAP-UP (if Cleanup): TRIM, RAM cache/swap
        └─ 7. FINAL CHECK → report (screen, terminal, and file)
```

Code (`lib/update_mint/`), one module per responsibility:

| Module | Responsibility |
|---|---|
| `main.py` | entry point: no arguments, drops back to the calling user when invoked with sudo, menu, sudo, engine thread, report, exit codes |
| `environment.py` | detects Mint/Ubuntu-Debian base/user/tools; clean environment for child processes (fixed PATH, no conda/Python variables) |
| `executor.py` | **every** external command goes through here: targeted `sudo -n`, logging, streaming, safe stopping, dry-run mode |
| `context.py` | shared state, `Step`, results, warnings/errors/issues |
| `diagnostics.py` | read-only probes: SMART, file systems, I/O errors, services, fwupd, Timeshift |
| `packages.py` | apt/dpkg interpretation: simulations, estimates, repositories, inventories, Mint ignored updates |
| `planner.py` | engine: analysis, safeguards, decisions, ordering, rounds, per-step space measurement |
| `steps/update.py`, `steps/repair.py`, `steps/clean.py` | the steps for each operation |
| `reboot.py` | required / recommended / not required reboot assessment |
| `storage.py`, `fs.py` | space measurement (`df`) and safe file removal |
| `security.py` | protected packages, battery, read-only state, single instance, allowed paths |
| `logger.py` | logs, rotation, interrupted-run marker |
| `report.py` | builds the report (the same structure is used on screen and in the file) |
| `ui/` | `tui.py` (curses), `base.py` (shared state and simple UI), `help.py` |
| `helpers/root_helper.py` | the only code that runs as root: old libraries, `/tmp` cleanup, debconf copies |

### Why Python + curses instead of Bash + whiptail

The prototype was a 1,462-line Bash script. The new version requires keyboard **and mouse** selection (`whiptail` has no mouse), command-output handling (SMART/fwupd JSON, apt simulations), a stateful planner, file removal with testable safeguards, and independently maintainable modules. Python 3 (always present on Mint) with `curses` (standard library) provides this without extra dependencies, and each module has a clearly defined responsibility. The trade-off is straightforward: it is more code than a single script and depends on `/usr/bin/python3` (which Mint always includes).

## 2. Initial analysis (read-only)

The analysis changes nothing. The only related operation, refreshing the local firmware metadata (`fwupdmgr refresh`), is done later, after the safety safeguards and just before the firmware question, so it never happens when a safeguard stops the run.

Disk space (`df`), battery (`/sys/class/power_supply`, ignoring batteries of wireless peripherals), dpkg/apt state, held packages (`apt-mark showhold`), updates ignored in Mint Update Manager (`/etc/mintupdate.blacklist` + `gsettings com.linuxmint.updates blacklisted-packages`), `full-upgrade` simulation and download/space estimate (`apt-get --assume-no`, without root), failed services (and which ones were active), SMART status for each disk, mounted file systems, kernel I/O errors from the last 7 days, Flatpak/Snap, connectivity, Timeshift, firmware (fwupd), and package inventories **before** changes (so the report can compare them).

## 3. Safeguards (conditions that make continuing unsafe)

| Condition | What happens |
|---|---|
| SMART **FAILING**, critical NVMe warning, attribute below threshold, severe ext4 errors, `/` mounted **read-only** | **nothing** is changed; only the read-only diagnosis is added to the report, with a BACKUP warning |
| Battery below 20% while unplugged | Update and Repair do not run (Cleanup may run) |
| Mild SMART warning / recent I/O errors | continues, but avoids optional heavy disk operations (full file verification, TRIM) and warns |
| APT/dpkg lock held by another program | waits up to 180 s; then skips apt-dependent work, **without removing locks** |
| **Official** repository is unresponsive, or `apt update` fails for a reason that cannot be tied to a specific repository | installs nothing (package index is incomplete) |
| Less than 300 MB free on `/` | the upgrade does not start |

## 4. Adaptive order

- Broken `dpkg`/`apt` and **Repair** selected → Repair runs **before** Update.
- Low disk space (under 3 GB) with Update + Cleanup selected → Cleanup runs **before** downloading. Update only → one minimal step (`apt-get clean`).
- Update only with broken dpkg → *minimal repair* (`dpkg --configure -a` + `apt --fix-broken install`) first.
- Otherwise: Update → Repair → Cleanup.

After each round, the engine **reassesses using fresh measurements** (is dpkg still broken? is there a new update that was not installed? is there leftover junk?) and repeats only phases that are still justified, for up to 3 rounds. Even when a new kernel is installed, old kernels are removed only through apt's `autoremove`, which protects the running kernel, the newest one, and the one before it (`apt-auto-removal`). The program also independently checks that the running kernel is not in the removal list and refuses `autoremove` if it is.

## 5. Steps

`root` = the command runs through `sudo -n`. Reversibility: **R** reversible · **P** partially reversible · **D** difficult to undo.

### UPDATE

| Step | Rev. | What it does / when |
|---|---|---|
| Checking connectivity | R | TCP 443 to `packages.linuxmint.com`, `archive.ubuntu.com`, `1.1.1.1` (or the apt/environment proxy) |
| Minimal repair before updating | P | only if dpkg is broken and Repair was **not** selected: `dpkg --configure -a`, `apt-get --fix-broken install` |
| Free minimal space | R | only if space is low and Cleanup was **not** selected: `apt-get clean` |
| Package lists | R | waits for the lock; `apt-get update`. On *Hash Sum mismatch*: clears only `/var/lib/apt/lists/partial` and retries. Third-party repository failure: updates the rest and warns; official repository failure: installs nothing |
| Safety snapshot | P | only if the update is significant (kernel, libc/systemd/Xorg/NVIDIA/Mesa/Cinnamon/firmware/GRUB, removals, ≥150 packages) **and** Timeshift is configured **and** there is enough space **and** no snapshot is newer than 24 h: `timeshift --create --tags O`. Never deletes snapshots |
| Updates | P | `apt-get full-upgrade` — see rules below |
| New kernel and DKMS drivers | R | only with a new kernel: checks initramfs/modules and `dkms status` for the new kernel |
| Flatpak apps | P | `flatpak update --system` (root) and `--user` |
| Snaps | P | `snap refresh` (only if Snap is installed) |
| Drivers (check) | R | `ubuntu-drivers devices`: **information only**; never replaces a working driver |
| Firmware (fwupd) | **D** | only with your choice in the dialog (default **no**) and while plugged in: `fwupdmgr update <ids>` |

`apt full-upgrade` rules: simulate first (`apt-get -s`); **never force** phased updates (apt decides when this computer is eligible); held packages remain held; updates **ignored in Mint Update Manager** are respected (explicitly install only the allowed list: `install --only-upgrade --no-remove`); if `full-upgrade` would remove ≥ 10 packages, the running kernel, or any essential/protected package, run `upgrade` only (remove nothing) and explain why; preserve your existing configuration (`--force-confold`).

### REPAIR (evidence-based only)

| Step | Rev. | Evidence → action |
|---|---|---|
| dpkg/apt integrity | R | `dpkg --audit`, `apt-get check`, `/var/lib/dpkg/updates` |
| Pending configuration | P | `dpkg --audit` has output (or pending updates exist) → `dpkg --configure -a` |
| Dependencies | P | `apt-get check` fails → simulate `--fix-broken install`; **do not** run it if the simulation fails, if it would remove an essential package, or if it would remove 10 or more packages |
| Repository indexes | R | abandoned partial downloads (removed), empty indexes (warns), last update older than 14 days (warns) |
| Essential files | P | `dpkg --verify` on core packages (apt, dpkg, libc6, bash, coreutils, util-linux, systemd); binaries/libraries only, never configuration files. **Expands to all packages only with evidence of software corruption** (*segfault* inside a system library within the last 7 days) and no disk warning — heavy reads are avoided when the disk has a warning. If files differ → `apt-get install --reinstall` (up to 5 packages) |
| Kernels and initramfs | P | installed kernel **without** `initrd.img` → `update-initramfs -c -k <v>` (checks `/boot` space) + `update-grub` |
| Boot | P | missing/empty `grub.cfg`, installed kernel missing from menu, or menu entry pointing to a nonexistent file → `update-grub` (**never** preventive); warns if `/boot/efi` is unmounted; reports slow boot and failure evidence (pstore, `/var/crash`) |
| System caches | R | each one only if stale: `ldconfig` (if `ldconfig -N -X -v` reports issues), `depmod` (missing `modules.dep`), `fc-cache` (missing fonts), GLib schemas, `.desktop`/MIME database, icon caches, `daemon-reload` (if `NeedDaemonReload=yes`) |
| File ownership in ~ | P | **root-owned** items in `~/.cache ~/.config ~/.local` (and `.ICEauthority/.Xauthority`) → `chown` back to the user, list in log; over 200 items: does nothing |
| Failed services | P | **investigates the cause** (`systemctl show`, `journalctl -u`). Failure from **before** this run: reports only (does not clear the state). Failure **during** this run, for a service that was active, enabled, not protected, and has no configuration error: **one** attempt at `systemctl restart` |
| Disk health | R | `smartctl -a --json` for each disk (requires `smartmontools`); reports warnings and never invents a diagnosis when the device provides no data |
| File systems | R | `findmnt`, `tune2fs -l` (mounted ext4), `btrfs device stats`, inodes, kernel I/O errors. **Never runs fsck on a mounted disk**; explains how to check from a Live USB |
| Flatpak (only after an error) | P | `flatpak repair --system/--user` |

### DEEP CLEANUP

| Step | Rev. | What it does |
|---|---|---|
| Orphans, configs, and apt cache | D | simulates `autoremove` (refuses if it includes a protected/essential package, the running kernel, or ≥ 40 packages); `autoremove --purge`; purges residual configs (`rc`) **except** packages whose configuration files you edited; `apt-get clean` |
| Old kernel leftovers | D | `/lib/modules/<v>` and `/usr/src/linux-headers-<v>` without an owning package, without `vmlinuz`/`initrd` in `/boot`, and not the running version; `*.old-dkms` in `/boot` |
| Unused Flatpak/Snap | D | `flatpak uninstall --unused`; old Snap revisions (logged; no *rollback*) |
| Journal | D | `journalctl --rotate` + `--vacuum-time=30d --vacuum-size=1024M` |
| Temporary files | D | `systemd-tmpfiles --clean` in `/tmp` and `/var/tmp`; root helper removes `/tmp` files unused for 7 days (atime **and** mtime), **skipping** open files, dumps (`core*`, `*.crash`), sockets, session directories, and dotfiles; old debconf `*-old` copies |
| Personal cache | D | as **you** (in Python): fully regenerable caches (thumbnails, pip, shaders…), browsers **only when closed**, other `~/.cache` content only when unused for 7 days, **except** `dconf ibus keyring doc huggingface torch whisper ollama lm-studio tracker*`; old Flatpak caches; large `.xsession-errors` |
| Developer caches | D | `conda clean --all -y`, `yarn cache clean`. **The npm cache is kept** (npm repairs it itself; deleting it only makes npm slower) |
| Where large files remain | R | report only: `du` in large locations the program **does not** delete (Trash, `~/.npm`, `/var/crash`, models…) |
| Wrap-up: TRIM | R | `fstrim -av` (skipped if the disk has a warning) |
| Wrap-up: RAM cache/swap | R | `sync` + `drop_caches`; recycles swap only when there is plenty of RAM. Does not free disk space |

## 6. Space measurement

Before and after **each step**, the program measures (`df`) the sum of **distinct** file systems for `/`, `/boot`, `/var`, and the home directory. The report separates what **Cleanup freed**, what **updates consumed**, and what **repairs** changed, and shows the net result. This is the space *actually* recovered (an open file does not count), not the sum of the sizes of deleted files.

## 7. Dry-run mode

`UPDATE_MINT_DRY_RUN=1 update-mint` runs the real analysis and planning, but **no command that changes the system is executed** (including personal-cache cleanup). Useful for previewing what it would do. This is not a usage option: the command still accepts no flags.
