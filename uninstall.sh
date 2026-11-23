#!/bin/bash
set -u
MARKER="update-mint:tool-file"
PREFIX="${UPDATE_MINT_PREFIX:-/usr/local}"
BIN="$PREFIX/bin/update-mint"
LIB="$PREFIX/lib/update-mint"
DOC="$PREFIX/share/doc/update-mint"
MANIFEST="$LIB/MANIFEST"

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

if [ "$(id -u)" -ne 0 ] && [ -z "${UPDATE_MINT_PREFIX:-}" ]; then
  exec sudo -E bash "$(readlink -f "${BASH_SOURCE[0]}")" "$@"
fi
[ -f "$MANIFEST" ] || fail "$MANIFEST was not found. The tool does not appear to have been installed by this installer. Nothing was removed."

removed=0
while IFS= read -r file_path; do
  [ -n "$file_path" ] || continue
  if ! manifest_entry_ok "$file_path"; then
    warn "Ignored a suspicious or unexpected manifest entry (not removed): $file_path"
    continue
  fi
  [ -e "$file_path" ] || [ -L "$file_path" ] || continue
  if [ "$file_path" = "$BIN" ] && ! grep -q "$MARKER" "$BIN" 2>/dev/null; then
    warn "$BIN no longer contains the tool marker (it was replaced by another file): it was NOT removed."
    continue
  fi
  rm -f -- "$file_path" && removed=$((removed + 1))
done < "$MANIFEST"
find "$LIB" -depth -name __pycache__ -type d -exec rm -rf -- {} + 2>/dev/null
find "$LIB" "$DOC" -depth -type d -empty -delete 2>/dev/null
ok "$removed tool file(s) removed."
for folder in "$LIB" "$DOC"; do
  if [ -d "$folder" ]; then
    warn "$folder still contains files that do NOT belong to the tool (they were left untouched):"
    find "$folder" -type f | sed 's/^/    /'
  fi
done

echo
echo "Your logs and reports are stored in ~/.local/state/update-mint/ and were not touched."
echo "To remove them manually: rm -r ~/.local/state/update-mint"
