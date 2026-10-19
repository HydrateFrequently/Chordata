# Chordata: belt tension from plucks, measured with the toolhead accelerometer.
#
# A Klipper/Kalico plugin. Pluck a belt like a guitar string; the accelerometer on the
# toolhead hears it, and Chordata shows its frequency, leaving out what isn't a clear
# pluck (too weak, a knock on the toolhead). CoreXY belts pull on each other, so both are
# plucked while turning the tensioners, until they match the target and each other.
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
        self.belt_tol = config.getfloat("belt_tolerance", 0.25, above=0.)   # Hz: a pluck this close to the target is on target
        self.belt_target = config.getfloat("belt_target", 110., minval=40., maxval=300.)  # as the setup prompt allows
        self.state, self.message = "idle", ""
        self.belts = {}           # the belt tension session, for the prompts
        self._quiet = None        # the background (fans, motors), recorded at the tap step
        self.belt_now = None      # "free" while listening to plucks ("tap" while learning the toolhead)
        self.belt_live = []       # plucks heard so far while listening
        self.belt_hum = []        # a fan humming on the belt's note in the newest analysis (Hz)
        self.tap_live = []        # the setup taps so far, each good or not
        self.measurement = {"phase": "idle", "belt": None}
        self._fans_off = {}       # fans Chordata turned off this session: section -> speed before
        self._fans_timer = None   # brings them back FANS_IDLE s after Chordata was last used
        self._fans_until = 0.
        self._tool_info = None
        self._accept = False
        self._history_clear()
        webhooks = self.printer.lookup_object("webhooks", None)
        if webhooks is not None:
            webhooks.register_endpoint("chordata/accept", self._handle_accept)
            webhooks.register_endpoint("chordata/history_reset", self._handle_history_reset)
        for name in ("START", "FREE", "TAP", "POS", "FAN", "FANS", "FAN_OK"):
            fn = getattr(self, "cmd_CHORDATA_" + name)
            self.gcode.register_command("_CHORDATA_" + name, fn,
                                        desc=getattr(self, "cmd_CHORDATA_%s_help" % name))
        self.gcode.register_command("CHORDATA_RECORD", self.cmd_CHORDATA_RECORD,
                                    desc=self.cmd_CHORDATA_RECORD_help)

    def get_status(self, eventtime):
        return {"state": self.state, "message": self.message, "tools": self._tools(), "accel": self._accel(),
                "belts": self.belts, "belt_target": self.belt_target, "belt_tol": self.belt_tol,
                "belt_now": self.belt_now if self.belt_now == "free" else None,
                "belt_live": self.belt_live, "belt_hum": getattr(self, "belt_hum", []), "tap_live": getattr(self, "tap_live", []),
                "belt_heard": getattr(self, "belt_heard", 0),
                "history": getattr(self, "history", []), "history_note": getattr(self, "history_note", None),
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
        self.belt_heard = 0       # onsets heard so far in this listen (the page flashes on each new one)
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
            gcmd.respond_info("Chordata: the %s hears up to %d Hz, less than twice the target: it can't hear "
                              "a pluck's tone at twice its note, which tells a pluck from a knock, so many plucks may be "
                              "refused. An ADXL345 or a Beacon hears both" % (acc["name"], acc["hi"]))
        self.belts = {"target": target, "tol": self.belt_tol,
                      "tap": [], "tap_ring": [], "tap_tau": None, "tap_done": False}
        self.belt_live = []
        self._history_clear()                # a new session, a new history
        self._restore_fans()                 # (a session closed without Close Chordata)
        import chordata_belts as belts
        belts.clear_cache()
        self._quiet = None                   # the background, recorded once per session at the tap step
        self.measurement = {"phase": "idle", "belt": None}
        gcmd.respond_info("Chordata: belt tension, target %.0f Hz" % target)

    @staticmethod
    def _cols(samples):
        return ([s[0] for s in samples], [s[1] for s in samples], [s[2] for s in samples],
                [s[3] for s in samples])

    cmd_CHORDATA_FREE_help = ("Chordata: Tuner Mode: show every pluck's frequency until a minute after the "
                              "last pluck, Done on the page, or SECONDS")
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
        """Pluck either belt and read each pluck's frequency, turning the tensioners
        between plucks. Nothing is averaged."""
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
            self._listen_id = getattr(self, "_listen_id", 0) + 1     # the history tells this listen's plucks apart
            listen = self._listen_id
            t_end = self.reactor.monotonic() + limit
            # it ends by itself a while after the last pluck (Done on the page ends it at once)
            heard = 0                            # onsets heard in the finished pieces
            while (t_off < limit - 0.5 and self.reactor.monotonic() < t_end and not self._accept
                   and t_off - (plucks[-1]["t"] if plucks else 0.) < self.FREE_IDLE):
                done = list(plucks)

                def live(part, done=done, off=t_off, base=heard):
                    cols = self._cols(part)
                    r = self._analyse(*cols, **kw)
                    now = self._mark_final(r["plucks"], part[-1][0] - part[0][0])
                    self.belt_live = (done + [dict(p, t=round(p["t"] + off, 2)) for p in now])[-40:]
                    self._history_add(listen, off, now, cols, q, part[-1][0] - part[0][0])
                    self.belt_hum = r.get("hum") or []
                    self.belt_heard = max(self.belt_heard, base + len(r.get("onsets") or ()))

                # where this piece starts, in the listen's time and the reactor's (Reset's floor)
                self._listen_t0 = (listen, t_off, self.reactor.monotonic())
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
                # the piece is all in: its verdicts are final (the live ones it repeats are replaced)
                self._off_reactor(self._history_add, listen, t_off, r["plucks"], self._cols(samples), q,
                                  samples[-1][0] - samples[0][0], final=True)
                if samples[-1][0] - samples[0][0] >= 4.0 or r.get("hum"):
                    hum = r.get("hum") or []      # the newest full piece says whether a fan hums now
                self.belt_live = plucks[-40:]
                heard += len(r.get("onsets") or ())
                self.belt_heard = max(self.belt_heard, heard)
                t_off += samples[-1][0] - samples[0][0]
                self._listen_t0 = (listen, t_off, None)     # between pieces: nothing recorded
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
            # (the belts' last Tuner reading too: they may sit a little off the target)
            last = (self.belts.get("free") or {}).get("last")
            hums = [] if gcmd.get_int("ANYWAY", 0) else belts.disturbing(
                q, getattr(self, "_accel_fs", None) or 3200., [self.belts.get("target")] + ([last] if last else []))
            fans = self._fan_list() if hums else []
            lines = [round(x, 1) for x, _ in hums]
            # only stop when a fan Chordata can turn off (or could, once okayed) is running: a hum from something it
            # can't (the motors held, a controller fan, a fan it doesn't know) is noted, and the
            # plucks themselves tell the belt from it
            stop = any((f["button"] or f["ask"]) and f["on"] for f in fans)
            self.belts = dict(self.belts, disturb={"lines": lines, "fans": fans} if stop else None,
                              hum_note=lines if hums and not stop else [])
            if stop:
                self._end_measurement()
                gcmd.respond_info("Chordata: steady hum at %s Hz on or near the belt's note: turn fans off, "
                                  "or measure anyway (ANYWAY=1)" % " and ".join("%.0f" % x for x in lines))
                return
            if hums:
                gcmd.respond_info("Chordata: steady hum at %s Hz near the belt's note, from nothing Chordata "
                                  "can turn off (motors, controller fan?): measuring with it"
                                  % " and ".join("%.0f" % x for x in lines))
            self.belt_now = "tap"
            self._measurement_phase("listening")
            self.message = "Tap the toolhead lightly, %d times, on the same spot" % belts.TAP_NEED
            if gcmd.get_int("UI", 0):
                self.gcode.run_script_from_command("_CHORDATA_UI STEP=tapnow")
            st = {"done": False}

            def live(part):
                # as often as the live pacing allows: each tap so far, good or not; it stops at 3 good ones
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
        b["tap_main"] = res["main"]          # the toolhead's own note
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

    # Which fans the user okayed Chordata to turn off: none until they say so (a fan that cools the
    # stepper drivers, whatever its name, must never be stopped by Chordata on its own)
    def _fans_ok_file(self):
        return os.path.join(self.out_dir, "fans_ok.json")

    def _fans_ok(self):
        """{section (lowercase): True} for the fans the user okayed (kept across restarts; read
        again when the file changes or goes, so deleting it takes every allowance back at once)."""
        if getattr(self, "out_dir", None) is None:
            return getattr(self, "_fok", {})
        try:
            stamp = os.stat(self._fans_ok_file()).st_mtime
        except OSError:
            stamp = None
        if not hasattr(self, "_fok") or getattr(self, "_fok_stamp", "unread") != stamp:
            self._fok, self._fok_stamp = {}, stamp
            try:
                with open(self._fans_ok_file()) as f:
                    d = json.load(f)
                self._fok = {str(k).lower(): True for k, v in d.items() if v is True}
            except Exception:
                pass
        return self._fok

    cmd_CHORDATA_FAN_OK_help = ("Chordata: allow (ALLOW=1) or forbid (ALLOW=0) Chordata to turn a fan off "
                                "(SECTION: its config section, or INDEX: its place in the fan list); kept across restarts")

    def cmd_CHORDATA_FAN_OK(self, gcmd):
        fans = self._fan_list()
        sec = gcmd.get("SECTION", None)
        if sec is not None:          # by its config section, e.g. SECTION="fan_generic aux_fan"
            i = next((j for j, f in enumerate(fans) if f["section"].lower() == sec.strip().lower()), -1)
        else:
            i = gcmd.get_int("INDEX", -1)
        if not 0 <= i < len(fans):
            raise gcmd.error("Chordata: no fan %s" % (sec if sec is not None else i))
        fan, allow = fans[i], bool(gcmd.get_int("ALLOW", 1, minval=0, maxval=1))
        if allow and not fan["ask"] and not fan["button"]:
            raise gcmd.error("Chordata: the %s %s; Chordata leaves it on" % (fan["label"], fan["why"]))
        ok = dict(self._fans_ok())
        if allow:
            ok[fan["section"].lower()] = True
        else:
            ok.pop(fan["section"].lower(), None)
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            with open(self._fans_ok_file() + ".part", "w") as f:
                json.dump(ok, f)
            os.replace(self._fans_ok_file() + ".part", self._fans_ok_file())
            stamp = os.stat(self._fans_ok_file()).st_mtime
        except OSError as error:
            raise gcmd.error("Chordata: could not save the fan choice (%s)" % error)
        self._fok, self._fok_stamp = ok, stamp
        d = (self.belts or {}).get("disturb")
        if d:                                # the card shows the change
            self.belts = dict(self.belts, disturb=dict(d, fans=self._fan_list()))
        gcmd.respond_info("Chordata: %s %s" % (fan["label"], "may be turned off by Chordata" if allow
                                                   else "is never turned off by Chordata"))

    def _fan_list(self):
        """The printer's fans from its config, each with whether Chordata may turn it off and
        whether it is on now: [{"section", "label", "kind", "button", "ask", "on", "why"}]. button: it
        may be turned off (the user okayed it); ask: it could be, once the user okays it."""
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
            if obj is None:
                # not found under any name: Chordata can't read or set it
                button, why = False, "could not be found"
            name = section.split(None, 1)[-1].lower()
            if kind == "fan_generic" and self._safety_name(name):
                # electronics and hotend fans are often a fan_generic a macro drives: by its name
                button, why = False, "seems to cool the electronics or the hotend (by its name)"
            ask = False
            if button and section.lower() not in self._fans_ok():
                button, ask, why = False, True, "is not okayed for Chordata to turn off"
            out.append({"section": section, "label": label, "kind": kind, "button": button, "ask": ask,
                        "on": obj is not None and (speed is None or speed > 0.), "why": why})
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
        self._history_clear()
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

    @staticmethod
    def _mark_final(plucks, dur):
        """Each live pluck marked final once its whole window is in (until then its reading may
        still move a little: the page shows it greyed)."""
        import chordata_belts as belts
        full = dur - (belts.SKIP + belts.WINDOW + 0.05)
        return [dict(p, final=p["t"] <= full) for p in plucks]

    # ---------------- the session's pluck history (Tuner Mode) ----------------
    HISTORY_KEEP = 100  # entries kept
    DTAP_GAP = (0.2, 0.8)   # s between the two knocks of a double tap (closer than DEAD: one event, see second_knock)
    KEY_STEP = 0.05     # s: a pluck is known by its listen and its time in it, rounded to this
    _hist_lock = threading.Lock()   # the live analysis (a thread) and the page's Reset both change it

    def _history_clear(self):
        with self._hist_lock:
            self.history = []          # what the page shows: {n, freq, ok, why, at}, newest last
            self.history_note = None   # {text, at}: a double tap cleared plucks
            self._hist = []            # (listen, t, key, entry), in the order heard
            self._hist_n = 0
            self._hist_gone = set()    # keys cleared: a later analysis of the same piece doesn't bring them back
            self._hist_floor = (getattr(self, "_listen_id", 0), float("inf"))   # (Reset) nothing before this
            self._knocks = {}          # key -> was it a toolhead knock (asked once per event)
            self._dtap_prev = None     # (listen, t, ok, key): the last piece's last event

    def _key(self, listen, t):
        return (listen, int(round(t / self.KEY_STEP)))

    @staticmethod
    def _known(key, keys):
        """key, or a key beside it, in keys (a time on the rounding edge: the live analysis and
        the final one may place an onset a hundredth apart). The one found, or None."""
        return next((k for k in (key, (key[0], key[1] - 1), (key[0], key[1] + 1)) if k in keys), None)

    def _hist_find(self, key):
        k = self._known(key, {h[2] for h in self._hist})
        return None if k is None else next(i for i, h in enumerate(self._hist) if h[2] == k)

    def _history_add(self, listen, off, plucks, cols, quiet, dur, final=False):
        """Plucks whose window is all in, into the session history: a pluck seen live, then in
        its piece's final analysis, is one entry (the final verdict wins). Then a double tap on the
        toolhead (two knocks about 0.2-0.8 s apart) clears the history, as Reset history does.
        plucks: one analysis' plucks (t from the start of cols); live ones not final yet are left
        for later."""
        if not hasattr(self, "_hist"):
            self._history_clear()
            self._hist_floor = (0, float("-inf"))
        with self._hist_lock:
            changed = False
            for p in plucks:
                if p.get("final") is False:
                    continue
                t = p["t"] + off
                key = self._key(listen, t)
                if self._known(key, self._hist_gone) or (listen, t) < self._hist_floor:
                    continue
                v = {"freq": p.get("freq"), "ok": bool(p.get("ok")), "why": p.get("why") or ""}
                i = self._hist_find(key)
                if i is not None:
                    e = self._hist[i][3]
                    if final and any(e[k] != v[k] for k in v):
                        self._hist[i] = self._hist[i][:3] + (dict(e, **v),)
                        changed = True
                    continue
                self._hist_n += 1
                self._hist.append((listen, t, key, dict(v, n=self._hist_n, at=round(time.time(), 2))))
                self._hist.sort(key=lambda h: (h[0], h[1]))
                changed = True
            self._hist = self._hist[-self.HISTORY_KEEP:]
        if self._double_tap(listen, off, plucks, cols, quiet, dur, final):
            changed = True
        if changed:
            self.history = [h[3] for h in self._hist]

    def _double_tap(self, listen, off, plucks, cols, quiet, dur=None, final=False):
        """Two consecutive refused events DTAP_GAP apart, both a knock on the toolhead (its note,
        from the setup taps): clear the whole history up to them (Reset history), the two knocks too. The
        two may lie in two pieces of the listen: a piece's last event is kept (final) and paired
        with the next piece's first."""
        import chordata_belts as belts
        main = (self.belts or {}).get("tap_main")
        if not main:
            return False

        def knock(key, at, until, attack=True):
            # toolhead_knock at most once per event (live and final share the answer). Not kept
            # while a live look may not hold the next event yet (until unknown): its window could
            # still change; the next look, or the piece's final analysis, asks again
            k = (self._known(key, self._knocks) or key)
            ck = k if attack else ("noattack",) + k
            if ck in self._knocks:
                return self._knocks[ck]
            if not attack and self._knocks.get(k):
                return True                   # (a knock with its attack is one without it too)
            try:
                v = bool(belts.toolhead_knock(*cols, at, main, None, quiet=quiet, until=until, attack=attack)[0])
            except Exception:
                logging.exception("chordata: double tap check")
                v = False
            if until is not None or final:
                self._knocks[ck] = v
            return v

        def quick(key, at, until):
            # a quick double tap: the second knock inside the first one's dead time (never an
            # event of its own); asked once per event, after the event is a knock (kept, as for
            # knock(), only once the look holds all of the event's dead time)
            k = ("quick",) + (self._known(key, self._knocks) or key)
            if k in self._knocks:
                return self._knocks[k]
            try:
                v = belts.second_knock(*cols, at, main, quiet=quiet)
            except Exception:
                logging.exception("chordata: quick double tap check")
                v = None
            if until is not None or final:
                self._knocks[k] = v
            return v

        def clear(ka, kb, tb):
            with self._hist_lock:
                # the same as Reset history: all of it goes, the knocks too; only what comes
                # after them stays (and nothing before them comes back with a later analysis)
                keys = tuple(k for k in (ka, kb) if k is not None)
                self._hist_gone.update(keys)
                gone = [h for h in self._hist if (h[0], h[1]) <= (listen, tb) or self._known(h[2], keys)]
                self._hist_gone.update(h[2] for h in gone)
                self._hist = [h for h in self._hist if h not in gone]
                self._hist_floor = max(self._hist_floor, (listen, tb + 0.5 * self.KEY_STEP))
                self.history_note = {"text": "History cleared (double tap)", "at": round(time.time(), 2),
                                     "upto": round(tb, 2)}     # (in the listen's time, as belt_live's t)
        # the events: (time in the listen, ok, key, knock?), the last piece's last one first
        ev = []
        fired = False
        prev = getattr(self, "_dtap_prev", None)
        if prev and prev[0] == listen and prev[1] < off:
            ev.append((prev[1], prev[2], prev[3], lambda k=prev[3]: self._knocks.get(self._known(k, self._knocks))))
        for i, p in enumerate(plucks):
            if p.get("final") is False:
                break                         # (and every later one: not all in yet)
            nxt = plucks[i + 1]["t"] if i + 1 < len(plucks) else None
            key = self._key(listen, p["t"] + off)
            # right after another event the first knock still rings: this one's note can't be
            # asked to start here (its own attack is the onset found)
            att = not (ev and p["t"] + off - ev[-1][0] <= self.DTAP_GAP[1])
            ev.append((p["t"] + off, bool(p.get("ok")), key, lambda k=key, t=p["t"], n=nxt, a=att: knock(k, t, n, a)))
            if not p.get("ok") and not (p.get("why") or "").startswith(("steady hum", "hidden by")) \
                    and not self._known(key, self._hist_gone) and (listen, p["t"] + off) >= self._hist_floor:
                # two quick knocks are one event: the second one inside it
                if knock(key, p["t"], nxt):
                    t2 = quick(key, p["t"], nxt)
                    if t2 is not None:
                        clear(key, None, t2 + off)
                        fired = True
        for (ta, oka, ka, kna), (tb, okb, kb, knb) in zip(ev, ev[1:]):
            if oka or okb or not self.DTAP_GAP[0] <= tb - ta <= self.DTAP_GAP[1]:
                continue
            if self._known(ka, self._hist_gone) or self._known(kb, self._hist_gone) \
                    or (listen, ta) < self._hist_floor:
                continue                      # (acted on already, or from before a Reset)
            if not (kna() and knb()):
                continue
            clear(ka, kb, tb)
            fired = True
        if final:
            # the piece's last event, for the next piece's first: its knock asked now, with this
            # piece's samples, and only when a next event could still be close enough
            last = plucks[-1] if plucks else None
            self._dtap_prev = None
            if last is not None:
                t, key = last["t"] + off, self._key(listen, last["t"] + off)
                if not last.get("ok") and dur is not None and last["t"] >= dur - self.DTAP_GAP[1]:
                    knock(key, last["t"], None)
                self._dtap_prev = (listen, t, bool(last.get("ok")), key)
        return fired

    def _handle_history_reset(self, web_request):
        """From the page (via Moonraker), also while G-code waits for a listen: a clean history.
        Plucks heard up to now in a running listen don't come back with its later analyses: the
        floor is now, in the listen's own time (t_off + a pluck's t: the pieces' recorded time,
        without the gaps between them)."""
        now = getattr(self, "_listen_t0", None)
        self._history_clear()                # (floor: before any later listen)
        if now and self.state == "listening" and (self.measurement or {}).get("belt") == "free":
            listen, t_off, mono = now
            with self._hist_lock:
                self._hist_floor = (listen, t_off + (0. if mono is None else max(0., self.reactor.monotonic() - mono)))
        web_request.send({"cleared": True})

    def _handle_accept(self, web_request):
        """From the page (via Moonraker), while G-code waits for the listen: end a free tune."""
        m = self.measurement or {}
        self._accept = self.state == "listening" and m.get("belt") == "free"
        web_request.send({"accepted": self._accept})

    # ---------------- raw recordings, readable from Mainsail (Kalico's own go to /tmp) ----------------
    REC_KEEP = 40
    LIVE_WAIT = 60.     # s at most for a live analysis still running when a listen ends
    LIVE_MIN = 0.2      # s: live analyses come at most this often (5 a second)
    LISTEN_STEP = 0.25  # s: a listen checks this often whether it should stop

    def _record(self, chip, toolhead, secs, live=None, stop=None):
        """Record for secs. live: a function of the samples so far that does the listen's live
        work (Tuner Mode, the taps), called as often as LIVE_MIN allows while recording, in a
        thread, so a slow host doesn't hold up Kalico. stop: checked every LISTEN_STEP s, True
        ends the recording."""
        import chordata_belts as belts
        belts.clear_cache(windows_only=True)     # the last recording's window spectra: not needed again
        client = chip.start_internal_client()
        timer = None
        if live is not None:
            busy = []
            pace = {"dt": self.LIVE_MIN}       # how long the last live analysis took
            got = {"msgs": 0, "rows": []}       # the samples gathered so far (only new messages are read)
            self._live_gen = gen_now = getattr(self, "_live_gen", 0) + 1

            def work(samples):
                t0 = time.monotonic()
                try:
                    if self._live_gen == gen_now:  # (a late one can't overwrite the final result)
                        live(samples)
                except Exception:
                    logging.exception("chordata: live belt analysis")
                finally:
                    pace["dt"] = time.monotonic() - t0
                    busy.clear()

            def tick(eventtime):
                try:
                    if not busy:
                        msgs = list(client.msgs)
                        if len(msgs) < got["msgs"]:          # (the helper started its list over)
                            got["msgs"], got["rows"] = 0, []
                        for m in msgs[got["msgs"]:]:
                            got["rows"].extend(x for x in m["data"] if x[0] >= client.request_start_time)
                        got["msgs"] = len(msgs)
                        if len(got["rows"]) > 200:
                            busy.append(1)
                            threading.Thread(target=work, args=(list(got["rows"]),), daemon=True).start()
                except Exception:
                    logging.exception("chordata: live belt samples")
                    return self.reactor.NEVER
                # as often as LIVE_MIN, but never more than a third of the time analysing: the
                # analysis thread holds Python's GIL while it runs, and Kalico's own work shares
                # the process with it
                return eventtime + max(self.LIVE_MIN, 3.0 * pace["dt"])
        try:
            if live is not None:
                timer = self.reactor.register_timer(tick, self.reactor.monotonic() + 0.5)
            if stop is not None:
                # in short steps, so a listen told to stop ends there
                end = self.reactor.monotonic() + secs
                while self.reactor.monotonic() < end:
                    toolhead.dwell(self.LISTEN_STEP)
                    toolhead.wait_moves()
                    if stop():
                        break
            else:
                toolhead.dwell(secs)
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
