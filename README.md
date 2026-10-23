# Chordata

Accelerometer based belt tension measuring for Kalico and Klipper.

Pluck a belt like a guitar string. The accelerometer on the toolhead hears it, and Chordata shows the belt's
frequency, so you can tension both CoreXY belts to the same, known value. No phone app, no holding a microphone
next to the belt: the sensor is already on the printer, and you keep both hands free.

## How it works

Chordata is a page on your printer, made for a phone held next to it. The screens below are from a
virtual printer, so the numbers are made up; the page is the real one.

<img src="docs/images/setup.png" width="260" alt="Set Target Frequency: the target, the accelerometer, a jog pad to park the toolhead, Start">

**1. Set up.** Enter the target frequency your printer's documentation gives and park the toolhead where it
says to pluck (the jog pad moves it; the position can be saved for next time). Press **Start**.

<img src="docs/images/hands-off.png" width="260" alt="Red screen: Stand by, recording background noise">

**2. Hands off.** The screen turns red for a moment while Chordata records the background (fans, motors),
so it can tell a pluck from what is always there.

<img src="docs/images/tap.png" width="260" alt="Yellow screen: Tap your toolhead, one of three check marks">
<img src="docs/images/tapped.png" width="260" alt="3 good taps: the toolhead's note, and the Tuner Mode button">

**3. Tap.** The screen turns yellow: tap one solid spot on the toolhead lightly until 3 taps count (each
shows a check mark or an X). Chordata learns how the toolhead rings by itself: at which frequencies, how
long and how broad each line is. Toolheads differ a lot (a plastic one rings briefly in broad lines, a stiff
all-metal one rings long in sharp lines), so each line is guarded as wide as it is. A line the tap set
ringing on a belt is never learned as the toolhead's, nor is a knock's impulse. The taps can't be skipped:
the belts are only measured once 3 good taps were heard. Then the card shows the toolhead's note and the
**Tuner Mode** button.

<img src="docs/images/tuner.png" width="260" alt="Green screen, Tuner Mode: the last pluck's frequency, how far from target and which way to turn, the average of the last plucks, the history">

**4. Tune (Tuner Mode).** The screen turns green: pluck either belt. The stream is analysed as it comes, so
each clear pluck shows its frequency within about a second, how far it is from your target and which way
to turn. A weak pluck or a knock is left out, greyed in the history with why. A knock is told from a pluck
by physics: a plucked belt pulls on the toolhead twice per swing, a knock doesn't. Under the number: the
average of the last plucks that agree (up to 5, within 1 Hz of each other), and from 4 such plucks a
**± Hz per pluck** from their scatter. Turn the tensioner a little between plucks until the belt is on
target (within 0.25 Hz), then do the other belt. A listen stops by itself after 30 minutes; start it again
to go on.

<img src="docs/images/done.png" width="260" alt="After Done: the last pluck, the average, the whole history">

**5. Match.** CoreXY belts pull on each other, so turning one moves the other a little: pluck both once
more at the end. Done when both are on target, and so match each other. After **Done**, the card keeps the
last pluck, the average and the whole history.

The page keeps a history of the session's plucks, newest at the top. **Reset history** starts it over, and
so does a **double tap** on the toolhead (two taps up to 0.8 s apart), so you can start over without
touching the phone.

If a fan hums on the belt's note, Chordata says so and offers to turn it off for the measurement, but only
fans you have **allowed** (the card asks once per fan, and the choice is kept). Fans it recognises as cooling
the hotend, the stepper drivers or the electronics are never offered. Fans it turned off come back on when
you close Chordata, when a print starts, or after 10 idle minutes.

Where to put the toolhead, which stretch of belt to pluck and the target frequency all come from your
printer's own documentation. Chordata can save the toolhead position for next time.

The **[user guide](docs/USER_GUIDE.md)** walks through setup and a full measurement; the
**[troubleshooting guide](docs/TROUBLESHOOTING.md)** helps when something fails.

## Requirements

- A Raspberry Pi Zero 2 W or faster host (the Zero 2 W, 3B, 4B and 5 keep up with Tuner Mode; the
  original Pi Zero is not supported)
- Kalico or mainline Klipper, Moonraker, and Mainsail or Fluidd
- An accelerometer on the toolhead (ADXL345, LIS2DW, a probe with one built in) and `[resonance_tester]`
- `[respond]` in printer.cfg, and numpy in the Klipper Python environment (input shaper needs it too)
- A CoreXY printer (two belts that must match)

## Install

```
cd ~ && git clone https://github.com/HydrateFrequently/Chordata.git
~/Chordata/install.sh
sudo systemctl restart moonraker klipper
```

The installer links the plugin into Klipper (`klippy/extras`, or Kalico's `klippy/plugins`), adds
`[include chordata.cfg]` and `[include chordata_settings.cfg]` at the top of printer.cfg (with a backup), and
an update entry for Mainsail's update manager. It also adds the **Chordata page**, served by Moonraker (a small
component, `[chordata_web]`), with a link in Mainsail's sidebar. Open it on a phone at
`http://<printer>/server/chordata` and lay the phone near the printer: it shows big signal-colour cards for what
your hands do, each pluck live in big numbers against the target, and a pad to move the toolhead to
your belt tensioning position. The screen stays on while the page is open. On a PC the page looks like Mainsail's
own pop-ups. The same steps also run as prompts from the **CHORDATA** macro in Mainsail or Fluidd.

