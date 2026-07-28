"""Tests for reachy_mini_link.rig. Must run inside Blender.

  blender --background reachy_mini.blend --python tests/run_blender_tests.py -- test_rig

Expected values come from docs/RIG_MAPPING.md and are verified against the
rig's driver constants, so these tests fail loudly if the rig is recalibrated.
"""
import math
import unittest

import bpy
from mathutils import Matrix

from reachy_mini_link import rig

# The shipped rig's rest pose is exactly zero on every bone, so neutral
# assertions only need to absorb float error from the matrix inverse.
TOL = 1e-6


def reset_pose():
    arm = bpy.data.objects["Armature"]
    for pb in arm.pose.bones:
        pb.location = (0.0, 0.0, 0.0)
        pb.rotation_euler = (0.0, 0.0, 0.0)
        pb.scale = (1.0, 1.0, 1.0)
    bpy.context.view_layer.update()


def read():
    bpy.context.view_layer.update()
    return rig.read(bpy.context.evaluated_depsgraph_get())


class TestNeutral(unittest.TestCase):
    def setUp(self):
        reset_pose()

    def test_head_is_identity_at_rest(self):
        state = read()
        for i in range(4):
            for j in range(4):
                self.assertAlmostEqual(
                    state.head[i][j], 1.0 if i == j else 0.0, delta=TOL,
                    msg=f"head[{i}][{j}]")

    def test_body_yaw_and_antennas_are_zero_at_rest(self):
        state = read()
        self.assertAlmostEqual(state.body_yaw, 0.0, delta=TOL)
        self.assertAlmostEqual(state.antennas[0], 0.0, delta=TOL)
        self.assertAlmostEqual(state.antennas[1], 0.0, delta=TOL)


class TestBodyYaw(unittest.TestCase):
    def setUp(self):
        reset_pose()

    def test_slider_drives_body_yaw_by_driver_constant(self):
        # Slider.Rot.Core LOC_Y * 25.386 -> Core.rotation_euler[1].
        bpy.data.objects["Armature"].pose.bones["Slider.Rot.Core"].location[1] = 0.05
        self.assertAlmostEqual(read().body_yaw, 1.269300, delta=1e-5)

    def test_full_slider_travel_is_the_robot_limit(self):
        bpy.data.objects["Armature"].pose.bones["Slider.Rot.Core"].location[1] = 0.11
        # MJCF yaw_body range is +/-2.792526 rad (+/-160 deg).
        self.assertAlmostEqual(read().body_yaw, 2.792460, delta=1e-4)
        self.assertAlmostEqual(math.degrees(read().body_yaw), 160.0, delta=0.01)


class TestAntennas(unittest.TestCase):
    def setUp(self):
        reset_pose()

    def test_left_slider_drives_left_antenna(self):
        bpy.data.objects["Armature"].pose.bones["Slider.Rot.Antenna.L"].location[1] = 0.05
        state = read()
        self.assertAlmostEqual(state.antennas[1], 1.428000, delta=1e-5)  # left
        self.assertAlmostEqual(state.antennas[0], 0.0, delta=TOL)        # right

    def test_right_slider_drives_right_antenna(self):
        bpy.data.objects["Armature"].pose.bones["Slider.Rot.Antenna.R"].location[1] = 0.05
        state = read()
        self.assertAlmostEqual(state.antennas[0], 1.428000, delta=1e-5)
        self.assertAlmostEqual(state.antennas[1], 0.0, delta=TOL)

    def test_fk_control_adds_to_the_slider(self):
        # .002 (slider-driven) and .003 (FK) are collinear with identical rest
        # frames, so the robot's single hinge value is their sum.
        arm = bpy.data.objects["Armature"]
        arm.pose.bones["Slider.Rot.Antenna.L"].location[1] = 0.05
        arm.pose.bones["Antenna.L.003"].rotation_euler[2] = 0.25
        self.assertAlmostEqual(read().antennas[1], 1.428000 + 0.25, delta=1e-5)


