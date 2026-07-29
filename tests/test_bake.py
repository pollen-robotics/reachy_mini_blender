"""Tests for reachy_mini_link.bake. Must run inside Blender.

  blender --background reachy_mini.blend --python tests/run_blender_tests.py -- test_bake
"""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import bpy

from reachy_mini_link import bake


def _fake_filepath(path):
    """Context manager standing in for `bpy.data.filepath == path`.

    bpy.data.filepath is a read-only bpy_struct property: mock.patch.object
    on bpy.data itself raises "attribute is read-only" even for the
    duration of a patch (Blender enforces this at the C level, not just on
    plain assignment). Patching the `data` attribute of the `bpy` module
    instead works, because bake.write_move and bpy.path.abspath both look
    it up as `bpy.data.filepath` at call time, and `bpy` is an ordinary
    Python module whose attributes mock.patch.object CAN reassign.
    """
    return mock.patch.object(bpy, "data", SimpleNamespace(filepath=path))


def reset_pose():
    arm = bpy.data.objects["Armature"]
    for pb in arm.pose.bones:
        pb.location = (0.0, 0.0, 0.0)
        pb.rotation_euler = (0.0, 0.0, 0.0)
        pb.scale = (1.0, 1.0, 1.0)
    bpy.context.view_layer.update()


class TestBakeSchema(unittest.TestCase):
    def setUp(self):
        reset_pose()
        self.scene = bpy.context.scene
        self.scene.render.fps = 24
        self.scene.render.fps_base = 1.0

    def test_keys_match_recordedmove(self):
        move = bake.bake(self.scene, description="unit", frame_start=1, frame_end=3)
        self.assertEqual(set(move), {"description", "time", "set_target_data"})
        self.assertEqual(move["description"], "unit")

    def test_one_sample_per_frame(self):
        move = bake.bake(self.scene, frame_start=1, frame_end=3)
        self.assertEqual(len(move["time"]), 3)
        self.assertEqual(len(move["set_target_data"]), 3)

    def test_timestamps_come_from_scene_fps(self):
        move = bake.bake(self.scene, frame_start=1, frame_end=3)
        self.assertAlmostEqual(move["time"][0], 0.0, delta=1e-9)
        self.assertAlmostEqual(move["time"][1], 1.0 / 24.0, delta=1e-9)
        self.assertAlmostEqual(move["time"][2], 2.0 / 24.0, delta=1e-9)

    def test_timestamps_honour_fps_base(self):
        self.scene.render.fps = 24
        self.scene.render.fps_base = 1.001          # 23.976 fps
        move = bake.bake(self.scene, frame_start=1, frame_end=2)
        expected = 1.0 / (self.scene.render.fps / self.scene.render.fps_base)
        self.assertAlmostEqual(move["time"][1], expected, delta=1e-9)

    def test_sample_fields_and_head_is_nested_4x4(self):
        move = bake.bake(self.scene, frame_start=1, frame_end=2)
        sample = move["set_target_data"][0]
        self.assertEqual(set(sample), {"head", "antennas", "body_yaw"})
        self.assertEqual(len(sample["head"]), 4)
        self.assertTrue(all(len(row) == 4 for row in sample["head"]))
        self.assertEqual(len(sample["antennas"]), 2)
        self.assertIsInstance(sample["body_yaw"], float)

    def test_head_is_nested_not_flat(self):
        # Guards the wire-vs-file format asymmetry.
        sample = bake.bake(self.scene, frame_start=1, frame_end=2)["set_target_data"][0]
        self.assertIsInstance(sample["head"][0], list)

    def test_defaults_to_scene_frame_range(self):
        self.scene.frame_start = 5
        self.scene.frame_end = 9
        move = bake.bake(self.scene)
        self.assertEqual(len(move["time"]), 5)

    def test_original_frame_is_restored(self):
        self.scene.frame_set(42)
        bake.bake(self.scene, frame_start=1, frame_end=3)
        self.assertEqual(self.scene.frame_current, 42)

    def test_reversed_range_raises_value_error(self):
        # end < start yields an empty range, which would otherwise write an
        # unusable {"time": [], "set_target_data": []} move file.
        with self.assertRaises(ValueError):
            bake.bake(self.scene, frame_start=5, frame_end=3)

    def test_single_frame_range_raises_value_error(self):
        # A single frame gives dt == duration == 0.0, which RecordedMove
        # cannot evaluate.
        with self.assertRaises(ValueError):
            bake.bake(self.scene, frame_start=1, frame_end=1)


