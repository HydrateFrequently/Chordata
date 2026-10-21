"""Printer interlocks and measurement lifecycle, without a running Klipper host."""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from chordata import Chordata
import chordata_belts as belts


class Command:
    def __init__(self, **values):
        self.values = values
        self.messages = []

    def get(self, key, default=None):
        return self.values.get(key, default)

    def get_int(self, key, default=None, **bounds):
        return int(self.get(key, default))

    def get_float(self, key, default=None, **bounds):
        return float(self.get(key, default))

    def error(self, message):
        return ValueError(message)

    def respond_info(self, message):
        self.messages.append(message)


def plugin(objects=None):
    obj = Chordata.__new__(Chordata)
    obj.reactor = SimpleNamespace(monotonic=lambda: 100., NEVER=1e30)
    objects = dict(objects or {})
    obj.printer = SimpleNamespace(lookup_object=lambda name, default=None: objects.get(name, default),
                                  lookup_objects=lambda module=None: list(objects.items()))
    obj.gcode = SimpleNamespace(respond_raw=lambda *args: None)
    obj.state, obj.message = "idle", ""
    obj.measurement = {"phase": "idle", "belt": None}
    obj.belt_now, obj.belt_live = None, []
    obj.belt_tol, obj.belt_target = .25, 110.
    obj._tool_info = {"corexy": True}
    obj.belts = {"target": 110., "tol": .25, "tap": [], "tap_done": True}
    return obj


def status(**values):
    return SimpleNamespace(get_status=lambda now: values)


