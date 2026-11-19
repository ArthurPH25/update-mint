# update-mint 2.0

A Linux Mint maintenance tool, available through a single command with no options:

```
update-mint
```

A terminal menu appears with three operations. Select whichever ones you want (keyboard or mouse), press Enter, and the program takes over:

1. FULLY UPDATE THE SYSTEM AND COMPUTER — apt, kernel, drivers, Flatpak, Snap, firmware.
2. FULLY REPAIR THE SYSTEM AND COMPUTER — dpkg/apt, system files, boot, services, disks, file systems.
3. PERFORM A DEEP CLEANUP OF THE SYSTEM AND COMPUTER — apt cache, orphaned packages, journal, temporary files, application caches.

It does not follow a fixed list of commands: it analyzes the computer (read-only), chooses the order (broken dpkg → repair first; low disk space → clean before downloading), runs the operations, reassesses the system, and repeats only what is still necessary. At the end, it displays a report with the space before and after, and whether a reboot is needed.

Help is available in the menu: press ? or F1 (or click the HELP button).

## Installation

Get the project (clone the repository or extract the downloaded archive), enter the `update-mint` directory, and run the installer. It asks for the administrator password:

```
cd update-mint
sudo ./install.sh
```

It installs only under `/usr/local` and does not overwrite anything that does not belong to the tool without showing what it is and asking for confirmation. If you confirm, a backup copy is kept alongside the original:

| Path | Description |
|---|---|
| `/usr/local/bin/update-mint` | the command |
| `/usr/local/lib/update-mint/` | the program, privileged helper, uninstaller, and `MANIFEST`/`VERSION` (the installed-file list and version) |
| `/usr/local/share/doc/update-mint/` | this documentation |

The command remains available after reboot (these are ordinary files under `/usr/local/bin`, which is already in Mint's PATH). Open a new terminal (or run `hash -r`) after installation.

> **Note:** if you already have an `update-mint` command in `~/bin`, `~/.local/bin`, or an alias in `~/.bashrc`, it takes precedence over the installed command. The installer warns you and does not modify those files.

Requirements: the system Python 3 (`/usr/bin/python3`, included with Mint; minimum 3.7) with `curses` (standard library). No other dependencies are required. Optional tools (`smartmontools`, `fwupd`, `timeshift`, `flatpak`, `dkms`, `needrestart`) are used if available. If one is missing, the corresponding step is skipped and the report explains why.

## Uninstallation

```
sudo /usr/local/lib/update-mint/uninstall.sh
```

Removes only the files listed in `MANIFEST` that lie inside the tool's own directories (entries with `..` or leading through a symbolic link are ignored with a warning), and directories left empty. It does not touch your logs, files you may have placed inside the tool's directories, or an `update-mint` command that does not carry the tool's marker.

## Preview without changing anything

`UPDATE_MINT_DRY_RUN=1 update-mint` does the real analysis and planning but runs no command that changes the system. It is not an option: the command still takes no arguments.

## Installing somewhere else

To install outside `/usr/local` (for example, to try it as a normal user), set `UPDATE_MINT_PREFIX`: `UPDATE_MINT_PREFIX=$HOME/test ./install.sh`. The installer does not use sudo in that case, and the uninstaller needs the same variable. `UPDATE_MINT_TARGET_HOME` changes whose home directory the installer checks for a conflicting `update-mint` command.

## Log location

`~/.local/state/update-mint/` — a complete log (each command, its output, and exit code) and one report per run; the 15 most recent of each are kept. The start and end of each run are also written to the system journal: `journalctl -t update-mint`.

## Security in one sentence

The program runs as you, not as root; only commands that need privileges are run through `sudo`. It never deletes personal files, the Trash, crash dumps, rotated logs, Timeshift snapshots, or AI models; never adds repositories, never forces phased updates, never removes the running kernel, never installs firmware without your explicit confirmation, and never reboots on its own. Details are in [docs/SECURITY.md](docs/SECURITY.md).

## Documentation

| File | Contents |
|---|---|
| [docs/FUNCTIONALITY.md](docs/FUNCTIONALITY.md) | architecture, decision flow, each step of the three operations, and the commands used |
| [docs/SECURITY.md](docs/SECURITY.md) | security rules, safeguards, privileges, interruptions, reversibility, backups |
| [docs/TOOLS.md](docs/TOOLS.md) | each tool used (and those rejected), with the reasons |
| [docs/REPORT.md](docs/REPORT.md) | how to read the final report |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | what the tool does not do and what has not been tested on real systems |
