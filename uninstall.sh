#!/bin/bash
# Chordata uninstaller: undoes what install.sh did.
#
#   ~/Chordata/uninstall.sh
#   sudo systemctl restart moonraker klipper
#   rm -rf ~/Chordata          # optional: the downloaded folder itself
#
# It removes the plugin links from Kalico/Klipper, the two includes from printer.cfg,
# the chordata.cfg link, the Chordata page ([chordata_web]) and the update entry from
# moonraker.conf, and the link in Mainsail's sidebar. Each changed file is backed up
# first (<name>.before-chordata-uninstall). Only links that point into this folder
# are removed; anything else is left alone.
# install.sh's own backups (*.before-chordata, *.before-chordata-branch) stay too.
# Kept, because they are yours: chordata_settings.cfg and the recordings in the
# chordata/ folder of your config. Delete them by hand if you don't want them.
# Same options as install.sh (KLIPPER_DIR, MOONRAKER_DIR, CONFIG_DIR).
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
KLIPPER_DIR=${KLIPPER_DIR:-$( [ -d ~/kalico/klippy ] && echo ~/kalico || echo ~/klipper )}
MOONRAKER_DIR=${MOONRAKER_DIR:-~/moonraker}
CONFIG_DIR=${CONFIG_DIR:-~/printer_data/config}

say() { echo "Chordata: $*"; }
die() { echo "Chordata: $*" >&2; exit 1; }

[ "$(id -u)" != 0 ] || [ -n "${CHORDATA_ALLOW_ROOT:-}" ] || die "run it as your normal user, not with sudo"
command -v python3 >/dev/null || die "python3 is required"

# a link we made: a symlink that points into this folder
unlink_ours() {
  local path="$1"
  if [ -L "$path" ]; then
    case "$(readlink -f "$path")" in
      "$HERE"/*) rm "$path"; say "removed $path"; return 0 ;;
    esac
    # the Chordata folder was moved or deleted: a dangling link to a file of the same name
    if [ ! -e "$path" ] && [ "$(basename "$(readlink "$path")")" = "$(basename "$path")" ]; then
      rm "$path"; say "removed $path (it pointed to a Chordata folder that is gone)"; return 0
    fi
  fi
  if [ -e "$path" ] || [ -L "$path" ]; then
    say "left $path alone (not a link to this folder)"
  fi
}
unexclude() {  # repo, path inside it
  local ex="$1/.git/info/exclude"
  [ -f "$ex" ] || return 0
  grep -vxF "$2" "$ex" > "$ex.chordata-tmp" || true
  cat "$ex.chordata-tmp" > "$ex"
  rm "$ex.chordata-tmp"
}
backup() {
  [ -e "$1.before-chordata-uninstall" ] || cp -pL "$1" "$1.before-chordata-uninstall"
}
# Remove Chordata's lines from a config file, line by line, keeping everything else exactly
# (other sections, comments, blank lines, CRLF endings). Written in place, so a symlinked
# file stays linked and keeps its mode. Prints how many lines went.
strip_lines() {  # file, kind: includes | sections
  python3 - "$1" "$2" <<'PY'
import re, sys
path, kind = sys.argv[1], sys.argv[2]
with open(path, newline="") as f:
    lines = f.read().splitlines(keepends=True)
out, removed = [], 0
if kind == "includes":
    ours = re.compile(r"^\[include (chordata|chordata_settings)\.cfg\][ \t]*([#;].*)?\r?\n?$")
    for line in lines:
        if ours.match(line):
            removed += 1
        else:
            out.append(line)
else:
    head = re.compile(r"^\[(chordata_web|update_manager chordata)\][ \t]*([#;].*)?\r?\n?$")
    skipping = False
    for line in lines:
        bare = line.strip()
        if head.match(line):
            skipping = True
            removed += 1
            if out and not out[-1].strip():     # the blank line install.sh put before it
                out.pop()
            continue
        # our section's own keys: up to a blank line, a comment or the next section
        if skipping and bare and not bare.startswith(("[", "#", ";")):
            removed += 1
            continue
        skipping = False
        out.append(line)
if removed:
    with open(path, "w", newline="") as f:
        f.write("".join(out))
print(removed)
PY
}

# 1. the plugin
for sub in klippy/plugins klippy/extras; do
  for f in chordata.py chordata_belts.py; do
    [ -e "$KLIPPER_DIR/$sub/$f" ] || [ -L "$KLIPPER_DIR/$sub/$f" ] || continue
    unlink_ours "$KLIPPER_DIR/$sub/$f"
    unexclude "$KLIPPER_DIR" "$sub/$f"
  done
done

# 2. macros: the two includes, then the link (chordata_settings.cfg itself stays)
if [ -f "$CONFIG_DIR/printer.cfg" ] && grep -qE '^\[include (chordata|chordata_settings)\.cfg\]' "$CONFIG_DIR/printer.cfg"; then
  backup "$CONFIG_DIR/printer.cfg"
  strip_lines "$CONFIG_DIR/printer.cfg" includes >/dev/null
  say "removed the includes from printer.cfg (backup: printer.cfg.before-chordata-uninstall)"
fi
if grep -qsE '^[ \t]*\[include[ \t]+chordata\.cfg' "$CONFIG_DIR"/*.cfg; then
  say "left chordata.cfg in place: a config file still includes it (remove that line, then run this again)"
else
  unlink_ours "$CONFIG_DIR/chordata.cfg"
fi

# 3. the page: the Moonraker component, its section and the update entry
unlink_ours "$MOONRAKER_DIR/moonraker/components/chordata_web.py"
unexclude "$MOONRAKER_DIR" "moonraker/components/chordata_web.py"
if [ -f "$CONFIG_DIR/moonraker.conf" ] && grep -qE '^\[(chordata_web|update_manager chordata)\]' "$CONFIG_DIR/moonraker.conf"; then
  backup "$CONFIG_DIR/moonraker.conf"
  strip_lines "$CONFIG_DIR/moonraker.conf" sections >/dev/null
  say "removed [chordata_web] and [update_manager chordata] from moonraker.conf (backup: moonraker.conf.before-chordata-uninstall)"
fi

# 4. the sidebar link
NAV="$CONFIG_DIR/.theme/navi.json"
if [ -f "$NAV" ] && grep -q '"Chordata"' "$NAV"; then
  backup "$NAV"
  python3 - "$NAV" <<'PY'
import json, os, sys, tempfile
path = sys.argv[1]
try:
    links = json.load(open(path))
except ValueError as exc:
    print("Chordata: navi.json is not valid JSON, left alone: %s" % exc)
    sys.exit(0)
if not isinstance(links, list):
    print("Chordata: navi.json is not a list of links, left alone")
    sys.exit(0)
links = [l for l in links if not (isinstance(l, dict) and l.get("title") == "Chordata")]
if not links:
    os.remove(path)
    sys.exit(0)
fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".navi-")
with os.fdopen(fd, "w") as f:
    json.dump(links, f, indent=2)
os.chmod(temp, 0o644)
os.replace(temp, path)
print("Chordata: removed the Chordata link from Mainsail's sidebar")
PY
fi

say "done. Restart Moonraker and Klipper: sudo systemctl restart moonraker klipper"
say "kept: chordata_settings.cfg and the chordata/ recordings folder in $CONFIG_DIR (delete them if you like)"
say "the downloaded folder can go too: rm -rf $HERE"