class TestHeadPose(unittest.TestCase):
    def setUp(self):
        reset_pose()

    def test_pure_translation_scales_to_metres(self):
        bpy.data.objects["Armature"].pose.bones["Head.001"].location = (0.0, 0.0, 0.02)
        state = read()
        t = state.head.translation
        # Head.001's local Z is world -Y (matrix_local: localZ = (0,-1,0)), so
        # assert on magnitude rather than guessing which world axis it lands on.
        self.assertAlmostEqual(t.length, 0.02 * rig.HEAD_TRANSLATION_SCALE, delta=1e-6)

    def test_rotation_and_translation_are_assembled_independently(self):
        # The robot wants R about the neutral head origin plus a translation
        # offset from it. The naive composed form M_cur @ M_rest^-1 yields
        # p_cur - R*p_rest instead, which differs once both are non-zero.
        # This test pins the correct form by checking the translation is the
        # scaled origin delta and nothing else.
        arm = bpy.data.objects["Armature"]
        pb = arm.pose.bones["Head.001"]
        pb.location = (0.0, 0.0, 0.03)
        pb.rotation_euler = (0.0, 0.0, math.radians(20.0))
        bpy.context.view_layer.update()
        dg = bpy.context.evaluated_depsgraph_get()
        state = rig.read(dg)

        m = rig.Mapping()
        ev = arm.evaluated_get(dg)
        base_pose = ev.pose.bones[m.base_bone].matrix
        head_pose = ev.pose.bones[m.head_bone].matrix
        base_rest = ev.data.bones[m.base_bone].matrix_local
        head_rest = ev.data.bones[m.head_bone].matrix_local
        cur = base_pose.inverted() @ head_pose
        rst = base_rest.inverted() @ head_rest

        expected_t = (cur.translation - rst.translation) * rig.HEAD_TRANSLATION_SCALE
        self.assertAlmostEqual((state.head.translation - expected_t).length, 0.0,
                               delta=1e-9)

        naive = (cur @ rst.inverted()).translation * rig.HEAD_TRANSLATION_SCALE
        self.assertGreater((naive - expected_t).length, 1e-5,
                           "test is vacuous unless the two forms actually differ")

    def test_rotation_is_recoverable(self):
        pb = bpy.data.objects["Armature"].pose.bones["Head.001"]
        pb.rotation_euler = (0.0, 0.0, math.radians(15.0))
        state = read()
        angle = state.head.to_3x3().to_quaternion().angle
        self.assertAlmostEqual(math.degrees(angle), 15.0, delta=0.01)

    def test_body_yaw_rotates_head_in_base_frame(self):
        # Critical: Head.001 is parented under Core. When the body yaws, the
        # head must rotate in the base frame. This test catches the bug where
        # base_bone is accidentally set to "Core" instead of "Base", which would
        # cancel the yaw out of the head pose.
        arm = bpy.data.objects["Armature"]
        arm.pose.bones["Slider.Rot.Core"].location[1] = 0.05
        state = read()
        # Head.001 is at rest locally, but Core has rotated, so head pose
        # in base frame should NOT be identity.
        head_angle = state.head.to_3x3().to_quaternion().angle
        self.assertAlmostEqual(head_angle, 1.269300, delta=1e-3,
                               msg="head rotation should contain body yaw")
        self.assertAlmostEqual(state.body_yaw, 1.269300, delta=1e-5,
                               msg="body yaw should be independently correct")


class TestSerialisation(unittest.TestCase):
    def setUp(self):
        reset_pose()

    def test_head_flat_is_16_floats_row_major(self):
        flat = read().head_flat()
        self.assertEqual(len(flat), 16)
        self.assertEqual([round(v, 6) for v in flat],
                         [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])

    def test_head_flat_row_major_order_is_not_column_major(self):
        # Put a known translation in so the last column is non-trivial, then
        # assert the translation lands in row-major positions 3, 7, 11.
        m = Matrix.Translation((1.0, 2.0, 3.0))
        state = rig.RigState(head=m, body_yaw=0.0, antennas=(0.0, 0.0))
        self.assertEqual(state.head_flat()[3], 1.0)
        self.assertEqual(state.head_flat()[7], 2.0)
        self.assertEqual(state.head_flat()[11], 3.0)

    def test_head_nested_is_4x4(self):
        nested = read().head_nested()
        self.assertEqual(len(nested), 4)
        self.assertTrue(all(len(row) == 4 for row in nested))


class TestErrors(unittest.TestCase):
    def test_missing_bone_raises_rigerror_naming_the_bone(self):
        reset_pose()
        bad = rig.Mapping(head_bone="NoSuchBone")
        with self.assertRaises(rig.RigError) as ctx:
            rig.read(bpy.context.evaluated_depsgraph_get(), bad)
        self.assertIn("NoSuchBone", str(ctx.exception))

    def test_missing_armature_raises_rigerror(self):
        bad = rig.Mapping(armature="NoSuchArmature")
        with self.assertRaises(rig.RigError):
            rig.read(bpy.context.evaluated_depsgraph_get(), bad)


if __name__ == "__main__":
    unittest.main()
