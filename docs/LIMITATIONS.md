# Known limitations

This page exists so you know **exactly** what has been validated and what has not.

## 1. Where it was tested

- **Ubuntu 24.04 (noble)** — the same base as Linux Mint 22.x — in a validation environment (container), with real apt, real dpkg, real `sudo` (ordinary user with `NOPASSWD`), and a dummy `/etc/linuxmint/info` file so the program would identify itself as Mint.
- **Linux Mint 22.3 "Zena" (Cinnamon)** on a real installation, with a physical disk: complete runs of the three operations (Update, Repair and Cleanup), checked through the generated logs.
- It was **not** tested on Mint 20/21 or LMDE. The code detects what is present (Mint version, Ubuntu/Debian base, tools) and does not assume components exist, but that is not a substitute for testing on your machine. Recommendation: use a recent Timeshift snapshot before the first run.
- Python: the code uses only features from 3.7+ (checked with `vermin`), but was **executed** only on 3.12 (the version included with Mint 22).

## 2. Actually validated vs. simulated only

| Area | Real (in the validation environment) | Simulated commands/files only |
|---|---|---|
| apt: `update`, `full-upgrade`, simulations, estimates, `clean`, `autoremove`, phased updates | ✔ (real upgrade of 13 packages, real phased update detected, third-party repository unavailable) | removal scenarios, new kernel, official repository unavailable |
| dpkg: `--audit`, `--configure`, `--verify` | ✔ | broken dpkg, large-scale file mismatches |
| targeted sudo, clean environment, installation under `/usr/local` with root ownership | ✔ | — |
| curses interface: menu, keyboard, mouse (SGR sequences), help, Ctrl+C, closed terminal, panel, report | ✔ in virtual terminal (pyte) and `tmux`, **not** in a graphical Mint terminal | — |
| `/tmp` and personal-cache cleanup, old libraries in use | ✔ | — |
| SMART (`smartctl`), NVMe | — (environment has no physical disk) | hand-built JSON in the format of `smartctl --json` |
| fwupd/LVFS, Timeshift, DKMS, `ubuntu-drivers`, Flatpak, Snap, needrestart | — | hand-built outputs (manual formats); each parser was checked against representative command output |
| systemd (journal, services), battery, GRUB/initramfs, `tune2fs`/btrfs | — (container has no complete systemd) | fake `systemctl`/`journalctl`/`tune2fs` and fake `/sys` and `/boot` files |
| Sleep inhibition (`systemd-inhibit`) | only the **handled failure** path (no logind) | success path not exercised |

In other words: decision logic in these areas was tested against the **expected output format**, not against real hardware/services. If a real tool's output differs from the documented format, its parser tends to **find nothing** (and the step reports "no data") rather than act. That is the safer failure mode, but an adjustment may be needed.

## 3. What the tool deliberately does not do (or cannot do)

- **Does not repair file systems**: never runs `fsck` on a mounted disk; only reads and provides instructions (Live USB / recovery mode).
- **Does not restore the dpkg database** (`/var/lib/dpkg/status`) if corrupted: it detects the problem (`dpkg --audit` fails) and reports it; manual restoration is too risky to automate.
- **Does not switch kernel series or drivers**: follows the metapackages you already have; only reports the recommended driver.
- **Does not enable the LVFS remote**, add PPAs, or change the Mint release.
- **Does not create Timeshift snapshots unless Timeshift is configured** and **does not delete** old snapshots. It also does not manage Docker, Podman, Wine, virtual environments, or ML environments (it only reports their size under "where large files remain").
- **The npm cache is not cleaned** (your v1.3 decision, retained).
- **Old Snap revisions** removed during cleanup do not have *rollback*.
- The interface and report are available in **English**.

## 4. Technical limits

- **Requires an interactive terminal** (it is a menu). Without one, it exits with a message. Under Wayland/X11, mouse support depends on the terminal reporting clicks; keyboard controls always work.
- **Mouse**: tested with simulated SGR sequences; unusual terminals may not report clicks (the keyboard still works).
- **Command output language**: the program forces `LC_ALL=C.UTF-8` for commands and parses apt 2.x messages (tested with apt 2.8). If a future apt version changes those messages, detections ("phased", "kept back") may stop working. The failure mode is safe (no action is taken).
- **Group check on symbolic-linked paths**: deciding whether a group is exclusive to its owner relies on the account database visible to the system (`/etc/passwd`, `/etc/group`). With network directories (LDAP/NIS) that do not list every user, a shared group may not be detected; accounts or members that cannot be looked up are treated as *not* exclusive (the path is refused).
- **Space measurement**: comes from `df`. On Btrfs/ZFS with snapshots or compression, actual free space may be less than the amount deleted (snapshots retain blocks). Writes by other programs during the run are included in the measurement.
- **Forced interruption** (second Ctrl+C, `kill`): dpkg receives `SIGTERM` (never `SIGKILL`) and may be left partially configured; the next run detects and repairs it.
- **Initial analysis takes about 10–15 s** (apt analyzes its cache several times; repeated results are reused). `dpkg --verify` and the search for "where large files remain" may take minutes on slow/full disks (there is a time limit per item).
- **Firmware**: depends on LVFS offering firmware for your device (many laptops, including popular brands in Brazil, do not have firmware through LVFS). In that case, the report says "no device with updatable firmware".
- The root helper is used only if `/usr/local/lib/update-mint` (its parent directories and every file and subdirectory below it, including `helpers/` and the Python files) belongs to root and is not writable by group or others. When run directly from your own project directory (development mode), it is rejected and dependent steps warn.
- A restricted `sudo` configuration (allowlist of permitted commands) may deny the `env` command used to pass variables to `apt-get`; in that case, package steps report a failure.
