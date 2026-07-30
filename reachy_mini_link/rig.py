"""Read the robot's DOFs out of the Blender rig.

Read-only: this module never assigns to bpy.data or to a pose channel.

Every bone name, axis index and constant here is documented with its
provenance in docs/RIG_MAPPING.md. The short version:

  - head     Head.001 relative to Base, rotation and translation assembled
             independently, translation scaled to metres
  - body_yaw Core.rotation_euler[1] -- local Y, which for that bone is world
             +Z, so it really is yaw
  - antennas Antenna.{R,L}.002 + .003 local Z summed, because the two bones
             are collinear continuations of the robot's single hinge

Head.001's rotation is orthonormalised (via a quaternion round-trip) before
being sent: the robot has no scale degree of freedom, so any bone scale
picked up on that control (e.g. an artist pressing S instead of G) is
projected out rather than shipped as a non-orthonormal, invalid IK target.
"""

from dataclasses import dataclass
from typing import Tuple

import bpy
from mathutils import Matrix

# Blender units -> metres. The rig is ~2.19x oversized despite the scene
# being set METRIC/METERS. Two independent anchors agree to 1.6%: the neutral
# head origin (0.177 m / 0.3869 BU = 0.45748) and the overall body width
# (0.160 m / 0.3554 BU = 0.45020). See docs/RIG_MAPPING.md.
HEAD_TRANSLATION_SCALE = 0.4575

# Change of basis: Base bone-local -> robot frame (X forward, Y left, Z up).
# A Blender bone's local Y runs ALONG the bone, and `Base` runs along world
# +Z, so the bone frame is NOT the robot frame:
#     Base localX -> world +X, localY -> world +Z, localZ -> world -Y
# Hence (vx, vy, vz)_bonelocal -> (-vz, -vx, vy)_robot.
# Verified against the rig: forward -> +X, up -> +Z, robot-left -> +Y, and
# all three rotation axes map correctly. det = +1, orthonormal.
BASE_TO_ROBOT = Matrix(((0, 0, -1),
                        (-1, 0, 0),
                        (0, 1, 0)))


class RigError(RuntimeError):
    """A mapped bone or object is missing — usually a renamed rig."""


@dataclass(frozen=True)
class Mapping:
    """Which rig element supplies each robot DOF.

    Defaults match the shipped reachy_mini.blend. Overriding these is how a
    renamed rig is accommodated without a code change.

    The *_sign fields are determined once by the simulator verification step
    and then left alone; they are constants, not user controls. The MJCF gives
    the two antennas different mounting quaternions, so their signs are
    established empirically rather than assumed equal.
    """

    armature: str = "Armature"
    base_bone: str = "Base"
    head_bone: str = "Head.001"

    body_yaw_bone: str = "Core"
    body_yaw_axis: int = 1          # local Y; for Core this is world +Z
    body_yaw_sign: float = 1.0

    antenna_r_bones: Tuple[str, ...] = ("Antenna.R.002", "Antenna.R.003")
    antenna_l_bones: Tuple[str, ...] = ("Antenna.L.002", "Antenna.L.003")
    antenna_axis: int = 2           # local Z, perpendicular to the shaft
    # -1.0, not +1.0: the daemon's MuJoCo backend negates the antenna target
    # relative to the rig's rotation direction, on BOTH write and read
    # (daemon/backend/mujoco/backend.py:260,334), so commanded and reported
    # values agree exactly and value-level readback tests cannot see the
    # inversion -- it only shows up in the physical tip motion. Sign
    # established by comparing measured tip-direction unit vectors (Blender
    # vs. simulated robot); see docs/RIG_MAPPING.md. Both antennas need the
    # same correction despite their different MJCF mounting quaternions.
    antenna_r_sign: float = -1.0
    antenna_l_sign: float = -1.0

    head_scale: float = HEAD_TRANSLATION_SCALE


@dataclass
class RigState:
    """One sample of the robot's DOFs, in the robot's own conventions."""

    head: Matrix                    # 4x4, base frame, metres, identity at rest
    body_yaw: float                 # radians
    antennas: Tuple[float, float]   # (right, left) radians

    def head_flat(self):
        """Row-major 16 floats — the /ws/sdk wire format."""
        return [float(v) for row in self.head for v in row]

    def head_nested(self):
        """Nested 4x4 — the RecordedMove move-file format."""
        return [[float(v) for v in row] for row in self.head]


def _pose_bone(arm, name):
    pb = arm.pose.bones.get(name)
    if pb is None:
        raise RigError(f"pose bone {name!r} not found in armature {arm.name!r}")
    return pb


def _rest_bone(arm, name):
    b = arm.data.bones.get(name)
    if b is None:
        raise RigError(f"rest bone {name!r} not found in armature {arm.name!r}")
    return b


def _sum_axis(arm, names, axis):
    return sum(_pose_bone(arm, n).rotation_euler[axis] for n in names)


def read(depsgraph, mapping=None):
    """Sample the rig. Returns a RigState.

    `depsgraph` must be current — call bpy.context.evaluated_depsgraph_get()
    after any pose change or scene.frame_set(). Raises RigError if the mapping
    does not match the rig.
    """
    m = mapping or Mapping()

    orig = bpy.data.objects.get(m.armature)
    if orig is None:
        raise RigError(f"armature object {m.armature!r} not found")
    arm = orig.evaluated_get(depsgraph)

    # -- head: Head.001 relative to Base -------------------------------
    cur = _pose_bone(arm, m.base_bone).matrix.inverted() @ _pose_bone(arm, m.head_bone).matrix
    rst = _rest_bone(arm, m.base_bone).matrix_local.inverted() @ _rest_bone(arm, m.head_bone).matrix_local

    # Assembled independently on purpose. The robot defines the head pose as a
    # rotation about the neutral head origin plus a translation offset from it,
    # so the translation must be the plain origin delta. The composed form
    # cur @ rst.inverted() would give p_cur - R*p_rest instead.
    #
    # The quaternion round-trip discards any bone scale on Head.001: the
    # robot has no scale DOF, and a non-orthonormal 3x3 (e.g. from scaling
    # the control instead of moving it) would be an invalid IK target sent
    # to the robot with no error.
    rotation = (BASE_TO_ROBOT
                @ (cur.to_3x3() @ rst.to_3x3().inverted()).to_quaternion().to_matrix()
                @ BASE_TO_ROBOT.transposed())
    translation = (BASE_TO_ROBOT @ (cur.translation - rst.translation)) * m.head_scale
    head = Matrix.Translation(translation) @ rotation.to_4x4()

    # -- body yaw ------------------------------------------------------
    body_yaw = m.body_yaw_sign * _pose_bone(arm, m.body_yaw_bone).rotation_euler[m.body_yaw_axis]

    # -- antennas, [right, left] ---------------------------------------
    right = m.antenna_r_sign * _sum_axis(arm, m.antenna_r_bones, m.antenna_axis)
    left = m.antenna_l_sign * _sum_axis(arm, m.antenna_l_bones, m.antenna_axis)

    return RigState(head=head, body_yaw=body_yaw, antennas=(right, left))
