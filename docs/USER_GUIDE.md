# Chordata user guide

This guide covers the fork at [alextverdyy/Chordata](https://github.com/alextverdyy/Chordata).
It works as a Klipper/Kalico measurement module, a Moonraker companion page, and a
Mainsail sidebar link. You can use the entire measurement workflow with AI disabled.

All screenshots below come from the shipped offline demo. Their readings are
illustrative; the toolhead position, belt span and target must come from your own
printer's documentation. No physical-printer validation is implied by these images.

## 1. Try the interface first

On a computer with Python 3:

```sh
git clone https://github.com/alextverdyy/Chordata.git
cd Chordata
python3 -m http.server 8765 --bind 127.0.0.1 --directory web
```

Open **http://127.0.0.1:8765/?demo** on that computer. Keep `?demo` in the URL:
without it, the page tries to connect to Moonraker on the same host. Demo mode
opens no printer or AI connection and disables printer commands. Stop this preview
server with Ctrl+C when finished. A static preview server does not measure belts.

![Desktop demo showing both belts and their plucks](images/desktop-demo.png)

For a narrow screen, the same interface stacks its controls and preserves the data:

<img src="images/mobile-demo.png" width="390" alt="Phone-sized Chordata demo with belt summaries and a frequency chart">

## 2. Prepare the printer

You need:

- A supported CoreXY configuration running Klipper or Kalico.
- Moonraker and a working Mainsail or Fluidd installation for the page.
- An accelerometer that Klipper can record, mounted on the toolhead.
- A working `[resonance_tester]` configuration and `[respond]` for the prompts.
- NumPy in the Python environment used by Klipper.
- Your manufacturer's measurement position, belt span and target frequency.

Start with a working accelerometer configuration; Chordata does not configure the
sensor's pins, axes or MCU. You can check the existing sensor with Klipper's
`ACCELEROMETER_QUERY` command, using the sensor selection required by your setup.
See the [Klipper resonance measurement instructions](https://www.klipper3d.org/Measuring_Resonances.html)
for sensor installation and checks.

The default target is 110 Hz and the default tolerance is 0.25 Hz. These are software
defaults, not a specification for your printer. Different spans on the same belt can
have different frequencies. Always compare recordings made at the same documented
position and span.

Finish or cancel any print before maintenance. The plugin rejects direct measurement
and saved-position commands while printing, paused, or already recording. Keep the
same sensor mounting and measurement method throughout a comparison.

## 3. Install on the printer host

SSH into the printer's Linux host as its normal user, then run:

```sh
cd ~
git clone https://github.com/alextverdyy/Chordata.git
cd Chordata
./install.sh
sudo systemctl restart moonraker klipper
```

Use the actual service names on your host if they differ. For Kalico, the service
may still be named `klipper`; check your installation rather than assuming its name.

The installer:

| Item | Default destination | Behavior |
| --- | --- | --- |
| Measurement modules | `~/klipper/klippy/extras/` or Kalico's `klippy/plugins/` | Symlinks to the checkout |
| Macros | `~/printer_data/config/chordata.cfg` | Symlink to the checkout |
| User settings | `~/printer_data/config/chordata_settings.cfg` | Created only when absent |
| Includes | Top of `printer.cfg` | Adds macros and settings, keeping `SAVE_CONFIG` last |
| Companion component | Moonraker's `moonraker/components/chordata_web.py` | Symlink to the checkout |
| Page configuration | `[chordata_web]` in `moonraker.conf` | Added when absent |
| Mainsail navigation | Config directory's `.theme/navi.json` | Preserves other links |
| Update entry | `[update_manager chordata]` in `moonraker.conf` | Points new installations at this fork |

The initial configuration backups end in `.before-chordata`. The installer refuses
to overwrite a regular plugin file; inspect and back up such files before replacing
them. It validates existing navigation JSON before changing printer configuration.
It never restarts services automatically.

### Nonstandard paths

Set paths explicitly when your installation differs:

```sh
KLIPPER_DIR="$HOME/kalico" \
KLIPPY_ENV="$HOME/klippy-env" \
MOONRAKER_DIR="$HOME/moonraker" \
CONFIG_DIR="$HOME/printer_data/config" \
./install.sh
```

`KLIPPER_DIR` must contain `klippy/`. `MOONRAKER_DIR` must contain
`moonraker/components/`. `CONFIG_DIR` must contain your `printer.cfg`.
If Moonraker source is missing, installation omits the page; the macro workflow
remains available.

If the installer reports missing NumPy, install it into Klipper's own environment,
not just the host's system Python:

```sh
~/klippy-env/bin/python -m pip install numpy
```

### Open the page

Reload Mainsail and select **Chordata** in its sidebar. You can also open:

```text
http://<printer-host>/server/chordata
```

Use your normal printer hostname and HTTPS URL if configured. Fluidd users can use
this direct URL. The same host must proxy `/server/`, `/access/` and `/websocket`
to Moonraker. See [troubleshooting](TROUBLESHOOTING.md) for page or connection errors.

If Moonraker requires authentication, expand **Printer connection**, enter its API
key and select **Authenticate**. This key stays in page memory; reloading forgets
it. It is separate from the provider key used by Laya or Jev.

## 4. Set the target and position

Edit `chordata_settings.cfg` in the config file browser:

```ini
[chordata]
# Replace these examples with values appropriate to your printer.
belt_target: 110
belt_tolerance: 0.25
```

Restart Klipper after changing configuration. You can also adjust the session target
in Chordata's initial prompts. A session target does not rewrite the config file.

Use the page's explicit toolhead controls, or Mainsail's own controls, to home and
move to the documented belt-measurement position. Select **Save belt tensioning
position** if you want to reuse it. **Go to belt tensioning position** is a separate
movement action and requires homing first. It only raises Z when the saved position
requires more clearance; it does not lower Z to the saved value.

Saved positions use G-code coordinates. Re-save after changing coordinate offsets
or the physical measurement setup. Do not copy another printer's saved coordinates.

## 5. Record both belts

```mermaid
flowchart TD
    setup[Set the manufacturer's target and measurement position] --> tap[Optional toolhead tap calibration]
    tap --> a[Record belt A]
    a --> b[Record belt B]
    b --> inspect[Inspect frequencies and rejected plucks]
    inspect --> decision{Both belts complete and on target?}
    decision -->|Yes| finish[Finish or repeat to confirm]
    decision -->|No| adjust[Adjust manually]
    adjust --> a
```

1. Select **Belt tension** on the page or run `CHORDATA` from the console to open
   setup. Select **Start** in that prompt when ready; this also switches off the
   part-cooling fan through the macro's `M106 S0` command.
2. Confirm the target and position. Chordata does not choose a manufacturer's target.
3. If using tap calibration, select **Listen for the tap**, keep your hands off while
   background noise is recorded, then lightly tap the toolhead when prompted. This
   identifies toolhead modes to avoid mistaking a knock for a belt. You may skip it.
4. Select **Listen to belt A**. Wait through the initial background recording, then
   pluck the documented belt span evenly, about once a second.
5. Inspect the counted and rejected plucks. When recording finishes, continue to B.
6. Repeat for **belt B**, using the same measurement method and position.
7. Read the comparison. If you adjust either tensioner, measure **both** belts again:
   the belts interact mechanically.

A recording can continue for a bounded extra interval to collect enough accepted
plucks. If a pluck is weak or rejected, wait for the prompt and pluck again rather
than striking much harder. Do not adjust a tensioner while a series is recording.

Closing a prompt or selecting **Stop** ends the prompt workflow; it is not an
immediate cancellation of an active G-code recording. Wait until Chordata is idle
before starting another measurement or adjusting the belts.

## 6. Read the chart and measurement data

![Measurement chart with its exact per-pluck data](images/measurement-data.png)

| Display | Meaning |
| --- | --- |
| Belt A / Belt B large number | Average accepted frequency for that series, in Hz |
| Counted plucks | Readings accepted by the deterministic signal analysis |
| Rejected plucks | Attempts excluded from the average; the table supplies the reason |
| Blue / amber | Belt A / belt B |
| Circle / cross | Counted / rejected point with a detected frequency |
| Dashed line | Current target frequency |
| Missing frequency | No reliable tone was detected; the rejected row remains in the table |
| Difference | Frequency difference between the two belt averages |

The live chart updates from detected plucks about once a second. It shows frequency
by pluck number, not a continuous accelerometer waveform. A rejected point can
stretch the chart scale; use the table for exact values.

Completion requires five accepted plucks on each belt and an average within the
configured tolerance. A partial series can show an average while still needing more
readings. Spread and standard deviation in the result data describe repeatability;
they do not establish sensor accuracy. The UI measures frequency, not force.

Starting a new attempt clears the previous result for that belt. Failed recordings
clear completion. Prior readings for the other belt can remain visible while you
measure the current belt; complete a fresh pair after adjustments.

### Export and recordings

Select **Measurement data** to expand the table, then **Export CSV** to download
the latest displayed A/B series. Columns are `belt`, `pluck`, `frequency_hz`,
`accepted` and `reason`. The live current series replaces that belt's previous series.
This export is a frequency summary, not raw accelerometer samples.

Raw recordings appear under `chordata/recordings/` in the config file browser.
Belt captures and background captures have separate names. Chordata retains the
latest 40 CSV files; save important captures elsewhere before continuing.

For a standalone capture:

```gcode
CHORDATA_RECORD SECONDS=10 NAME=reference
```

To select a named sensor, use the name supported by your Klipper configuration:

```gcode
CHORDATA_RECORD CHIP=hotend SECONDS=10 NAME=reference
```

Here `hotend` is an example sensor name, not a required name. Supported recording
duration is 0.5–30 seconds. The CSV contains time and X/Y/Z acceleration samples.

## 7. Enable optional Laya or Jev review

You can leave this section untouched. Measurement, prompts, charts and exports work
with review disabled.

![Optional review settings with an illustrative Laya endpoint and no key](images/ai-settings.png)

1. Expand **Optional Laya / Jev review**.
2. Check **Enable review** and choose the provider.
3. Enter the full inference URL reachable **from the Moonraker host**.
4. Choose a model supported by the provider; `auto` is the default.
5. Enter a provider API key if required and select **Save review settings**.
6. Finish a measurement, then select **Review measurements**.

| Provider | Example URL | Model behavior |
| --- | --- | --- |
| Laya `laya-serve` | `http://laya-host:8000/v1/systemone` | `auto` lets Laya route, or enter an installed checkpoint name |
| Jev / TypeSafe | `https://api.typesafe.ai/v1/systemone` | `auto` maps to `jev-latest`, or choose a supported model |

These are endpoint examples. Chordata does not provision Laya, download checkpoints,
create a TypeSafe account or supply provider credentials. A proxy is supported if it
implements the same System One contract. An OpenAI-style `/chat/completions` endpoint
is not compatible with this request shape.

For a Laya service on another computer, `localhost` in the endpoint would refer to
the printer host, not that other computer. Use a hostname or IP reachable from the
printer. Use HTTPS when sending credentials across an untrusted network.

### What review does

Moonraker sends a compact allowlisted summary: target, tolerance, counts, frequency,
spread, timestamps and standardized rejection reasons. It sends no raw accelerometer
recording, printer configuration or local path. Requests are manual; enabling the
feature does not send every live pluck to the service.

The provider chooses a bounded next check: repeat the pair, inspect the setup,
compare the measurements, or report insufficient evidence. The result never runs
G-code, changes a target, accepts a rejected pluck or sets the completion state.
Provider calls have a 10-second deadline; redirects are refused and HTTPS
certificates are verified.

Confidence describes the provider's uncertainty. The default threshold is 0.7.
Low-confidence or invalid responses are withheld, as is comparison advice on an
incomplete pair. Measurements or settings changed during a review invalidate it.
A new measurement also clears displayed old advice.

**AI review is experimental.** The live Laya trials recorded in
[VALIDATION.md](VALIDATION.md) abstained, including on a clean synthetic pair. They
do not establish reliable belt-quality classification. A normal frequency result
and an uncertain AI review can coexist. Do not lower the threshold just to obtain
an answer.

### Credentials and disabling

The Moonraker API key authenticates this page to your printer; the provider API key
authenticates Moonraker to Laya/Jev. They are different credentials.

The provider key is stored on the printer host in Moonraker's data directory as
`chordata-ai.json`, with mode `0600`. It is reserved from Moonraker's file manager
and is never returned through the settings API. A blank provider key field retains
the stored key only when the endpoint and provider stay the same. Changing either
clears the old key unless you enter a new one.

To remove the stored key, check **Clear saved provider key when saving**, then save.
To stop future provider calls, uncheck **Enable review** and save. Disabling does
not remove the stored key unless you also select the clear option.

## 8. Updates and migration

Before updating, finish maintenance and preserve any local code changes. For a clean
checkout of this fork:

```sh
cd ~/Chordata
git pull --ff-only
./install.sh
sudo systemctl restart moonraker klipper
```

Mainsail's update manager can use the generated `[update_manager chordata]` entry.
The installer's symlinks keep modules connected to the checkout, so restart Klipper
and Moonraker after changing Python modules. Reload the browser for page changes.

### Migrate an existing upstream install

If your current `~/Chordata` is clean and still points at `HydrateFrequently/Chordata`:

```sh
cd ~/Chordata
git status --short
git remote rename origin upstream
git remote add origin https://github.com/alextverdyy/Chordata.git
git fetch origin
git merge --ff-only origin/main
git branch --set-upstream-to=origin/main main
./install.sh
```

Stop and preserve local changes if `git status --short` reports any. If an `upstream`
remote already exists, inspect `git remote -v` and adjust the remote names. Do not
force a failed fast-forward. These commands assume you are on `main`; the tracking
change makes future `git pull --ff-only` updates follow the fork.

Existing Moonraker update entries are preserved by the installer. Edit only this
entry's `origin` to follow the fork:

```ini
[update_manager chordata]
type: git_repo
path: ~/Chordata
origin: https://github.com/alextverdyy/Chordata.git
primary_branch: main
managed_services: klipper moonraker
```

Keep your actual path and managed service names, then restart Moonraker and Klipper.
The user settings file and saved recordings remain in the printer config directory.

### Remove Chordata

Remove its two include lines from `printer.cfg`, `[chordata_web]` and its update
entry from `moonraker.conf`, and the Chordata item from `.theme/navi.json`. Remove
only the Chordata symlinks installed in Klipper/Kalico, Moonraker and the config
directory after confirming their targets. Restart the affected services. Preserve
`chordata_settings.cfg` and `chordata/` if you want the saved settings and recordings.
You may separately remove the private AI settings file from Moonraker's data
directory. Do not restore an old whole-file backup over unrelated newer settings.

## Further reference

- [Troubleshooting](TROUBLESHOOTING.md)
- [Recorded validation and hardware limitations](VALIDATION.md)
- [Klipper measuring resonances](https://www.klipper3d.org/Measuring_Resonances.html)
- [Moonraker API documentation](https://moonraker.readthedocs.io/en/latest/external_api/introduction/)
- [TypeSafe System One API](https://docs.typesafe.ai/api)

To reproduce the screenshots after changing the interface:

```sh
npm ci
npx playwright install chromium
node tools/capture-docs.cjs
```

The screenshot tool uses only the offline demo and illustrative settings.
