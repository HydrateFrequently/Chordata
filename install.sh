#!/bin/bash
# Chordata installer for Kalico or Klipper (with Moonraker and Mainsail or Fluidd).
#
#   cd ~ && git clone https://github.com/HydrateFrequently/Chordata.git
#   ~/Chordata/install.sh
#
# It links the files from this folder (so a git pull updates them), adds two includes
# at the top of printer.cfg (with a backup) and an update entry to moonraker.conf.
# Run it again any time; it does nothing twice. Options (environment variables):
#   KLIPPER_DIR   default: ~/kalico, else ~/klipper
#   KLIPPY_ENV    default: ~/klippy-env
#   CONFIG_DIR    default: ~/printer_data/config
set -e
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
KLIPPER_DIR=${KLIPPER_DIR:-$( [ -d ~/kalico/klippy ] && echo ~/kalico || echo ~/klipper )}
KLIPPY_ENV=${KLIPPY_ENV:-~/klippy-env}
CONFIG_DIR=${CONFIG_DIR:-~/printer_data/config}

say() { echo "Chordata: $*"; }
die() { echo "Chordata: $*" >&2; exit 1; }

[ "$(id -u)" != 0 ] || [ -n "$CHORDATA_ALLOW_ROOT" ] || die "run it as your normal user, not with sudo"
[ -d "$KLIPPER_DIR/klippy" ] || die "no Kalico/Klipper in $KLIPPER_DIR (set KLIPPER_DIR=...)"
[ -f "$CONFIG_DIR/printer.cfg" ] || die "no printer.cfg in $CONFIG_DIR (set CONFIG_DIR=...)"

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
  ln -sfn "$HERE/$f" "$KLIPPER_DIR/$sub/$f"
  exclude "$KLIPPER_DIR" "$sub/$f"
done
say "plugin linked into $KLIPPER_DIR/$sub"

# 2. macros and settings
ln -sfn "$HERE/chordata.cfg" "$CONFIG_DIR/chordata.cfg"
if [ ! -e "$CONFIG_DIR/chordata_settings.cfg" ]; then
  cat > "$CONFIG_DIR/chordata_settings.cfg" <<'EOF'
# Chordata settings (yours to edit; install.sh never overwrites this file)
[chordata]
# Starting target frequency in Hz (change it on the first prompt too). Use the value
# your printer's guide gives for the stretch of belt you pluck.
#belt_target: 110
# How close the average of 5 good plucks must be to the target, in Hz
#belt_tolerance: 0.25
EOF
  say "wrote chordata_settings.cfg"
fi
added=""
for inc in chordata.cfg chordata_settings.cfg; do
  grep -q "^\[include $inc\]" "$CONFIG_DIR/printer.cfg" && continue
  added="$added[include $inc]\n"
done
if [ -n "$added" ]; then
  cp "$CONFIG_DIR/printer.cfg" "$CONFIG_DIR/printer.cfg.before-chordata"
  # at the top: the SAVE_CONFIG block at the end must stay last
  { printf "$added"; cat "$CONFIG_DIR/printer.cfg.before-chordata"; } > "$CONFIG_DIR/printer.cfg"
  say "added the includes at the top of printer.cfg (backup: printer.cfg.before-chordata)"
fi
grep -rqs "^\[respond\]" "$CONFIG_DIR"/*.cfg || say "note: printer.cfg needs [respond] for the prompts"
grep -rqs "^\[resonance_tester\]" "$CONFIG_DIR"/*.cfg || say "note: Chordata needs an accelerometer and [resonance_tester] in printer.cfg"

# 3. updates from Mainsail's update manager
if [ -f "$CONFIG_DIR/moonraker.conf" ] && ! grep -q '^\[update_manager chordata\]' "$CONFIG_DIR/moonraker.conf"; then
  cat >> "$CONFIG_DIR/moonraker.conf" <<EOF

[update_manager chordata]
type: git_repo
path: $HERE
origin: https://github.com/HydrateFrequently/Chordata.git
primary_branch: main
managed_services: klipper
EOF
  say "added [update_manager chordata] to moonraker.conf"
fi

say "Chordata comes without any warranty: you use it at your own risk."
say "done. Restart Moonraker and Klipper: sudo systemctl restart moonraker klipper"
say "then run the CHORDATA macro in Mainsail or Fluidd."
