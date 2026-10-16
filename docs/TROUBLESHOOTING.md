# Troubleshooting

Begin with the exact symptom below. If reporting an issue, include the relevant
versions, sensor model, command and error text. Keep API keys out of logs and screenshots.

## The sidebar link is missing

Reload Mainsail after installation. Check that the active config directory contains
`.theme/navi.json` and that it has a Chordata item. The installer preserves other
navigation items. If it reports invalid JSON, fix that existing file first, then
rerun the installer. Try the direct URL `http://<printer-host>/server/chordata` to
separate a navigation problem from a Moonraker component problem.

## The page returns 404

Check that Moonraker loaded `[chordata_web]` from the configuration it actually uses.
Restart Moonraker after installing the component. Confirm that the symlink in its
`moonraker/components/` directory resolves to this checkout and that the checkout
still contains `web/index.html`.

If direct access to Moonraker's configured port works but the normal printer URL
does not, check the reverse proxy's `/server/` routing. Do not assume every host uses
port 7125; use the port in that Moonraker instance's configuration.

## The page loads but stays disconnected

The page opens `/websocket` on the same host and scheme. Check proxy WebSocket
forwarding and use the normal printer URL, not the standalone static demo server.
The static server only supports `?demo`; it is not a Moonraker replacement.

If a request returns 401/403, expand **Printer connection** and authenticate with a
Moonraker API key. The page forgets this key on reload. A Laya/Jev provider key cannot
authenticate the page to Moonraker. On an HTTPS page the connection uses `wss://`.

## “Chordata plugin not loaded” or an unknown command

The web component and Klipper plugin load independently. Check for
`[include chordata.cfg]` and `[include chordata_settings.cfg]` in the active
`printer.cfg`; the settings file must contain `[chordata]`. Restart Klipper after
installing. Check `klippy.log` for an import or configuration error and confirm the
symlinks resolve to existing files.

If NumPy is missing, install it into Klipper's Python environment as described in
the [user guide](USER_GUIDE.md#3-install-on-the-printer-host).

## The printer is busy

Finish or cancel the print. A paused print still counts as busy. Direct recording,
tap and saved-position commands also reject a currently active Chordata measurement.
Wait for recording and final analysis to finish before retrying. Closing the prompt
does not immediately cancel a queued G-code recording.

## No accelerometer, or the sensor sends no data

Check that `[resonance_tester]` references a working sensor. Confirm ordinary Klipper
sensor queries work first. A sensor can be configured without being mounted where
it can hear this belt. Chordata's default belt workflow uses the first accelerometer
listed by the resonance tester; `CHORDATA_RECORD CHIP=...` can help capture a named
sensor independently. Check sensor wiring, MCU connectivity and mounting before
changing the analysis code.

## Plucks are weak, rejected or inconsistent

- Wait through the initial background recording before plucking.
- Use the documented belt span and toolhead position; keep both consistent.
- Pluck evenly and avoid contacting the toolhead or frame.
- Reduce avoidable background vibration and check the sensor mount.
- Review rejection reasons in **Measurement data** instead of relying on a single
  large number. A strong knock is not necessarily a useful belt pluck.
- If interference resembles the toolhead's own resonance, repeat tap calibration.

A few accepted plucks can yield a provisional average. Completion still requires
five accepted readings per belt within the target tolerance. If no clear pluck can
be captured repeatedly, use the manufacturer's alternative tensioning method.
Do not invent a target from the loudest sensor peak.

## The two belts look matched but Chordata is not done

Matching each other is only one comparison. Both averages must be within tolerance
of the configured target, with enough accepted plucks. After any adjustment,
complete a fresh A/B pair. Starting a new attempt intentionally clears that belt's
old result and the previous completion state.

## A saved position is missing or movement is rejected

Home the required axes first, then save a valid position explicitly. Saved positions
are in G-code coordinates; re-save after changing offsets or your measurement setup.
Klipper still enforces its movement limits. Inspect the error and actual coordinates
rather than editing saved JSON to bypass an invalid movement.

## AI settings cannot be saved

Check Moonraker authentication and the `/access/` proxy route. The full endpoint
must use HTTP or HTTPS, with no URL-embedded credentials, query string or fragment.
A provider API key belongs in the password field. The model name must be valid and
minimum confidence must be between 0 and 1. Moonraker also needs permission to write
its own data directory.

If you changed endpoint or provider, re-enter the new provider key: Chordata clears
the old destination's credential deliberately. A blank field retains a key only
for the same endpoint/provider.

## AI review is unavailable or uncertain

| Result | Check |
| --- | --- |
| Disabled | Enable review and save if you want a provider request |
| No measurements | Record a belt first |
| Unavailable | Verify service reachability from the Moonraker host, full URL, credentials, model and TLS certificate |
| Just requested / running | Wait for the current request; repeated requests are rate-limited |
| Uncertain / abstained | Use the measurement data or repeat the series; confidence is below the threshold or evidence is incomplete |
| Measurements changed | Finish the new recording and request a fresh review |

The request deadline is 10 seconds; a model cold-start longer than that can fail.
A healthy provider should answer the System One contract at your exact URL. No path
is appended automatically, redirects are not followed, and chat-completions APIs do
not satisfy this contract. A container hostname must be reachable from the printer
host, not merely from another container. `localhost` refers to the Moonraker host.

Laya/Jev review is optional and experimental. The recorded live Laya trials
abstained even on a clean synthetic pair. Do not treat an uncertain answer as a
printer fault or lower the threshold simply to force advice. Measurement continues
working when review is off or the service is unavailable.

## Recordings or exports are missing

**Export CSV** contains the current displayed frequency series only. Raw samples
are separate files under `chordata/recordings/` in the config directory. Writes occur
in a background thread, so allow time for large files to finish. Chordata retains
40 raw CSV files; a background capture counts as a file too. Check `klippy.log` and
directory permissions if a completed recording is not saved.

## Updates appear to follow upstream instead of the fork

Inspect `git remote -v` in the checkout and `[update_manager chordata]` in
`moonraker.conf`. Both should refer to `https://github.com/alextverdyy/Chordata.git`
when following this fork. Existing update entries are not overwritten by the
installer. See [migration](USER_GUIDE.md#migrate-an-existing-upstream-install).
Preserve local changes before updating; do not force-reset them.

## Gather a useful issue report

Include:

- Klipper/Kalico, Moonraker and Chordata revisions, and your frontend version.
- Printer kinematics, accelerometer model and its mount location.
- The command or UI step, expected result and exact error.
- Relevant redacted `klippy.log` / `moonraker.log` excerpts.
- If measurement is the issue, the raw belt/background CSV pair and target/span
  details you are comfortable sharing.

Review files before attaching them. Do not share `chordata-ai.json`, provider keys
or the Moonraker API key. The [validation record](VALIDATION.md) lists which hardware
and provider behavior has not yet been verified.