class SafetyTests(unittest.TestCase):
    def test_all_direct_commands_reject_printing_and_paused(self):
        for state in ("printing", "paused"):
            for command in ("START", "FREE", "TAP", "POS", "RECORD"):
                with self.subTest(state=state, command=command):
                    obj = plugin({"print_stats": status(state=state)})
                    with self.assertRaisesRegex(ValueError, "finish or cancel"):
                        getattr(obj, "cmd_CHORDATA_" + command)(Command())

    def test_free_tune_needs_the_tap_and_leaves_belts_alone(self):
        obj = plugin({"print_stats": status(state="standby")})
        obj.belts["tap_done"] = False
        before = json.dumps(obj.belts, sort_keys=True)
        with self.assertRaisesRegex(ValueError, "tap the toolhead"):
            obj.cmd_CHORDATA_FREE(Command())
        self.assertEqual(json.dumps(obj.belts, sort_keys=True), before)
        self.assertEqual(obj.state, "idle")

    def test_free_tune_ends_by_itself_after_a_minute_without_plucks(self):
        chip = object()
        obj = plugin({"print_stats": status(state="standby"),
                      "resonance_tester": SimpleNamespace(accel_chips=[("adxl345", chip)]),
                      "toolhead": SimpleNamespace(wait_moves=lambda: None)})
        obj._record_quiet = lambda *args, **kw: [(0., 0., 0., 0.)] * 10
        looks = []

        def record(chip, toolhead, secs, live=None, stop=None, keep=None, sink=None):
            # the stream: a look every second at its last `keep` s, the new second to the sink
            for k in range(1, 1000):
                t1 = float(k)
                rows = [(max(0., t1 - keep) + i * .01, 0., 0., 0.) for i in range(int(min(t1, keep) * 100))]
                sink([(t1 - 1. + i * .01, 0., 0., 0.) for i in range(100)])
                live(rows)
                looks.append(t1)
                if stop():
                    return rows
            return rows
        obj._record = record
        saved = []
        obj._save_recording = lambda name, samples, append=False: saved.append((name, append))
        calls = []

        def analyse(*args, **kw):
            # one pluck at 0.5 s into the stream, heard by the first looks only
            calls.append(1)
            return {"plucks": [{"t": .5, "ok": True, "freq": 110., "why": ""}], "onsets": [.5]} \
                if len(calls) <= 2 else {"plucks": [], "onsets": []}
        with patch.object(belts, "analyse", analyse):
            obj.cmd_CHORDATA_FREE(Command())
        # the last pluck at 0.5 s: it stops once more than 60 s passed after it
        self.assertEqual(looks[-1], 61.)
        self.assertEqual(obj.state, "idle")
        # the whole listen goes into one file, as it comes
        self.assertEqual(len(saved), len(looks))
        self.assertEqual(len({n for n, _ in saved}), 1)
        self.assertTrue(all(a for _, a in saved))

    def _tap_plugin(self, result):
        obj = plugin({"print_stats": status(state="standby"),
                      "resonance_tester": SimpleNamespace(accel_chips=[("adxl345", object())]),
                      "toolhead": SimpleNamespace(wait_moves=lambda: None)})
        obj._record_quiet = lambda *args, **kw: [(0., 0., 0., 0.)] * 10
        stops = []

        def record(chip, toolhead, secs, live=None, stop=None, keep=None, sink=None):
            live([(0., 0., 0., 0.)] * 300)
            stops.append(stop())
            return [(0., 0., 0., 0.)] * 300
        obj._record = record
        obj._save_recording = lambda *args, **kw: None
        with patch.object(belts, "analyse_taps", lambda *a, **k: result):
            obj.cmd_CHORDATA_TAP(Command())
        return obj, stops

    def test_three_good_taps_make_the_fingerprint(self):
        main = {"freq": 105.8, "tau": .1, "width": 4., "guard": 2.}
        taps = [{"t": 1., "ok": True, "why": "", "freq": 105.8}, {"t": 2., "ok": False, "why": "another spot", "freq": 80.},
                {"t": 3., "ok": True, "why": "", "freq": 105.7}, {"t": 4., "ok": True, "why": "", "freq": 105.8}]
        obj, stops = self._tap_plugin({"taps": taps, "good": 3, "bad": 1, "main": main, "modes": [105.8],
                                       "ring": [main], "tau": .1, "note": ""})
        self.assertEqual(stops, [True])                  # it stops by itself at 3 good taps
        self.assertTrue(obj.belts["tap_done"])
        self.assertEqual(obj.belts["tap_main"], main)
        self.assertEqual(obj.belts["tap_bad"], 1)
        self.assertEqual(obj.tap_live, taps)

    def test_note_above_the_chip_range_is_kept_and_flagged(self):
        main = {"freq": 403.2, "tau": .1, "width": 4., "guard": 2.}
        taps = [{"t": float(i), "ok": True, "why": "", "freq": 403.2} for i in (1, 2, 3)]
        result = {"taps": taps, "good": 3, "bad": 0, "main": main, "modes": [403.2], "ring": [main], "tau": .1, "note": ""}
        with patch.object(Chordata, "_accel", lambda self: {"name": "BMI160", "lo": 0, "hi": 353}):
            obj, _ = self._tap_plugin(result)
        self.assertEqual(obj.belts["tap_main"], main)     # still learned and guarded
        self.assertEqual(obj.belts["tap_range"], "above what the BMI160 hears well")
        with patch.object(Chordata, "_accel", lambda self: {"name": "ADXL345", "lo": 0, "hi": 1440}):
            obj, _ = self._tap_plugin(result)
        self.assertEqual(obj.belts["tap_range"], "")

    def test_nine_bad_taps_stop_with_advice(self):
        taps = [{"t": float(i), "ok": False, "why": "two taps at once", "freq": None} for i in range(9)]
        obj, stops = self._tap_plugin({"taps": taps, "good": 0, "bad": 9, "main": None, "modes": [],
                                       "ring": [], "tau": None, "note": "no good tap"})
        self.assertEqual(stops, [True])
        self.assertFalse(obj.belts["tap_done"])
        self.assertEqual(obj.belts["tap_note"], "too many bad taps")

    def test_nine_taps_on_another_spot_say_so(self):
        taps = [{"t": 1., "ok": True, "why": "", "freq": 105.8}] + [
            {"t": float(i + 2), "ok": False, "why": "another spot", "freq": 80.} for i in range(9)]
        obj, stops = self._tap_plugin({"taps": taps, "good": 1, "bad": 9, "main": None, "modes": [],
                                       "ring": [], "tau": None, "note": "1 of 3 good taps"})
        self.assertEqual(stops, [True])
        self.assertFalse(obj.belts["tap_done"])
        self.assertEqual(obj.belts["tap_note"], "taps on another spot")

    def test_active_sd_and_pause_interlocks_without_print_stats(self):
        for name, value in (("virtual_sdcard", status(is_active=True)),
                            ("pause_resume", status(is_paused=True)),
                            ("idle_timeout", status(state="Printing"))):
            with self.subTest(object=name):
                with self.assertRaises(ValueError):
                    plugin({name: value})._require_idle(Command())

    def test_manual_idle_allowed_with_standby_print_stats(self):
        plugin({"print_stats": status(state="standby"),
                "idle_timeout": status(state="Printing")})._require_idle(Command())

    def test_printune_measuring_blocks_chordata(self):
        for state in ("listening", "shaper", "soaking"):
            with self.subTest(state=state):
                obj = plugin({"printune": SimpleNamespace(state=state)})
                with self.assertRaisesRegex(ValueError, "Printune is using the printer"):
                    obj.cmd_CHORDATA_START(Command())
        plugin({"printune": SimpleNamespace(state="idle")})._require_idle(Command())

    def test_measurement_excludes_other_direct_commands(self):
        obj = plugin()
        obj._begin_measurement("A", 5)
        with self.assertRaisesRegex(ValueError, "another measurement"):
            obj.cmd_CHORDATA_START(Command())

    def test_nonfinite_target_rejected_without_resetting_session(self):
        obj = plugin({"resonance_tester": object()})
        before = obj.belts
        with self.assertRaisesRegex(ValueError, "finite"):
            obj.cmd_CHORDATA_START(Command(TARGET="nan"))
        self.assertIs(obj.belts, before)

    def test_wait_failure_releases_measurement(self):
        def fail():
            raise RuntimeError("shutdown")
        obj = plugin({"resonance_tester": SimpleNamespace(accel_chips=[("xy", object())]),
                      "toolhead": SimpleNamespace(wait_moves=fail)})
        with self.assertRaisesRegex(RuntimeError, "shutdown"):
            obj.cmd_CHORDATA_TAP(Command())
        self.assertEqual(obj.state, "idle")
        self.assertEqual(obj.measurement["phase"], "error")

    def test_start_clears_old_live_results(self):
        obj = plugin({"resonance_tester": object()})
        obj.belt_live = [{"freq": 110.}]
        obj.measurement = {"phase": "complete", "result": {"freq": 110.}}
        obj.cmd_CHORDATA_START(Command())
        self.assertEqual(obj.belt_live, [])
        self.assertEqual(obj.measurement, {"phase": "idle", "belt": None})

    def test_status_exposes_elapsed_without_mutating_snapshot(self):
        obj = plugin()
        obj._bpos = None
        obj._begin_measurement("B", 5)
        snapshot = obj.get_status(103.5)
        self.assertEqual(snapshot["measurement"]["elapsed"], 3.5)
        self.assertEqual(snapshot["measurement"]["belt"], "B")
        self.assertEqual(obj.measurement["elapsed"], 0.)

    def test_background_is_recorded_once_per_session(self):
        obj = plugin({"resonance_tester": SimpleNamespace(accel_chips=[("xy", object())]),
                      "toolhead": SimpleNamespace(wait_moves=lambda: None)})
        obj._quiet = None
        lengths, quiets = [], []
        def record(chip, toolhead, secs, **kw):
            lengths.append(secs)
            return [(i * secs / 99., float(len(lengths)), 0., 0.) for i in range(100)]
        obj._record = record
        obj._save_recording = lambda *args, **kw: None
        def analysis(*args, **kwargs):
            quiets.append(kwargs["quiet"])
            return {"hum": [], "plucks": [{"t": 1., "ok": True, "why": "", "freq": 110.}]}
        with patch("chordata_belts.analyse", side_effect=analysis):
            obj.cmd_CHORDATA_FREE(Command(SECONDS=8.))
            obj.cmd_CHORDATA_FREE(Command(SECONDS=8.))
        self.assertEqual(lengths.count(1.5), 1)          # one background, then only the plucks
        self.assertEqual(quiets[0], quiets[1])

    def _accel_plugin(self, settings):
        cf = SimpleNamespace(get_status=lambda now: {"settings": settings})
        obj = plugin({"configfile": cf, "resonance_tester": object()})
        obj._accel_cfg = None
        return obj

    def test_accelerometer_from_the_config(self):
        cases = [({"resonance_tester": {"accel_chip": "adxl345"}, "adxl345": {}}, None, ("ADXL345", 1440)),
                 ({"resonance_tester": {"accel_chip": "adxl345 tool"}, "adxl345 tool": {"rate": 1600}}, None, ("ADXL345", 720)),
                 ({"resonance_tester": {"accel_chip_x": "lis3dh", "accel_chip_y": "adxl345"}}, None, ("LIS3DH", 149)),
                 ({"resonance_tester": {"accel_chip": "mpu9250"}}, 4000., ("MPU-9250", 1130)),
                 ({"resonance_tester": {"accel_chip": "mpu9250"}}, 1000., ("MPU-6050", 184)),
                 ({"resonance_tester": {"accel_chip": "beacon"}}, None, ("Beacon", None)),
                 ({"resonance_tester": {"accel_chip": "beacon"}}, 3083.4, ("Beacon", 1387))]
        for settings, fs, want in cases:
            obj = self._accel_plugin(settings)
            obj._accel_fs = fs
            a = obj._accel()
            self.assertEqual((a["name"], a["hi"]), want, settings)
            self.assertEqual(a["lo"], 0)
        self.assertIsNone(self._accel_plugin({})._accel())

    def test_target_out_of_the_accelerometers_range_is_refused(self):
        obj = self._accel_plugin({"resonance_tester": {"accel_chip": "lis3dh"}, "printer": {"kinematics": "corexy"}})
        obj._tool_info = None
        with self.assertRaisesRegex(ValueError, "out of range"):
            obj.cmd_CHORDATA_START(Command(TARGET=160.))

    def test_position_file_rejects_nonfinite_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            obj = plugin()
            obj.out_dir = directory
            with open(obj._belt_pos_file(), "w") as output:
                json.dump({"x": float("nan"), "y": 20.}, output)
            self.assertIsNone(obj._belt_pos())

    def test_position_move_restores_gcode_state_after_failure(self):
        scripts = []
        def run(script):
            scripts.append(script)
            if "G1 X" in script:
                raise RuntimeError("Move out of range")
        obj = plugin({"toolhead": SimpleNamespace(wait_moves=lambda: None,
                      get_status=lambda now: {"homed_axes": "xyz"}),
                      "gcode_move": status(gcode_position=[50., 50., 20.]),
                      "gcode": SimpleNamespace(run_script_from_command=run)})
        obj._bpos = {"x": 100., "y": 100., "z": 10.}
        with self.assertRaisesRegex(RuntimeError, "out of range"):
            obj.cmd_CHORDATA_POS(Command(GO=1))
        self.assertTrue(scripts[-1].startswith("RESTORE_GCODE_STATE"))
        self.assertNotIn("G1 Z", scripts[1])  # saved Z below current Z never lowers it


