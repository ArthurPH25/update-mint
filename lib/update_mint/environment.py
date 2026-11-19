from __future__ import annotations

import os
import pwd
import grp
import shutil
import platform
from dataclasses import dataclass, field
from typing import Dict, Optional

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"



_PRESERVE_ENV = ("HOME", "USER", "LOGNAME", "TERM", "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY",
           "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "XDG_CURRENT_DESKTOP",
           "XDG_SESSION_TYPE", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS", "NO_COLOR",
           "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY")

TOOLS = (
    "sudo", "apt-get", "apt-cache", "apt-mark", "apt-config", "dpkg", "dpkg-query", "flatpak", "snap",
    "fwupdmgr", "smartctl", "timeshift", "dkms", "needrestart", "ubuntu-drivers", "fuser",
    "lsof", "fstrim", "journalctl", "systemctl", "systemd-analyze", "systemd-inhibit",
    "systemd-tmpfiles", "findmnt", "lsblk", "tune2fs", "btrfs", "update-initramfs", "update-grub",
    "ldconfig", "depmod", "fc-cache", "fc-list", "glib-compile-schemas", "update-desktop-database",
    "update-mime-database", "gtk-update-icon-cache", "logger", "gsettings", "sync", "swapon",
    "swapoff", "mintupdate-cli", "pgrep", "ionice", "nice", "tail", "timeout", "logrotate",
)


def read_key_values(path: str) -> Dict[str, str]:
    'Reads a ``KEY=value`` file (os-release, linuxmint/info, etc.). Never raises an exception.'
    data: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return data


def helper_path() -> str:
    'Path to the helper that runs as root (located next to the package, in ``helpers/``).'
    lib = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    return os.path.join(lib, "helpers", "root_helper.py")


def find_executable(name: str, path: Optional[str] = None) -> Optional[str]:
    return shutil.which(name, path=path if path is not None else SAFE_PATH)


@dataclass
class Environment:
    user: str = ""
    uid: int = 0
    home: str = ""
    group: str = ""
    is_root: bool = False
    sudo_user: str = ""
    mint: bool = False
    mint_release: str = ""
    mint_codename: str = ""
    mint_edition: str = ""
    os_name: str = "Linux"
    base: str = "?"             
    base_codename: str = ""
    kernel: str = ""
    utf8: bool = True
    user_path: str = ""      
    conda: str = ""
    tools: Dict[str, Optional[str]] = field(default_factory=dict)

    def has_tool(self, name: str) -> bool:
        return bool(self.tools.get(name))

    @property
    def description(self) -> str:
        if self.mint:
            ed = f" ({self.mint_edition})" if self.mint_edition else ""
            return f"Linux Mint {self.mint_release} {self.mint_codename.capitalize()}{ed}".replace("  ", " ")
        return self.os_name


def _detect_utf8() -> bool:
    for k in ("LC_ALL", "LC_CTYPE", "LANG"):
        v = os.environ.get(k)
        if v:
            return "utf-8" in v.lower() or "utf8" in v.lower()
    return False


def detect() -> Environment:
    'Collects the information needed about the machine before making any decisions.'
    env_data = Environment()
    env_data.uid = os.geteuid()
    env_data.is_root = env_data.uid == 0
    env_data.sudo_user = os.environ.get("SUDO_USER", "")
    try:
        pw = pwd.getpwuid(env_data.uid)
        env_data.user, env_data.home = pw.pw_name, pw.pw_dir
        env_data.group = grp.getgrgid(pw.pw_gid).gr_name
    except KeyError:
        env_data.user = os.environ.get("USER", "?")
        env_data.home = os.environ.get("HOME", "/")
        env_data.group = env_data.user
    home_env = os.environ.get("HOME", "")
    if home_env and os.path.isabs(home_env) and os.path.isdir(home_env) and not env_data.is_root:
        env_data.home = home_env          
    env_data.user_path = os.environ.get("PATH", SAFE_PATH)
    env_data.utf8 = _detect_utf8()
    env_data.kernel = platform.release()   

    osr = read_key_values("/etc/os-release")
    info = read_key_values("/etc/linuxmint/info")
    env_data.os_name = osr.get("PRETTY_NAME", "Linux")
    env_data.mint = bool(info) or osr.get("ID") == "linuxmint"
    if env_data.mint:
        env_data.mint_release = info.get("RELEASE") or osr.get("VERSION_ID", "")
        env_data.mint_codename = info.get("CODENAME") or osr.get("VERSION_CODENAME", "")
        env_data.mint_edition = info.get("EDITION", "")
    if osr.get("UBUNTU_CODENAME"):
        env_data.base, env_data.base_codename = "ubuntu", osr["UBUNTU_CODENAME"]
    elif osr.get("DEBIAN_CODENAME") or os.path.exists("/etc/debian_version"):
        env_data.base, env_data.base_codename = "debian", osr.get("DEBIAN_CODENAME", "")

    env_data.tools = {n: find_executable(n) for n in TOOLS}
    env_data.conda = _find_conda(env_data)
    return env_data


def _find_conda(env_data: Environment) -> str:
    c = shutil.which("conda", path=env_data.user_path)
    if c:
        return c
    for d in ("miniconda3", "anaconda3", "miniforge3", "mambaforge"):
        cand = os.path.join(env_data.home, d, "bin", "conda")
        if os.access(cand, os.X_OK):
            return cand
    return "/opt/conda/bin/conda" if os.access("/opt/conda/bin/conda", os.X_OK) else ""


def child_environment(extra: Optional[Dict[str, str]] = None, user_path: Optional[str] = None) -> Dict[str, str]:
    "Build a clean environment for child processes, excluding unsafe Python and linker overrides."
    env = {k: os.environ[k] for k in _PRESERVE_ENV if k in os.environ}
    env["PATH"] = user_path if user_path else SAFE_PATH
    env["LC_ALL"] = "C.UTF-8"
    env["LANG"] = "C.UTF-8"
    if extra:
        env.update(extra)
    return env



ENV_APT = {
    "DEBIAN_FRONTEND": "noninteractive",
    "DEBCONF_NONINTERACTIVE_SEEN": "true",
    "UCF_FORCE_CONFFOLD": "1",
    "NEEDRESTART_MODE": "l",
    "NEEDRESTART_SUSPEND": "1",
    "APT_LISTCHANGES_FRONTEND": "none",
}


APT_OPTIONS = ["-y", "-q", "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold",
              "-o", "Acquire::Retries=3", "-o", "DPkg::Lock::Timeout=300", "-o", "Dpkg::Use-Pty=0"]