class TestBakeCapturesAnimation(unittest.TestCase):
    def setUp(self):
        reset_pose()
        self.scene = bpy.context.scene
        self.scene.render.fps = 24
        self.scene.render.fps_base = 1.0
        arm = bpy.data.objects["Armature"]
        slider = arm.pose.bones["Slider.Rot.Core"]
        slider.location[1] = 0.0
        slider.keyframe_insert("location", index=1, frame=1)
        slider.location[1] = 0.05
        slider.keyframe_insert("location", index=1, frame=3)

    def tearDown(self):
        arm = bpy.data.objects["Armature"]
        # Must NOT use animation_data_clear(): drivers live in the same
        # AnimData block as the action, so that call also deletes the rig's
        # three calibration drivers (Core + both antenna sliders), which the
        # test_rig suite depends on. Clear only the action.
        if arm.animation_data:
            arm.animation_data.action = None
        reset_pose()

    def test_body_yaw_changes_across_frames(self):
        move = bake.bake(self.scene, frame_start=1, frame_end=3)
        yaws = [s["body_yaw"] for s in move["set_target_data"]]
        self.assertAlmostEqual(yaws[0], 0.0, delta=1e-6)
        self.assertAlmostEqual(yaws[2], 1.269300, delta=1e-5)
        self.assertGreater(yaws[2], yaws[0])


class TestWriteMove(unittest.TestCase):
    def setUp(self):
        reset_pose()

    def test_writes_loadable_json(self):
        move = bake.bake(bpy.context.scene, description="io", frame_start=1, frame_end=2)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "io.json")
            bake.write_move(path, move)
            with open(path) as fh:
                loaded = json.load(fh)
        self.assertEqual(loaded["description"], "io")
        self.assertEqual(len(loaded["time"]), 2)

    def test_is_json_serialisable_with_plain_floats(self):
        move = bake.bake(bpy.context.scene, frame_start=1, frame_end=2)
        json.dumps(move)   # must not raise on mathutils types

    def test_returns_resolved_absolute_path(self):
        move = bake.bake(bpy.context.scene, frame_start=1, frame_end=2)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "io.json")
            resolved = bake.write_move(path, move)
        self.assertEqual(resolved, os.path.abspath(path))

    def test_double_slash_path_with_unsaved_blend_raises_value_error(self):
        # Reproduces the bug report: an unsaved .blend leaves bpy.data.filepath
        # == '', so bpy.path.abspath cannot resolve "//" and previously left
        # a relative path for os.path.abspath to resolve against the process
        # cwd (e.g. "/" from a desktop launcher), causing a surprising write
        # attempt outside the repo/home directory.
        move = bake.bake(bpy.context.scene, frame_start=1, frame_end=2)
        with _fake_filepath(""):
            with self.assertRaises(ValueError) as ctx:
                bake.write_move("//moves/untitled.json", move)
        self.assertIn("save", str(ctx.exception).lower())

    def test_double_slash_path_with_saved_blend_resolves_next_to_it(self):
        move = bake.bake(bpy.context.scene, frame_start=1, frame_end=2)
        with tempfile.TemporaryDirectory() as d:
            fake_blend = os.path.join(d, "fake.blend")
            with _fake_filepath(fake_blend):
                resolved = bake.write_move("//moves/untitled.json", move)
            expected = os.path.join(d, "moves", "untitled.json")
            self.assertEqual(resolved, expected)
            with open(expected) as fh:
                loaded = json.load(fh)
        self.assertEqual(len(loaded["time"]), 2)

    def test_absolute_path_works_with_unsaved_blend(self):
        # The documented workaround for the "//" + unsaved-blend case.
        move = bake.bake(bpy.context.scene, frame_start=1, frame_end=2)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "untitled.json")
            with _fake_filepath(""):
                resolved = bake.write_move(path, move)
            self.assertEqual(resolved, os.path.abspath(path))
            with open(path) as fh:
                loaded = json.load(fh)
        self.assertEqual(len(loaded["time"]), 2)


if __name__ == "__main__":
    unittest.main()
