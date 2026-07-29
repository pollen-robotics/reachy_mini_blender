"""Tests for reachy_mini_link.rig. Must run inside Blender.

  blender --background reachy_mini.blend --python tests/run_blender_tests.py -- test_rig

Expected values come from docs/RIG_MAPPING.md and are verified against the
rig's driver constants, so these tests fail loudly if the rig is recalibrated.
"""
import math
import unittest

import bpy
from mathutils import Matrix, Vector

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

        # Both forms are re-expressed in the robot frame via BASE_TO_ROBOT
        # (see rig.py) before comparison with the pose actually returned.
        expected_t = rig.BASE_TO_ROBOT @ (
            (cur.translation - rst.translation) * rig.HEAD_TRANSLATION_SCALE)
        self.assertAlmostEqual((state.head.translation - expected_t).length, 0.0,
                               delta=1e-9)

        naive = rig.BASE_TO_ROBOT @ (
            (cur @ rst.inverted()).translation * rig.HEAD_TRANSLATION_SCALE)
        self.assertGreater((naive - expected_t).length, 1e-5,
                           "test is vacuous unless the two forms actually differ")

    def test_rotation_is_recoverable(self):
        pb = bpy.data.objects["Armature"].pose.bones["Head.001"]
        pb.rotation_euler = (0.0, 0.0, math.radians(15.0))
        state = read()
        angle = state.head.to_3x3().to_quaternion().angle
        self.assertAlmostEqual(math.degrees(angle), 15.0, delta=0.01)

    def test_bone_scale_does_not_leak_into_head_rotation(self):
        # The robot has no scale DOF. A non-orthonormal rotation block (e.g.
        # from an artist pressing S instead of G on Head.001) must be
        # projected back to a pure rotation rather than sent as-is.
        bpy.data.objects["Armature"].pose.bones["Head.001"].scale = (1.5, 1.5, 1.5)
        state = read()
        self.assertAlmostEqual(state.head.to_3x3().determinant(), 1.0, delta=1e-6)

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

    # -- BASE_TO_ROBOT: bone-local frame is not the robot frame -----------
    #
    # A Blender bone's local Y runs ALONG the bone, and `Base` runs along
    # world +Z, so `Base`'s bone-local frame is NOT the robot's REP-103
    # frame (+X forward, +Y left, +Z up). These tests move/rotate Head.001
    # along known WORLD directions and check the reported pose lands on the
    # correct ROBOT axis. Without BASE_TO_ROBOT (i.e. the old, buggy code)
    # these fail: e.g. moving forward would report a -Z (down) translation
    # instead of +X.

    def _move_head_world(self, world_dir):
        """Move Head.001 by 3cm along a WORLD direction.

        The pose bone's `location` is in bone-local space, so the world
        direction must be converted through the rest matrix first.
        """
        arm = bpy.data.objects["Armature"]
        pb = arm.pose.bones["Head.001"]
        rest3 = arm.data.bones["Head.001"].matrix_local.to_3x3()
        pb.location = rest3.inverted() @ Vector(world_dir).normalized() * 0.03
        return read()

    def test_translation_forward_maps_to_robot_plus_x(self):
        # Rig +Y is the rig's forward/face direction.
        t = self._move_head_world((0.0, 1.0, 0.0)).head.translation
        expected = 0.03 * rig.HEAD_TRANSLATION_SCALE  # 0.013725
        self.assertAlmostEqual(t.x, expected, delta=1e-5)
        self.assertAlmostEqual(t.y, 0.0, delta=1e-6)
        self.assertAlmostEqual(t.z, 0.0, delta=1e-6)

    def test_translation_up_maps_to_robot_plus_z(self):
        t = self._move_head_world((0.0, 0.0, 1.0)).head.translation
        expected = 0.03 * rig.HEAD_TRANSLATION_SCALE
        self.assertAlmostEqual(t.z, expected, delta=1e-5)
        self.assertAlmostEqual(t.x, 0.0, delta=1e-6)
        self.assertAlmostEqual(t.y, 0.0, delta=1e-6)

    def test_translation_robot_left_maps_to_robot_plus_y(self):
        # Rig -X is where Antenna.L sits -- the robot's left, i.e. robot +Y.
        t = self._move_head_world((-1.0, 0.0, 0.0)).head.translation
        expected = 0.03 * rig.HEAD_TRANSLATION_SCALE
        self.assertAlmostEqual(t.y, expected, delta=1e-5)
        self.assertAlmostEqual(t.x, 0.0, delta=1e-6)
        self.assertAlmostEqual(t.z, 0.0, delta=1e-6)

    def test_rotation_bone_local_y_is_robot_yaw(self):
        # Head.001 bone-local Y == world Z; a +20deg turn there must appear
        # as yaw about robot Z.
        pb = bpy.data.objects["Armature"].pose.bones["Head.001"]
        pb.rotation_euler[1] = math.radians(20.0)
        euler = read().head.to_3x3().to_euler('XYZ')
        self.assertAlmostEqual(math.degrees(euler.x), 0.0, delta=0.01)
        self.assertAlmostEqual(math.degrees(euler.y), 0.0, delta=0.01)
        self.assertAlmostEqual(math.degrees(euler.z), 20.0, delta=0.01)

    def test_rotation_bone_local_x_is_robot_pitch(self):
        # Head.001 bone-local X == world X; a +20deg turn there must appear
        # as pitch, euler XYZ ~= (0, -20, 0) deg.
        pb = bpy.data.objects["Armature"].pose.bones["Head.001"]
        pb.rotation_euler[0] = math.radians(20.0)
        euler = read().head.to_3x3().to_euler('XYZ')
        self.assertAlmostEqual(math.degrees(euler.x), 0.0, delta=0.01)
        self.assertAlmostEqual(math.degrees(euler.y), -20.0, delta=0.01)
        self.assertAlmostEqual(math.degrees(euler.z), 0.0, delta=0.01)

    def test_rotation_bone_local_z_is_robot_roll(self):
        # Head.001 bone-local Z == world -Y; a +20deg turn there must appear
        # as roll, euler XYZ ~= (-20, 0, 0) deg.
        pb = bpy.data.objects["Armature"].pose.bones["Head.001"]
        pb.rotation_euler[2] = math.radians(20.0)
        euler = read().head.to_3x3().to_euler('XYZ')
        self.assertAlmostEqual(math.degrees(euler.x), -20.0, delta=0.01)
        self.assertAlmostEqual(math.degrees(euler.y), 0.0, delta=0.01)
        self.assertAlmostEqual(math.degrees(euler.z), 0.0, delta=0.01)


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