class CaptureTests(unittest.TestCase):
    def test_stream_stopped_when_dwell_fails(self):
        events = []
        client = SimpleNamespace(finish_measurements=lambda: events.append("finish"))
        chip = SimpleNamespace(start_internal_client=lambda: client)
        def fail(seconds):
            raise RuntimeError("shutdown")
        with self.assertRaisesRegex(RuntimeError, "shutdown"):
            plugin()._record(chip, SimpleNamespace(dwell=fail), 2.)
        self.assertEqual(events, ["finish"])

    def test_stream_stopped_when_timer_registration_fails(self):
        events = []
        client = SimpleNamespace(finish_measurements=lambda: events.append("finish"))
        chip = SimpleNamespace(start_internal_client=lambda: client)
        obj = plugin()
        def fail(*args):
            raise RuntimeError("timer unavailable")
        obj.reactor.register_timer = fail
        with self.assertRaisesRegex(RuntimeError, "timer unavailable"):
            obj._record(chip, SimpleNamespace(), 2., live=lambda samples: None)
        self.assertEqual(events, ["finish"])

    def test_late_live_analysis_cannot_overwrite_final_result(self):
        workers = []
        samples = [(i / 1000., 0., 0., 0.) for i in range(300)]
        client = SimpleNamespace(msgs=[{"data": samples}], request_start_time=0.,
                                 finish_measurements=lambda: None, get_samples=lambda: samples)
        obj = plugin()
        obj.belt_now = "free"
        obj.reactor.register_timer = lambda tick, when: setattr(obj, "tick", tick) or 1
        obj.reactor.unregister_timer = lambda timer: None
        class Worker:
            def __init__(self, target, args=(), daemon=None, **kw):
                workers.append(lambda: target(*args))
            def start(self):
                pass
            def is_alive(self):
                return False
        clock = [100.]
        obj.reactor.monotonic = lambda: clock[0]
        obj.reactor.pause = lambda when: clock.__setitem__(0, when)
        with patch("chordata.threading.Thread", Worker):
            obj._record(SimpleNamespace(start_internal_client=lambda: client),
                        SimpleNamespace(dwell=lambda seconds: obj.tick(100.)), 2.,
                        live=lambda part: setattr(obj, "belt_live", ["stale"]))
            obj.belt_live = ["final"]
            workers[0]()
        self.assertEqual(obj.belt_live, ["final"])
        # the recording waits (yielding to the reactor) for a live analysis still running,
        # LIVE_WAIT at most, so the final one doesn't run beside it
        self.assertGreaterEqual(clock[0], 100. + obj.LIVE_WAIT)

    def test_listen_end_waits_for_the_live_analysis(self):
        workers, order = [], []
        samples = [(i / 1000., 0., 0., 0.) for i in range(300)]
        client = SimpleNamespace(msgs=[{"data": samples}], request_start_time=0.,
                                 finish_measurements=lambda: None, get_samples=lambda: samples)
        obj = plugin()
        obj.belt_now = "free"
        obj.reactor.register_timer = lambda tick, when: setattr(obj, "tick", tick) or 1
        obj.reactor.unregister_timer = lambda timer: None
        clock = [100.]
        obj.reactor.monotonic = lambda: clock[0]

        def pause(when):            # the thread runs while the reactor waits
            order.append("wait")
            clock[0] = when
            while workers:
                workers.pop(0)()
        obj.reactor.pause = pause

        class Worker:
            def __init__(self, target, args=(), daemon=None, **kw):
                workers.append(lambda: target(*args))

            def start(self):
                pass

            def is_alive(self):
                return False
        called = []
        with patch("chordata.threading.Thread", Worker):
            obj._record(SimpleNamespace(start_internal_client=lambda: client),
                        SimpleNamespace(dwell=lambda seconds: obj.tick(100.)), 2., live=called.append)
        self.assertEqual(called, [])              # the worker ran inside the wait (too late to count: skipped)
        self.assertEqual(order, ["wait"])         # one short wait, then done
        self.assertLess(clock[0], 100.1)


class AnalysisValidationTests(unittest.TestCase):
    def setUp(self):
        self.t = np.arange(3200) / 3200.
        self.axes = [np.zeros(3200) for _ in range(3)]

    def test_short_recording_reports_no_data(self):
        result = belts.analyse([0.], [0.], [0.], [0.])
        self.assertIsNone(result["freq"])
        self.assertEqual(result["verdict"], "none")

    def test_mismatched_axes_rejected(self):
        with self.assertRaisesRegex(ValueError, "equally sized"):
            belts.analyse(self.t, self.axes[0][:-1], *self.axes[1:])

    def test_nonfinite_samples_rejected(self):
        for value in (np.nan, np.inf, -np.inf):
            with self.subTest(value=value):
                self.axes[0][30] = value
                with self.assertRaisesRegex(ValueError, "non-finite"):
                    belts.analyse(self.t, *self.axes)

    def test_duplicate_or_reversed_timestamps_rejected(self):
        for change in (0., -.001):
            with self.subTest(change=change):
                self.t[30] = self.t[29] + change
                with self.assertRaisesRegex(ValueError, "increase strictly"):
                    belts.analyse(self.t, *self.axes)

    def test_low_sample_rate_rejected(self):
        with self.assertRaisesRegex(ValueError, "sample rate"):
            belts.analyse(np.arange(3200) / 100., *self.axes)

    def test_invalid_quiet_recording_rejected(self):
        for quiet in ([[]], [np.zeros(3), np.zeros(2), np.zeros(3)],
                      [np.array([np.nan])] * 3):
            with self.subTest(quiet=quiet):
                with self.assertRaisesRegex(ValueError, "quiet recording"):
                    belts.analyse(self.t, *self.axes, quiet=quiet)

    def test_invalid_parameters_rejected(self):
        for params in ({"target": np.nan}, {"target": -110.}, {"prev": np.inf},
                       {"other": 0.}, {"expect": 0}, {"expect": 2.5},
                       {"expect": True}, {"avoid": [np.nan]}):
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    belts.analyse(self.t, *self.axes, **params)

    def test_upstream_tap_ring_objects_are_accepted(self):
        from test_belts import rec, series, quiet
        result = belts.analyse(*rec(series([91.] * 5)), quiet=quiet(), expect=5,
                               avoid=[{"freq": 163., "guard": 3., "tau": .1}])
        self.assertEqual(result["used"], 5)
        self.assertAlmostEqual(result["freq"], 91., delta=.4)

    def test_invalid_tap_ring_objects_rejected(self):
        for mode in ({"freq": float("nan")}, {"freq": 110., "guard": -1.},
                     {"freq": 110., "tau": float("inf")}, {"guard": 2.}):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                belts.analyse(self.t, *self.axes, avoid=[mode])

    def test_metadata_remains_available_without_a_pluck(self):
        result = belts.analyse(self.t, *self.axes)
        self.assertEqual(result["metadata"]["sample_count"], 3200)
        self.assertEqual(result["metadata"]["sample_rate"], 3200.)
        self.assertAlmostEqual(result["metadata"]["duration"], 1., places=2)
        self.assertIsNone(result["stddev"])
        self.assertIsNone(result["spread"])

    def test_metadata_reports_observed_repeatability(self):
        from test_belts import rec, series, quiet
        result = belts.analyse(*rec(series([91.] * 5)), quiet=quiet(), expect=5)
        self.assertEqual(result["used"], 5)
        self.assertAlmostEqual(result["freq"], 91., delta=.4)
        self.assertGreaterEqual(result["spread"], 0.)
        self.assertLess(result["spread"], 1.)
        self.assertLess(result["stddev"], .5)

    @staticmethod
    def _plucks(starts, other_hz=None, tau=0.8, attack=0., phase=2.):
        """110 Hz plucks (1F across, 2F along the belt); other_hz: the other belt ringing along"""
        fs, rng = 3200., np.random.default_rng(1)
        t = np.arange(int(8 * fs)) / fs
        ax, ay, az = (rng.normal(0, 30, len(t)) for _ in range(3))
        lines = [(110., 1500., 0.)] + ([(other_hz, 1425., phase)] if other_hz else [])
        for t0 in starts:
            tt = t - t0
            env = np.where(tt >= 0, np.exp(-np.clip(tt, 0, None) / tau), 0.)
            if attack:
                env = env * (1 - np.exp(-np.clip(tt, 0, None) / attack))
            for fr, a, ph in lines:
                side = a * env * np.sin(2 * np.pi * fr * tt + ph)
                along = 3 * a * env * env * np.sin(4 * np.pi * fr * tt + 2 * ph)
                ax += 0.7 * along + 0.3 * side
                ay += 0.7 * along - 0.3 * side
                az += 0.4 * side
        quiet = [np.random.default_rng(9).normal(0, 30, int(1.5 * fs)) for _ in range(3)]
        return (t, ax, ay, az), quiet

    def test_belts_beating_are_not_two_plucks(self):
        # the other belt rings along a few Hz off: the level dips and comes back, also while
        # a soft attack is still rising
        for off, attack, phase in ((8., 0., 2.), (4., 0.04, 5.), (6., 0.03, 4.), (8., 0.06, 2.)):
            rec, quiet = self._plucks([1.0 + 1.2 * i for i in range(5)], other_hz=110. + off,
                                      attack=attack, phase=phase)
            for free in (False, True):
                result = belts.analyse(*rec, target=110, quiet=quiet, free=free)
                self.assertNotIn("two plucks at once", [x["why"] for x in result["plucks"]],
                                 (off, attack, phase, free))

    def test_a_second_pluck_in_the_window_is_refused(self):
        rec, quiet = self._plucks([1.0, 1.3, 3.0, 4.5, 6.0], tau=0.3)
        for free in (False, True):
            result = belts.analyse(*rec, target=110, quiet=quiet, free=free)
            self.assertEqual([x["why"] for x in result["plucks"]], ["two plucks at once", "", "", ""])


