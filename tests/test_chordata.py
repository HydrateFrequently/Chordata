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
    obj.printer = SimpleNamespace(lookup_object=lambda name, default=None: objects.get(name, default))
    obj.gcode = SimpleNamespace(respond_raw=lambda *args: None)
    obj.state, obj.message = "idle", ""
    obj.measurement = {"phase": "idle", "belt": None}
    obj.belt_now, obj.belt_live = None, []
    obj.belt_tol, obj.belt_target = .25, 110.
    obj._tool_info = {"corexy": True}
    obj.belts = {"target": 110., "tol": .25, "plucks": 5, "round": 0, "A": {"freq": 110.},
                 "B": {"freq": 110.}, "done": True, "heard": ["A"], "good": {}, "tap": [], "tap_done": True}
    return obj


def status(**values):
    return SimpleNamespace(get_status=lambda now: values)


class SafetyTests(unittest.TestCase):
    def test_all_direct_commands_reject_printing_and_paused(self):
        for state in ("printing", "paused"):
            for command in ("START", "LISTEN", "TAP", "POS", "RECORD"):
                with self.subTest(state=state, command=command):
                    obj = plugin({"print_stats": status(state=state)})
                    with self.assertRaisesRegex(ValueError, "finish or cancel"):
                        getattr(obj, "cmd_CHORDATA_" + command)(Command())

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

    def test_belts_need_a_tap_first(self):
        obj = plugin({"resonance_tester": SimpleNamespace(accel_chips=[("xy", object())]),
                      "toolhead": SimpleNamespace(wait_moves=lambda: None)})
        obj.belts = dict(obj.belts, tap_done=False)
        recorded = []
        obj._record = lambda *args, **kwargs: recorded.append(args) or []
        with self.assertRaisesRegex(ValueError, "tap the toolhead first"):
            obj.cmd_CHORDATA_LISTEN(Command(BELT="A"))
        self.assertEqual(recorded, [])

    def test_failed_capture_clears_old_result_and_completion(self):
        toolhead = SimpleNamespace(wait_moves=lambda: None)
        obj = plugin({"resonance_tester": SimpleNamespace(accel_chips=[("xy", object())]),
                      "toolhead": toolhead})
        obj._record = lambda *args, **kwargs: []
        with self.assertRaisesRegex(ValueError, "no data"):
            obj.cmd_CHORDATA_LISTEN(Command(BELT="A"))
        self.assertIsNone(obj.belts["A"])
        self.assertFalse(obj.belts["done"])
        self.assertEqual(obj.belts["heard"], [])
        self.assertEqual(obj.state, "idle")
        self.assertEqual(obj.measurement["phase"], "error")
        self.assertIn("no data", obj.measurement["error"])

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

    def test_success_requires_fresh_pair_and_publishes_final_metadata(self):
        obj = plugin({"resonance_tester": SimpleNamespace(accel_chips=[("xy", object())]),
                      "toolhead": SimpleNamespace(wait_moves=lambda: None)})
        obj.belts["heard"] = []
        obj._record = lambda *args, **kwargs: [(i / 3200., 0., 0., 0.) for i in range(100)]
        obj._save_recording = lambda *args: None
        def analysis(*args, **kwargs):
            return {"freq": 110., "used": 5, "heard": 5, "note": "five good plucks",
                    "plucks": [{"t": i, "ok": True, "why": "", "freq": 110.} for i in range(5)]}
        with patch("chordata_belts.analyse", side_effect=analysis):
            obj.cmd_CHORDATA_LISTEN(Command(BELT="A"))
            self.assertFalse(obj.belts["done"])
            self.assertEqual(obj.belts["round"], 0)
            obj.cmd_CHORDATA_LISTEN(Command(BELT="B"))
        self.assertTrue(obj.belts["done"])
        self.assertEqual(obj.belts["round"], 1)
        self.assertEqual(obj.belts["A"]["round"], 1)
        self.assertEqual(obj.belts["B"]["round"], 1)
        self.assertEqual(obj.measurement["accepted"], 5)
        self.assertEqual(obj.measurement["phase"], "complete")
        self.assertGreater(obj.belts["B"]["measured_at"], 0.)

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
            obj._record(chip, SimpleNamespace(), 2., live={})
        self.assertEqual(events, ["finish"])

    def test_late_live_analysis_cannot_overwrite_final_result(self):
        workers = []
        samples = [(i / 1000., 0., 0., 0.) for i in range(300)]
        client = SimpleNamespace(msgs=[{"data": samples}], request_start_time=0.,
                                 finish_measurements=lambda: None, get_samples=lambda: samples)
        obj = plugin()
        obj.belt_now = "A"
        obj.reactor.register_timer = lambda tick, when: setattr(obj, "tick", tick) or 1
        obj.reactor.unregister_timer = lambda timer: None
        class Worker:
            def __init__(self, target, args, daemon):
                workers.append(lambda: target(*args))
            def start(self):
                pass
        with patch("chordata.threading.Thread", Worker), patch("chordata_belts.analyse", return_value={"plucks": ["stale"], "used": 1, "heard": 1}):
            obj._record(SimpleNamespace(start_internal_client=lambda: client),
                        SimpleNamespace(dwell=lambda seconds: obj.tick(100.)), 2., live={})
            obj.belt_live = ["final"]
            workers[0]()
        self.assertEqual(obj.belt_live, ["final"])


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


if __name__ == "__main__":
    unittest.main()
