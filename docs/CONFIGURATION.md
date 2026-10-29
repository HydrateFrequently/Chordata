# Configuration reference

Everything Chordata reads or writes in your printer's configuration: the settings you can change, the
macros and commands in `chordata.cfg`, the entries the installer adds to `printer.cfg` and `moonraker.conf`,
and the files Chordata keeps in its own folder.

Paths below assume the standard layout, with the configuration in `~/printer_data/config` (shown as
`<config>`) and Chordata checked out in `~/Chordata`.

## The files at a glance

| File | What it is | Edit it? |
|---|---|---|
| `<config>/chordata_settings.cfg` | Your settings: the `[chordata]` section | Yes. The installer writes it once and never overwrites it |
| `<config>/chordata.cfg` | Chordata's macros | No. It is a link to `~/Chordata/chordata.cfg`, so every update replaces it |
| `<config>/printer.cfg` | Gets `[include chordata.cfg]` and `[include chordata_settings.cfg]` at the top | Only to remove Chordata by hand |
| `<config>/moonraker.conf` | Gets `[chordata_web]` (the page) and `[update_manager chordata]` (updates) | Only if your setup differs, see below |
| `<config>/.theme/navi.json` | Mainsail's sidebar links; the installer adds the Chordata link | No |
| `<config>/chordata/` | Chordata's own folder: recordings, allowed fans, the saved toolhead position | Delete files in it if you want, see below |

The plugin itself (`chordata.py` and `chordata_belts.py`) is linked into Kalico's `klippy/plugins` or
Klipper's `klippy/extras`, and the page's Moonraker component (`chordata_web.py`) into
`~/moonraker/moonraker/components`. They are links to the checkout, so they update with it.

## `[chordata]` settings (`chordata_settings.cfg`)

The installer writes this file with the options commented out, so the defaults apply until you change one.
Restart Klipper after editing it.

```ini
[chordata]
#belt_target: 110
#belt_tolerance: 0.25
#recordings_keep: 5
#path: ~/printer_data/config/chordata
```

| Option | Default | Allowed | What it does |
|---|---|---|---|
| `belt_target` | `110` | 40–300 (Hz) | The target frequency the setup screen starts with. Use the value your printer's documentation gives for the stretch of belt you pluck. You can change it on the setup screen for the session; that change is not written back here. |
| `belt_tolerance` | `0.25` | above 0 (Hz) | How close a pluck must be to the target to show **On target**. The last pluck decides; the average under it shows whether that holds. |
| `recordings_keep` | `5` | 1 or more | How many recordings Chordata keeps in `chordata/recordings/`, newest first. Older ones are deleted when new ones are written. A 30-minute Tuner Mode listen is about 300 MB of CSV. |
| `path` | `<config>/chordata` | a folder path, `~` allowed | Where Chordata keeps its folder (recordings, allowed fans, the saved position). |

A settings file written before 24 October 2026 lists only the first two options; add the others by hand if
you need them. Its comment above `belt_tolerance` says "the average of 5 good plucks" must be within it,
which was out of date: the table above is what the code does.

## What else `printer.cfg` needs

Chordata doesn't add these, it needs them already there:

- **An accelerometer** on the toolhead and **`[resonance_tester]`**, as for input shaping. Chordata uses the
  first accelerometer `[resonance_tester]` lists, unless a command names another one with `CHIP=`.
- **`[respond]`**, so the prompts can show in Mainsail or Fluidd.
- **CoreXY kinematics.** On other printers the belt step refuses to start.

## Macros in `chordata.cfg`

`chordata.cfg` is a link to the checkout. To change a macro, copy it into your own file under a new name; an
edit to `chordata.cfg` itself is lost on the next update.

