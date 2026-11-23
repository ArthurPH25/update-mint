#!/bin/bash
set -u
MARKER="update-mint:tool-file"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${UPDATE_MINT_PREFIX:-/usr/local}"
BIN="$PREFIX/bin/update-mint"
LIB="$PREFIX/lib/update-mint"
DOC="$PREFIX/share/doc/update-mint"
MANIFEST="$LIB/MANIFEST"
APP_VERSION="$(sed -n 's/^APP_VERSION = \"\(.*\)\"/\1/p' "$SOURCE_DIR/lib/update_mint/constants.py" 2>/dev/null | head -1)"
PY=/usr/bin/python3

color_ok=$'\e[32m'; color_warning=$'\e[33m'; color_error=$'\e[31m'; color_reset=$'\e[0m'
[ -t 1 ] || { color_ok=""; color_warning=""; color_error=""; color_reset=""; }
ok()  { printf '%s✔%s %s\n' "$color_ok" "$color_reset" "$*"; }
warn()  { printf '%s⚠%s %s\n' "$color_warning" "$color_reset" "$*"; }
fail() { printf '%s✖%s %s\n' "$color_error" "$color_reset" "$*" >&2; exit 1; }

manifest_entry_ok() {
  local p="$1" allowed real_parent real_allowed
  case "$p" in
    "$BIN") allowed="$PREFIX/bin" ;;
    "$LIB"/?*) allowed="$LIB" ;;
    "$DOC"/?*) allowed="$DOC" ;;
    *) return 1 ;;
  esac
  case "$p" in *[[:cntrl:]]*) return 1 ;; esac
  case "$p/" in */../*|*/./*|*//*) return 1 ;; esac
  [ -e "$p" ] || [ -L "$p" ] || return 0
  real_parent="$(realpath -e -- "${p%/*}" 2>/dev/null)" || return 1
  real_allowed="$(realpath -e -- "$allowed" 2>/dev/null)" || return 1
  case "$real_parent/" in "$real_allowed"/*) return 0 ;; esac
  return 1
}

ask() {
  local answer=""
  [ -r /dev/tty ] || return 1
  read -r -p "$1 [y/N] " answer < /dev/tty || return 1
  case "$answer" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

if [ "$(id -u)" -ne 0 ] && [ -z "${UPDATE_MINT_PREFIX:-}" ]; then
  echo "Installation in $PREFIX requires administrator privileges; invoking sudo..."
  exec sudo -E bash "$(readlink -f "${BASH_SOURCE[0]}")" "$@"
fi
[ -d "$SOURCE_DIR/lib/update_mint" ] || fail "lib/update_mint was not found next to this installer ($SOURCE_DIR)."

[ -x "$PY" ] || fail "$PY is required (the system Python 3; Linux Mint includes it)."
"$PY" -I -c 'import sys, curses, json, fcntl; sys.exit(0 if sys.version_info >= (3, 7) else 1)' 2>/dev/null \
  || fail "The system Python must be version 3.7 or newer and provide curses/json/fcntl (standard on Mint)."
if [ ! -r /etc/linuxmint/info ] && ! grep -qi '^ID=linuxmint' /etc/os-release 2>/dev/null; then
  warn "This system does not appear to be Linux Mint (the tool was designed for it)."
  ask "Install anyway?" || fail "Installation cancelled; nothing was changed."
fi

REPLACE_BINARY=0
if [ -e "$BIN" ] || [ -L "$BIN" ]; then
  if [ -f "$BIN" ] && grep -q "$MARKER" "$BIN" 2>/dev/null; then
    ok "An existing update-mint installation was found at $BIN; it will be updated."
  else
    warn "$BIN already exists and does NOT belong to this tool:"
    stat -c '%A %U:%G %s bytes  %n' "$BIN" | sed 's/^/    /'
    head -5 "$BIN" 2>/dev/null | sed 's/^/    | /'
    ask "Replace it? A backup will be kept at $BIN.backup-DATE; nothing is deleted." || fail "Your file was kept; nothing was changed."
    REPLACE_BINARY=1
  fi
fi
if [ -e "$LIB" ] && [ ! -f "$MANIFEST" ]; then
  fail "$LIB already exists and does not contain this tool's MANIFEST. I will not modify it. Rename or remove it and run the installer again."
fi

if [ -f "$MANIFEST" ]; then
  while IFS= read -r file_path; do
    [ -n "$file_path" ] || continue
    if manifest_entry_ok "$file_path"; then
      rm -f -- "$file_path"
    else
      warn "Ignored a suspicious or unexpected manifest entry (not removed): $file_path"
    fi
  done < "$MANIFEST"
  find "$LIB" -depth -name __pycache__ -type d -exec rm -rf -- {} + 2>/dev/null
  find "$LIB" -depth -type d -empty -delete 2>/dev/null
fi
if [ "$REPLACE_BINARY" = 1 ]; then
  mv -- "$BIN" "$BIN.backup-$(date +%Y%m%d-%H%M%S)" || fail "Could not keep a backup of the previous file."
fi

owner=()
[ "$(id -u)" -eq 0 ] && owner=(-o root -g root)
MANIFEST_FILES="$(mktemp)"
trap 'rm -f "$MANIFEST_FILES"' EXIT
install_file() {
  install -D "${owner[@]}" -m "$1" -- "$2" "$3" || fail "Failed to install $3"
  echo "$3" >> "$MANIFEST_FILES"
}

install_file 755 "$SOURCE_DIR/bin/update-mint" "$BIN"
while IFS= read -r -d '' f; do
  rel="${f#"$SOURCE_DIR"/lib/}"
  mode=644; case "$rel" in helpers/*.py) mode=755 ;; esac
  install_file "$mode" "$f" "$LIB/$rel"
done < <(find "$SOURCE_DIR/lib/update_mint" "$SOURCE_DIR/lib/helpers" -type f -name '*.py' -print0)
install_file 755 "$SOURCE_DIR/uninstall.sh" "$LIB/uninstall.sh"
printf '%s\n' "${APP_VERSION:-unknown}" > "$LIB/VERSION"; echo "$LIB/VERSION" >> "$MANIFEST_FILES"
for d in "$SOURCE_DIR"/README.md "$SOURCE_DIR"/docs/*.md; do
  [ -f "$d" ] && install_file 644 "$d" "$DOC/$(basename "$d")"
done
echo "$MANIFEST" >> "$MANIFEST_FILES"
install -D "${owner[@]}" -m 644 -- "$MANIFEST_FILES" "$MANIFEST"
"$PY" -I -m compileall -q "$LIB" >/dev/null 2>&1 || warn "Python pre-compilation failed (the tool will still work, but may start a little more slowly)."
ok "Installed: $BIN  (version ${APP_VERSION:-?})"
ok "Program: $LIB · documentation: $DOC"

output="$("$BIN" --verify-installation 2>&1 </dev/null)"; check_status=$?
if [ "$check_status" -eq 2 ] && printf '%s' "$output" | grep -q "does not accept options"; then
  ok "The command starts correctly."
else
  warn "The command was installed, but the initial check returned an unexpected result: $output"
fi

USERNAME="${SUDO_USER:-${USER:-root}}"
TARGET_HOME="${UPDATE_MINT_TARGET_HOME:-$(getent passwd "$USERNAME" 2>/dev/null | cut -d: -f6)}"
if [ -n "$TARGET_HOME" ] && [ -d "$TARGET_HOME" ]; then
  for part in "$TARGET_HOME/bin/update-mint" "$TARGET_HOME/.local/bin/update-mint"; do
    if [ -e "$part" ]; then
      warn "Another 'update-mint' exists at $part. It usually comes before /usr/local/bin in the user's PATH and will take precedence."
      warn "To use the newly installed version: rename it (mv '$part' '$part.backup') and open a new terminal. It was not modified."
    fi
  done
  for rc_file in .bashrc .bash_aliases .profile .zshrc; do
    if [ -f "$TARGET_HOME/$rc_file" ] && grep -Eq '^[[:space:]]*(alias|function)[[:space:]]+update-mint' "$TARGET_HOME/$rc_file" 2>/dev/null; then
      warn "An 'update-mint' alias/function exists in $TARGET_HOME/$rc_file and takes precedence over the installed command. Remove it there if needed."
    fi
  done
fi

echo
echo "Done. Open a NEW terminal (or run: hash -r) and type: update-mint"
echo "Help: press ? or F1 in the menu · documentation: $DOC · uninstall: sudo $LIB/uninstall.sh"
