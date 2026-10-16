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
        self.belt_target = config.getfloat("belt_target", 110., minval=20., maxval=400.)
        self.state, self.message = "idle", ""
        self.belts = {}           # the belt tension session, for the prompts
        self.belt_now = None      # the belt being listened to ("tap" while learning the toolhead)
        self.belt_plucks = 5
        self.belt_live = []       # plucks heard so far while listening
        self._tool_info = None
        for name in ("START", "LISTEN", "TAP", "POS"):
            fn = getattr(self, "cmd_CHORDATA_" + name)
            self.gcode.register_command("_CHORDATA_" + name, fn,
                                        desc=getattr(self, "cmd_CHORDATA_%s_help" % name))
        self.gcode.register_command("CHORDATA_RECORD", self.cmd_CHORDATA_RECORD,
                                    desc=self.cmd_CHORDATA_RECORD_help)

    def get_status(self, eventtime):
        return {"state": self.state, "message": self.message, "tools": self._tools(),
                "belts": self.belts, "belt_target": self.belt_target, "belt_tol": self.belt_tol,
                "belt_now": self.belt_now if self.belt_now in ("A", "B") else None,
                "belt_live": self.belt_live, "belt_pos": self._belt_pos()}

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

    # ---------------- belt tension from plucks ----------------
    cmd_CHORDATA_START_help = "Chordata: start belt tensioning (TARGET=Hz)"

    def cmd_CHORDATA_START(self, gcmd):
        if self.printer.lookup_object("resonance_tester", None) is None:
            raise gcmd.error("Chordata: belt tension needs an accelerometer ([resonance_tester])")
        if not self._tools()["corexy"]:
            raise gcmd.error("Chordata: the belt step is made for CoreXY printers")
        target = gcmd.get_float("TARGET", self.belt_target, minval=20., maxval=400.)
        plucks = 5      # on target: the average of 5 good plucks
        self.belt_plucks = plucks
        self.belts = {"target": target, "plucks": plucks, "round": 0,
                      "A": None, "B": None, "heard": [], "advice": "Measure belt A", "done": False,
                      "tol": self.belt_tol, "tap": [], "fails": {"A": 0, "B": 0}, "deaf": False,
                      "possible": False, "good": {}, "prev": {}}
        gcmd.respond_info("Chordata: belt tension, target %.0f Hz, %d plucks per belt" % (target, plucks))

    cmd_CHORDATA_LISTEN_help = "Chordata: record plucks of one belt (BELT=A|B) and find its frequency"

    def cmd_CHORDATA_LISTEN(self, gcmd):
        try:
            self._belt_listen(gcmd)
        except Exception:
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
        rt = self.printer.lookup_object("resonance_tester")
        chips = getattr(rt, "accel_chips", None) or []
        if not chips:
            raise gcmd.error("Chordata: no accelerometer found")
        chip = chips[0][1]
        n = self.belts["plucks"]
        secs = gcmd.get_float("SECONDS", 2.0 + 1.3 * n, minval=2., maxval=30.)
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.wait_moves()
        try:
            # a second of background first (fans, motors), so they aren't taken for the belt
            self.belt_live = []
            self.state, self.message = "listening", "Hands off the printer for a moment"
            quiet = self._record(chip, toolhead, 1.5)
            self.belt_now = belt
            self.message = "Pluck belt %s about once a second until %d are counted" % (belt, n)
            if gcmd.get_int("UI", 0):
                self.gcode.run_script_from_command("_CHORDATA_UI STEP=pluck BELT=%s" % belt)
            live = dict(quiet=[[s[i] for s in quiet] for i in (1, 2, 3)], expect=n,
                        avoid=self.belts.get("tap", []), target=self.belts["target"],
                        prev=(self.belts.get("good") or {}).get(belt),
                        other=(self.belts.get("B" if belt == "A" else "A") or {}).get("freq"))
            samples = self._record(chip, toolhead, secs, live=live)
        finally:
            self.belt_now = None
            self.state, self.message = "idle", ""
        if not samples:
            raise gcmd.error("Chordata: the accelerometer sent no data")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._save_recording("belt%s-%s-quiet" % (belt, stamp), quiet)
        self._save_recording("belt%s-%s" % (belt, stamp), samples)
        t = [s[0] for s in samples]
        other = (self.belts.get("B" if belt == "A" else "A") or {}).get("freq")
        good = dict(self.belts.get("good") or {})
        res = belts.analyse(t, [s[1] for s in samples], [s[2] for s in samples],
                            [s[3] for s in samples], target=self.belts["target"], expect=n,
                            quiet=[[s[i] for s in quiet] for i in (1, 2, 3)],
                            avoid=self.belts.get("tap", []), other=other, prev=good.get(belt))
        self.belt_live = res["plucks"]
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
        clear = any(p["ok"] or p["why"] not in ("no belt tone", "too weak", "a knock, not a belt")
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
        for p in res["plucks"]:
            if not p["ok"]:
                gcmd.respond_info("Chordata: belt %s, pluck at %.1f s left out: %s" % (belt, p["t"], p["why"]))
        if res["freq"] is None:
            gcmd.respond_info("Chordata: belt %s: %s" % (belt, res["note"]))
        else:
            gcmd.respond_info("Chordata: belt %s: %.1f Hz (%s)" % (belt, res["freq"], res["note"]))

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
                if "x" not in self._bpos or "y" not in self._bpos:
                    self._bpos = None
            except Exception:
                pass
        return self._bpos

    cmd_CHORDATA_POS_help = ("Chordata: SAVE=1 stores where the toolhead is as the belt tensioning "
                                  "position; GO=1 moves there (Z only ever goes up)")

    def cmd_CHORDATA_POS(self, gcmd):
        th = self.printer.lookup_object("toolhead")
        homed = th.get_status(self.reactor.monotonic())["homed_axes"]
        if "x" not in homed or "y" not in homed:
            raise gcmd.error("Chordata: home the printer first")
        pos = self.printer.lookup_object("gcode_move").get_status(self.reactor.monotonic())["gcode_position"]
        if gcmd.get_int("SAVE", 0):
            d = {"x": round(pos[0], 2), "y": round(pos[1], 2), "z": round(pos[2], 2) if "z" in homed else None}
            os.makedirs(self.out_dir, exist_ok=True)
            with open(self._belt_pos_file(), "w") as f:
                json.dump(d, f)
            self._bpos = {k: v for k, v in d.items() if v is not None}
            gcmd.respond_info("Chordata: belt tensioning position saved (X %.1f Y %.1f)" % (d["x"], d["y"]))
            return
        if gcmd.get_int("GO", 0):
            bp = self._belt_pos()
            if not bp:
                raise gcmd.error("Chordata: no belt tensioning position saved yet")
            g = self.printer.lookup_object("gcode")
            moves = ["SAVE_GCODE_STATE NAME=_chordata_bpos", "G90"]
            if "z" in homed and bp.get("z") is not None and pos[2] < bp["z"]:
                moves.append("G1 Z%.2f F600" % bp["z"])           # up first, never down
            moves += ["G1 X%.2f Y%.2f F6000" % (bp["x"], bp["y"]), "RESTORE_GCODE_STATE NAME=_chordata_bpos"]
            g.run_script_from_command("\n".join(moves))

    cmd_CHORDATA_TAP_help = ("Chordata: learn how the toolhead rings by itself from one light tap, "
                                  "so a tap is never taken for a belt")

    def cmd_CHORDATA_TAP(self, gcmd):
        try:
            self._belt_tap(gcmd)
        except Exception:
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
        toolhead.wait_moves()
        try:
            self.state, self.message = "listening", "Hands off the printer for a moment"
            quiet = self._record(chip, toolhead, 1.5)
            self.belt_now = "tap"
            self.message = "Tap the toolhead once, lightly"
            if gcmd.get_int("UI", 0):
                self.gcode.run_script_from_command("_CHORDATA_UI STEP=tapnow")
            samples = self._record(chip, toolhead, 3.0)
        finally:
            self.belt_now = None
            self.state, self.message = "idle", ""
        if not samples:
            raise gcmd.error("Chordata: the accelerometer sent no data")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._save_recording("tap-%s-quiet" % stamp, quiet)
        self._save_recording("tap-%s" % stamp, samples)
        res = belts.analyse_tap([s[0] for s in samples], [s[1] for s in samples],
                                [s[2] for s in samples], [s[3] for s in samples],
                                quiet=[[s[i] for s in quiet] for i in (1, 2, 3)])
        b = dict(self.belts)
        b["tap"] = res["modes"]
        b["tap_note"] = res["note"]
        self.belts = b
        if res["modes"]:
            gcmd.respond_info("Chordata: the toolhead rings by itself at %s Hz; ignored when listening to the belts"
                              % ", ".join("%.1f" % m for m in res["modes"]))
        else:
            gcmd.respond_info("Chordata: tap: %s" % res["note"])

    # ---------------- raw recordings, readable from Mainsail (Kalico's own go to /tmp) ----------------
    REC_KEEP = 40

    def _record(self, chip, toolhead, secs, live=None):
        """Record for secs. live: analyse() arguments; then the plucks heard so far are
        put in belt_live about once a second while recording (in a thread, so a slow
        host doesn't hold up Kalico)."""
        client = chip.start_internal_client()
        timer = None
        if live is not None:
            import chordata_belts as belts
            busy = []
            self._live_gen = gen_now = getattr(self, "_live_gen", 0) + 1

            def work(samples):
                try:
                    res = belts.analyse([s[0] for s in samples], [s[1] for s in samples],
                                        [s[2] for s in samples], [s[3] for s in samples], **live)
                    if self.belt_now in ("A", "B") and self._live_gen == gen_now:
                        self.belt_live = res["plucks"]
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
            timer = self.reactor.register_timer(tick, self.reactor.monotonic() + 1.5)
        try:
            toolhead.dwell(secs)
            n = (live or {}).get("expect")
            if n:
                # a pluck was left out (the other belt ringing, a knock): keep listening,
                # a little while, until the plucks needed are counted
                toolhead.wait_moves()
                end = self.reactor.monotonic() + 2.0 + 1.3 * n
                while self.reactor.monotonic() < end:
                    heard = self.belt_live or []
                    if not heard or sum(1 for p in heard if p.get("ok")) >= n:
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

    def _save_recording(self, name, samples):
        """Write samples as ACCELEROMETER_MEASURE's CSV into chordata/recordings."""
        if not samples:
            return None
        d = os.path.join(self.out_dir, "recordings")
        path = os.path.join(d, name + ".csv")
        keep = self.REC_KEEP

        def write():
            # in a thread like Kalico's own writer: a 30 s recording takes a while on a Pi
            try:
                os.makedirs(d, exist_ok=True)
                with open(path + ".part", "w") as f:
                    f.write("#time,accel_x,accel_y,accel_z\n")
                    f.writelines("%.6f,%.6f,%.6f,%.6f\n" % tuple(s[:4]) for s in samples)
                os.replace(path + ".part", path)
                old = sorted((os.path.getmtime(os.path.join(d, n)), n)
                             for n in os.listdir(d) if n.endswith(".csv"))
                for _, n in old[:-keep]:
                    os.remove(os.path.join(d, n))
            except OSError as e:
                logging.warning("chordata: could not save recording %s: %s", name, e)
        threading.Thread(target=write, daemon=True).start()
        return path

    cmd_CHORDATA_RECORD_help = ("Chordata: record an accelerometer for SECONDS and save it where "
                                "Mainsail can download it (CHIP=, SECONDS=, NAME=)")

    def cmd_CHORDATA_RECORD(self, gcmd):
        name = gcmd.get("CHIP", "")
        chip = self._find_chip(name)
        if chip is None:
            raise gcmd.error("Chordata: no accelerometer %s" % (name or "found"))
        secs = gcmd.get_float("SECONDS", 10., minval=.5, maxval=30.)
        label = re.sub(r"[^A-Za-z0-9_-]", "_", gcmd.get("NAME", "")) or time.strftime("%Y%m%d-%H%M%S")
        label = "%s-%s" % (re.sub(r"[^A-Za-z0-9_-]", "_", name or "accel"), label)
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.wait_moves()
        gcmd.respond_info("Chordata: recording %.0f s, go" % secs)
        samples = self._record(chip, toolhead, secs)
        if not samples:
            raise gcmd.error("Chordata: the accelerometer sent no data")
        path = self._save_recording(label, samples)
        gcmd.respond_info("Chordata: saving %d samples to %s" % (
            len(samples), os.path.relpath(path, self.cfg_dir)))


def load_config(config):
    return Chordata(config)
