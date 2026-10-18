# Chordata: belt tension from plucks, measured with the toolhead accelerometer.
#
# A Klipper/Kalico plugin. Pluck a belt like a guitar string; the accelerometer on the
# toolhead hears it, and Chordata shows its frequency, judges each pluck (loud enough,
# not a knock on the toolhead, not the other belt) and averages the good ones. CoreXY
# belts pull on each other, so both are measured after every adjustment.
#
# Install: install.sh (links this file and chordata_belts.py into Klipper's extras, or
# Kalico's plugins, and includes chordata.cfg). Needs an accelerometer and
# [resonance_tester] in printer.cfg, and numpy in the klippy environment.
#
# Started as the belt module of Printune (https://github.com/HydrateFrequently/Printune).
# Copyright (C) 2026  Chordata contributors
# This file may be distributed under the terms of the GNU GPLv3 license.

import json
import logging
import math
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


class Chordata:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.cfg_dir = os.path.dirname(os.path.expanduser(
            self.printer.get_start_args()["config_file"]))
        self.out_dir = os.path.expanduser(
            config.get("path", os.path.join(self.cfg_dir, "chordata")))
        self.belt_tol = config.getfloat("belt_tolerance", 0.25, above=0.)   # Hz, average of 5 plucks
        self.belt_target = config.getfloat("belt_target", 110., minval=40., maxval=300.)  # as the setup prompt allows
        self.state, self.message = "idle", ""
        self.belts = {}           # the belt tension session, for the prompts
        self._quiet = None        # the background (fans, motors), recorded at the tap step
        self.belt_now = None      # the belt being listened to ("tap" while learning the toolhead)
        self.belt_plucks = 5
        self.belt_live = []       # plucks heard so far while listening
        self.belt_hum = []        # a fan humming on the belt's note in the newest analysis (Hz)
        self.tap_live = []        # the setup taps so far, each good or not
        self.measurement = {"phase": "idle", "belt": None}
        self._fans_off = {}       # fans Chordata turned off this session: section -> speed before
        self._fans_timer = None   # brings them back FANS_IDLE s after Chordata was last used
        self._fans_until = 0.
        self._tool_info = None
        self._accept = False
        webhooks = self.printer.lookup_object("webhooks", None)
        if webhooks is not None:
            webhooks.register_endpoint("chordata/accept", self._handle_accept)
        for name in ("START", "LISTEN", "BOTH", "FREE", "TAP", "POS", "FAN", "FANS"):
            fn = getattr(self, "cmd_CHORDATA_" + name)
            self.gcode.register_command("_CHORDATA_" + name, fn,
                                        desc=getattr(self, "cmd_CHORDATA_%s_help" % name))
        self.gcode.register_command("CHORDATA_RECORD", self.cmd_CHORDATA_RECORD,
                                    desc=self.cmd_CHORDATA_RECORD_help)

    def get_status(self, eventtime):
        return {"state": self.state, "message": self.message, "tools": self._tools(), "accel": self._accel(),
                "belts": self.belts, "belt_target": self.belt_target, "belt_tol": self.belt_tol,
                "belt_now": self.belt_now if self.belt_now in ("A", "B", "free") else None,
                "belt_live": self.belt_live, "belt_hum": getattr(self, "belt_hum", []), "tap_live": getattr(self, "tap_live", []),
                "belt_pos": self._belt_pos(),
                "measurement": dict(self.measurement, elapsed=round(max(
                    0., eventtime - self.measurement["started_at"]), 1))
                if self.measurement.get("phase") in ("starting", "background", "listening", "analysing", "recording")
                else self.measurement}

    def _require_idle(self, gcmd):
        """Direct console commands must obey the same interlocks as the web UI."""
        if self.state != "idle":
            raise gcmd.error("Chordata: another measurement is running")
        # Printune on the same printer: two recordings at once would hear each other's plucks
        other = self.printer.lookup_object("printune", None)
        if getattr(other, "state", None) in ("listening", "shaper", "soaking"):
            raise gcmd.error("Chordata: Printune is using the printer, wait until it is done")
        now = self.reactor.monotonic()
        stats = self.printer.lookup_object("print_stats", None)
        if stats is not None and stats.get_status(now).get("state") in ("printing", "paused"):
            raise gcmd.error("Chordata: finish or cancel the print before belt maintenance")
        sd = self.printer.lookup_object("virtual_sdcard", None)
        if sd is not None and sd.get_status(now).get("is_active"):
            raise gcmd.error("Chordata: finish or cancel the print before belt maintenance")
        paused = self.printer.lookup_object("pause_resume", None)
        if paused is not None and paused.get_status(now).get("is_paused"):
            raise gcmd.error("Chordata: finish or cancel the print before belt maintenance")
        idle = self.printer.lookup_object("idle_timeout", None)
        if stats is None and idle is not None and idle.get_status(now).get("state") == "Printing":
            raise gcmd.error("Chordata: wait for the printer to become idle")

    def _begin_measurement(self, belt, expected=None, phase="background"):
        self._fans_touch()
        self.state = "listening"
        self.belt_live, self.belt_hum = [], []
        self.belt_now = None
        self._accept = False
        self.measurement = {"phase": phase, "belt": belt, "expected": expected,
                            "started_at": self.reactor.monotonic(), "elapsed": 0.,
                            "accepted": 0, "heard": 0, "result": None, "error": None}

    def _measurement_phase(self, phase):
        self.measurement = dict(self.measurement, phase=phase)

    def _end_measurement(self, result=None, error=None):
        self._fans_touch()
        self.measurement = dict(self.measurement, phase="error" if error else "complete",
                                elapsed=round(max(0., self.reactor.monotonic()
                                                  - self.measurement["started_at"]), 1),
                                accepted=(result or {}).get("used", 0),
                                heard=(result or {}).get("heard", 0), result=result,
                                error=str(error) if error else None)
        self.belt_now = None
        self.state, self.message = "idle", ""

    def _tools(self):
        # what the printer has: fixed once klippy is ready
        if self._tool_info is None:
            kin = ""
            try:
                kin = self.printer.lookup_object("configfile").get_status(
                    self.reactor.monotonic())["settings"]["printer"]["kinematics"]
            except Exception:
                pass
            look = self.printer.lookup_object
            self._tool_info = {
                "accel": look("resonance_tester", None) is not None,
                "shaketune": look("shaketune", None) is not None,
                "corexy": kin in ("corexy", "limited_corexy", "corexz", "limited_corexz"),
                "kinematics": kin}
        return self._tool_info

    # What each accelerometer can hear as Klipper and Kalico set it up: the upper limit is the
    # smaller of the datasheet's -3 dB bandwidth at the configured rate and 0.45 x that rate.
    # The lower limit is 0 Hz for all of them (DC-coupled, no high-pass filter turned on).
    #   adxl345: bandwidth rate/2 (default rate 3200): 0.45 x rate, 1440 Hz
    #   lis2dw: 1600 Hz, high-performance, BW_FILT=00: 400 Hz anti-alias filter (ST AN5038 table 12)
    #   lis3dh: 1344 Hz in high-resolution mode: ODR/9, 149 Hz (ST AN3308 table 3)
    #   mpu9250 (MPU-9250/9255/6500/6515): 4 kHz, DLPF bypassed: 1130 Hz; an MPU-6050 on the same
    #     driver runs at 1 kHz with a 184 Hz filter (told apart by its sample rate)
    #   icm20948: 4.5 kHz, filter bypassed: 1209 Hz
    #   bmi160: 1600 Hz, normal filter: 684 Hz in X and Y, 353 Hz in Z (the lower is used)
    #   beacon: chip and filter not published: 0.45 x its measured sample rate
    ACCELS = {"adxl345": "ADXL345", "lis2dw": "LIS2DW12", "lis3dh": "LIS3DH", "mpu9250": "MPU-9250",
              "icm20948": "ICM-20948", "bmi160": "BMI160", "beacon": "Beacon"}
    ACCEL_BW = {"lis2dw": 400., "lis3dh": 149., "mpu9250": 1130., "icm20948": 1209., "bmi160": 353.}

    def _accel(self):
        """The accelerometer [resonance_tester] measures with (the one Chordata records), and the
        range of frequencies it hears reliably: name, lo, hi (None: not known until it has
        recorded once)."""
        if getattr(self, "_accel_cfg", None) is None:      # the config doesn't change while Kalico runs
            section, rate = "", 3200.
            try:
                st = self.printer.lookup_object("configfile").get_status(self.reactor.monotonic())["settings"]
                rt = st.get("resonance_tester") or {}
                names = rt.get("accel_chips") or rt.get("accel_chip_x") or rt.get("accel_chip") or ""
                section = str(names).split(",")[0].strip().lower()
                rate = float((st.get(section) or {}).get("rate", 3200))
            except Exception:
                return None                          # not ready yet: asked again next time
            self._accel_cfg = (section, rate)
        section, rate = self._accel_cfg
        if not section:
            return None
        kind = section.split()[0]
        fs = getattr(self, "_accel_fs", None)
        name, hi = self.ACCELS.get(kind, section.upper()), self.ACCEL_BW.get(kind)
        if kind == "adxl345":
            hi = 0.45 * rate
        elif kind == "mpu9250" and fs and fs < 2000.:
            name, hi = "MPU-6050", 184.             # the 1 kHz chip on the same driver
        elif hi is None and fs:
            hi = 0.45 * fs                          # not published: what its sample rate allows
        return {"name": name, "lo": 0, "hi": None if hi is None else int(hi)}

    def _analyse(self, *cols, **kw):
        """The belt engine's analyse(), told how high the accelerometer hears (a pluck's
        tone at twice its note is then only looked for where the sensor can hear it)."""
        import chordata_belts as belts
        acc = self._accel()
        return self._off_reactor(belts.analyse, *cols, hears=acc["hi"] if acc else None, **kw)

    def _off_reactor(self, fn, *args, **kw):
        """fn(*args, **kw), in a thread when called on Kalico's own (reactor) thread, which
        keeps running meanwhile: an analysis can take seconds on a small Pi, and a reactor
        held that long stops the heaters' updates (an MCU shutdown after 3 s)."""
        pause = getattr(self.reactor, "pause", None)
        if pause is None or threading.current_thread() is not threading.main_thread():
            return fn(*args, **kw)          # (the live analysis: already in its own thread)
        out = {}

        def work():
            try:
                out["r"] = fn(*args, **kw)
            except BaseException as error:
                out["e"] = error
        t = threading.Thread(target=work, daemon=True)
        t.start()
        while t.is_alive():
            pause(self.reactor.monotonic() + 0.05)
        if "e" in out:
            raise out["e"]
        return out["r"]

    def _check_target(self, gcmd, target):
        """A target the accelerometer can't hear gives wrong or no readings: refuse it. Checked
        at the start and again before each measure (an MPU-6050 or a Beacon is only known once
        it has recorded)."""
        acc = self._accel()
        if acc and acc["hi"] is not None and target > acc["hi"]:
            raise gcmd.error("Chordata: error frequency target out of range: the %s hears 0-%d Hz"
                             % (acc["name"], acc["hi"]))

    # ---------------- belt tension from plucks ----------------
    cmd_CHORDATA_START_help = "Chordata: start belt tensioning (TARGET=Hz)"

    def cmd_CHORDATA_START(self, gcmd):
        self._require_idle(gcmd)
        if self.printer.lookup_object("resonance_tester", None) is None:
            raise gcmd.error("Chordata: belt tension needs an accelerometer ([resonance_tester])")
        if not self._tools()["corexy"]:
            raise gcmd.error("Chordata: the belt step is made for CoreXY printers")
        target = gcmd.get_float("TARGET", self.belt_target, minval=20., maxval=400.)
        if not math.isfinite(target):
            raise gcmd.error("Chordata: TARGET must be finite")
        self._check_target(gcmd, target)
        acc = self._accel()
        if acc and acc["hi"] is not None and 2 * target > acc["hi"]:
            gcmd.respond_info("Chordata: the %s hears up to %d Hz, less than twice the target: a pluck is "
                              "told from a knock by the taps learned and by how long it rings. A knock on the frame that "
                              "rings long near the target can still read as the belt: pluck the belt, don't tap the frame"
                              % (acc["name"], acc["hi"]))
        plucks = 5      # on target: the average of 5 good plucks
        self.belt_plucks = plucks
        self.belts = {"target": target, "plucks": plucks, "round": 0,
                      "A": None, "B": None, "heard": [], "advice": "Measure belt A", "done": False,
                      "tol": self.belt_tol, "tap": [], "tap_ring": [], "tap_tau": None, "tap_done": False, "fails": {"A": 0, "B": 0}, "deaf": False,
                      "possible": False, "good": {}, "prev": {}}
        self.belt_live = []
        self._restore_fans()                 # (a session closed without Close Chordata)
        import chordata_belts as belts
        belts.clear_cache()
        self._quiet = None                   # the background, recorded once per session at the tap step
        self.measurement = {"phase": "idle", "belt": None}
        gcmd.respond_info("Chordata: belt tension, target %.0f Hz, %d plucks per belt" % (target, plucks))

    cmd_CHORDATA_LISTEN_help = "Chordata: record plucks of one belt (BELT=A|B) and find its frequency"

    def cmd_CHORDATA_LISTEN(self, gcmd):
        self._require_idle(gcmd)
        try:
            self._belt_listen(gcmd)
        except Exception as error:
            if self.state != "idle":
                self._end_measurement(error=error)
            if gcmd.get_int("UI", 0):
                # don't leave a "pluck now" prompt up without a button
                self.gcode.respond_raw("// action:prompt_end")
            raise

    def _belt_listen(self, gcmd):
        import chordata_belts as belts      # numpy: only needed here
        if not self.belts:
            raise gcmd.error("Chordata: run _CHORDATA_START first")
        belt = gcmd.get("BELT", "A").upper()
        if belt not in ("A", "B"):
            raise gcmd.error("Chordata: BELT is A or B")
        if not self.belts.get("tap_done"):
            # without it a stiff toolhead's ringing can be taken for a belt
            raise gcmd.error("Chordata: tap the toolhead first (_CHORDATA_TAP)")
        self._check_target(gcmd, self.belts["target"])
        rt = self.printer.lookup_object("resonance_tester")
        chips = getattr(rt, "accel_chips", None) or []
        if not chips:
            raise gcmd.error("Chordata: no accelerometer found")
        chip = chips[0][1]
        n = self.belts["plucks"]
        secs = gcmd.get_float("SECONDS", 2.0 + 1.3 * n, minval=2., maxval=30.)
        if not math.isfinite(secs):
            raise gcmd.error("Chordata: SECONDS must be finite")
        toolhead = self.printer.lookup_object("toolhead")
        self._begin_measurement(belt, n, phase="starting" if getattr(self, "_quiet", None) else "background")
        # An attempt supersedes this belt's old reading even if capture fails.
        self.belts = dict(self.belts, **{belt: None, "done": False, "belt_good": {"A": False, "B": False},
                                       "heard": [b for b in self.belts.get("heard", []) if b != belt]})
        toolhead.wait_moves()
        try:
            # the session's background (fans, motors), so they aren't taken for the belt
            self.belt_live = []
            quiet = self._record_quiet(chip, toolhead)
            self.belt_now = belt
            self._measurement_phase("listening")
            self.message = "Pluck belt %s until %d are counted" % (belt, n)
            if gcmd.get_int("UI", 0):
                self.gcode.run_script_from_command("_CHORDATA_UI STEP=pluck BELT=%s" % belt)
            live = dict(quiet=[[s[i] for s in quiet] for i in (1, 2, 3)], expect=n,
                        avoid=self.belts.get("tap_ring") or self.belts.get("tap", []), target=self.belts["target"],
                        prev=(self.belts.get("good") or {}).get(belt),
                        other=(self.belts.get("B" if belt == "A" else "A") or {}).get("freq"))
            samples = self._record(chip, toolhead, secs, live=live)
        finally:
            self.belt_now = None
            self._measurement_phase("analysing")
        if not samples:
            raise gcmd.error("Chordata: the accelerometer sent no data")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._save_recording("belt%s-%s" % (belt, stamp), samples)
        other = (self.belts.get("B" if belt == "A" else "A") or {}).get("freq")
        good = dict(self.belts.get("good") or {})
        try:
            res = self._analyse(*self._cols(samples), target=self.belts["target"], expect=n,
                                quiet=[[s[i] for s in quiet] for i in (1, 2, 3)],
                                avoid=self.belts.get("tap_ring") or self.belts.get("tap", []), other=other, prev=good.get(belt))
        except ValueError as error:
            raise gcmd.error("Chordata: invalid accelerometer recording: %s" % error)
        self._apply_belt(belt, res)
        self._end_measurement(result=res)
        self._report_belt(gcmd, belt, res)

    @staticmethod
    def _cols(samples):
        return ([s[0] for s in samples], [s[1] for s in samples], [s[2] for s in samples],
                [s[3] for s in samples])

    def _apply_belt(self, belt, res):
        """A belt's final reading into the session: the previous good one, failures, the
        A/B round and the advice once both were heard."""
        import chordata_belts as belts
        good = dict(self.belts.get("good") or {})
        res["measured_at"] = time.time()
        res["round"] = self.belts["round"] + 1
        self.belt_live = res["plucks"]
        self.belt_hum = res.get("hum") or []
        b = dict(self.belts)                 # a new object: status updates send what changed
        b[belt] = res
        if res["freq"] is not None:
            b["prev"] = dict(b.get("prev") or {}, **({belt: good[belt]} if belt in good else {}))
            good[belt] = res["freq"]
            b["good"] = good
            b["possible"] = True
        # a sensor that can't hear the belts at all: say so instead of guessing
        fails = dict(b.get("fails") or {"A": 0, "B": 0})
        # a failed series: not one clear pluck in it (plucks that only disagree don't count)
        clear = bool(res.get("hum")) or any(
            p["ok"] or p["why"] not in ("no belt tone", "too weak", "a knock, not a belt", "steady hum, not a pluck")
            for p in res["plucks"])
        fails[belt] = 0 if clear else fails.get(belt, 0) + 1
        b["fails"] = fails
        ever = b.get("ever") or res["freq"] is not None
        b["ever"] = ever
        b["deaf"] = not ever and fails[belt] >= 2
        heard = set(b.get("heard", [])) - {belt}
        if res["freq"] is not None:
            heard.add(belt)
        heard = sorted(heard)
        if heard == ["A", "B"]:
            # both belts measured since the last instruction: the next one
            b["round"] += 1
            b["advice"], upd = belts.plan(b)
            b.update(upd)
            heard = []
        else:
            other = "B" if belt == "A" else "A"
            b["advice"] = ("Pluck belt %s again" % belt if res["freq"] is None
                           else "Now measure belt %s" % other)
        b["heard"] = heard
        self.belts = b

    def _report_belt(self, gcmd, belt, res):
        for p in res["plucks"]:
            if not p["ok"]:
                gcmd.respond_info("Chordata: belt %s, pluck at %.1f s left out: %s" % (belt, p["t"], p["why"]))
        if res["freq"] is None:
            gcmd.respond_info("Chordata: belt %s: %s" % (belt, res["note"]))
        else:
            gcmd.respond_info("Chordata: belt %s: %.1f Hz (%s)" % (belt, res["freq"], res["note"]))

    cmd_CHORDATA_BOTH_help = ("Chordata: both belts in one listen: pluck belt A until it is counted, "
                              "tap the toolhead, then pluck belt B")

    def cmd_CHORDATA_BOTH(self, gcmd):
        self._require_idle(gcmd)
        try:
            self._belt_both(gcmd)
        except Exception as error:
            if self.state != "idle":
                self._end_measurement(error=error)
            if gcmd.get_int("UI", 0):
                self.gcode.respond_raw("// action:prompt_end")
            raise
        finally:
            self.measurement = dict(self.measurement, step=None)

    def _belt_both(self, gcmd):
        """Belt A, a tap on the toolhead as the sign to switch, then belt B, in one
        recording. The tap is found the way knocks are left out of a series (the toolhead
        rings at what the first tap taught)."""
        import chordata_belts as belts
        if not self.belts:
            raise gcmd.error("Chordata: run _CHORDATA_START first")
        if not self.belts.get("tap_done"):
            raise gcmd.error("Chordata: tap the toolhead first (_CHORDATA_TAP)")
        self._check_target(gcmd, self.belts["target"])
        rt = self.printer.lookup_object("resonance_tester")
        chips = getattr(rt, "accel_chips", None) or []
        if not chips:
            raise gcmd.error("Chordata: no accelerometer found")
        chip = chips[0][1]
        n = self.belts["plucks"]
        secs = gcmd.get_float("SECONDS", 60., minval=10., maxval=120.)
        if not math.isfinite(secs):
            raise gcmd.error("Chordata: SECONDS must be finite")
        ui = gcmd.get_int("UI", 0)
        toolhead = self.printer.lookup_object("toolhead")
        self._begin_measurement("both", n, phase="starting" if getattr(self, "_quiet", None) else "background")
        good = dict(self.belts.get("good") or {})
        self.belts = dict(self.belts, A=None, B=None, done=False, heard=[],
                          belt_good={"A": False, "B": False})
        avoid = self.belts.get("tap_ring") or self.belts.get("tap", [])
        tgt = self.belts["target"]
        margin = belts.SKIP + belts.WINDOW + 0.05
        st = {"step": "A", "a_end": None, "b_start": None, "a_freq": None, "done": False, "tap_at": None}
        self._both = st
        toolhead.wait_moves()
        try:
            quiet = self._record_quiet(chip, toolhead)
            q = [[s[i] for s in quiet] for i in (1, 2, 3)]
            self.belt_now = "A"
            self._measurement_phase("listening")
            self.measurement = dict(self.measurement, step="A")
            self.message = "Pluck belt A until %d are counted" % n
            if ui:
                self.gcode.run_script_from_command("_CHORDATA_UI STEP=bothpluck")

            main = self.belts.get("tap_main")

            def switch(seg, plucks, i):
                """Was event i the tap that switches to belt B? With the toolhead's note learned at
                setup: a knock where that note is louder than belt A's. Without one (a
                stiff toolhead): anything that isn't one more pluck of belt A."""
                p, fa = plucks[i], st["a_freq"]
                # belt A on the toolhead's own note: the notes can't be told apart, so fall back too
                if not main or (fa is not None and abs(fa - main["freq"]) <= main["guard"] + belts.SPREAD):
                    return not (p["ok"] and fa is not None and abs(p["freq"] - fa) <= belts.SPREAD)
                nxt = plucks[i + 1]["t"] if i + 1 < len(plucks) else None
                return self._off_reactor(belts.toolhead_knock, *self._cols(seg), p["t"], main, fa, quiet=q, until=nxt)[0]

            def knock_after(samples, t):
                """The first tap on the toolhead after t: where belt B starts (half a second
                after it: the phone buzzes then), or None."""
                seg = [x for x in samples if x[0] >= t]
                if len(seg) < 200:
                    return None
                res = self._analyse(*self._cols(seg), quiet=q, avoid=avoid, target=tgt, free=True)
                taps = [p for i, p in enumerate(res["plucks"]) if switch(seg, res["plucks"], i)]
                return seg[0][0] + taps[0]["t"] + 0.5 if taps else None

            def live(samples):
                # runs about once a second, in a thread: the step the user is on, and the plucks so far
                end = samples[-1][0]
                if st["step"] == "A":
                    t0 = samples[0][0]
                    res = self._analyse(*self._cols(samples), quiet=q, avoid=avoid, target=tgt,
                                        expect=n, prev=good.get("A"))
                    if st.get("final"):         # a late live result can't change the final result
                        return
                    self.belt_live = res["plucks"]
                    self.belt_hum = res.get("hum") or []
                    self.measurement = dict(self.measurement, accepted=res["used"], heard=res["heard"])
                    if self._enough(n, res["plucks"], end - t0):
                        ok = [p for p in res["plucks"] if p["ok"]]
                        st["a_end"] = t0 + ok[min(len(ok), n) - 1]["t"] + margin
                        st["a_freq"] = res["freq"]
                        if res["freq"] is not None:     # A's card shows it while B is plucked
                            self.belts = dict(self.belts, A=res)
                        st["step"], st["tap_at"] = "tap", end
                        self.belt_live = []
                        self.measurement = dict(self.measurement, step="tap", accepted=0, heard=0)
                        self.message = "Tap the toolhead, then pluck belt B"
                elif st["step"] == "tap":
                    b_start = knock_after(samples, st["a_end"])
                    if st.get("final"):
                        return
                    st["b_start"] = b_start
                    if st["b_start"] is not None:
                        st["step"] = "B"
                        self.belt_now = "B"
                        self.belt_live = []
                        self.measurement = dict(self.measurement, step="B")
                        self.message = "Pluck belt B until %d are counted" % n
                    elif end - st["tap_at"] > 8.0:
                        self.message = "No tap heard yet: tap the toolhead a little firmer"
                else:
                    seg = [x for x in samples if x[0] >= st["b_start"]]
                    if len(seg) < 200:
                        return
                    res = self._analyse(*self._cols(seg), quiet=q, avoid=avoid, target=tgt,
                                        expect=n, prev=good.get("B"), other=st["a_freq"])
                    if st.get("final"):
                        return
                    self.belt_live = res["plucks"]
                    self.belt_hum = res.get("hum") or []
                    self.measurement = dict(self.measurement, accepted=res["used"], heard=res["heard"])
                    if self._enough(n, res["plucks"], end - seg[0][0]):
                        st["done"] = True

            samples = self._record(chip, toolhead, secs, live=live, stop=lambda: st["done"])
            st["final"] = True
        finally:
            st["final"] = True
            self.belt_now = None
            self._measurement_phase("analysing")
        if not samples:
            raise gcmd.error("Chordata: the accelerometer sent no data")
        self._save_recording("both-%s" % time.strftime("%Y%m%d-%H%M%S"), samples)
        kw = dict(quiet=q, avoid=avoid, target=tgt, expect=n)
        try:
            # a slow host may not have kept up live: the switch is looked for again in all of it
            if st["a_end"] is None:
                res = self._analyse(*self._cols(samples), quiet=q, avoid=avoid, target=tgt, free=True)
                for i, p in enumerate(res["plucks"]):
                    before = [x["freq"] for x in res["plucks"][:i] if x["ok"]]
                    if len(before) >= belts.MIN_GOOD:
                        st["a_freq"] = float(sorted(before)[len(before) // 2])
                        if switch(samples, res["plucks"], i):
                            st["a_end"] = samples[0][0] + p["t"]
                            st["b_start"] = st["a_end"] + 0.5
                            break
            if st["a_end"] is not None and st["b_start"] is None:
                st["b_start"] = knock_after(samples, st["a_end"])
            seg = [x for x in samples if st["a_end"] is None or x[0] < st["a_end"]]
            ra = self._analyse(*self._cols(seg), prev=good.get("A"), **kw)
            if st["b_start"] is not None:
                seg = [x for x in samples if x[0] >= st["b_start"]]
                rb = (self._analyse(*self._cols(seg), prev=good.get("B"), other=ra["freq"], **kw)
                      if len(seg) > 200 else None)
            else:
                rb = None
        except ValueError as error:
            raise gcmd.error("Chordata: invalid accelerometer recording: %s" % error)
        if rb is None:
            rb = {"freq": None, "verdict": "none", "used": 0, "heard": 0, "plucks": [], "spread": None,
                  "stddev": None, "metadata": {}, "hum": [],
                  "note": ("no tap heard, so belt B wasn't listened to" if st["a_end"] is not None
                           else "belt A wasn't counted, so belt B wasn't listened to")}
        self._apply_belt("A", ra)
        self._apply_belt("B", rb)
        self._end_measurement(result=rb)
        self._report_belt(gcmd, "A", ra)
        self._report_belt(gcmd, "B", rb)

    cmd_CHORDATA_FREE_help = ("Chordata: Tuner Mode: show every pluck's frequency, with no counting and no "
                              "belt A/B steps, until a minute after the last pluck, Done on the page, or SECONDS")
    FREE_MAX = 600.     # s: a free tune nobody ends from the page stops by itself after this
    FREE_PIECE = 8.     # s: recorded and analysed in pieces this long
    FREE_IDLE = 60.     # s: with no pluck heard for this long, a free tune ends by itself

    def cmd_CHORDATA_FREE(self, gcmd):
        self._require_idle(gcmd)
        try:
            self._belt_free(gcmd)
        except Exception as error:
            if self.state != "idle":
                self._end_measurement(error=error)
            if gcmd.get_int("UI", 0):
                self.gcode.respond_raw("// action:prompt_end")
            raise

    def _belt_free(self, gcmd):
        """For a good ear: pluck either belt and read each pluck's
        frequency. Nothing is averaged and the A/B session is left as it was."""
        import chordata_belts as belts
        if not self.belts:
            raise gcmd.error("Chordata: run _CHORDATA_START first")
        if not self.belts.get("tap_done"):
            raise gcmd.error("Chordata: tap the toolhead first (_CHORDATA_TAP)")
        self._check_target(gcmd, self.belts["target"])
        rt = self.printer.lookup_object("resonance_tester")
        chips = getattr(rt, "accel_chips", None) or []
        if not chips:
            raise gcmd.error("Chordata: no accelerometer found")
        chip = chips[0][1]
        # no time limit by default: it listens until FREE_IDLE after the last pluck or Done on the page (FREE_MAX at most)
        secs = gcmd.get_float("SECONDS", 0., minval=0., maxval=self.FREE_MAX)
        if not math.isfinite(secs):
            raise gcmd.error("Chordata: SECONDS must be finite")
        limit = secs or self.FREE_MAX
        toolhead = self.printer.lookup_object("toolhead")
        self._begin_measurement("free", None, phase="starting" if getattr(self, "_quiet", None) else "background")
        toolhead.wait_moves()
        try:
            quiet = self._record_quiet(chip, toolhead)
            self.belt_now = "free"
            self._measurement_phase("listening")
            self.message = "Pluck either belt"
            if gcmd.get_int("UI", 0):
                self.gcode.run_script_from_command("_CHORDATA_UI STEP=freepluck IDLE=%d"
                                                   % round(self.FREE_IDLE))
            q = [[s[i] for s in quiet] for i in (1, 2, 3)]
            avoid = self.belts.get("tap_ring") or self.belts.get("tap", [])
            kw = dict(quiet=q, avoid=avoid, target=self.belts["target"], free=True)
            # in short pieces: an open-ended listen would pile up samples (memory on a small Pi)
            # and the live analysis would slow down as the recording grows
            plucks, t_off, samples, hum = [], 0.0, None, []
            rec_name = "free-%s" % time.strftime("%Y%m%d-%H%M%S")
            t_end = self.reactor.monotonic() + limit
            # it ends by itself a while after the last pluck (Done on the page ends it at once)
            while (t_off < limit - 0.5 and self.reactor.monotonic() < t_end and not self._accept
                   and t_off - (plucks[-1]["t"] if plucks else 0.) < self.FREE_IDLE):
                done = list(plucks)

                def live(part, done=done, off=t_off):
                    r = self._analyse(*self._cols(part), **kw)
                    self.belt_live = (done + [dict(p, t=round(p["t"] + off, 2)) for p in r["plucks"]])[-40:]
                    self.belt_hum = r.get("hum") or []

                samples = self._record(chip, toolhead, min(self.FREE_PIECE, limit - t_off), live=live,
                                       stop=lambda: self._accept)
                if not samples:
                    break
                self._save_recording(rec_name, samples, append=True)
                try:
                    r = self._analyse(*self._cols(samples), **kw)
                except ValueError as error:
                    raise gcmd.error("Chordata: invalid accelerometer recording: %s" % error)
                plucks = (plucks + [dict(p, t=round(p["t"] + t_off, 2)) for p in r["plucks"]])[-200:]
                if samples[-1][0] - samples[0][0] >= 4.0 or r.get("hum"):
                    hum = r.get("hum") or []      # the newest full piece says whether a fan hums now
                self.belt_live = plucks[-40:]
                t_off += samples[-1][0] - samples[0][0]
        finally:
            self.belt_now = None
            self._measurement_phase("analysing")
        if samples is None:
            raise gcmd.error("Chordata: the accelerometer sent no data")
        ok = [p["freq"] for p in plucks if p["ok"] and p.get("freq") is not None]
        res = {"freq": ok[-1] if ok else None, "plucks": plucks, "used": len(ok), "heard": len(plucks), "hum": hum,
               "verdict": "free" if ok else "none", "note": "%d of %d plucks clear" % (len(ok), len(plucks))}
        self.belt_live = plucks[-40:]
        self.belt_hum = hum
        self.belts = dict(self.belts, free={"plucks": plucks[-40:], "last": ok[-1] if ok else None, "hum": hum})
        self._end_measurement(result=res)
        if ok:
            gcmd.respond_info("Chordata: Tuner Mode: last pluck %.1f Hz (%s)" % (ok[-1], ", ".join("%.1f" % f for f in ok)))
        else:
            gcmd.respond_info("Chordata: Tuner Mode: no clear pluck heard")

    # ---------------- belt tensioning position (the user's own, saved from the jog pad) ----------------
    def _belt_pos_file(self):
        return os.path.join(self.out_dir, "belt_position.json")

    def _belt_pos(self):
        if not hasattr(self, "_bpos"):
            self._bpos = None
            try:
                with open(self._belt_pos_file()) as f:
                    d = json.load(f)
                self._bpos = {k: float(d[k]) for k in ("x", "y", "z") if d.get(k) is not None}
                if ("x" not in self._bpos or "y" not in self._bpos
                        or any(not math.isfinite(v) for v in self._bpos.values())):
                    self._bpos = None
            except Exception:
                pass
        return self._bpos

    cmd_CHORDATA_POS_help = ("Chordata: SAVE=1 stores where the toolhead is as the belt tensioning "
                                  "position; GO=1 moves there (Z only ever goes up)")

    def cmd_CHORDATA_POS(self, gcmd):
        self._require_idle(gcmd)
        save = gcmd.get_int("SAVE", 0, minval=0, maxval=1)
        go = gcmd.get_int("GO", 0, minval=0, maxval=1)
        if save + go != 1:
            raise gcmd.error("Chordata: choose exactly one of SAVE=1 or GO=1")
        th = self.printer.lookup_object("toolhead")
        th.wait_moves()
        homed = th.get_status(self.reactor.monotonic())["homed_axes"]
        if "x" not in homed or "y" not in homed:
            raise gcmd.error("Chordata: home the printer first")
        pos = self.printer.lookup_object("gcode_move").get_status(self.reactor.monotonic())["gcode_position"]
        if any(not math.isfinite(float(v)) for v in pos[:3]):
            raise gcmd.error("Chordata: toolhead position must be finite")
        if save:
            d = {"x": round(pos[0], 2), "y": round(pos[1], 2), "z": round(pos[2], 2) if "z" in homed else None}
            os.makedirs(self.out_dir, exist_ok=True)
            with open(self._belt_pos_file() + ".part", "w") as f:
                json.dump(d, f)
            os.replace(self._belt_pos_file() + ".part", self._belt_pos_file())
            self._bpos = {k: v for k, v in d.items() if v is not None}
            gcmd.respond_info("Chordata: belt tensioning position saved (X %.1f Y %.1f)" % (d["x"], d["y"]))
            return
        if go:
            bp = self._belt_pos()
            if not bp:
                raise gcmd.error("Chordata: no belt tensioning position saved yet")
            g = self.printer.lookup_object("gcode")
            moves = ["G90"]
            if "z" in homed and bp.get("z") is not None and pos[2] < bp["z"]:
                moves.append("G1 Z%.2f F600" % bp["z"])           # up first, never down
            moves.append("G1 X%.2f Y%.2f F6000" % (bp["x"], bp["y"]))
            g.run_script_from_command("SAVE_GCODE_STATE NAME=_chordata_bpos")
            try:
                g.run_script_from_command("\n".join(moves))
            finally:
                g.run_script_from_command("RESTORE_GCODE_STATE NAME=_chordata_bpos")

    cmd_CHORDATA_TAP_help = ("Chordata: learn how the toolhead rings by itself from one light tap, "
                                  "so a tap is never taken for a belt")

    def cmd_CHORDATA_TAP(self, gcmd):
        self._require_idle(gcmd)
        try:
            self._belt_tap(gcmd)
        except Exception as error:
            if self.state != "idle":
                self._end_measurement(error=error)
            if gcmd.get_int("UI", 0):
                self.gcode.respond_raw("// action:prompt_end")
            raise

    def _belt_tap(self, gcmd):
        import chordata_belts as belts
        if not self.belts:
            raise gcmd.error("Chordata: run _CHORDATA_START first")
        rt = self.printer.lookup_object("resonance_tester")
        chips = getattr(rt, "accel_chips", None) or []
        if not chips:
            raise gcmd.error("Chordata: no accelerometer found")
        chip = chips[0][1]
        toolhead = self.printer.lookup_object("toolhead")
        self._begin_measurement("tap", belts.TAP_NEED)
        self.tap_live = []
        toolhead.wait_moves()
        try:
            quiet = self._record_quiet(chip, toolhead, fresh=True)
            q = [[s[i] for s in quiet] for i in (1, 2, 3)]
            # step 0: something humming on the belt's note (or twice it) in the background is
            # turned off before measuring, rather than measured around
            hums = [] if gcmd.get_int("ANYWAY", 0) else belts.disturbing(
                q, getattr(self, "_accel_fs", None) or 3200.,
                [self.belts.get("target")] + list((self.belts.get("good") or {}).values()))
            self.belts = dict(self.belts, disturb={"lines": [round(x, 1) for x, _ in hums],
                                                   "fans": self._fan_list()} if hums else None)
            if hums:
                self._end_measurement()
                gcmd.respond_info("Chordata: steady hum at %s Hz on or near the belt's note: turn fans off, "
                                  "or measure anyway (ANYWAY=1)" % " and ".join("%.0f" % x for x, _ in hums))
                return
            self.belt_now = "tap"
            self._measurement_phase("listening")
            self.message = "Tap the toolhead lightly, %d times, on the same spot" % belts.TAP_NEED
            if gcmd.get_int("UI", 0):
                self.gcode.run_script_from_command("_CHORDATA_UI STEP=tapnow")
            st = {"done": False}

            def live(part):
                # about once a second: each tap so far, good or not; it stops at 3 good ones
                r = belts.analyse_taps(*self._cols(part), quiet=q, target=self.belts.get("target"))
                if st.get("final"):             # a late live result can't overwrite the final marks
                    return
                self.tap_live = r["taps"]
                self.measurement = dict(self.measurement, accepted=r["good"], heard=len(r["taps"]))
                if self._taps_over(r):
                    st["done"] = True

            samples = self._record(chip, toolhead, self.TAP_MAX, live=live, stop=lambda: st["done"])
            st["final"] = True
        finally:
            self.belt_now = None
            self._measurement_phase("analysing")
        if not samples:
            raise gcmd.error("Chordata: the accelerometer sent no data")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._save_recording("tap-%s-quiet" % stamp, quiet)
        self._save_recording("tap-%s" % stamp, samples)
        try:
            res = self._off_reactor(belts.analyse_taps, *self._cols(samples), quiet=q, target=self.belts.get("target"))
        except ValueError as error:
            raise gcmd.error("Chordata: invalid accelerometer recording: %s" % error)
        self.tap_live = res["taps"]
        stiff = self._taps_stiff(res)
        b = dict(self.belts)
        b["tap"] = res["modes"]
        b["tap_ring"] = res["ring"]          # per line: how long it rings and how wide it is
        b["tap_main"] = res["main"]          # the toolhead's own note: a knock there switches to belt B
        # A note above the accelerometer's listed range is still learned and guarded (the chip damps
        # it, it doesn't go deaf, so it can still show up), but its reading is less sure: say so
        acc = self._accel() or {}
        b["tap_range"] = ("above what the %s hears well" % acc["name"]
                          if res["main"] and acc.get("hi") and res["main"]["freq"] > acc["hi"] else "")
        b["tap_tau"] = res["tau"]
        b["taps"] = res["taps"]
        b["tap_good"], b["tap_bad"] = res["good"], res["bad"]
        b["tap_note"] = ("the tap rang nothing clearly" if stiff else self._taps_hum(res) or (
                         self._taps_failed(res) if res["bad"] >= belts.TAP_FAILS else res["note"]))
        # three good taps on the same spot (a stiff toolhead rings nothing near the belts: that counts too)
        b["tap_done"] = res["good"] >= belts.TAP_NEED or stiff
        self.belts = b
        self._end_measurement(result=dict(res, used=res["good"], heard=len(res["taps"])))
        if res["good"] >= belts.TAP_NEED:
            gcmd.respond_info("Chordata: %d good taps of %d; the toolhead rings by itself at %s; ignored when "
                              "listening to the belts" % (res["good"], len(res["taps"]), ", ".join(
                                  "%.1f Hz (%.2f s, +-%.1f Hz)" % (r["freq"], r["tau"], r["guard"]) for r in res["ring"])))
        else:
            gcmd.respond_info("Chordata: taps: %d good of %d (%s)" % (res["good"], len(res["taps"]), b["tap_note"]))

    TAP_MAX = 120.      # s: the setup tap listen stops by itself after this
    def _taps_stiff(self, r):
        """As many heard taps as needed, all ringing nothing clearly: a stiff toolhead, nothing to learn."""
        import chordata_belts as belts
        return not r["good"] and sum(1 for x in r["taps"] if x["why"] == "too soft") >= belts.TAP_NEED

    def _taps_hum(self, r):
        """No good tap, and as many taps as needed heard but not ringing, at least two of them
        hidden by a steady hum (a fan on or beside the toolhead's note): say so, the user turns
        that fan off. None otherwise."""
        import chordata_belts as belts
        hid = [x["why"] for x in r["taps"] if x["why"].startswith("hidden by a steady hum")]
        soft = sum(1 for x in r["taps"] if x["why"] == "too soft")
        if r["good"] or len(hid) < 2 or len(hid) + soft < belts.TAP_NEED:
            return None
        return max(set(hid), key=hid.count).replace("hidden by a ", "")

    def _taps_failed(self, r):
        """Why the setup gave up: mostly taps on another note means another spot (or the first
        good tap was the odd one), anything else something shaking the printer."""
        spot = sum(1 for x in r["taps"] if x["why"] == "another spot")
        return "taps on another spot" if 2 * spot > r["bad"] else "too many bad taps"

    def _taps_over(self, r):
        import chordata_belts as belts
        return r["good"] >= belts.TAP_NEED or r["bad"] >= belts.TAP_FAILS or self._taps_stiff(r) \
            or bool(self._taps_hum(r))

    # ---------------- fans: turned off when they hum on the belt's note (step 0) ----------------
    # The kinds Chordata may turn off, and the ones it never touches: a controller fan runs
    # while the motors are held, a heater fan while the hotend is warm, and a temperature fan
    # keeps something below a set temperature (often the electronics). Those stay on.
    FAN_KINDS = {"fan": True, "fan_generic": True,
                 "controller_fan": False, "heater_fan": False, "temperature_fan": False}
    FAN_WHY = {"controller_fan": "runs while the motors are on",
               "heater_fan": "runs while the hotend is warm: it stops once the hotend has cooled",
               "temperature_fan": "keeps something below a set temperature"}

    # (matched with "_", "-" and spaces taken out: hot_end, heat-break; whole words for "pi")
    SAFETY_NAMES = ("board", "mobo", "mcu", "pcb", "electronic", "controller", "control", "ctrl", "driver",
                    "stepper", "rpi", "raspberry", "raspi", "sbc", "skr", "octopus", "spider", "manta", "ebb",
                    "tmc", "elec", "host", "cpu", "skirt", "bay", "case", "hotend", "heatbreak", "extruder",
                    "nozzle", "toolfan", "toolheadfan", "psu", "power")
    SAFETY_WORDS = ("pi",)

    def _safety_name(self, name):
        """A fan_generic named for the electronics or the hotend (a macro often drives those)."""
        words = re.split(r"[\s_\-]+", name.lower())
        flat = "".join(words)
        return any(w in flat for w in self.SAFETY_NAMES) or any(w in words for w in self.SAFETY_WORDS)

    def _fan_list(self):
        """The printer's fans from its config, each with whether Chordata may turn it off and
        whether it is on now: [{"section", "label", "kind", "button", "on", "why"}]."""
        try:
            settings = self.printer.lookup_object("configfile").get_status(
                self.reactor.monotonic())["settings"]
        except Exception:
            return []
        # the config's settings name every section in lowercase; the printer object (and so
        # SET_FAN_SPEED FAN=) keeps the case it was written in: [fan_generic Bed_Fan]
        try:
            names = {n.lower(): n for n, _ in self.printer.lookup_objects()}
        except Exception:
            names = {}
        out = []
        for key in sorted(settings):
            section = names.get(key, key)
            kind = section.split()[0]
            if kind not in self.FAN_KINDS or (kind == "fan" and section != "fan"):
                continue
            label = "Part cooling fan" if section == "fan" else section.split(None, 1)[-1].replace("_", " ")
            obj = self.printer.lookup_object(section, None)
            try:
                speed = float(obj.get_status(self.reactor.monotonic()).get("speed", 0.))
            except Exception:
                speed = None
            button, why = self.FAN_KINDS[kind], self.FAN_WHY.get(kind, "")
            name = section.split(None, 1)[-1].lower()
            if kind == "fan_generic" and self._safety_name(name):
                # electronics and hotend fans are often a fan_generic a macro drives: by its name
                button, why = False, "seems to cool the electronics or the hotend (by its name)"
            out.append({"section": section, "label": label, "kind": kind,
                        "button": button, "on": speed is None or speed > 0., "why": why})
        return out

    def _fan_script(self, section, speed):
        if section == "fan":
            return "M106 S%d" % round(255 * speed) if speed > 0 else "M107"
        return "SET_FAN_SPEED FAN=%s SPEED=%.3f" % (section.split(None, 1)[-1], speed)

    cmd_CHORDATA_FAN_help = ("Chordata: turn a fan off for the measurement (INDEX: its place in the fan list); "
                             "it comes back on with _CHORDATA_FANS")

    def cmd_CHORDATA_FAN(self, gcmd):
        self._require_idle(gcmd)
        fans = self._fan_list()
        i = gcmd.get_int("INDEX", -1)
        if not 0 <= i < len(fans):
            raise gcmd.error("Chordata: no fan %d" % i)
        fan = fans[i]
        if not fan["button"]:
            raise gcmd.error("Chordata: the %s %s; Chordata leaves it on" % (fan["label"], fan["why"]))
        obj = self.printer.lookup_object(fan["section"], None)
        try:
            # the speed asked for, to ask for again: Kalico shows it as "value"; Klipper only
            # as "speed", which is already times max_power
            st = obj.get_status(self.reactor.monotonic())
            speed = st.get("value")
            if speed is None:
                settings = self.printer.lookup_object("configfile").get_status(
                    self.reactor.monotonic())["settings"]
                top = float((settings.get(fan["section"].lower()) or {}).get("max_power", 1.))
                speed = float(st.get("speed", 0.)) / top if top > 0. else 0.
            speed = min(1., max(0., float(speed)))
        except Exception:
            speed = 0.
        self._fans_off.setdefault(fan["section"], speed)
        self.gcode.run_script_from_command(self._fan_script(fan["section"], 0.))
        self._fans_touch()
        gcmd.respond_info("Chordata: %s off until Chordata is closed" % fan["label"])

    cmd_CHORDATA_FANS_help = "Chordata: turn the fans Chordata turned off back to how they were"

    def cmd_CHORDATA_FANS(self, gcmd):
        import chordata_belts as belts
        self._restore_fans()
        belts.clear_cache()                  # (Close Chordata: the session's cached fits go)

    FANS_IDLE = 600.    # s without Chordata after which the fans it turned off come back on

    def _fans_touch(self):
        """Chordata is in use: the fans it turned off stay off FANS_IDLE s more."""
        self._fans_until = self.reactor.monotonic() + self.FANS_IDLE
        if getattr(self, "_fans_off", None) and getattr(self, "_fans_timer", None) is None:
            self._fans_timer = self.reactor.register_timer(self._fans_check, self.reactor.monotonic() + 2.0)

    def _printing(self, eventtime):
        """A print running (or paused): what Klipper's print_stats and virtual_sdcard say. (Not
        idle_timeout's "Printing": that only means the toolhead is busy, which Chordata's own
        fan and dwell commands make it.)"""
        stats = self.printer.lookup_object("print_stats", None)
        sd = self.printer.lookup_object("virtual_sdcard", None)
        try:
            return (stats is not None and stats.get_status(eventtime).get("state") in ("printing", "paused")) \
                or (sd is not None and bool(sd.get_status(eventtime).get("is_active")))
        except Exception:
            return False

    def _fans_check(self, eventtime):
        """Every 2 s while fans are off: they come back on when a print starts (a print never
        runs with a fan Chordata turned off: the card closed with its X, a step that failed)
        or FANS_IDLE s after Chordata was last used. Not while a command runs (the G-code lock:
        it would land inside the user's macro)."""
        if not self._fans_off:
            self._fans_timer = None
            return self.reactor.NEVER
        printing = self._printing(eventtime)
        if not printing and (self.state != "idle" or eventtime < self._fans_until):
            return eventtime + 2.0
        # idle, not while a command runs (it would land inside the user's macro). A print holds
        # the G-code lock nearly all the time (each line runs with it, heat-up and homing too),
        # so then the fans go in the queue: run_script waits its turn and the print lets it in
        # at its next line
        mutex = getattr(self.gcode, "get_mutex", None)
        if not printing and mutex is not None and mutex().test():
            return eventtime + 1.0
        self._fans_timer = None
        if mutex is None:
            self._restore_fans(script=self.gcode.run_script, printing=printing)
            return self.reactor.NEVER
        # read the speeds and set them inside the lock: waiting for it outside, the start macro
        # may set a fan meanwhile, and the old speed would then undo it (review 7). (A timer
        # isn't inside a command, so taking the lock is allowed: run_script does the same)
        with mutex():
            self._restore_fans(script=self.gcode.run_script_from_command, printing=printing)
        return self.reactor.NEVER

    def _restore_fans(self, script=None, printing=False):
        off = getattr(self, "_fans_off", None) or {}
        self._fans_off = {}
        for section, speed in sorted(off.items()):
            # the part fan in a print is the slicer's: the queued M106 would only land after the
            # start macro (heat-up holds the G-code lock) and undo the slicer's M107 on layer 1.
            # The slicer sets it itself when the print needs it
            if printing and section == "fan":
                continue
            obj = self.printer.lookup_object(section, None)
            try:
                now = float(obj.get_status(self.reactor.monotonic()).get("speed", 0.))
            except Exception:
                now = 0.
            # a fan set since (the slicer's M106, the user's macro) keeps what it was set to
            if speed > 0. and now <= 0.:
                try:
                    (script or self.gcode.run_script_from_command)(self._fan_script(section, speed))
                except Exception:
                    logging.exception("chordata: restoring %s", section)

    # ---------------- the background (fans, motors): recorded once per session, at the tap step ----------------
    def _record_quiet(self, chip, toolhead, fresh=False):
        """The background is recorded once, when the toolhead is tapped, and used for every
        measure after it: a fan that comes on later is told from the belt by the plucks
        themselves (a pluck starts its tone, a fan hums the same before and after)."""
        if not fresh and getattr(self, "_quiet", None):
            return self._quiet
        self._measurement_phase("background")
        self.state, self.message = "listening", "Hands off the printer for a moment"
        quiet = self._record(chip, toolhead, 1.5)
        self._quiet = quiet or None
        if quiet and len(quiet) > 100 and quiet[-1][0] > quiet[0][0]:
            self._accel_fs = (len(quiet) - 1) / (quiet[-1][0] - quiet[0][0])
        return quiet

    def _progress(self, n, plucks=None, dur=None):
        """Plucks counted so far. Only plucks heard to the end of their window count: one cut short can still be
        judged differently once all of it is in."""
        import chordata_belts as belts
        heard = self.belt_live if plucks is None else plucks
        dur = getattr(self, "_live_dur", 0.0) if dur is None else dur
        full = dur - (belts.SKIP + belts.WINDOW + 0.05)
        fr = [p["freq"] for p in heard or () if p.get("ok") and p["t"] <= full]
        return len(fr)

    def _enough(self, n, plucks=None, dur=None):
        """All n plucks counted."""
        return bool(n) and self._progress(n, plucks, dur) >= n

    def _handle_accept(self, web_request):
        """From the page (via Moonraker), while G-code waits for the listen: end a free tune."""
        m = self.measurement or {}
        self._accept = self.state == "listening" and m.get("belt") == "free"
        web_request.send({"accepted": self._accept})

    # ---------------- raw recordings, readable from Mainsail (Kalico's own go to /tmp) ----------------
    REC_KEEP = 40
    LIVE_WAIT = 60.     # s at most for a live analysis still running when a listen ends

    def _record(self, chip, toolhead, secs, live=None, stop=None):
        """Record for secs. live: analyse() arguments; then the plucks heard so far are
        put in belt_live about once a second while recording (in a thread, so a slow
        host doesn't hold up Kalico). live can also be a function of the samples so far,
        that does its own live work. stop: checked once a second, True ends the recording."""
        if live is not None:
            import chordata_belts as belts
        client = chip.start_internal_client()
        timer = None
        if live is not None:
            busy = []
            self._live_gen = gen_now = getattr(self, "_live_gen", 0) + 1

            def work(samples):
                try:
                    if callable(live):           # a listen with its own live steps (both belts)
                        if self._live_gen == gen_now:
                            live(samples)
                        return
                    res = self._analyse([s[0] for s in samples], [s[1] for s in samples],
                                        [s[2] for s in samples], [s[3] for s in samples], **live)
                    if self.belt_now in ("A", "B", "free") and self._live_gen == gen_now:
                        self.belt_live = res["plucks"]
                        self.belt_hum = res.get("hum") or []
                        self._live_dur = samples[-1][0] - client.request_start_time
                        self.measurement = dict(self.measurement, accepted=res["used"],
                                                heard=res["heard"])
                except Exception:
                    logging.exception("chordata: live belt analysis")
                finally:
                    busy.clear()

            def tick(eventtime):
                try:
                    if not busy:
                        samples = [s for m in list(client.msgs) for s in m["data"]
                                   if s[0] >= client.request_start_time]
                        if len(samples) > 200:
                            busy.append(1)
                            threading.Thread(target=work, args=(samples,), daemon=True).start()
                except Exception:
                    logging.exception("chordata: live belt samples")
                    return self.reactor.NEVER
                return eventtime + 1.0
        try:
            if live is not None:
                self._live_dur = 0.0
                timer = self.reactor.register_timer(tick, self.reactor.monotonic() + 1.5)
            n = None if callable(live) else (live or {}).get("expect")
            if stop is not None or n:
                # in steps of a second, so a series that is already good ends there
                end = self.reactor.monotonic() + secs
                while self.reactor.monotonic() < end:
                    toolhead.dwell(1.0)
                    toolhead.wait_moves()
                    if stop() if stop is not None else self._enough(n):
                        n = None
                        break
            else:
                toolhead.dwell(secs)
            if n:
                # a pluck was left out (the other belt ringing, a knock): keep listening,
                # a little while, until the plucks needed are counted
                toolhead.wait_moves()
                end = self.reactor.monotonic() + 2.0 + 1.3 * n
                self._live_dur = 0.0
                while self.reactor.monotonic() < end:
                    if not self.belt_live or self._enough(n):
                        break
                    self.message = "Pluck belt %s again" % self.belt_now
                    toolhead.dwell(1.0)
                    toolhead.wait_moves()
        finally:
            try:
                client.finish_measurements()     # never leave the chip streaming
            finally:
                # after finish: dwell returns while ~2 s are still queued, keep showing those
                if timer is not None:
                    self.reactor.unregister_timer(timer)
                    self._live_gen += 1          # a late live result can't overwrite the final one
                    # and the live analysis still running finishes first: the final one beside
                    # it would double the memory (about 140 MB each for a minute's listen: a
                    # Pi Zero 2 W has 512 MB)
                    wait = self.reactor.monotonic() + self.LIVE_WAIT
                    while busy and self.reactor.monotonic() < wait:
                        self.reactor.pause(self.reactor.monotonic() + 0.05)
        return client.get_samples()

    def _find_chip(self, name):
        """A chip by Kalico's CHIP= name (beacon, hotend for [adxl345 hotend]) or its section name."""
        rt = self.printer.lookup_object("resonance_tester", None)
        chips = getattr(rt, "accel_chips", None) or []
        if not name:
            return chips[0][1] if chips else None
        cands = [name] + ["%s %s" % (k, name) for k in
                          ("adxl345", "lis2dw", "lis3dh", "mpu9250", "icm20948", "bmi160")]
        for c in cands:
            obj = self.printer.lookup_object(c, None)
            if obj is not None and hasattr(obj, "start_internal_client"):
                return obj
        return None

    _rec_prev = None    # the last writer thread: each waits for it, so pieces go in in order

    def _save_recording(self, name, samples, append=False):
        """Write samples as ACCELEROMETER_MEASURE's CSV into chordata/recordings. append: add
        to that file (a free tune is recorded in pieces, and all of it is kept)."""
        if not samples:
            return None
        d = os.path.join(self.out_dir, "recordings")
        path = os.path.join(d, name + ".csv")
        keep = self.REC_KEEP

        def write():
            # in a thread like Kalico's own writer: a 30 s recording takes a while on a Pi.
            # One at a time and in order (a lock would not keep the order), so the pieces of
            # a free tune go in in sequence.
            if prev is not None:
                prev.join()
            try:
                os.makedirs(d, exist_ok=True)
                rows = ("%.6f,%.6f,%.6f,%.6f\n" % tuple(s[:4]) for s in samples)
                if append:
                    new = not os.path.exists(path)
                    with open(path, "a") as f:
                        if new:
                            f.write("#time,accel_x,accel_y,accel_z\n")
                        f.writelines(rows)
                else:
                    with open(path + ".part", "w") as f:
                        f.write("#time,accel_x,accel_y,accel_z\n")
                        f.writelines(rows)
                    os.replace(path + ".part", path)
                old = sorted((os.path.getmtime(os.path.join(d, n)), n)
                             for n in os.listdir(d) if n.endswith(".csv"))
                for _, n in old[:-keep]:
                    os.remove(os.path.join(d, n))
            except OSError as e:
                logging.warning("chordata: could not save recording %s: %s", name, e)
        prev = self._rec_prev
        t = threading.Thread(target=write, daemon=True)
        self._rec_prev = t
        t.start()
        return path

    cmd_CHORDATA_RECORD_help = ("Chordata: record an accelerometer for SECONDS and save it where "
                                "Mainsail can download it (CHIP=, SECONDS=, NAME=)")

    def cmd_CHORDATA_RECORD(self, gcmd):
        self._require_idle(gcmd)
        name = gcmd.get("CHIP", "")
        chip = self._find_chip(name)
        if chip is None:
            raise gcmd.error("Chordata: no accelerometer %s" % (name or "found"))
        secs = gcmd.get_float("SECONDS", 10., minval=.5, maxval=30.)
        if not math.isfinite(secs):
            raise gcmd.error("Chordata: SECONDS must be finite")
        label = re.sub(r"[^A-Za-z0-9_-]", "_", gcmd.get("NAME", "")) or time.strftime("%Y%m%d-%H%M%S")
        label = "%s-%s" % (re.sub(r"[^A-Za-z0-9_-]", "_", name or "accel"), label)
        toolhead = self.printer.lookup_object("toolhead")
        self._begin_measurement(None)
        self._measurement_phase("recording")
        try:
            toolhead.wait_moves()
            gcmd.respond_info("Chordata: recording %.0f s, go" % secs)
            samples = self._record(chip, toolhead, secs)
            if not samples:
                raise gcmd.error("Chordata: the accelerometer sent no data")
        except Exception as error:
            self._end_measurement(error=error)
            raise
        self._end_measurement()
        path = self._save_recording(label, samples)
        gcmd.respond_info("Chordata: saving %d samples to %s" % (
            len(samples), os.path.relpath(path, self.cfg_dir)))


def load_config(config):
    return Chordata(config)
