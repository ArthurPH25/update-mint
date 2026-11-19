# Tools and commands used — and why

Rule: each tool must provide a **concrete benefit**, be official or widely recognized, and be maintained. No installing dozens of packages. The program **does not install any tools** (including `smartmontools`): if something is missing, the corresponding step is skipped and the report explains how to enable it.

## Required (included with Mint)

| Tool | Use | Concrete benefit |
|---|---|---|
| `apt-get` | `update`, `full-upgrade`/`upgrade`, `install --only-upgrade --no-remove`, `--fix-broken install`, `autoremove --purge`, `purge`, `clean`, `-s` (simulate), `--assume-no` (estimate), `check` | official package manager. **`-s`** shows what it would do without changing anything; **`--assume-no`** reports download size and additional space **without root**; **`check`** detects broken dependencies |
| `dpkg` | `--audit`, `--configure -a`, `--verify`, `--print-architecture` | `--audit` finds partially installed packages; `--configure -a` finishes interrupted installations; **`--verify`** checks (using the package's own md5) whether binaries/libraries are corrupted, without installing `debsums` |
| `dpkg-query` | version inventory, state (`ii`/`rc`), `Essential`/`Priority`, `Conffiles`, source package | basis for the before/after report, essential-package protection, and preservation of edited configuration files |
| `apt-mark showhold` | held packages | respects packages held by you or the system |
| `apt-config dump` | detect apt proxy | avoids reporting "no internet" when behind a proxy |
| `systemctl` | `--failed`, `list-units`, `show -p …`, `is-active`, `restart` (1 case), `daemon-reload` (with evidence) | investigates services **before** taking action |
| `journalctl` | `-u <unit>`, `-k` (I/O errors), `--disk-usage`, `--rotate`, `--vacuum-time/--vacuum-size` | service failure causes; disk-failure signals; trims the journal through its **own mechanism** (no `rm` under `/var/log/journal`) |
| `findmnt`, `lsblk` | mounts and options (`ro`), physical disk list (JSON) | detects read-only remounts and identifies which disks to query through SMART |
| `tune2fs -l` | mounted ext4 state and error counter | **non-destructive** check (reads the superblock), avoiding the temptation to run `fsck` on a mounted disk |
| `df`, `du` | measure space before/after each step; size of large directories | actual numbers (what the disk recovered), not an estimate based on deleted-file sizes |
| `find` | locate root-owned items in `~`, delete partial apt downloads | uses `-xdev` and owner/age filters |
| `fuser` (or `lsof`) | identify who holds the apt/dpkg lock | waits and **identifies the process**, rather than failing or removing the lock |
| `systemd-inhibit` | prevent sleep/shutdown during maintenance | dpkg being interrupted by sleep is a problem |
| `systemd-tmpfiles --clean` | clean `/tmp` and `/var/tmp` according to system rules | respects systemd's own cleanup policy |
| `fstrim -av` | SSD TRIM at the end of cleanup | returns free blocks to the SSD |
| `ldconfig -N -X -v` | **check** (without changing anything) for inconsistent library links/files | runs `ldconfig` for real only if this check reports a problem |
| `logger` | marks start/end in the journal (`journalctl -t update-mint`) | maintenance timeline in the system log itself |
| `sudo` | `-n` per command, `-v` once | minimum, targeted privilege |

## Optional (used **if available**)

| Tool | Use | Concrete benefit |
|---|---|---|
| `smartctl` (smartmontools) | `-a --json=c -n standby` | actual disk health (reallocated/pending sectors, NVMe, temperature). `-n standby` does not wake a sleeping disk. If missing, reports that and continues |
| `fwupdmgr` (fwupd/LVFS) | `get-remotes`, `get-updates --json` (analysis); `refresh` (after the safeguards, before the firmware question); `update <ids>` | official firmware, classified as **critical vs. peripheral**, with confirmation. Respects a disabled LVFS remote (does not enable it) |
| `timeshift` | `--list`, `--create --tags O --scripted` | restore point before significant updates, only when there is enough space; never deletes snapshots |
| `flatpak` | `update`, `uninstall --unused`, `repair` (only after an error) | system and user Flatpak apps |
| `snap` | `refresh`, `list --all`, `remove --revision` | only if Snap is installed (Mint does not use it by default) |
| `dkms` | `status` | checks whether driver modules were built for the new kernel (video/Wi-Fi) before reboot |
| `ubuntu-drivers` | `devices` | reports recommended vs. installed driver; **never replaces** a working driver |
| `needrestart` | `-b` | services/sessions using old libraries (otherwise, own `/proc` scan through the helper) |
| `update-initramfs`, `update-grub`, `depmod`, `fc-cache`, `glib-compile-schemas`, `update-desktop-database`, `update-mime-database`, `gtk-update-icon-cache` | rebuild caches/menus | **only with evidence** that they are stale or missing |
| `conda`, `yarn` | `conda clean --all -y`, `yarn cache clean` | developer caches (run as **you**, with your PATH) |

## Mint-specific mechanisms considered

- **Update Manager (`mintupdate`)**: calculates the list using the same `dist-upgrade` logic (`cache.upgrade(True)`), groups by source package, and respects an ignore list. The program reads **both sources** for this list (`/etc/mintupdate.blacklist`, also used by `mintupdate-cli`, and `gsettings com.linuxmint.updates blacklisted-packages`, used by the "Ignore" button). `mintupdate-cli upgrade` installs with `apt-get install <list>`; `update-mint` does the equivalent when ignored packages exist.
- **Ubuntu phased updates**: apt 2.8 reports "The following upgrades have been deferred due to phasing" — the program only reports this and **does not** use `APT::Get::Always-Include-Phased-Updates`.

## Evaluated and **not** adopted

| Alternative | Reason |
|---|---|
| `debsums` | requires installing a package; `dpkg --verify` (already present) uses the same md5 checks |
| `aptitude` | redundant with apt-get; its additional dependency resolver is exactly what could cause unexpected removals |
| `ncdu` | interactive; `du -sb -x` on candidate directories is enough for the report |
| `bleachbit` and similar tools | use generic rules; here each file type uses its own mechanism |
| `python-apt` (`apt` module) | tightly couples the tool to the apt and Python versions; `apt-get` simulations are more stable and testable |
| `whiptail`/`dialog` | `whiptail` has no mouse; the requirement was keyboard **and** mouse. Python's `curses` is already available |
| `fsck` on a mounted disk | destructive; the program only **reads** and explains how to check from a Live USB/recovery environment |
| `smartctl -t` (long SMART tests) | unnecessary heavy load for automatic maintenance |
| `systemctl reset-failed`/mass restart | erases evidence and changes state without a known cause |
| apt `Status-Fd` for progress | **tested and rejected**: with `-o APT::Status-Fd=1`, dpkg scripts fail with `echo: I/O error`; progress comes from normal output (`Get:`, `Unpacking`, `Setting up`) |
