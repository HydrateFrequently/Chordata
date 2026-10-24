#!/bin/bash
# Chordata installer for Kalico or Klipper (with Moonraker and Mainsail or Fluidd).
#
#   cd ~ && git clone https://github.com/HydrateFrequently/Chordata.git
#   ~/Chordata/install.sh
#
# The update entry follows the branch this folder has checked out: main for normal
# installs. To try work in progress: git -C ~/Chordata checkout dev, then run this again.
#
# It links the files from this folder (so a git pull updates them), adds two includes
# at the top of printer.cfg (with a backup), the Chordata page and an update entry to
# moonraker.conf, and a link in Mainsail's sidebar.
# Run it again any time; it does nothing twice. Options (environment variables):
#   KLIPPER_DIR   default: ~/kalico, else ~/klipper
#   KLIPPY_ENV    default: ~/klippy-env
#   MOONRAKER_DIR default: ~/moonraker
#   CONFIG_DIR    default: ~/printer_data/config
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
KLIPPER_DIR=${KLIPPER_DIR:-$( [ -d ~/kalico/klippy ] && echo ~/kalico || echo ~/klipper )}
KLIPPY_ENV=${KLIPPY_ENV:-~/klippy-env}
MOONRAKER_DIR=${MOONRAKER_DIR:-~/moonraker}
CONFIG_DIR=${CONFIG_DIR:-~/printer_data/config}

say() { echo "Chordata: $*"; }
die() { echo "Chordata: $*" >&2; exit 1; }

[ "$(id -u)" != 0 ] || [ -n "${CHORDATA_ALLOW_ROOT:-}" ] || die "run it as your normal user, not with sudo"
[ -d "$KLIPPER_DIR/klippy" ] || die "no Kalico/Klipper in $KLIPPER_DIR (set KLIPPER_DIR=...)"
[ -f "$CONFIG_DIR/printer.cfg" ] || die "no printer.cfg in $CONFIG_DIR (set CONFIG_DIR=...)"
command -v python3 >/dev/null || die "python3 is required"

# Check existing navigation before changing any printer configuration.
python3 - "$CONFIG_DIR/.theme/navi.json" <<'PYVALIDATE'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
if path.exists():
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
            raise ValueError("expected an array of navigation objects")
    except (ValueError, OSError) as exc:
        sys.exit("Chordata: fix existing navi.json before installing: " + str(exc))
PYVALIDATE

link_file() {
  local source="$1" destination="$2"
  if [ -e "$destination" ] && [ ! -L "$destination" ]; then
    die "refusing to replace regular file $destination; back it up and remove it first"
  fi
  ln -sfn "$source" "$destination"
}
# Back up configuration once; reruns preserve the original backup.
if [ -f "$CONFIG_DIR/moonraker.conf" ] && [ ! -e "$CONFIG_DIR/moonraker.conf.before-chordata" ]; then
  cp -p "$CONFIG_DIR/moonraker.conf" "$CONFIG_DIR/moonraker.conf.before-chordata"
fi

# the pluck analysis needs numpy (Klipper's input shaper tools need it too)
if [ -x "$KLIPPY_ENV/bin/python" ] && ! "$KLIPPY_ENV/bin/python" -c "import numpy" 2>/dev/null; then
  say "note: numpy is missing in $KLIPPY_ENV; install it with: $KLIPPY_ENV/bin/pip install numpy"
fi

# keep a repo's update check clean of our links
exclude() {  # repo, path inside it
  local ex="$1/.git/info/exclude"
  [ -d "$1/.git" ] || return 0
  grep -qxF "$2" "$ex" 2>/dev/null || echo "$2" >> "$ex"
}

# 1. the plugin (Kalico: klippy/plugins, mainline Klipper: klippy/extras)
if [ -d "$KLIPPER_DIR/klippy/plugins" ]; then sub=klippy/plugins; else sub=klippy/extras; fi
for f in chordata.py chordata_belts.py; do
  link_file "$HERE/$f" "$KLIPPER_DIR/$sub/$f"
  exclude "$KLIPPER_DIR" "$sub/$f"
done
say "plugin linked into $KLIPPER_DIR/$sub"

# 2. macros and settings
link_file "$HERE/chordata.cfg" "$CONFIG_DIR/chordata.cfg"
if [ ! -e "$CONFIG_DIR/chordata_settings.cfg" ]; then
  cat > "$CONFIG_DIR/chordata_settings.cfg" <<'EOF'
# Chordata settings (yours to edit; install.sh never overwrites this file)
[chordata]
# Starting target frequency in Hz (change it on the first prompt too). Use the value
# your printer's guide gives for the stretch of belt you pluck.
#belt_target: 110
# How close a pluck must be to the target to show On target, in Hz
#belt_tolerance: 0.25
# How many recordings to keep in chordata/recordings (the newest; a 30-minute listen is about 300 MB)
#recordings_keep: 5
# Where Chordata keeps its folder (recordings, allowed fans, the saved toolhead position)
#path: ~/printer_data/config/chordata
# All options and macros: docs/CONFIGURATION.md
EOF
  say "wrote chordata_settings.cfg"
fi
added=""
for inc in chordata.cfg chordata_settings.cfg; do
  grep -qxF "[include $inc]" "$CONFIG_DIR/printer.cfg" && continue
  added="$added[include $inc]\n"
