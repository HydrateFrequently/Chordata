# Changelog

What changed in each update of Chordata's main branch, newest first.
Update from Mainsail's Update Manager, then restart Klipper.

## Unreleased (dev)

- **New belt detection engine.** Each pluck's tones are fitted as decaying notes, so the belt's
  note is told apart from its 2F, the toolhead's ring, a fan's hum and the other belt even when
  they sit close together. In simulated tests a belt was called good at the wrong frequency in
  4 of 2,200 series, against 215 before; readings are about 5 times tighter.
- When the belt's note and twice it can't be told apart (a loose belt, a toolhead or frame note
  at half the belt's), the pluck is refused and you are asked to pluck again, instead of a guess.
- A quick second pluck, a knock or a toolhead tap is no longer counted as a belt pluck.
- **Fan off buttons:** when a fan hums near the belt's note, Chordata offers to turn it off for
  the measurement. Mainboard, hotend and other safety fans never get a button. Fans come back on
  when Chordata is closed, after 10 idle minutes, or when a print starts; in a print the part
  fan is left to the slicer.
- The analysis runs beside Klipper's main loop, so a slow host never holds up the printer.
- Fixed: fans named with capitals (`[fan_generic Bed_Fan]`) couldn't be turned off and showed
  as on; Chordata now uses each fan's own name.
- The hum check before the taps only stops for a hum within about 2.7 Hz of the belt's note.
  A steady hum further off, or near twice the note (like a stepper's holding buzz), no longer
  blocks the measurement.
- A hum near the belt's note from something Chordata can't turn off (the motors held, a
  controller fan, a fan without an off button) no longer stops the tap step: it is noted on the
  card and the taps are listened to. The step only stops while a fan with an off button runs.

## 2026-10-18

- Fixed: Kalico refused to load `chordata.cfg` ("Missing end of comment tag" in `_CHORDATA_BELTS`), so a printer
  with Chordata included didn't start. A `{# ... #}` comment in the macro was cut off at its closing `#` by the
  config parser; it is now a plain `#` comment line.

## 2026-10-17 (3)

- **Tuner Mode gives fewer false readings.** Two plucks close together could land in one
  measurement and show a wrong number, for example twice the belt's note or a toolhead note.
  Such a measurement is now refused as "two plucks at once". Belts that beat against each
  other (the other belt ringing along) are still read normally.
- A toolhead note close to twice the belt's note is now kept out of the reading more reliably.
- Tuner Mode only shows readings close to the target you set.

## 2026-10-17 (2)

- If the toolhead's note is higher than your accelerometer hears well, the tap result says so.

## 2026-10-17

- **New look on a PC:** Chordata opens as a pop-up like Mainsail's own calibration dialogs.
  Phones keep the large, hands-friendly layout.
- The phone screen stays on while Chordata is open.
- Fans and other steady hums are told apart from plucks and taps ("turn fans off" when one
  covers the belt's note). The background is recorded once per session.
- Toolhead setup: tap three times; check and cross marks show each tap. After nine bad taps
  Chordata suggests what to look for.
- Every listen stops by itself. Tuner Mode saves the whole session and stops a minute after
  the last pluck.
- New mode names: Guided, Pluck - Tap - Pluck, Tuner, Measure Toolhead Frequency.
  A "How it works" page explains each one.
- Set the target frequency in the box and press Go or Start; you don't have to press Set first.
- The setup button now just says "Start". Cue cards use clear colours and a short beep.
- Back and Close Chordata buttons; the graph only shows while measuring and on results.
- The printer-connection box only appears when Moonraker refuses the connection.
- Chordata refuses to measure while Printune is using the printer.
- New `uninstall.sh` removes everything the installer added and keeps your settings and
  recordings. The README covers install, update and uninstall.

## 2026-10-16

- First release: belt tension from plucks, measured with the toolhead accelerometer, as a
  Klipper/Kalico plugin with its own page in Mainsail.
- A toolhead tap is required before measuring, so the toolhead's own notes are ignored.
- A belt right next to a stiff toolhead's note can still be read.
