#!/bin/bash
# Chordata uninstaller: undoes what install.sh did.
#
#   ~/Chordata/uninstall.sh
#   sudo systemctl restart moonraker klipper
#   rm -rf ~/Chordata          # optional: the downloaded folder itself
#
# It removes the plugin links from Kalico/Klipper, the two includes from printer.cfg (or
# from the file of yours that holds them), the chordata.cfg link, the Chordata page ([chordata_web]) and the update entry from
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

# Is a line matching REGEX in printer.cfg or a file it includes (Klipper's way: relative to the
# including file, globs allowed, nested)? Not any .cfg in the folder: Klipper's SAVE_CONFIG
# backups (printer-YYYYMMDD_HHMMSS.cfg) and old copies sit there too.
cfg_has() {  # extended regex (Python re), matched per line
  python3 - "$CONFIG_DIR/printer.cfg" "$1" <<'PY'
import glob, os, re, sys
want = re.compile(sys.argv[2])
inc = re.compile(r"^\[include[ \t]+([^\]]+?)[ \t]*\]")     # (Klipper's: not indented, no ~)
seen, todo = set(), [os.path.realpath(sys.argv[1])]
while todo:
    path = todo.pop()
    if path in seen or not os.path.isfile(path):
        continue
    seen.add(path)
    try:
        lines = open(path, errors="replace").read().splitlines()
    except OSError:
        continue
    for line in lines:
        if want.search(line):
            sys.exit(0)
        m = inc.match(line)
        if m:
            pat = os.path.join(os.path.dirname(path), m.group(1))
            todo += [os.path.realpath(p) for p in sorted(glob.glob(pat))]
sys.exit(1)
PY
}

# The files (printer.cfg and what it includes, as above) with a line matching REGEX, one per
# line; with a FILE as the second argument instead: does the include tree reach that file (by
# any include, a glob too)? exit 0 if so
cfg_files_with() {
  python3 - "$CONFIG_DIR/printer.cfg" "$1" "${2:-}" <<'PY'
import glob, os, re, sys
want = re.compile(sys.argv[2])
reach = os.path.realpath(sys.argv[3]) if sys.argv[3] else None
inc = re.compile(r"^\[include[ \t]+([^\]]+?)[ \t]*\]")
seen, todo = set(), [os.path.realpath(sys.argv[1])]
while todo:
    path = todo.pop()
    if path in seen or not os.path.isfile(path):
        continue
    seen.add(path)
    try:
        lines = open(path, errors="replace").read().splitlines()
    except OSError:
        continue
    if reach is None and any(want.search(line) for line in lines):
        print(path)
    for line in lines:
        m = inc.match(line)
        if m:
            pat = os.path.join(os.path.dirname(path), m.group(1))
            todo += [os.path.realpath(p) for p in sorted(glob.glob(pat))]
if reach is not None:
    sys.exit(0 if reach in seen else 1)
PY
}

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
    ours = re.compile(r"^\[include[ \t]+(chordata|chordata_settings)\.cfg[ \t]*\][ \t]*([#;].*)?\r?\n?$")
    for line in lines:
        if ours.match(line):
            removed += 1
        else:
            out.append(line)
else:
    head = re.compile(r"^\[(chordata_web|update_manager chordata)\][ \t]*([#;].*)?\r?\n?$")
    skipping, held = False, []
    for line in lines:
        bare = line.strip()
        if head.match(line):
            out.extend(held)
            held = []
            skipping = True
            removed += 1
            if out and not out[-1].strip():     # the blank line install.sh put before it
                out.pop()
            continue
        # our section's own lines, up to the next section: comments and blank lines are held, gone
        # if a key of ours follows them (they were inside our section), kept if a section does
        # (they belong to it)
        if skipping and not bare.startswith("["):
            if bare and not bare.startswith(("#", ";")):
                removed += 1 + len(held)
                held = []
            else:
                held.append(line)
            continue
        skipping = False
        out.extend(held)
        held = []
        out.append(line)
    out.extend(held)                    # (at the end of the file: kept)
if removed:
    with open(path, "w", newline="") as f:
        f.write("".join(out))
print(removed)
PY
}

INC='^\[include[ \t]+(chordata|chordata_settings)\.cfg[ \t]*\]'
# 1. macros: the two includes, wherever they are (printer.cfg, or a file of yours it includes),
# first: Klipper would not start with [chordata] still included once the plugin is gone
if [ -f "$CONFIG_DIR/printer.cfg" ]; then
  while IFS= read -r cfg; do
    [ -n "$cfg" ] || continue
    backup "$cfg"
    strip_lines "$cfg" includes >/dev/null
    say "removed the includes from $cfg (backup: $(basename "$cfg").before-chordata-uninstall)"
  done < <(cfg_files_with "$INC")
  for f in chordata.cfg chordata_settings.cfg; do
    if [ -e "$CONFIG_DIR/$f" ] && cfg_files_with x "$CONFIG_DIR/$f" >/dev/null; then
      die "a config file still includes $f in a form this script does not know (a glob?): remove that line, then run this again (the includes above were removed; the plugin and the links are still in place)"
    fi
  done
fi
unlink_ours "$CONFIG_DIR/chordata.cfg"

# 2. the plugin
for sub in klippy/plugins klippy/extras; do
  for f in chordata.py chordata_belts.py; do
    [ -e "$KLIPPER_DIR/$sub/$f" ] || [ -L "$KLIPPER_DIR/$sub/$f" ] || continue
    unlink_ours "$KLIPPER_DIR/$sub/$f"
    unexclude "$KLIPPER_DIR" "$sub/$f"
  done
done

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
import json, os, shutil, sys, tempfile
path = sys.argv[1]
real = os.path.realpath(path)           # (a link stays a link: its target is written)
try:
    links = json.load(open(real))
except ValueError as exc:
    print("Chordata: navi.json is not valid JSON, left alone: %s" % exc)
    sys.exit(0)
if not isinstance(links, list):
    print("Chordata: navi.json is not a list of links, left alone")
    sys.exit(0)
links = [l for l in links if not (isinstance(l, dict) and l.get("title") == "Chordata")]
# (an empty list stays as a file: a navi.json of yours that is a link to elsewhere keeps its place)
fd, temp = tempfile.mkstemp(dir=os.path.dirname(real), prefix=".navi-")
with os.fdopen(fd, "w") as f:
    json.dump(links, f, indent=2)
shutil.copymode(real, temp)
os.replace(temp, real)
print("Chordata: removed the Chordata link from Mainsail's sidebar")
PY
fi

say "done. Restart Moonraker and Klipper: sudo systemctl restart moonraker klipper"
say "kept: chordata_settings.cfg and the chordata/ recordings folder in $CONFIG_DIR (delete them if you like)"
say "the downloaded folder can go too: rm -rf $(printf %q "$HERE")"