done
if [ -n "$added" ]; then
  [ -e "$CONFIG_DIR/printer.cfg.before-chordata" ] || cp -p "$CONFIG_DIR/printer.cfg" "$CONFIG_DIR/printer.cfg.before-chordata"
  # at the top: the SAVE_CONFIG block at the end must stay last
  { printf '%b' "$added"; cat "$CONFIG_DIR/printer.cfg"; } > "$CONFIG_DIR/printer.cfg.chordata-tmp"
  cat "$CONFIG_DIR/printer.cfg.chordata-tmp" > "$CONFIG_DIR/printer.cfg"
  rm "$CONFIG_DIR/printer.cfg.chordata-tmp"
  say "added the includes at the top of printer.cfg (backup: printer.cfg.before-chordata)"
fi
grep -rqs "^\[respond\]" "$CONFIG_DIR"/*.cfg || say "note: printer.cfg needs [respond] for the prompts"
grep -rqs "^\[resonance_tester\]" "$CONFIG_DIR"/*.cfg || say "note: Chordata needs an accelerometer and [resonance_tester] in printer.cfg"

# 3. the Chordata page: a Moonraker component serves it at /server/chordata
if [ -d "$MOONRAKER_DIR/moonraker/components" ]; then
  link_file "$HERE/moonraker/chordata_web.py" "$MOONRAKER_DIR/moonraker/components/chordata_web.py"
  exclude "$MOONRAKER_DIR" "moonraker/components/chordata_web.py"
  if [ -f "$CONFIG_DIR/moonraker.conf" ] && ! grep -q '^\[chordata_web\]' "$CONFIG_DIR/moonraker.conf"; then
    printf '\n[chordata_web]\n' >> "$CONFIG_DIR/moonraker.conf"
    say "added [chordata_web] to moonraker.conf"
  fi
  # a link in Mainsail's sidebar
  mkdir -p "$CONFIG_DIR/.theme"
  python3 - "$CONFIG_DIR/.theme/navi.json" "$HERE/web/navi.json" <<'PY'
import json, os, sys, shutil, tempfile
dst, src = sys.argv[1], sys.argv[2]
links = []
if os.path.exists(dst):
    with open(dst) as f:
        links = json.load(f)
    if not os.path.exists(dst + ".before-chordata"):
        shutil.copy2(dst, dst + ".before-chordata")
links = [l for l in links if l.get("title") != "Chordata"]
with open(src) as f:
    links += json.load(f)
fd, temp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".navi-")
try:
    with os.fdopen(fd, "w") as f:
        json.dump(links, f, indent=2)
    os.chmod(temp, 0o644)
    os.replace(temp, dst)
finally:
    if os.path.exists(temp):
        os.unlink(temp)
PY
  say "Chordata page added (Mainsail sidebar, or http://<printer>/server/chordata)"
else
  say "note: no Moonraker in $MOONRAKER_DIR (set MOONRAKER_DIR=...): the page is left out, the CHORDATA macro still works"
fi

# 4. updates from Mainsail's update manager, following the branch checked out here
BRANCH=$(git -C "$HERE" symbolic-ref --quiet --short HEAD 2>/dev/null || echo main)
if [ -f "$CONFIG_DIR/moonraker.conf" ] && grep -q '^\[update_manager chordata\]' "$CONFIG_DIR/moonraker.conf"; then
  python3 - "$CONFIG_DIR/moonraker.conf" "$BRANCH" <<'PY'
import os, re, shutil, sys, tempfile
path, branch = sys.argv[1], sys.argv[2]
text = open(path).read()
head = re.search(r"^\[update_manager chordata\][^\n]*$", text, re.M)   # same match as the grep above
end = re.search(r"^\[", text[head.end():], re.M)
stop = head.end() + end.start() if end else len(text)
section = text[head.end():stop]
line = re.search(r"^primary_branch:[ \t]*(\S*)[ \t]*$", section, re.M)
if line and line.group(1) == branch:
    sys.exit(0)
if line:
    section = section[:line.start()] + "primary_branch: " + branch + section[line.end():]
else:
    section = "\nprimary_branch: " + branch + section
if not os.path.exists(path + ".before-chordata-branch"):
    shutil.copy2(path, path + ".before-chordata-branch")
fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".moonraker-")
with os.fdopen(fd, "w") as out:
    out.write(text[:head.end()] + section + text[stop:])
os.replace(temp, path)
print("Chordata: the update entry now follows the %s branch" % branch)
PY
elif [ -f "$CONFIG_DIR/moonraker.conf" ]; then
  cat >> "$CONFIG_DIR/moonraker.conf" <<EOF

[update_manager chordata]
type: git_repo
path: $HERE
origin: https://github.com/HydrateFrequently/Chordata.git
primary_branch: $BRANCH
managed_services: klipper moonraker
EOF
  say "added [update_manager chordata] to moonraker.conf"
fi

say "Chordata comes without any warranty: you use it at your own risk."
say "done. Restart Moonraker and Klipper: sudo systemctl restart moonraker klipper"
say "then open Chordata in the Mainsail sidebar (http://<printer>/server/chordata on a phone), or run the CHORDATA macro."
