# Chordata

Accelerometer based belt tension measuring for Kalico and Klipper.

Pluck a belt like a guitar string. The accelerometer on the toolhead hears it, and Chordata shows the belt's
frequency, so you can tension both CoreXY belts to the same, known value. No phone app, no holding a microphone
next to the belt: the sensor is already on the printer, and you keep both hands free.

> **Status: very early in development.** Chordata was written entirely with an AI assistant ("vibecoded").
> It comes without any warranty (GPL-3.0): you use it at your own risk, and no responsibility is accepted for
> anything that happens to your printer. Chordata only measures; it never prints, and it moves the toolhead
> only when you press "Go to belt tensioning position".

## What it does

1. **Tap.** You tap the toolhead once. Chordata learns how the toolhead rings by itself: at which frequencies,
   how long and how broad each line is. Toolheads differ a lot (a plastic one rings briefly in broad lines,
   a stiff all-metal one rings long in sharp lines), so each line is guarded as wide as it is, and plucks
   that come while a long-ringing toolhead still rings from a knock are still found. A knock is never taken
   for a belt.
2. **Pluck.** You pluck belt A, then belt B, about once a second. Each pluck is judged: loud enough, not a
   knock, not the other belt ringing along. Bad plucks are left out, and Chordata keeps listening until
   5 good plucks are counted ("3 of 5 counted").
3. **Read and adjust.** For each belt you get its frequency, how far it is from your target, and which way to
   turn. CoreXY belts pull on each other, so both are measured again after every adjustment. A belt is good
   when the average of 5 good plucks is within 0.25 Hz of the target.

Where to put the toolhead, which stretch of belt to pluck and the target frequency all come from your
printer's own documentation. Chordata can save the toolhead position for next time.

## Requirements

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
`http://<printer>/server/chordata`: it shows each pluck live in big numbers, both belts against the target, and a
pad to move the toolhead to your belt tensioning position. The same steps also run as prompts from the
**CHORDATA** macro in Mainsail or Fluidd.

Settings (`chordata_settings.cfg`): `belt_target` (Hz, default 110; you can also change it on the first prompt)
and `belt_tolerance` (Hz, default 0.25). Every recording is saved in `chordata/recordings/` in the config
folder, so you can download it from Mainsail. `CHORDATA_RECORD SECONDS=10 NAME=test` records any accelerometer
(`CHIP=` as in Klipper's own commands) into the same folder.

## How it was tested

- The pluck analysis was checked on real recordings from a CoreXY printer, with both a Beacon and an NH36 toolhead board equipped with an ADXL345 mounted to the extruder. A Zoom H5 recorder with the XYH-5 microphone provided a reference: the averages agreed within 0.0 to 0.13 Hz. Bad plucks should be rejected and not counted. A good pluck needs to be at least 12db SINAD.

Interesting detail: the accelerometer mostly hears the belt at twice its frequency, so Chordata looks for
  the tone and twice the tone together.
- The same module ran on that real printer as part of Printune (Kalico), measuring both belts.
- As a standalone plugin forked from Printunes belt tension module, Chordata was installed with install.sh on virtual printers (real Kalico and real
  mainline Klipper with a simulated MCU and a simulated accelerometer) and run end to end, with bad plucks
  mixed in on purpose. **It has not yet been run on a physical printer as a standalone plugin.** However, printunes belt tension module has been tested on my own printer.
- `tests/test_belts.py` checks the pluck judge on synthetic recordings (and on real ones, if you have them).

## Origin

Chordata started as the belt tension step of [Printune](https://github.com/HydrateFrequently/Printune), a
tuning wizard for Kalico/Klipper, and is now available on its own.

License: GPL-3.0-or-later.
