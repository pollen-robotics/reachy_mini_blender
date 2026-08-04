"""Roundtrip tests for import_move: file -> keyframes -> rig.read().

Runs inside Blender against the bundled rig (see run_blender_tests.py).
The pipeline under test is the full inverse mapping: analytic Head.001
basis (through the Core parent), body yaw through the slider driver,
antennas on the free FK bones. If any rig assumption (inherit flags,
driver shape, constraint ranges) is wrong, the roundtrip drifts and
these bounds fail.
"""

import json
import math
import os
import struct
import tempfile
import unittest

import bpy
from mathutils import Euler, Matrix

from reachy_mini_link import import_move, rig


def _synthetic_move(fps, duration):
    """A smooth move whose samples land exactly on scene frames."""
    n = int(duration * fps) + 1
    times, frames = [], []
    for i in range(n):
        t = i / fps
        rot = Euler((0.25 * math.sin(2.0 * t),
                     0.20 * math.sin(1.3 * t + 0.7),
                     0.30 * math.sin(0.9 * t + 1.1)), "XYZ").to_matrix()
        head = rot.to_4x4()
        head.translation = (0.010 * math.sin(1.7 * t),
                            0.008 * math.sin(2.3 * t + 0.4),
                            0.012 * math.sin(1.1 * t + 2.0))
        times.append(t)
        frames.append({
            "head": [[float(v) for v in row] for row in head],
            "antennas": [0.8 * math.sin(2.1 * t),
                         -0.6 * math.sin(1.6 * t + 0.3)],
            "body_yaw": 0.5 * math.sin(0.8 * t + 0.2),
        })
    return {"description": "synthetic roundtrip", "time": times,
            "set_target_data": frames}


def _write_move(move, with_audio=False):
    d = tempfile.mkdtemp(prefix="reachy_import_test_")
    path = os.path.join(d, "move.json")
    with open(path, "w") as fh:
        json.dump(move, fh)
    if with_audio:
        _write_silent_wav(os.path.join(d, "move.wav"), seconds=1.0)
    return path


def _write_silent_wav(path, seconds=1.0, rate=16000):
    n = int(seconds * rate)
    with open(path, "wb") as fh:
        fh.write(b"RIFF" + struct.pack("<I", 36 + 2 * n) + b"WAVE")
        fh.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate,
                                       rate * 2, 2, 16))
        fh.write(b"data" + struct.pack("<I", 2 * n) + b"\x00" * (2 * n))


def _clear_animation():
    arm = bpy.data.objects["Armature"]
    # Detach the action but keep animation_data alive: the rig's slider
    # drivers live there, and animation_data_clear() would delete them
    # with it (silently changing where import keys land).
    if arm.animation_data:
        arm.animation_data.action = None
    for pb in arm.pose.bones:
        pb.location = (0.0, 0.0, 0.0)
        pb.rotation_euler = (0.0, 0.0, 0.0)
    scene = bpy.context.scene
    if scene.sequence_editor:
        scene.sequence_editor_clear()


