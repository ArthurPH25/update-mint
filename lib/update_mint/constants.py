
'Fixed constants and policies for update-mint.\n\nNothing here is a user option: behavior is automatic. Numbers are kept\nin this file to avoid scattered "magic numbers" throughout the code.\nValues marked (v1.3) come from the prototype and were retained.\n'
from __future__ import annotations

import re

APP_NAME = 'update-mint'
APP_VERSION = "2.0.0"



MAX_ROUNDS = 3            
LOW_SPACE_MB = 3072     
CRITICAL_SPACE_MB = 300    
MIN_BOOT_MB = 150       
CACHE_DAYS = 7             
TEMP_DAYS = 7               
JOURNAL_DAYS = 30          
JOURNAL_MAX_MB = 1024          
KEEP_LOGS = 15           
UPGRADE_REMOVAL_LIMIT = 10   
AUTOREMOVE_LIMIT = 40         
CRITICAL_BATTERY = 20       
FIRMWARE_BATTERY_THRESHOLD = 50      
REINSTALL_LIMIT = 5      
APT_LOCK_WAIT_SECONDS = 180    
FWUPD_TIMEOUT_SECONDS = 120        




PROTECTED_PACKAGE_PATTERN = re.compile(
    r"^(mint-meta-.*|cinnamon(-.*)?|lightdm(-.*)?|xserver-xorg(-.*)?|xorg|"
    r"network-manager(-.*)?|nemo(-.*)?|muffin|mintupdate|mintsystem|mintsources|"
    r"mintdrivers|mintinstall|sudo|apt|dpkg|systemd|libc6|bash|coreutils|grub-.*|"
    r"shim-signed|initramfs-tools|ubuntu-desktop.*|"
    r"linux-(image|headers)-(generic|amd64).*|linux-generic.*)$"
)


CORE_PACKAGES = ("apt", "dpkg", "libc6", "bash", "coreutils", "util-linux", "systemd")


CORE_PACKAGE_PATTERN = re.compile(
    r"^(libc6|libc-bin|systemd|libsystemd0|udev|dbus|dbus-daemon|xserver-xorg-core|"
    r"nvidia-.*|linux-firmware|cinnamon|cinnamon-session|lightdm|libssl3t64|libssl3|"
    r"libssl1\.1)(:|$)"
)


KERNEL_ERROR_PATTERN = re.compile(
    r"I/O error|EXT4-fs error|Buffer I/O error|blk_update_request|"
    r"Remounting filesystem read-only|ata[0-9.]+: .*(failed|error)|"
    r"nvme[0-9]+.*(timeout|reset)|Kernel panic|Oops:|BUG:",
    re.IGNORECASE,
)
LIBRARY_SEGFAULT_PATTERN = re.compile(r"segfault at .* in (lib\S+\.so\S*)", re.IGNORECASE)



REGENERABLE_CACHES = ("thumbnails", "pip", "yt-dlp", "mesa_shader_cache",
                       "mesa_shader_cache_db", "nvidia", "node-gyp")

BROWSERS = (
    (("firefox", "firefox-bin"), "mozilla"),
    (("chrome",), "google-chrome"),
    (("chromium", "chromium-browser"), "chromium"),
    (("brave",), "BraveSoftware"),
    (("msedge",), "microsoft-edge"),
    (("vivaldi-bin",), "vivaldi"),
    (("opera",), "opera"),
)

PROTECTED_CACHES = ("dconf", "ibus", "keyring", "doc", "huggingface", "torch",
                     "whisper", "ollama", "lm-studio", "tracker", "tracker3")
XSESSION_ERROR_LIMIT = 50 * 1024 * 1024   


DUMP_FILE_PATTERNS = ("core", "core.*", "*.core", "*.crash", "*.dmp")

PRESERVED_TEMP_PREFIXES = ("systemd-private-", ".", "snap-private-tmp")



CRITICAL_FIRMWARE_PATTERN = re.compile(
    r"system firmware|bios|uefi|embedded controller|management engine|thunderbolt|"
    r"nvme|ssd|hard drive|dock|dbx|\bkek\b|\bpk\b|secure boot|\bme\b",
    re.IGNORECASE,
)
CRITICAL_FIRMWARE_PLUGINS = ("uefi_capsule", "uefi_dbx", "uefi_pk", "uefi_kek", "thunderbolt",
                       "nvme", "intel_me", "intel_spi", "dell_dock", "dell_k2_dock",
                       "lenovo_thinklmi", "flashrom", "synaptics_mst", "ata", "scsi")
