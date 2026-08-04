"""Tests for seamless.close_loop: the move must truly loop after it.

Runs inside Blender against the bundled rig (see run_blender_tests.py).
The promise under test is C1 continuity at the seam: for every move
channel, value and slope at the (new) end equal value and slope at the
start, and the baked artifact's last sample matches its first.
"""

import unittest

import bpy

from reachy_mini_link import bake, import_move, keyframed, rig, seamless

from tests.test_import_move import (_clear_animation, _synthetic_move,
                                    _write_move)


def _move_fcurves():
    arm = bpy.data.objects["Armature"]
    cb = keyframed._channelbag(arm)
    chans = set(keyframed._move_channels(arm, rig.Mapping()))
    return [f for f in cb.fcurves
            if (f.data_path, f.array_index) in chans
            and len(f.keyframe_points) >= 2]


class CloseLoopTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _clear_animation()
        fps = bpy.context.scene.render.fps
        # The synthetic move's sinusoids end mid-phase: nowhere near the
        # starting pose, exactly the case the loop closer exists for.
        move_path = _write_move(_synthetic_move(fps, duration=2.0))
        import_move.apply(bpy.context, move_path, load_audio=False)
        cls.end_before = bpy.context.scene.frame_end
        cls.stats = seamless.close_loop(bpy.context, blend=0.5)

    @classmethod
    def tearDownClass(cls):
        _clear_animation()

    def test_touches_the_keyed_channels(self):
        self.assertEqual(self.stats["channels"], len(_move_fcurves()))
        self.assertGreater(self.stats["channels"], 0)

    def test_scene_end_extended_by_blend(self):
        fps = bpy.context.scene.render.fps
        self.assertEqual(bpy.context.scene.frame_end,
                         self.end_before + round(0.5 * fps))

    def test_end_value_equals_start_value(self):
        for fc in _move_fcurves():
            first, last = fc.keyframe_points[0], fc.keyframe_points[-1]
            self.assertEqual(last.co[0], bpy.context.scene.frame_end)
            self.assertAlmostEqual(last.co[1], first.co[1], places=6,
                                   msg=fc.data_path)

    def test_end_slope_equals_start_slope(self):
        for fc in _move_fcurves():
            first, last = fc.keyframe_points[0], fc.keyframe_points[-1]
            dx_f = first.handle_right[0] - first.co[0]
            dx_l = last.co[0] - last.handle_left[0]
            slope_f = (first.handle_right[1] - first.co[1]) / dx_f
            slope_l = (last.co[1] - last.handle_left[1]) / dx_l
            self.assertAlmostEqual(slope_l, slope_f, places=5,
                                   msg=fc.data_path)

    def test_baked_move_loops(self):
        """The exported artifact itself must start and end on one pose."""
        scene = bpy.context.scene
        move = bake.bake(scene, description="loop-test")
        first, last = (move["set_target_data"][0],
                       move["set_target_data"][-1])
        for row_a, row_b in zip(first["head"], last["head"]):
            for a, b in zip(row_a, row_b):
                self.assertAlmostEqual(a, b, places=3)
        for a, b in zip(first["antennas"], last["antennas"]):
            self.assertAlmostEqual(a, b, places=3)
        self.assertAlmostEqual(first["body_yaw"], last["body_yaw"],
                               places=3)


class AutoBlendTest(unittest.TestCase):
    """blend=None sizes the return from the pose gap and move speed."""

    def tearDown(self):
        _clear_animation()

    def _import(self, duration):
        _clear_animation()
        fps = bpy.context.scene.render.fps
        move_path = _write_move(_synthetic_move(fps, duration=duration))
        import_move.apply(bpy.context, move_path, load_audio=False)

    def test_auto_blend_within_bounds(self):
        self._import(2.0)
        stats = seamless.close_loop(bpy.context)
        self.assertGreaterEqual(stats["blend"], seamless._BLEND_MIN)
        self.assertLessEqual(stats["blend"], seamless._BLEND_MAX)

    def test_closed_move_gets_minimal_return(self):
        """After one closing pass the gap is zero: pass two is minimal."""
        self._import(2.0)
        far = seamless.close_loop(bpy.context)
        near = seamless.close_loop(bpy.context)
        self.assertLessEqual(near["blend"], far["blend"])
        self.assertAlmostEqual(near["blend"], seamless._BLEND_MIN, places=6)

    def test_still_closes_the_loop(self):
        self._import(2.0)
        seamless.close_loop(bpy.context)
        for fc in _move_fcurves():
            first, last = fc.keyframe_points[0], fc.keyframe_points[-1]
            self.assertAlmostEqual(last.co[1], first.co[1], places=6)


class CloseLoopIdempotentTest(unittest.TestCase):
    """Closing an already-closed loop only appends another still tail."""

    def tearDown(self):
        _clear_animation()

    def test_second_pass_keeps_endpoints_equal(self):
        _clear_animation()
        fps = bpy.context.scene.render.fps
        move_path = _write_move(_synthetic_move(fps, duration=1.0))
        import_move.apply(bpy.context, move_path, load_audio=False)
        seamless.close_loop(bpy.context, blend=0.5)
        seamless.close_loop(bpy.context, blend=0.5)
        for fc in _move_fcurves():
            first, last = fc.keyframe_points[0], fc.keyframe_points[-1]
            self.assertAlmostEqual(last.co[1], first.co[1], places=6)


class NoAnimationTest(unittest.TestCase):
    def test_raises_without_keys(self):
        _clear_animation()
        with self.assertRaises(rig.RigError):
            seamless.close_loop(bpy.context)