Settings (`chordata_settings.cfg`): `belt_target` (Hz, default 110; you can also change it on the first prompt)
and `belt_tolerance` (Hz, default 0.25). The fans you allowed Chordata to turn off are kept in
`chordata/fans_ok.json` in the config folder (delete it to take them all back). Every recording is saved in `chordata/recordings/` in the config
folder, so you can download it from Mainsail. `CHORDATA_RECORD SECONDS=10 NAME=test` records any accelerometer
(`CHIP=` as in Klipper's own commands) into the same folder.

## Update

Mainsail's (or Fluidd's) update manager shows Chordata next to Klipper and Moonraker; update it there. By hand:
`cd ~/Chordata && git pull && ./install.sh`, then restart Moonraker and Klipper.

## Uninstall

```
~/Chordata/uninstall.sh
sudo systemctl restart moonraker klipper
rm -rf ~/Chordata
```

The uninstaller removes the plugin links, the two includes in printer.cfg, the Chordata page and the update entry
in moonraker.conf, and the sidebar link, backing up each file it changes (`*.before-chordata-uninstall`). Your
`chordata_settings.cfg` and the recordings in `chordata/` stay; delete them by hand if you don't want them.

## How it was tested

- The pluck analysis was checked on real recordings from a CoreXY printer, with both a Beacon and an NH36 toolhead board equipped with an ADXL345 mounted to the extruder. A Zoom H5 recorder with the XYH-5 microphone provided a reference, read over the same stretch of each pluck as Chordata reads: each belt's average agreed within 0.21 Hz, and the difference between the two belts (what tensioning matches) within 0.16 Hz. Bad plucks should be rejected and not counted. A good pluck's tone (or twice the tone) must stand at least 12 dB above the background noise at that frequency, and the other at least 6 dB.

Interesting detail: the accelerometer mostly hears the belt at twice its frequency, so Chordata looks for
  the tone and twice the tone together.
- The real-time engine (Tuner Mode reads the stream as it comes) was checked on five Tuner Mode sessions
  recorded on that printer, each against the microphone: about two thirds of the counted plucks read within
  0.5 Hz of the microphone and over half within 0.25 Hz, with no toolhead tap counted as a pluck. The same
  recordings replayed through the whole plugin on a virtual printer gave the same counts, and readings within
  0.02 Hz, as the engine run on them directly. Every engine change is judged on these real recordings; synthetic benches
  (a belt with a second line beside it, a sensor where the tone dominates, knocks, clipped knocks, a stiff
  toolhead) may only veto a change, never set a threshold.
- The same module ran on that real printer as part of Printune (Kalico), measuring both belts.
- As a standalone plugin forked from Printunes belt tension module, Chordata was installed with install.sh on virtual printers (real Kalico and real
  mainline Klipper with a simulated MCU and a simulated accelerometer) and run end to end, with bad plucks
  and double taps mixed in on purpose. It also runs as a standalone plugin on my own printer (a CoreXY on
  Kalico, ADXL345), where Tuner Mode, the history, Reset and the double tap were tried out.
- `tests/test_belts.py` checks the pluck judge on synthetic recordings (and on real ones, if you have them).
- `python3 -m unittest discover -s tests` checks the printer interlocks (no measuring during a print or while
  another measurement runs), input checks, cleanup after errors, the pluck history and double tap, that no
  fan is turned off without your OK, installer reruns and the uninstaller. `npm test` checks the page
  in a browser (demo, live updates, CSV export, phone and PC layouts). GitHub Actions runs both.
- Speed and stability were checked on a Pi 5 (a 4-minute Tuner Mode session costs about 11 s of CPU) and
  on emulated Raspberry Pis from the Zero to the Pi 5 (CPU-quota containers), where the number shows about
  0.7 s after the pluck on every model.

## Branches

- `main` is what you install, and what Mainsail's update manager updates from. It only changes through
  reviewed pull requests from `dev`.
- `dev` is where work happens. It may be broken at any time. To try it anyway, on the printer:
  `cd ~/Chordata && git fetch && git checkout dev && ./install.sh`, then restart Moonraker and Klipper.
  The installer points Mainsail's update entry at the branch you have checked out, so updates then come
  from `dev`. Go back the same way with `git checkout main`.
- `ai-review` is a separate version with optional AI review of the measurements.

Pull requests go to `dev`, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Origin

Chordata started as the belt tension step of [Printune](https://github.com/HydrateFrequently/Printune), a
tuning wizard for Kalico/Klipper, and is now available on its own. The live chart, the measurement table, the
safety interlocks, the installer hardening, the guides and the test suite were contributed by
[alextverdyy](https://github.com/alextverdyy). An optional AI measurement review lives on the
[`ai-review`](https://github.com/HydrateFrequently/Chordata/tree/ai-review) branch; `main` makes no AI decisions.

## Disclaimer

Chordata was written entirely with an AI assistant ("vibecoded"). It comes without any warranty
(GPL-3.0): you use it at your own risk, and no responsibility is accepted for anything that happens to your
printer. Chordata only measures; it never prints, and it moves the toolhead only when you press one of its
move buttons (Home, the arrows, Move for a typed X/Y, or Go to belt tensioning position).

License: GPL-3.0-or-later.
