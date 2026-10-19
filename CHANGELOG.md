# Changelog

What changed in each update of Chordata's main branch, newest first.
Update from Mainsail's Update Manager, then restart Klipper.

## Unreleased (dev)

- **No false "good" beside a fan line:** where twice a belt's note sits on a steady line (a fan
  or motor line that wanders a few Hz, like 178-188 Hz on a Beacon), the reading could latch onto
  that line's skirt and read the same wrong number pluck after pluck (89.7 Hz for a belt the
  microphone heard at 90.9). Twice the note is now no evidence there, and a reading is always the
  top of a spectral line, never its slope. That belt now reads 91.0 Hz.
- **Average of the last plucks:** under the number, Tuner Mode shows the average of the last
  plucks that agree (up to 5, within twice the tolerance), so you can pluck and turn on and on
  and still see where the belt sits.
- **Each reading shows how far it can be trusted:** Tuner Mode and the history show **± Hz per
  pluck**, the 95 % interval of one reading from the scatter of the plucks just before it that
  lie close together (all within twice the tolerance, one after another, at most 5; from 3 plucks
  on). It is how much a belt's tone varies from pluck to pluck, not the engine's own bias. "On
  target" still goes by the pluck shown.
- **Smaller engine changes:** with a set number of plucks, the latest ones now count (a belt just
  tensioned still settles); the accelerometer's reliable range from its datasheet now limits
  where plucks and tones are looked for.
- **Pluck history in Tuner Mode:** the page lists the session's plucks, newest at the top, the
  refused ones greyed with the reason. **Reset history** clears it (also while listening), and so
  does a double tap on the toolhead. Needs a Moonraker restart once (its
  Chordata component has a new endpoint).
- **Calmer page:** no full-screen flashes any more; only the box around the number lights up for
  each pluck heard. The Done and Reset buttons no longer get rebuilt under your finger, and How it
  works is now seven short steps (the details behind More).
- **One way to measure: Tuner Mode.** Guided Mode and Pluck - Tap - Pluck Mode are gone. After
  the toolhead taps, Tuner Mode shows every clear pluck of either belt: turn the tensioners until
  both are on target and match. Simpler, and fewer refusals (no counting to 5, no switch tap).
- **Belt detection: back to the proven engine.** The fitted-tone engine tried on dev counted only
  about half the real plucks on a Nebula 370 and analysed 6-30 times slower (a Pi 5 couldn't keep
  up live); measured against a microphone, the earlier engine found 94 % of real plucks with no
  tap or knock counted on an ADXL345. Its taps now learn the toolhead's note from the note most
  taps ring, not the first tap's loudest line (a Beacon rings a higher mode first now and then).
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