class ReactorTests(unittest.TestCase):
    def test_long_analysis_runs_beside_the_reactor(self):
        import time as _time
        obj = plugin()
        paused = []
        obj.reactor = SimpleNamespace(monotonic=_time.monotonic, NEVER=1e30,
                                      pause=lambda when: paused.append(when) or _time.sleep(0.01))
        self.assertEqual(obj._off_reactor(lambda a, b=0: (_time.sleep(0.2), a + b)[1], 2, b=3), 5)
        self.assertGreater(len(paused), 3)               # the reactor kept running meanwhile
        with self.assertRaisesRegex(ValueError, "bad"):
            obj._off_reactor(lambda: (_ for _ in ()).throw(ValueError("bad")))


class FakeMutex:
    """The G-code lock: test() says whether a command holds it; taking it runs what the
    holder does before letting go (on_take), as the reactor would."""

    def __init__(self, held=False, on_take=None):
        self.held, self.on_take = held, on_take

    def test(self):
        return self.held

    def __enter__(self):
        if self.on_take:
            self.on_take()
        self.held = True
        return self

    def __exit__(self, *a):
        self.held = False


class FanTests(unittest.TestCase):
    """Step 0: fans humming on the belt's note are turned off (never the safety fans) and
    turned back on when Chordata closes."""

    class _All(dict):
        """Every fan okayed by the user (the tests of turning fans off and back on)."""
        def __contains__(self, key):
            return True

    def _fan_plugin(self, speeds, okayed=True):
        settings = {"fan": {}, "fan_generic exhaust_fan": {}, "heater_fan hotend_fan": {},
                    "controller_fan board": {}, "temperature_fan chamber": {}, "printer": {}}
        objects = {"configfile": status(settings=settings), "print_stats": status(state="standby")}
        for name, speed in speeds.items():
            objects[name] = status(speed=speed)
        obj = plugin(objects)
        obj._fans_off = {}
        obj._fok = self._All() if okayed else {}
        obj.scripts = []

        def run(script):            # the fan takes the speed it is set to, as Kalico's would
            obj.scripts.append(script)
            w = script.split()
            if w[0] in ("M106", "M107"):
                objects["fan"].get_status(0)["speed"] = float(w[1][1:]) / 255. if w[0] == "M106" else 0.
            elif w[0] == "SET_FAN_SPEED":
                objects["fan_generic " + w[1][4:]].get_status(0)["speed"] = float(w[2][6:])
        obj.gcode = SimpleNamespace(respond_raw=lambda *a: None, run_script_from_command=run, run_script=run)
        obj.timers, obj.callbacks = [], []
        obj.reactor = SimpleNamespace(monotonic=lambda: 100., NEVER=1e30,
                                      register_timer=lambda f, when: obj.timers.append((f, when)) or f,
                                      register_callback=obj.callbacks.append)
        return obj

    def test_fan_list_marks_the_safety_fans(self):
        obj = self._fan_plugin({"fan": .5, "fan_generic exhaust_fan": 0., "heater_fan hotend_fan": 1.,
                                "controller_fan board": 1., "temperature_fan chamber": .3})
        fans = {f["section"]: f for f in obj._fan_list()}
        self.assertEqual(set(fans), {"fan", "fan_generic exhaust_fan", "heater_fan hotend_fan",
                                     "controller_fan board", "temperature_fan chamber"})
        self.assertTrue(fans["fan"]["button"] and fans["fan"]["on"])
        self.assertTrue(fans["fan_generic exhaust_fan"]["button"])
        self.assertFalse(fans["fan_generic exhaust_fan"]["on"])
        for k in ("heater_fan hotend_fan", "controller_fan board", "temperature_fan chamber"):
            self.assertFalse(fans[k]["button"], k)
            self.assertTrue(fans[k]["why"])

    def test_turned_off_then_restored_as_they_were(self):
        obj = self._fan_plugin({"fan": .5, "fan_generic exhaust_fan": .8, "heater_fan hotend_fan": 1.,
                                "controller_fan board": 1., "temperature_fan chamber": .3})
        order = [f["section"] for f in obj._fan_list()]
        obj.cmd_CHORDATA_FAN(Command(INDEX=order.index("fan")))
        obj.cmd_CHORDATA_FAN(Command(INDEX=order.index("fan_generic exhaust_fan")))
        self.assertEqual(obj.scripts, ["M107", "SET_FAN_SPEED FAN=exhaust_fan SPEED=0.000"])
        obj.scripts.clear()
        obj.cmd_CHORDATA_FANS(Command())
        self.assertEqual(sorted(obj.scripts), ["M106 S128", "SET_FAN_SPEED FAN=exhaust_fan SPEED=0.800"])
        obj.scripts.clear()
        obj.cmd_CHORDATA_FANS(Command())          # once only
        self.assertEqual(obj.scripts, [])

    def test_back_on_after_ten_idle_minutes_or_when_a_print_starts(self):
        obj = self._fan_plugin({"fan": .5})
        stats = {"state": "standby"}
        look = obj.printer.lookup_object
        obj.printer.lookup_object = lambda n, d=None: SimpleNamespace(get_status=lambda now: stats) \
            if n == "print_stats" else look(n, d)
        lock = [False]
        m = FakeMutex()
        m.test = lambda: lock[0]
        obj.gcode.get_mutex = lambda: m
        i = [f["section"] for f in obj._fan_list()].index("fan")
        obj.cmd_CHORDATA_FAN(Command(INDEX=i))
        (check, when), = obj.timers
        obj.scripts.clear()
        idle_end = 100. + obj.FANS_IDLE
        self.assertEqual(check(150.), 152.)              # idle, but not for 10 minutes yet
        obj.state = "listening"
        self.assertEqual(check(idle_end + 1.), idle_end + 3.)    # in use: not yet
        obj.state = "idle"
        lock[0] = True
        self.assertEqual(check(idle_end + 1.), idle_end + 2.)    # a command runs: a second later
        lock[0] = False
        self.assertEqual(check(idle_end + 1.), obj.reactor.NEVER)
        self.assertEqual(obj.scripts, ["M106 S128"])
        # a print start brings them back at once (and Chordata's own M107 is no print start)
        obj.timers.clear()
        obj.cmd_CHORDATA_FAN(Command(INDEX=i))
        (check, when), = obj.timers
        obj.scripts.clear()
        self.assertEqual(check(120.), 122.)
        self.assertEqual(obj.scripts, [])
        stats["state"] = "printing"
        self.assertEqual(check(124.), obj.reactor.NEVER)
        self.assertEqual(obj.scripts, [])            # the part fan is the slicer's in a print

    def test_back_on_while_a_print_holds_the_gcode_lock(self):
        # a print runs every line with the G-code lock held: the fans are queued, not held back.
        # The part fan is left to the slicer: queued, it would land after the start macro and
        # undo the slicer's M107 on the first layer
        obj = self._fan_plugin({"fan": .5, "fan_generic exhaust_fan": .8})
        stats = {"state": "standby"}
        look = obj.printer.lookup_object
        obj.printer.lookup_object = lambda n, d=None: SimpleNamespace(get_status=lambda now: stats) \
            if n == "print_stats" else look(n, d)
        m = FakeMutex(held=True)
        obj.gcode.get_mutex = lambda: m
        order = [f["section"] for f in obj._fan_list()]
        obj.cmd_CHORDATA_FAN(Command(INDEX=order.index("fan")))
        obj.cmd_CHORDATA_FAN(Command(INDEX=order.index("fan_generic exhaust_fan")))
        (check, when), = obj.timers
        obj.scripts.clear()
        stats["state"] = "printing"            # (the card closed with its X, then a print)
        self.assertEqual(check(102.), obj.reactor.NEVER)
        self.assertEqual(obj.scripts, ["SET_FAN_SPEED FAN=exhaust_fan SPEED=0.800"])
        self.assertEqual(obj._fans_off, {})    # and nothing is left to restore later

    def test_a_fan_the_start_macro_sets_while_waiting_keeps_its_speed(self):
        # the restore waits for the lock the start macro holds; what the macro set meanwhile
        # stays (the speed is read inside the lock, not before it)
        obj = self._fan_plugin({"fan_generic exhaust_fan": .8})
        stats = {"state": "standby"}
        look = obj.printer.lookup_object
        obj.printer.lookup_object = lambda n, d=None: SimpleNamespace(get_status=lambda now: stats) \
            if n == "print_stats" else look(n, d)
        m = FakeMutex(held=True, on_take=lambda: obj.gcode.run_script("SET_FAN_SPEED FAN=exhaust_fan SPEED=0.3"))
        obj.gcode.get_mutex = lambda: m
        obj.cmd_CHORDATA_FAN(Command(INDEX=[f["section"] for f in obj._fan_list()].index("fan_generic exhaust_fan")))
        (check, when), = obj.timers
        obj.scripts.clear()
        stats["state"] = "printing"
        self.assertEqual(check(102.), obj.reactor.NEVER)
        self.assertEqual(obj.scripts, ["SET_FAN_SPEED FAN=exhaust_fan SPEED=0.3"])     # the macro's only

    def test_a_fan_set_since_keeps_its_new_speed(self):
        # the slicer's M106 (or the user's) after Chordata turned the fan off isn't undone
        obj = self._fan_plugin({"fan": .5})
        obj.cmd_CHORDATA_FAN(Command(INDEX=[f["section"] for f in obj._fan_list()].index("fan")))
        obj.gcode.run_script("M106 S255")
        obj.scripts.clear()
        obj.cmd_CHORDATA_FANS(Command())
        self.assertEqual(obj.scripts, [])

    def test_restored_to_the_speed_asked_for_not_times_max_power(self):
        obj = self._fan_plugin({"fan": .4})       # Klipper: speed .4 = asked .8 x max_power .5
        obj.printer.lookup_object("configfile").get_status(0)["settings"]["fan"] = {"max_power": .5}
        i = [f["section"] for f in obj._fan_list()].index("fan")
        obj.cmd_CHORDATA_FAN(Command(INDEX=i))
        obj.scripts.clear()
        obj.cmd_CHORDATA_FANS(Command())
        self.assertEqual(obj.scripts, ["M106 S204"])

    def test_a_fan_named_with_capitals_is_found_and_turned_off_by_its_own_name(self):
        # the config's settings are all lowercase; the object and SET_FAN_SPEED keep the case
        # (on the Nebula: [fan_generic Bed_Circulation_Fan], refused as bed_circulation_fan)
        obj = self._fan_plugin({"fan_generic Bed_Circulation_Fan": .6})
        obj.printer.lookup_object("configfile").get_status(0)["settings"]["fan_generic bed_circulation_fan"] = {}
        fans = {f["section"]: f for f in obj._fan_list()}
        self.assertIn("fan_generic Bed_Circulation_Fan", fans)
        self.assertTrue(fans["fan_generic Bed_Circulation_Fan"]["on"])
        order = [f["section"] for f in obj._fan_list()]
        obj.cmd_CHORDATA_FAN(Command(INDEX=order.index("fan_generic Bed_Circulation_Fan")))
        self.assertEqual(obj.scripts, ["SET_FAN_SPEED FAN=Bed_Circulation_Fan SPEED=0.000"])
        obj.scripts.clear()
        obj.cmd_CHORDATA_FANS(Command())
        self.assertEqual(obj.scripts, ["SET_FAN_SPEED FAN=Bed_Circulation_Fan SPEED=0.600"])

    def test_a_generic_fan_named_for_the_electronics_gets_no_button(self):
        obj = self._fan_plugin({"fan_generic mainboard_fan": 1., "fan_generic exhaust_fan": 1.})
        obj.printer.lookup_object("configfile").get_status(0)["settings"]["fan_generic mainboard_fan"] = {}
        fans = {f["section"]: f for f in obj._fan_list()}
        self.assertFalse(fans["fan_generic mainboard_fan"]["button"])
        self.assertTrue(fans["fan_generic exhaust_fan"]["button"])

    def test_no_fan_is_turned_off_until_the_user_okays_it(self):
        import tempfile
        obj = self._fan_plugin({"fan": .5, "fan_generic exhaust_fan": .8, "heater_fan hotend_fan": 1.}, okayed=False)
        obj.out_dir = tempfile.mkdtemp()
        fans = {f["section"]: f for f in obj._fan_list()}
        self.assertEqual([(fans[k]["button"], fans[k]["ask"]) for k in ("fan", "fan_generic exhaust_fan", "heater_fan hotend_fan")],
                         [(False, True), (False, True), (False, False)])
        order = [f["section"] for f in obj._fan_list()]
        with self.assertRaisesRegex(ValueError, "not okayed"):
            obj.cmd_CHORDATA_FAN(Command(INDEX=order.index("fan_generic exhaust_fan")))
        self.assertEqual(obj.scripts, [])
        # okayed: from then on it may be turned off, also after a restart (the choice is in a file)
        obj.cmd_CHORDATA_FAN_OK(Command(INDEX=order.index("fan_generic exhaust_fan"), ALLOW=1))
        del obj._fok
        fans = {f["section"]: f for f in obj._fan_list()}
        self.assertEqual((fans["fan_generic exhaust_fan"]["button"], fans["fan"]["button"]), (True, False))
        obj.cmd_CHORDATA_FAN(Command(INDEX=order.index("fan_generic exhaust_fan")))
        self.assertEqual(obj.scripts, ["SET_FAN_SPEED FAN=exhaust_fan SPEED=0.000"])
        # and taken back, by its section name
        obj.cmd_CHORDATA_FAN_OK(Command(SECTION="Fan_Generic Exhaust_Fan", ALLOW=0))
        self.assertFalse({f["section"]: f for f in obj._fan_list()}["fan_generic exhaust_fan"]["button"])
        # deleting the file takes every allowance back at once, without a restart
        obj.cmd_CHORDATA_FAN_OK(Command(SECTION="fan", ALLOW=1))
        self.assertTrue({f["section"]: f for f in obj._fan_list()}["fan"]["button"])
        os.remove(obj._fans_ok_file())
        self.assertFalse({f["section"]: f for f in obj._fan_list()}["fan"]["button"])
        # a safety fan can't be okayed at all
        with self.assertRaisesRegex(ValueError, "leaves it on"):
            obj.cmd_CHORDATA_FAN_OK(Command(INDEX=order.index("heater_fan hotend_fan"), ALLOW=1))

    def test_safety_fans_are_never_turned_off(self):
        obj = self._fan_plugin({"heater_fan hotend_fan": 1., "controller_fan board": 1., "temperature_fan chamber": 1.})
        order = [f["section"] for f in obj._fan_list()]
        for k in ("heater_fan hotend_fan", "controller_fan board", "temperature_fan chamber"):
            with self.assertRaisesRegex(ValueError, "leaves it on"):
                obj.cmd_CHORDATA_FAN(Command(INDEX=order.index(k)))
        self.assertEqual(obj.scripts, [])

    def test_a_hum_from_a_fan_not_okayed_yet_stops_the_tap_step_to_ask(self):
        self.test_a_hum_on_the_note_stops_the_tap_step(okayed=False)

    def test_a_hum_on_the_note_stops_the_tap_step(self, okayed=True):
        from test_belts import quiet
        obj = self._fan_plugin({"fan_generic exhaust_fan": 1.}, okayed=okayed)
        q = quiet(fan=110.5)
        obj._record_quiet = lambda *a, **k: [(i / 3200., q[0][i], q[1][i], q[2][i]) for i in range(len(q[0]))]
        obj._accel_fs = 3200.
        objects = {"resonance_tester": SimpleNamespace(accel_chips=[("adxl345", object())]),
                   "toolhead": SimpleNamespace(wait_moves=lambda: None)}
        look = obj.printer.lookup_object
        obj.printer = SimpleNamespace(lookup_object=lambda n, d=None: objects.get(n, look(n, d)))
        recorded = []
        obj._record = lambda *a, **k: recorded.append(1) or []
        obj.cmd_CHORDATA_TAP(Command())
        self.assertEqual(recorded, [])                       # no tap listen
        self.assertEqual(obj.state, "idle")
        self.assertTrue(obj.belts["disturb"]["lines"])
        self.assertTrue(abs(obj.belts["disturb"]["lines"][0] - 110.5) < 0.5)
        fan = next(f for f in obj.belts["disturb"]["fans"] if f["section"] == "fan_generic exhaust_fan")
        self.assertEqual((fan["button"], fan["ask"]), (okayed, not okayed))
        # measure anyway: the taps are listened to (this fake accelerometer sends nothing)
        with self.assertRaisesRegex(ValueError, "no data"):
            obj.cmd_CHORDATA_TAP(Command(ANYWAY=1))
        self.assertEqual(recorded, [1])
        self.assertIsNone(obj.belts["disturb"])

    def test_a_fan_that_cannot_be_found_gets_no_button_and_does_not_count_as_on(self):
        obj = self._fan_plugin({})
        fans = {f["section"]: f for f in obj._fan_list()}
        self.assertFalse(fans["fan_generic exhaust_fan"]["button"])
        self.assertFalse(fans["fan_generic exhaust_fan"]["on"])

    def _tap_with_background(self, speeds, fan_hz):
        from test_belts import quiet
        obj = self._fan_plugin(speeds)
        q = quiet(fan=fan_hz)
        obj._record_quiet = lambda *a, **k: [(i / 3200., q[0][i], q[1][i], q[2][i]) for i in range(len(q[0]))]
        obj._accel_fs = 3200.
        objects = {"resonance_tester": SimpleNamespace(accel_chips=[("adxl345", object())]),
                   "toolhead": SimpleNamespace(wait_moves=lambda: None)}
        look = obj.printer.lookup_object
        obj.printer = SimpleNamespace(lookup_object=lambda n, d=None: objects.get(n, look(n, d)))
        recorded = []
        obj._record = lambda *a, **k: recorded.append(1) or []
        with self.assertRaisesRegex(ValueError, "no data"):     # this fake accelerometer sends nothing
            obj.cmd_CHORDATA_TAP(Command())
        return obj, recorded

    def test_a_hum_from_nothing_chordata_can_turn_off_does_not_stop_the_tap_step(self):
        # only the controller fan runs (the motors are held): noted, and the taps are listened to
        obj, recorded = self._tap_with_background({"controller_fan board": 1., "fan_generic exhaust_fan": 0.}, 110.5)
        self.assertEqual(recorded, [1])
        self.assertIsNone(obj.belts["disturb"])
        self.assertTrue(abs(obj.belts["hum_note"][0] - 110.5) < 0.5)

    def test_a_hum_near_twice_the_note_does_not_stop_the_tap_step(self):
        # the Nebula: 211 Hz with a 110 Hz target, and a fan Chordata could turn off running
        obj, recorded = self._tap_with_background({"fan_generic exhaust_fan": 1.}, 211.0)
        self.assertEqual(recorded, [1])
        self.assertIsNone(obj.belts["disturb"])
        self.assertEqual(obj.belts["hum_note"], [])


