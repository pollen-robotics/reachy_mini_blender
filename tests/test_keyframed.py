"""Roundtrip tests for keyframed: fcurves -> .keys.json -> fcurves.

Runs inside Blender against the bundled rig (see run_blender_tests.py).
The promise under test is exactness: an animator's keys, handles and
interpolation modes survive collect() -> apply() bit-for-bit (modulo
the seconds<->frames conversion, exact at the authoring fps).
"""

import os
import tempfile
import unittest

import bpy

from reachy_mini_link import import_move, keyframed

from tests.test_import_move import (_clear_animation, _synthetic_move,
                                    _write_move)


def _write_keys(data):
    d = tempfile.mkdtemp(prefix="reachy_keys_test_")
    path = os.path.join(d, "move.keys.json")
    with open(path, "w") as fh:
        fh.write(keyframed.dumps(data))
    return path


def _fcurve_snapshot():
    """Every move-channel key as comparable tuples."""
    arm = bpy.data.objects["Armature"]
    cb = keyframed._channelbag(arm)
    out = {}
    for fc in cb.fcurves:
        keys = []
        for kp in fc.keyframe_points:
            keys.append((tuple(kp.co), tuple(kp.handle_left),
                         tuple(kp.handle_right), kp.interpolation,
                         kp.handle_left_type, kp.handle_right_type,
                         kp.easing, kp.back, kp.amplitude, kp.period))
        out[(fc.data_path, fc.array_index)] = keys
    return out


class ExactRoundtripTest(unittest.TestCase):
    """collect() then apply() must restore the exact same fcurves."""

    @classmethod
    def setUpClass(cls):
        _clear_animation()
        fps = bpy.context.scene.render.fps
        move_path = _write_move(_synthetic_move(fps, duration=2.0))
        import_move.apply(bpy.context, move_path, load_audio=False)

        # Hand-edit like an animator: move a handle, switch a key to
        # LINEAR, make another ELASTIC with tuned easing - the roundtrip
        # must keep all of it.
        arm = bpy.data.objects["Armature"]
        cb = keyframed._channelbag(arm)
        fc = next(f for f in cb.fcurves if len(f.keyframe_points) >= 3)
        kp = fc.keyframe_points[1]
        kp.interpolation = "LINEAR"
        kp2 = fc.keyframe_points[2]
        kp2.handle_left_type = "FREE"
        kp2.handle_left = (kp2.handle_left[0] - 1.0, kp2.handle_left[1] + 0.01)
        kp0 = fc.keyframe_points[0]
        kp0.interpolation = "ELASTIC"
        kp0.easing = "EASE_OUT"
        kp0.amplitude = 0.6
        kp0.period = 3.5
        fc.update()

        cls.before = _fcurve_snapshot()
        cls.data = keyframed.collect(bpy.context)
        cls.keys_path = _write_keys(cls.data)
        _clear_animation()
        cls.stats = keyframed.apply(bpy.context, cls.keys_path,
                                    load_audio=False)

    @classmethod
    def tearDownClass(cls):
        _clear_animation()

    def test_channels_and_key_counts_survive(self):
        after = _fcurve_snapshot()
        self.assertEqual(set(after), set(self.before))
        for ch in self.before:
            self.assertEqual(len(after[ch]), len(self.before[ch]))

    def test_keys_handles_and_modes_identical(self):
        after = _fcurve_snapshot()
        for ch, keys in self.before.items():
            for (b, a) in zip(keys, after[ch]):
                for vb, va in zip(b[:3], a[:3]):   # co, handles
                    self.assertAlmostEqual(vb[0], va[0], places=4)
                    self.assertAlmostEqual(vb[1], va[1], places=6)
                self.assertEqual(b[3:], a[3:])     # ipo + handle types

    def test_stats(self):
        self.assertEqual(self.stats["keys"],
                         sum(len(k) for k in self.before.values()))
        self.assertEqual(self.stats["channels"], len(self.before))


class SidecarLookupTest(unittest.TestCase):
    def test_sidecar_path(self):
        self.assertEqual(keyframed.sidecar_path("/tmp/x/wave.json"),
                         "/tmp/x/wave.keys.json")

    def test_find_sidecar(self):
        d = tempfile.mkdtemp(prefix="reachy_keys_test_")
        dense = os.path.join(d, "wave.json")
        open(dense, "w").write("{}")
        self.assertIsNone(keyframed.find_sidecar(dense))
        open(os.path.join(d, "wave.keys.json"), "w").write("{}")
        self.assertEqual(keyframed.find_sidecar(dense),
                         os.path.join(d, "wave.keys.json"))


class AudioTest(unittest.TestCase):
    def tearDown(self):
        _clear_animation()

    def test_apply_loads_audio_next_to_dense_json(self):
        _clear_animation()
        fps = bpy.context.scene.render.fps
        move_path = _write_move(_synthetic_move(fps, duration=1.0),
                                with_audio=True)
        import_move.apply(bpy.context, move_path, load_audio=False)
        data = keyframed.collect(bpy.context)
        keys_path = keyframed.sidecar_path(move_path)
        with open(keys_path, "w") as fh:
            fh.write(keyframed.dumps(data))
        _clear_animation()
        stats = keyframed.apply(bpy.context, keys_path)
        self.assertIsNotNone(stats["audio"])
        se = bpy.context.scene.sequence_editor
        strips = (se.strips_all if hasattr(se, "strips_all")
                  else se.sequences_all)
        self.assertEqual(len([s for s in strips if s.type == "SOUND"]), 1)


class FormatErrorTest(unittest.TestCase):
    def _write(self, text):
        d = tempfile.mkdtemp(prefix="reachy_keys_test_")
        path = os.path.join(d, "bad.keys.json")
        open(path, "w").write(text)
        return path

    def test_rejects_non_sidecar_json(self):
        with self.assertRaises(keyframed.KeysFormatError):
            keyframed.load(self._write('{"time": [0, 1]}'))

    def test_rejects_newer_version(self):
        with self.assertRaises(keyframed.KeysFormatError):
            keyframed.load(self._write(
                '{"format": "reachy_mini_keyframed_move", "version": 99,'
                ' "channels": [{"keys": [[0,0,0,0,0,0,"BEZIER",'
                '"FREE","FREE"]]}]}'))

    def test_rejects_malformed_key(self):
        with self.assertRaises(keyframed.KeysFormatError):
            keyframed.load(self._write(
                '{"format": "reachy_mini_keyframed_move", "version": 1,'
                ' "channels": [{"keys": [[0, 1]]}]}'))


if __name__ == "__main__":
    unittest.main()