class RoundtripTest(unittest.TestCase):
    """Import with cleanup disabled must reproduce the file exactly."""

    @classmethod
    def setUpClass(cls):
        _clear_animation()
        scene = bpy.context.scene
        cls.fps = scene.render.fps / scene.render.fps_base
        cls.move = _synthetic_move(cls.fps, duration=3.0)
        cls.path = _write_move(cls.move)
        cls.stats = import_move.apply(
            bpy.context, cls.path, smooth_sigma=0.0, tolerance=0.0,
            snap_to_frames=False, load_audio=False)

    @classmethod
    def tearDownClass(cls):
        _clear_animation()

    def test_every_sample_becomes_a_key(self):
        n = len(self.move["time"])
        self.assertEqual(self.stats["samples"], n)
        self.assertEqual(self.stats["keys"], 9 * n)

    def test_keys_land_on_sliders_not_fk_tails(self):
        # Yaw and antennas must be keyed through the sliders driving the
        # base bones. Keys on the free tail bones (.003) would visually
        # bend the antenna at the bone joint instead of sweeping the
        # whole antenna around the robot's hinge.
        arm = bpy.data.objects["Armature"]
        ad = arm.animation_data
        cb = ad.action.layers[0].strips[0].channelbag(ad.action_slot)
        paths = {fc.data_path for fc in cb.fcurves}
        for slider in ("Slider.Rot.Core", "Slider.Rot.Antenna.L",
                       "Slider.Rot.Antenna.R"):
            self.assertIn(f'pose.bones["{slider}"].location', paths)
        for tail in ("Antenna.L.003", "Antenna.R.003", "Core"):
            self.assertNotIn(f'pose.bones["{tail}"].rotation_euler', paths)

    def test_rig_read_matches_input(self):
        scene = bpy.context.scene
        frame0 = scene.frame_start
        worst_t, worst_r, worst_y, worst_a = 0.0, 0.0, 0.0, 0.0
        for i in range(0, len(self.move["time"]), 7):
            scene.frame_set(frame0 + i)
            state = rig.read(bpy.context.evaluated_depsgraph_get())
            want = Matrix(self.move["set_target_data"][i]["head"])

            dt = (state.head.translation - want.translation).length
            rot_delta = (state.head.to_3x3().transposed() @ want.to_3x3())
            dr = abs(rot_delta.to_quaternion().angle)
            dy = abs(state.body_yaw
                     - self.move["set_target_data"][i]["body_yaw"])
            da = max(
                abs(state.antennas[0]
                    - self.move["set_target_data"][i]["antennas"][0]),
                abs(state.antennas[1]
                    - self.move["set_target_data"][i]["antennas"][1]))
            worst_t = max(worst_t, dt)
            worst_r = max(worst_r, dr)
            worst_y = max(worst_y, dy)
            worst_a = max(worst_a, da)

        # Keys sit exactly on the sampled frames, so the only error budget
        # is the mapping math itself (and float churn through the driver).
        self.assertLess(worst_t, 5e-4, "head translation drift (m)")
        self.assertLess(worst_r, 5e-3, "head rotation drift (rad)")
        self.assertLess(worst_y, 5e-3, "body yaw drift (rad)")
        self.assertLess(worst_a, 5e-3, "antenna drift (rad)")


class CleanupRoundtripTest(unittest.TestCase):
    """Default cleanup must compress hard while staying visually faithful."""

    @classmethod
    def setUpClass(cls):
        _clear_animation()
        scene = bpy.context.scene
        cls.fps = scene.render.fps / scene.render.fps_base
        cls.move = _synthetic_move(cls.fps, duration=3.0)
        cls.path = _write_move(cls.move)
        cls.stats = import_move.apply(
            bpy.context, cls.path, smooth_sigma=0.0, tolerance=1.0,
            snap_to_frames=True, load_audio=False)

    @classmethod
    def tearDownClass(cls):
        _clear_animation()

    def test_compresses(self):
        self.assertLess(self.stats["keys"], self.stats["samples"] * 9 * 0.7)

    def test_still_faithful(self):
        scene = bpy.context.scene
        frame0 = scene.frame_start
        for i in range(0, len(self.move["time"]), 11):
            scene.frame_set(frame0 + i)
            state = rig.read(bpy.context.evaluated_depsgraph_get())
            want = Matrix(self.move["set_target_data"][i]["head"])
            self.assertLess(
                (state.head.translation - want.translation).length, 4e-3)
            rot_delta = state.head.to_3x3().transposed() @ want.to_3x3()
            self.assertLess(abs(rot_delta.to_quaternion().angle), 4e-2)