if __name__ == "__main__":
    unittest.main()


class HistoryTests(unittest.TestCase):
    """Tuner Mode's session history: every final pluck once, cleared by a new session, Reset
    and a double tap on the toolhead (the whole history, as Reset history)."""
    COLS = ([0.], [0.], [0.], [0.])

    @staticmethod
    def pk(t, ok=True, freq=110., why="", **kw):
        return dict({"t": t, "ok": ok, "freq": freq, "why": why if ok else (why or "a knock")}, **kw)

    def _filled(self, n=7):
        obj = plugin()
        obj.belts["tap_main"] = {"freq": 60., "guard": 2.}
        obj._history_add(1, 0., [self.pk(float(i)) for i in range(1, n + 1)] + [self.pk(n + .5, ok=False)],
                         self.COLS, None, 20., final=True)
        return obj

    def test_live_then_final_is_one_entry_and_the_final_verdict_wins(self):
        obj = plugin()
        obj._history_add(1, 0., [self.pk(1., final=True), self.pk(2., final=False)], self.COLS, None, 2.5)
        self.assertEqual([(h["n"], h["freq"], h["ok"]) for h in obj.history], [(1, 110., True)])
        obj._history_add(1, 0., [self.pk(1.02, ok=False, why="too weak"), self.pk(2., freq=111.)],
                         self.COLS, None, 8., final=True)
        self.assertEqual([(h["n"], h["freq"], h["ok"], h["why"]) for h in obj.history],
                         [(1, 110., False, "too weak"), (2, 111., True, "")])
        self.assertEqual(obj.get_status(100.)["history"], obj.history)
        # the same time in another listen (another Tuner run) is another pluck; the history stays
        obj._history_add(2, 0., [self.pk(1.)], self.COLS, None, 8., final=True)
        self.assertEqual(len(obj.history), 3)

    def test_a_tuner_run_puts_each_pluck_in_once(self):
        obj = plugin({"print_stats": status(state="standby"),
                      "resonance_tester": SimpleNamespace(accel_chips=[("adxl345", object())]),
                      "toolhead": SimpleNamespace(wait_moves=lambda: None)})
        obj._record_quiet = lambda *args, **kw: [(0., 0., 0., 0.)] * 10
        obj._save_recording = lambda *args, **kw: None
        part = [(i * .01, 0., 0., 0.) for i in range(800)]

        def record(chip, toolhead, secs, live=None, stop=None, keep=None, sink=None):
            live(part)                    # heard live (its window all in), then in the final analysis
            return part
        obj._record = record
        res = {"plucks": [self.pk(1.)], "onsets": [1.]}
        with patch.object(belts, "analyse", lambda *a, **k: res):
            obj.cmd_CHORDATA_FREE(Command(SECONDS=8.))
            obj.cmd_CHORDATA_FREE(Command(SECONDS=8.))
        self.assertEqual([h["n"] for h in obj.history], [1, 2])     # one per run, kept across runs

    def test_cleared_by_start_close_and_reset(self):
        obj = self._filled()
        obj._tool_info = {"corexy": True}
        obj.printer = SimpleNamespace(lookup_object=lambda name, default=None:
                                      object() if name == "resonance_tester" else default)
        obj.cmd_CHORDATA_START(Command())
        self.assertEqual(obj.history, [])
        obj = self._filled()
        obj.cmd_CHORDATA_FANS(Command())               # Close Chordata
        self.assertEqual(obj.history, [])
        obj = self._filled()
        sent = []
        obj._handle_history_reset(SimpleNamespace(send=sent.append))
        self.assertEqual((obj.history, sent), ([], [{"cleared": True}]))

    def test_reset_while_listening_keeps_earlier_plucks_out(self):
        obj = self._filled()
        obj.state, obj.measurement = "listening", {"phase": "listening", "belt": "free"}
        obj._listen_t0 = (1, 8., 99.)                  # its piece from 8 s started 1 s ago: Reset at 9 s
        obj._handle_history_reset(SimpleNamespace(send=lambda r: None))
        obj._history_add(1, 0., [self.pk(3.), self.pk(9.5)], self.COLS, None, 12., final=True)
        self.assertEqual([h["n"] for h in obj.history], [1])

    def test_reset_right_after_a_listen_starts_keeps_its_plucks(self):
        obj = self._filled()
        obj.state, obj.measurement = "listening", {"phase": "listening", "belt": "free"}
        obj._listen_id, obj._listen_t0 = 2, (2, 0., 100.)   # recording, no live analysis yet
        obj._handle_history_reset(SimpleNamespace(send=lambda r: None))
        obj._history_add(2, 0., [self.pk(.5), self.pk(2.)], self.COLS, None, 8., final=True)
        self.assertEqual(len(obj.history), 2)

    def test_reset_in_a_later_listen_leaves_out_only_its_earlier_plucks(self):
        obj = self._filled()
        obj.state, obj.measurement = "listening", {"phase": "listening", "belt": "free"}
        obj._listen_id, obj._listen_t0 = 2, (2, 8., 97.)    # the second piece, 3 s in: Reset at 11 s
        obj._handle_history_reset(SimpleNamespace(send=lambda r: None))
        obj._history_add(2, 8., [self.pk(2.), self.pk(4.)], self.COLS, None, 8., final=True)
        self.assertEqual([h["freq"] for h in obj.history], [110.])
        obj._history_add(3, 0., [self.pk(1.)], self.COLS, None, 8., final=True)      # the next listen: all of it
        self.assertEqual(len(obj.history), 2)

    def test_double_tap_clears_the_whole_history_once(self):
        obj = self._filled()
        calls = []

        def knock(*args, **kw):
            calls.append((args[4], kw["until"]))
            return True, 20., None
        taps = [self.pk(1.), self.pk(9., ok=False), self.pk(9.4, ok=False)]
        with patch.object(belts, "toolhead_knock", knock):
            obj._history_add(1, 0., taps, self.COLS, None, 12., final=True)
            obj._history_add(1, 0., taps, self.COLS, None, 12., final=True)     # seen again: once only
            # and again a hundredth later (the live and final analyses may place an onset apart)
            obj._history_add(1, 0., [self.pk(9.03, ok=False), self.pk(9.42, ok=False)], self.COLS, None, 12.,
                             final=True)
        self.assertEqual(calls, [(9., 9.4), (9.4, None)])
        self.assertEqual(obj.history, [])
        self.assertEqual(obj.history_note["text"], "History cleared (double tap)")
        self.assertEqual(obj.get_status(100.)["history_note"], obj.history_note)

    def test_double_tap_across_two_pieces(self):
        obj = self._filled()
        calls = []

        def knock(*args, **kw):
            calls.append((args[4], kw["until"]))
            return True, 20., None
        with patch.object(belts, "toolhead_knock", knock):
            # the first knock at the end of one piece (listen 2, 0-8 s), the second 0.5 s later in the next
            obj._history_add(2, 0., [self.pk(7.6, ok=False)], self.COLS, None, 8., final=True)
            self.assertIsNone(obj.history_note)
            obj._history_add(2, 8., [self.pk(.1, ok=False, final=True), self.pk(3., final=False)],
                             self.COLS, None, 3.5)                                  # live
            obj._history_add(2, 8., [self.pk(.1, ok=False), self.pk(3.)], self.COLS, None, 8., final=True)
        self.assertEqual(calls, [(7.6, None), (.1, 3.)])
        self.assertEqual([(h["n"], h["ok"]) for h in obj.history], [(11, True)])     # only what came after
        self.assertEqual(obj.history_note["text"], "History cleared (double tap)")

    def test_quick_double_tap_is_one_event_with_a_second_knock_in_it(self):
        obj = self._filled()
        with patch.object(belts, "toolhead_knock", lambda *a, **k: (True, 20., None)), \
                patch.object(belts, "second_knock", lambda *a, **k: a[4] + 0.3):
            obj._history_add(1, 0., [self.pk(9., ok=False), self.pk(10.5)], self.COLS, None, 12., final=True)
        self.assertEqual([h["freq"] for h in obj.history], [110.])     # only the pluck after it
        self.assertEqual(obj.history_note["text"], "History cleared (double tap)")

    def test_a_live_look_without_the_next_event_does_not_keep_its_knock_answer(self):
        obj = self._filled()
        answers = iter([False, True])
        with patch.object(belts, "toolhead_knock", lambda *a, **k: (next(answers), 20., None)), \
                patch.object(belts, "second_knock", lambda *a, **k: None):
            obj._history_add(1, 0., [self.pk(9., ok=False, final=True)], self.COLS, None, 9.8)   # live
            obj._history_add(1, 0., [self.pk(9., ok=False)], self.COLS, None, 12., final=True)
        self.assertEqual(obj._knocks.get(obj._key(1, 9.)), True)

    def test_double_tap_keeps_what_comes_after_and_earlier_plucks_stay_gone(self):
        obj = self._filled()
        with patch.object(belts, "toolhead_knock", lambda *a, **k: (True, 20., None)):
            obj._history_add(1, 0., [self.pk(9., ok=False), self.pk(9.4, ok=False), self.pk(10.5)], self.COLS, None, 12., final=True)
            # a later analysis of the same listen sees the earlier plucks again: they don't come back
            obj._history_add(1, 0., [self.pk(3.), self.pk(9., ok=False), self.pk(9.4, ok=False), self.pk(10.5)],
                             self.COLS, None, 12., final=True)
        self.assertEqual([h["ok"] for h in obj.history], [True])
        self.assertEqual(len(obj.history), 1)

    def test_no_clear_with_a_pluck_between_or_outside_the_gap(self):
        for taps in ([self.pk(9.), self.pk(9.3), self.pk(9.6)],        # a counted pluck between
                     [self.pk(9.), self.pk(9.9)], [self.pk(9.), self.pk(9.1)]):
            for i, p in enumerate(taps):
                if i != 1 or len(taps) == 2:
                    p["ok"], p["why"] = False, "a knock"
            with self.subTest(taps=[p["t"] for p in taps]):
                obj = self._filled()
                with patch.object(belts, "toolhead_knock", lambda *a, **k: (True, 20., None)):
                    obj._history_add(1, 0., taps, self.COLS, None, 12., final=True)
                self.assertEqual(sum(h["ok"] for h in obj.history), 7 + (len(taps) == 3))
                self.assertIsNone(obj.history_note)

    def test_no_double_tap_without_the_toolhead_note_or_a_knock(self):
        obj = self._filled()
        del obj.belts["tap_main"]
        calls = []
        with patch.object(belts, "toolhead_knock", lambda *a, **k: calls.append(1) or (True, 20., None)):
            obj._history_add(1, 0., [self.pk(9., ok=False), self.pk(9.4, ok=False)], self.COLS, None, 12., final=True)
        self.assertEqual((calls, len(obj.history)), ([], 10))
        obj = self._filled()
        with patch.object(belts, "toolhead_knock", lambda *a, **k: (False, 3., None)):
            obj._history_add(1, 0., [self.pk(9., ok=False), self.pk(9.4, ok=False)], self.COLS, None, 12., final=True)
        self.assertEqual(sum(h["ok"] for h in obj.history), 7)
        self.assertIsNone(obj.history_note)