| Macro | For you? | What it does |
|---|---|---|
| `CHORDATA` | Yes | Starts Chordata from Mainsail or Fluidd. Refuses while the plugin isn't loaded or while printing or paused. |
| `_CHORDATA_BELTS` | Internal | Runs each step. `STEP=` is `setup` (default), `go`, `save`, `start`, `tap` or `free`. `DT=` (−5, −1, +1, +5 from the pop-up) or `HZ=` (typed on the page) changes the target, rounded and held between 40 and 300 Hz. `ANYWAY=1` measures despite a hum. It keeps the session's target in its variable `target` (0: use `belt_target`). |
| `_CHORDATA_SETUP` | Internal | The **Set Target Frequency** prompt: target, accelerometer and its range, the target buttons, Go to / Save belt tensioning position, Start. Start is hidden while the target is above what the accelerometer hears. |
| `_CHORDATA_UI` | Internal | The other prompts. `STEP=` is `tapnow`, `tapped` (the taps' result, or the hum card with the fan buttons), `freepluck`, `freedone` or `stop`. |
| `_CHORDATA_FANOFF` | Internal | Turns off an allowed fan (`INDEX=`, its place in the fan list), waits 5 s for it to slow down, then records the background again. |
| `_CHORDATA_FANALLOW` | Internal | Allows Chordata to turn off a fan (`INDEX=`), then shows the hum card again. |
| `_CHORDATA_CLOSE` | Internal | Close Chordata: writes the session's recordings, turns back on the fans Chordata turned off, clears the history, ends the prompt. |
| `_CHORDATA_ST` | Optional | Runs Shake&Tune's `COMPARE_BELTS_RESPONSES`, if Shake&Tune is installed. Nothing in Chordata calls it. |

## G-code commands from the plugin

| Command | Parameters | What it does |
|---|---|---|
| `CHORDATA_RECORD` | `SECONDS=` 0.5–30 (default 10), `NAME=`, `CHIP=` | Records an accelerometer and saves it at once as `chordata/recordings/<chip>-<name>.csv` (the time if `NAME` is left out). `CHIP=` is the accelerometer's name as in Klipper's own commands. |
| `_CHORDATA_START` | `TARGET=` 20–400 Hz (default `belt_target`) | Starts a session. Needs `[resonance_tester]` and CoreXY. |
| `_CHORDATA_TAP` | `UI=1`, `ANYWAY=1` | Records the background, then listens for the taps (120 s at most, or until the page's ‹ Back, Close Chordata or × is pressed). `ANYWAY=1` skips the hum check. |
| `_CHORDATA_FREE` | `UI=1`, `SECONDS=` 0–1800 | Tuner Mode. Without `SECONDS` it ends 60 s after the last pluck, on **Done** on the page, on the page's **‹ Back**, **Close Chordata** or ×, or after 30 minutes. Other G-code waits while it listens. |
| `_CHORDATA_POS` | `SAVE=1` or `GO=1` | Saves where the toolhead is as the belt tensioning position, or moves there. Needs X and Y homed. Z only ever moves up. |
| `_CHORDATA_FAN` | `INDEX=` | Turns off an allowed fan until Chordata is closed. |
| `_CHORDATA_FANS` | | Writes the session's recordings and turns the fans Chordata turned off back to how they were. |
| `_CHORDATA_FAN_OK` | `SECTION=` or `INDEX=`, `ALLOW=` 1 (default) or 0 | Allows or forbids Chordata to turn off a fan, kept across restarts. `SECTION` is its config section, for example `SECTION="fan_generic aux_fan"`. |

All of them except `_CHORDATA_FANS` and `_CHORDATA_FAN_OK` refuse while printing, while paused or while
another Chordata step runs.

## Which fans Chordata may turn off

- **Never:** `controller_fan`, `heater_fan` and `temperature_fan`, and a `fan_generic` whose name points to the
  electronics, the stepper drivers, the host, the power supply or the hotend. The full word list is
  `SAFETY_NAMES` in `chordata.py`.
- **Only once you allow it:** the part cooling fan (`[fan]`) and any other `fan_generic`. The hum card asks once
  per fan; your answer is kept in `chordata/fans_ok.json`.
- Fans Chordata turned off come back on when you close Chordata, when a print starts, or 10 minutes after
  Chordata was last used. When a print starts, the part cooling fan is left to the slicer.
- Which fans are off is kept in memory only: after a Klipper restart, every fan is back to how Klipper starts
  it.

## Files in the `chordata/` folder

| File | Contents | Safe to delete? |
|---|---|---|
| `recordings/*.csv` | Raw accelerometer samples: a header `#time,accel_x,accel_y,accel_z`, then one row per sample (time in s, acceleration in mm/s²). `free-<date>-<time>.csv` is a Tuner Mode listen, `tap-<date>-<time>.csv` the taps, `tap-<date>-<time>-quiet.csv` the background, and `CHORDATA_RECORD` writes `<chip>-<name>.csv`. Written when you close Chordata, after 10 minutes without a new recording, or when Klipper stops, so the SD card is written seldom. | Yes |
| `fans_ok.json` | The fans you allowed, as `{"fan_generic aux_fan": true}`. | Yes: deleting it takes every allowance back |
| `belt_position.json` | The saved belt tensioning position, as `{"x": ..., "y": ..., "z": ...}` (Z is empty if Z wasn't homed). | Yes: Go to belt tensioning position then disappears until you save again |

## `moonraker.conf`

```ini
[chordata_web]

[update_manager chordata]
type: git_repo
path: ~/Chordata
origin: https://github.com/HydrateFrequently/Chordata.git
primary_branch: main
managed_services: klipper moonraker
```

- **`[chordata_web]`** serves the page at `http://<printer>/server/chordata`. Its one option, `page:`, points
  to another HTML file to serve instead of `~/Chordata/web/index.html`. The page is read fresh on every visit,
  so a page update needs no restart.
- **`[update_manager chordata]`** lets Mainsail's or Fluidd's update manager update Chordata and restart
  Klipper and Moonraker afterwards. `primary_branch` follows the branch checked out in `~/Chordata` each time
  you run `install.sh` (`main` normally, `dev` to try work in progress).

## Installer options

`install.sh` takes these environment variables when your folders differ from the defaults:

| Variable | Default | Must contain |
|---|---|---|
| `KLIPPER_DIR` | `~/kalico`, else `~/klipper` | `klippy/` |
| `KLIPPY_ENV` | `~/klippy-env` | Klipper's Python environment (with numpy) |
| `MOONRAKER_DIR` | `~/moonraker` | `moonraker/components/` |
| `CONFIG_DIR` | `~/printer_data/config` | `printer.cfg` |

Running it again changes nothing that is already in place. It backs up what it changes the first time:
`printer.cfg.before-chordata`, `.theme/navi.json.before-chordata`, and `moonraker.conf.before-chordata-branch`
when it changes the update branch.

## Fixed values

These are set in the code, not in a config file:

| What | Value |
|---|---|
| Tuner Mode ends after the last pluck | 60 s |
| Longest Tuner Mode listen | 30 minutes |
| Longest tap listen | 120 s |
| Good taps needed | 3 (9 that don't count stop the step) |
| Fans come back on after Chordata was last used | 10 minutes |
| Double tap on the toolhead (clears the history) | two taps up to 0.8 s apart |
| History kept | the last 100 plucks |
| Recordings waiting in RAM before part goes to the SD card | 10 % of the free RAM, 8–64 MB |