class AudioSidecarTest(unittest.TestCase):
    def setUp(self):
        _clear_animation()

    def tearDown(self):
        _clear_animation()

    def test_wav_sidecar_becomes_strip(self):
        move = _synthetic_move(24.0, duration=1.0)
        path = _write_move(move, with_audio=True)
        stats = import_move.apply(bpy.context, path, load_audio=True)
        self.assertTrue(stats["audio"].endswith(".wav"))
        se = bpy.context.scene.sequence_editor
        strips = (se.strips_all if hasattr(se, "strips_all")
                  else se.sequences_all)
        self.assertEqual(len([s for s in strips if s.type == "SOUND"]), 1)

    def test_reimport_replaces_previous_import_strip(self):
        move = _synthetic_move(24.0, duration=1.0)
        path_a = _write_move(move, with_audio=True)
        path_b = _write_move(move, with_audio=True)
        import_move.apply(bpy.context, path_a, load_audio=True)
        import_move.apply(bpy.context, path_b, load_audio=True)
        se = bpy.context.scene.sequence_editor
        strips = (se.strips_all if hasattr(se, "strips_all")
                  else se.sequences_all)
        sounds = [s for s in strips if s.type == "SOUND"]
        self.assertEqual(len(sounds), 1)
        self.assertTrue(sounds[0].sound.filepath.startswith(
            os.path.dirname(path_b)))

    def test_silent_move_clears_previous_import_strip(self):
        """Importing a move with no audio must not keep the old sound."""
        move = _synthetic_move(24.0, duration=1.0)
        path_with = _write_move(move, with_audio=True)
        path_silent = _write_move(move, with_audio=False)
        import_move.apply(bpy.context, path_with, load_audio=True)
        stats = import_move.apply(bpy.context, path_silent, load_audio=True)
        self.assertIsNone(stats["audio"])
        se = bpy.context.scene.sequence_editor
        strips = (se.strips_all if hasattr(se, "strips_all")
                  else se.sequences_all)
        self.assertEqual(len([s for s in strips if s.type == "SOUND"]), 0)

    def test_hand_added_strips_survive_import(self):
        d = tempfile.mkdtemp(prefix="reachy_import_test_")
        wav = os.path.join(d, "song.wav")
        _write_silent_wav(wav)
        scene = bpy.context.scene
        se = scene.sequence_editor_create()
        strips = se.strips if hasattr(se, "strips") else se.sequences
        strips.new_sound(name="song", filepath=wav, channel=1, frame_start=1)
        path = _write_move(_synthetic_move(24.0, 1.0), with_audio=True)
        import_move.apply(bpy.context, path, load_audio=True)
        all_strips = (se.strips_all if hasattr(se, "strips_all")
                      else se.sequences_all)
        names = sorted(s.name for s in all_strips if s.type == "SOUND")
        self.assertEqual(names, ["move", "song"])

    def test_audio_only_move(self):
        move = {"description": "just sound", "time": [0.0],
                "audio_only": True}
        path = _write_move(move, with_audio=True)
        stats = import_move.apply(bpy.context, path, load_audio=True)
        self.assertEqual(stats["samples"], 0)
        self.assertIsNotNone(stats["audio"])


class FormatErrorTest(unittest.TestCase):
    def test_rejects_non_move_json(self):
        d = tempfile.mkdtemp(prefix="reachy_import_test_")
        path = os.path.join(d, "nope.json")
        with open(path, "w") as fh:
            json.dump({"foo": 1}, fh)
        with self.assertRaises(import_move.MoveFormatError):
            import_move.load_move(path)

    def test_rejects_mismatched_lengths(self):
        d = tempfile.mkdtemp(prefix="reachy_import_test_")
        path = os.path.join(d, "bad.json")
        with open(path, "w") as fh:
            json.dump({"time": [0.0, 0.1], "set_target_data": [{}]}, fh)
        with self.assertRaises(import_move.MoveFormatError):
            import_move.load_move(path)


if __name__ == "__main__":
    unittest.main()