class RecordingBufferTests(unittest.TestCase):
    """Recordings wait in RAM and reach the disk in whole files: at Close Chordata, after a long
    idle, or when they would take too much RAM (an SD card written seldom, not every few seconds)."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.obj = plugin()
        self.obj.out_dir = self.dir
        self.obj.reactor = SimpleNamespace(monotonic=lambda: 100., NEVER=1e30,
                                           register_timer=lambda cb, when: "timer")
        self.obj._rec_buf, self.obj._rec_prev, self.obj._rec_timer = {}, None, None
        self.obj.state = "idle"

    def files(self):
        self.obj._rec_prev and self.obj._rec_prev.join()
        d = os.path.join(self.dir, "recordings")
        return sorted(n for n in os.listdir(d) if not n.startswith(".")) if os.path.isdir(d) else []

    def piece(self, t0):
        return [(t0 + i / 3200., 1., 2., 9806.65) for i in range(3200)]

    def test_pieces_wait_in_ram_until_close(self):
        for k in range(3):
            self.obj._save_recording("free-1", self.piece(k), append=True)
        self.assertEqual(self.files(), [])
        self.obj._flush_recordings()
        self.assertEqual(self.files(), ["free-1.csv"])     # (files() waits for the writer)
        with open(os.path.join(self.dir, "recordings", "free-1.csv")) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], "#time,accel_x,accel_y,accel_z")
        self.assertEqual(len(lines), 1 + 3 * 3200)

    def test_too_much_for_ram_is_written_and_appended_to(self):
        self.obj._rec_limit = lambda: 200000          # bytes: about two pieces
        for k in range(3):
            self.obj._save_recording("free-2", self.piece(k), append=True)
        self.obj._flush_recordings()
        self.assertEqual(self.files(), ["free-2.csv"])
        with open(os.path.join(self.dir, "recordings", "free-2.csv")) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1 + 3 * 3200)     # one header, every sample once, in order
        ts = [float(x.split(",")[0]) for x in lines[1:]]
        self.assertTrue(all(b > a for a, b in zip(ts, ts[1:])))

    def test_while_listening_no_csv_full_ram_set_aside_as_numbers(self):
        self.obj._rec_limit = lambda: 200000
        self.obj.state = "listening"
        for k in range(3):
            self.obj._save_recording("free-3", self.piece(k), append=True)
        d = os.path.join(self.dir, "recordings")
        self.assertEqual(sorted(os.listdir(d)), [".free-3.bin"])        # numbers, no CSV yet
        self.obj.state = "idle"
        self.obj._flush_recordings()
        self.assertEqual(self.files(), ["free-3.csv"])                    # the .bin is gone
        with open(os.path.join(d, "free-3.csv")) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1 + 3 * 3200)
        ts = [float(x.split(",")[0]) for x in lines[1:]]
        self.assertTrue(all(b > a for a, b in zip(ts, ts[1:])))

    def test_a_recording_asked_for_is_written_at_once(self):
        self.obj._save_recording("accel-now", self.piece(0), now=True)
        self.assertEqual(self.files(), ["accel-now.csv"])


class ConfigLoadTest(unittest.TestCase):
    """chordata.cfg read as Klipper reads it (" #" starts an inline comment) and every macro
    compiled as Klipper compiles it: a {# ... #} comment lost its closing tag that way once."""

    def test_every_macro_compiles(self):
        import configparser
        try:
            import jinja2
        except ImportError:
            self.skipTest("jinja2 not installed (Klipper's own dependency)")
        cfg = configparser.RawConfigParser(strict=False, inline_comment_prefixes=(";", "#"))
        cfg.read(os.path.join(os.path.dirname(__file__), "..", "chordata.cfg"))
        env = jinja2.Environment("{%", "%}", "{", "}", extensions=["jinja2.ext.do"])
        macros = [s for s in cfg.sections() if s.startswith("gcode_macro ") and cfg.has_option(s, "gcode")]
        self.assertTrue(macros)
        for section in macros:
            env.from_string(cfg.get(section, "gcode"))


class MacroRecursionTest(unittest.TestCase):
    """Klipper refuses a macro that runs itself ("called recursively"), e.g. after Go/Save in the setup prompt."""

    def test_no_macro_runs_itself(self):
        import re
        with open(os.path.join(os.path.dirname(__file__), "..", "chordata.cfg")) as f:
            text = f.read()
        for name, body in re.findall(r"^\[gcode_macro (\S+)\]\n(.*?)(?=^\[|\Z)", text, re.M | re.S):
            for line in body.splitlines():
                cmd = line.strip().split(" ")[0]
                self.assertNotEqual(cmd, name, "%s runs itself: %s" % (name, line.strip()))
