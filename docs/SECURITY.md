# update-mint security

The tool uses administrative privileges, so the rules below are **implemented in code**, not just intentions.

## 1. Privilege model

- The program **runs as the user**. Only commands that require privileges run through `sudo -n`, one at a time; the password is requested **once**, **after** you make your selection in the menu (if you leave the menu, it is never requested). While the program runs, a thread refreshes the sudo timestamp so long updates do not ask for the password again.
- It is not run entirely as root: if you habitually call `sudo update-mint`, it drops back to your user (`sudo -u YOUR_USER`) and only then uses targeted sudo commands.
- Everything in your home directory (`~/.cache`, user Flatpak, conda, yarn) is handled **by you**, in your own process. It is never run as root, so ownership in `~` does not change. The file-ownership repair exists specifically to fix files that an older sudo command may have left owned by root.
- The **only** code that runs as root is `helpers/root_helper.py` (3 actions with their own safeguards: list old libraries still in use, clean `/tmp`, remove old `*-old` debconf copies). It runs only if the tool directory, **all parent directories**, and **everything below the tool directory** (`helpers/`, `update_mint/`, every Python file and `__pycache__`) belong to root and are not writable by group or others, and nothing below it is a symbolic link; otherwise, the program refuses to use it.
- Without privileges (you declined or do not have sudo), it continues **only** with work that does not require an administrator and marks the rest as "skipped".
- Every command is an **argument list** (never `shell=True`): file names cannot inject commands. The program's only two `sh -c` invocations (RAM cache and swap recycling) use **fixed** scripts; the swap name is passed as a positional argument.
- Child commands run with a **clean environment**: fixed `PATH` (`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`) and no `PYTHONPATH`, `LD_LIBRARY_PATH`, `CONDA_*`, or `VIRTUAL_ENV`. Users with active conda `(base)` will not accidentally run conda's `apt`, `dpkg`, `curl`, or `python3`; the launcher uses `/usr/bin/python3 -I`.

## 2. What the tool never does

| Never | How it is enforced |
|---|---|
| delete personal files | removal is limited to fixed locations (caches, temporary files, package leftovers); `fs.validate_root` rejects `/`, `/home`, the home directory itself, system roots, and symbolic links |
| follow symbolic links or cross file systems during cleanup | `clean_tree` works through directory file descriptors (`openat`/`unlinkat`) and opens every directory with `O_NOFOLLOW`, so a directory swapped for a link during cleanup cannot redirect the removal; it refuses a symbolic link in the middle of the cleanup path (a root-owned one, such as `/home` pointing to another disk, is accepted only if every directory of the resolved path cannot be modified by other users; a group-writable directory counts as safe only if its group is exclusive to the owner: the owner's primary group, with no other account having it as primary group or listed as a member) and checks that the opened directory is where it was validated; it compares `st_dev` and treats a link as a file (deletes the link, never its target) |
| delete the Trash, `~/.npm`, AI models (`huggingface torch whisper ollama lm-studio`), `dconf ibus keyring doc tracker*`, or browser profiles | protected list in `constants.py`; only regenerable content is eligible |
| delete crash dumps (`/var/crash`, `/sys/fs/pstore`, `core*`, `*.crash`), rotated logs (including `~/.xsession-errors.old`), Timeshift snapshots, conda, or Docker | no step touches them; `/tmp` cleanup preserves `core*`, `*.crash`, and `*.dmp` |
| add PPAs/repositories, switch Mint releases, or force unreleased updates | only uses `apt-get` with sources **already configured**; phased apt updates are never forced |
| remove the running kernel | `autoremove` is rejected if its list includes the running kernel (or a protected/essential package, or ≥ 40 packages); `full-upgrade` falls back to `upgrade` and `--fix-broken` is not run if they would remove it; `autoremove` also does not run if its simulation failed |
| remove essential packages in `full-upgrade` or `--fix-broken` | simulates first (also before the minimal repair that precedes an update); failed simulation, running kernel, essential/protected package, or ≥ 10 removals → `upgrade` only / do not run |
| purge configuration files you edited | packages in `rc` state (removed, configuration kept) with a configuration file that differs from the original (md5) are preserved |
| run `fsck` on a mounted file system | there is no `fsck` invocation; it only reads (`tune2fs -l`, `btrfs device stats`) and explains how to check outside the running system |
| reinstall GRUB, regenerate initramfs, or rebuild the menu "just in case" | only with evidence (missing file, kernel missing from menu…) |
| enable a disabled service, clear an old failure state, or disable security | services are investigated only; restart is attempted only for a failure **that occurred during this run** and has a transient cause |
| remove apt/dpkg *lock* files | waits for the process holding the lock (and identifies it); never removes a lock file |
| use `chmod -R 777` or generic `rm -rf` on a system directory | neither exists in the code. Recursive removals are restricted to: (a) Python via `fs.py` (validated root, no links, same file system); (b) `rm -rf --one-file-system` only for `/lib/modules/<version>` and `/usr/src/linux-headers-<version>`, with validated path format (`valid_orphan_path`) and no owning package; (c) `find /var/tmp -maxdepth 1 -name 'flatpak-cache-*'` (fixed arguments); (d) `find ... -delete` on fixed directories only: the contents of `/var/lib/apt/lists/partial`, files in `/var/lib/snapd/cache`, and `*.old-dkms` files directly in `/boot` |
| install firmware without confirmation | dialog with risk warnings, default **No**; never on battery; BIOS/UEFI only in the last option |
| reboot the computer | only reports: required / recommended / not required |
| silently ignore an error | every command that changes something and fails becomes a warning/error in the report, even if the step itself did not complain; removal errors in the personal cache and in `/tmp` (including those returned by the root helper) also become warnings |

## 3. System protection safeguards

Documented in [FUNCTIONALITY.md §3](FUNCTIONALITY.md). In summary: failing disk (SMART/FS) or `/` mounted read-only → **nothing is changed**; battery below 20% → Update/Repair do not run; official repository unresponsive, or `apt update` failing for a reason that cannot be tied to a repository → nothing is installed; less than 300 MB free → nothing is installed; apt locked → no attempt is made.

## 4. Interruptions

| Situation | Behavior |
|---|---|
| **Ctrl+C** once | safely finishes the current step (dpkg is not cut off mid-operation) and **does not start** the next one; displays the report; exit code 130 |
| Ctrl+C again | forces termination: `SIGTERM` to the step's process tree (never `SIGKILL` to a package manager) — dpkg may be left incomplete; the next run detects and repairs it |
| Close the terminal | treated as a stop request (finishes the current step); the panel does not loop forever |
| Power loss / `kill -9` | a marker (`~/.local/state/update-mint/in-progress`) reveals on the next run that execution ended unexpectedly, and the analysis checks dpkg (`dpkg --audit`, `/var/lib/dpkg/updates`) before anything else |
| Internet connection drops mid-run | apt downloads **before** installing; a download failure does not leave the system half-installed |
| Disk space runs out mid-run | upgrade does not start with less than 300 MB free; when space is low, cleanup runs before downloading |
| Two simultaneous runs | single-instance lock (kernel `flock` on an empty file, no PID file, released automatically even after a crash, not inherited by child processes: the descriptor is close-on-exec and every `subprocess` call passes `close_fds=True`; never follows a symbolic link); if no lock file can be created or locked at all, the program does not start |
| Computer going to sleep/shutting down | `systemd-inhibit --what=sleep:shutdown` during execution (if a logind session exists; otherwise it warns) |

## 5. Backups and reversibility

- **Timeshift**: if configured, before a **significant** update (kernel, libc/systemd/Xorg/NVIDIA/Mesa/Cinnamon/firmware/GRUB, removals, ≥ 150 packages), when there is no recent snapshot (under 24 h) and there is enough space on the snapshot device, the program creates one. Without enough space or without Timeshift configured, it **does not create one** and **recommends one** in the report. **It never deletes snapshots.**
- The installer and uninstaller treat `MANIFEST` as data: an entry is deleted only if it is exactly the command or a file inside the tool's own `lib`/`doc` directories, has no `.`/`..`/empty path components, and its parent directory (physically resolved) is still inside them; anything else is ignored with a warning.
- Each step is classified (in the plan and documentation) as **reversible** (reads, indexes, regenerable caches), **partially reversible** (upgrades: can be rolled back through a snapshot or manual downgrade; the previous kernel remains installed and bootable), or **difficult to undo** (package/orphan removal, cleanup, firmware).
- The report explains what **was not** done and why (held, phased, ignored items, or items rejected by a safeguard).

## 6. Attack-surface review

- External inputs are limited to system-command output and apt/Mint configuration files. They are read as **text** and interpreted with regular expressions; nothing is executed from them.
- Package and file names from those outputs are used only as **argument-list entries** (never in a shell), and recursive removals are limited to the cases described in the table above.
- The root helper accepts only 3 fixed subcommands (`libraries`, `tmp`, `debconf`); `tmp` accepts only `/tmp` and `/var/tmp`.
