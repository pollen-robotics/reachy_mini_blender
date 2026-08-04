"""Turn a RecordedMove JSON back into editable keyframes on the rig.

The exact inverse of rig.read() + bake.bake(): a move recorded by
puppeteering the real robot (Marionette, the daemon's own recorder, or a
community dataset from the Hub) becomes location/rotation keys an animator
can refine in the Graph Editor, then re-export or play back.

Where the keys land, and why:

  - Head.001        location + rotation_euler. Computed analytically from
                    rest matrices (no per-frame depsgraph round-trips), by
                    inverting the same change-of-basis rig.read() applies.
  - body yaw        on the slider that drives Core, not on Core itself:
                    Core.rotation_euler has a driver, so keys there would be
                    dead. The driver's linear coefficient is read from its
                    expression at import time (nothing hardcoded); a rig
                    without that driver gets keys on the yaw bone directly.
  - antennas        on each chain's base bone (Antenna.*.002, through its
                    slider, same driver inversion as yaw). The base is the
                    robot's actual hinge: keying the free tail bone instead
                    would rotate only the distal segment - the antenna
                    visually bends where the bones meet instead of sweeping.
                    The tail bones are reset to zero to keep the sum honest.

Raw captures are ~30-100 Hz with sensor noise: unusable as keyframes as-is.
Each channel goes through motionclean (Gaussian low-pass, then RDP
simplification), so what lands in the Graph Editor is the handful of keys
that actually shape the motion. Timestamps in real recordings are not
uniform (Marionette captures whenever its loop gets CPU), so everything
works off the file's own time array.
"""

import bisect
import json
import math
import pathlib
import re

import bpy
from mathutils import Euler, Matrix

from . import motionclean, rig

# Default cleanup tolerances, scaled by the user-facing `tolerance` factor.
# Head translation is bounded at ~0.5 mm on the robot (converted to rig
# units through head_scale); rotations at ~0.3 deg. Both are far below
# anything visible on the physical head, so cleanup at 1.0 is lossless in
# practice while cutting a 30 Hz capture down to a few dozen keys.
_EPS_ROBOT_METERS = 0.0005
_EPS_RADIANS = 0.005

_AUDIO_SUFFIXES = (".wav", ".ogg", ".oga", ".mp3", ".flac")

# 'var * 25.386' or '25.386 * var' - the shipped rig's driver shape.
_LINEAR_EXPR = re.compile(
    r"^\s*(?:var\s*\*\s*([-+0-9.eE]+)|([-+0-9.eE]+)\s*\*\s*var)\s*$")


class MoveFormatError(ValueError):
    """The JSON file is not a RecordedMove."""


def load_move(path):
    """Read and structurally validate a RecordedMove JSON file."""
    with open(path, "r", encoding="utf-8") as fh:
        try:
            move = json.load(fh)
        except ValueError as exc:
            raise MoveFormatError(f"not valid JSON: {exc}") from exc
    if not isinstance(move, dict) or "time" not in move:
        raise MoveFormatError("no 'time' array - not a RecordedMove file")
    if not move.get("audio_only"):
        frames = move.get("set_target_data")
        if not isinstance(frames, list) or len(frames) != len(move["time"]):
            raise MoveFormatError(
                "'set_target_data' missing or not matching 'time'")
        if len(move["time"]) < 2:
            raise MoveFormatError("fewer than 2 samples")
    return move


def find_audio_sidecar(json_path):
    """The move's audio file, if any. Marionette writes .wav, we write .ogg."""
    base = pathlib.Path(json_path)
    for suffix in _AUDIO_SUFFIXES:
        cand = base.with_suffix(suffix)
        if cand.exists():
            return str(cand)
    return None


def _driven_write_target(arm_obj, bone, axis):
    """(data_path, index, value_scale) for keys targeting a driven channel.

    If the bone's rotation channel is driven by a slider through a linear
    expression, keys must go on the slider (keys under a driver are never
    evaluated) with values divided by the driver's coefficient. Without a
    driver, keys go on the bone channel itself.
    """
    driven_path = f'pose.bones["{bone}"].rotation_euler'
    ad = arm_obj.animation_data
    if ad is not None:
        for d in ad.drivers:
            if d.data_path != driven_path or d.array_index != axis:
                continue
            drv = d.driver
            match = (_LINEAR_EXPR.match(drv.expression)
                     if drv.type == "SCRIPTED" else None)
            transform_ok = (
                match and len(drv.variables) == 1
                and drv.variables[0].targets[0].bone_target
                and drv.variables[0].targets[0].transform_type.startswith("LOC_"))
            if not transform_ok:
                raise rig.RigError(
                    f"{driven_path}[{axis}] has a driver this importer "
                    "cannot invert (expected 'var * <k>' on a slider "
                    "location)")
            k = float(match.group(1) or match.group(2))
            if k == 0.0:
                raise rig.RigError(
                    f"driver on {driven_path}[{axis}] has a zero coefficient")
            tgt = drv.variables[0].targets[0]
            loc_axis = {"LOC_X": 0, "LOC_Y": 1, "LOC_Z": 2}[tgt.transform_type]
            return (f'pose.bones["{tgt.bone_target}"].location', loc_axis,
                    1.0 / k)
    return (driven_path, axis, 1.0)


def _zero_free_antenna_bones(arm_obj, m):
    """Reset the FK tail bones of each antenna chain to zero rotation.

    Imported antenna keys drive the base bone (chain[0], through its
    slider); rig.read() sums the whole chain, so a leftover pose on the
    free .003 bones would silently offset every antenna angle - and
    visually kink the antenna where the two bones meet.
    """
    for chain in (m.antenna_r_bones, m.antenna_l_bones):
        for name in chain[1:]:
            pb = arm_obj.pose.bones.get(name)
            if pb is not None:
                pb.rotation_euler[m.antenna_axis] = 0.0


def _head_channels(arm_obj, m, frames):
    """Per-sample Head.001 basis loc/euler + Core basis yaw, analytically.

    Head.001 is parented to the yaw bone, so its matrix_basis depends on
    the yaw of the same sample. Rather than stepping the scene and letting
    the depsgraph solve it (seconds of churn for long captures), the pose
    chain is composed from rest matrices:

        pose(bone) = pose(parent) @ rest(parent)^-1 @ rest(bone) @ basis

    valid here because every bone involved has default inherit flags
    (verified on the shipped rig, and the roundtrip test would catch a
    violation).
    """
    B = rig.BASE_TO_ROBOT
    Bt = B.transposed()

    base_rest = rig._rest_bone(arm_obj, m.base_bone).matrix_local
    core_rest = rig._rest_bone(arm_obj, m.body_yaw_bone).matrix_local
    head_rest = rig._rest_bone(arm_obj, m.head_bone).matrix_local

    # Base may itself be posed (it is static, so once is enough).
    base_pose = rig._pose_bone(arm_obj, m.base_bone).matrix.copy()

    rst = base_rest.inverted() @ head_rest
    rst3 = rst.to_3x3()
    rst_t = rst.translation.copy()
    core_pre = base_pose @ base_rest.inverted() @ core_rest
    head_rel = core_rest.inverted() @ head_rest

    yaw_axis = "XYZ"[m.body_yaw_axis]

    loc = ([], [], [])
    eul = ([], [], [])
    prev_euler = None
    for frame in frames:
        H = Matrix(frame["head"])
        cur3 = Bt @ H.to_3x3() @ B @ rst3
        cur = cur3.to_4x4()
        cur.translation = rst_t + (Bt @ H.to_translation()) / m.head_scale

        core_rot = m.body_yaw_sign * float(frame.get("body_yaw", 0.0))
        core_pose = core_pre @ Matrix.Rotation(core_rot, 4, yaw_axis)
        m_pose = base_pose @ cur
        basis = (core_pose @ head_rel).inverted() @ m_pose

        t = basis.to_translation()
        e = basis.to_3x3().to_euler("XYZ", prev_euler) if prev_euler \
            else basis.to_3x3().to_euler("XYZ")
        prev_euler = e
        for i in range(3):
            loc[i].append(t[i])
            eul[i].append(e[i])
    return loc, eul


def apply(context, filepath, mapping=None, smooth_sigma=0.02, tolerance=1.0,
          snap_to_frames=True, load_audio=True, set_scene_range=True):
    """Import a move file onto the rig. Returns a stats dict.

    tolerance scales the cleanup epsilons (0 keeps every sample as a key);
    smooth_sigma is the Gaussian low-pass width in seconds (0 disables).
    """
    m = mapping or rig.Mapping()
    arm_obj = bpy.data.objects.get(m.armature)
    if arm_obj is None:
        raise rig.RigError(f"armature object {m.armature!r} not found")

    move = load_move(filepath)
    stats = {"samples": 0, "keys": 0, "duration": 0.0, "audio": None}

    scene = context.scene
    fps = scene.render.fps / scene.render.fps_base
    frame0 = scene.frame_start

    if not move.get("audio_only"):
        t0 = float(move["time"][0])
        times = [float(t) - t0 for t in move["time"]]
        frames = move["set_target_data"]
        stats["samples"] = len(times)
        stats["duration"] = times[-1]

        eps_loc = (_EPS_ROBOT_METERS / m.head_scale) * tolerance
        eps_rot = _EPS_RADIANS * tolerance

        loc, eul = _head_channels(arm_obj, m, frames)
        yaw_target = _driven_write_target(arm_obj, m.body_yaw_bone,
                                          m.body_yaw_axis)
        # Antennas go on the base bone of each chain (through its slider):
        # that is the robot's actual hinge, so the whole antenna sweeps.
        # Keys on the free tail bone would only rotate the distal part -
        # a visual kink instead of a rotation.
        ant_r_target = _driven_write_target(arm_obj, m.antenna_r_bones[0],
                                            m.antenna_axis)
        ant_l_target = _driven_write_target(arm_obj, m.antenna_l_bones[0],
                                            m.antenna_axis)
        yaw = [m.body_yaw_sign * float(f.get("body_yaw", 0.0)) for f in frames]
        # File order is [right, left]; see bake.py / RigState.antennas.
        ant_r = [m.antenna_r_sign * float(f["antennas"][0]) for f in frames]
        ant_l = [m.antenna_l_sign * float(f["antennas"][1]) for f in frames]

        head_path = f'pose.bones["{m.head_bone}"]'
        channels = []
        for i in range(3):
            channels.append((f"{head_path}.location", i,
                             loc[i], eps_loc, False, 1.0))
            channels.append((f"{head_path}.rotation_euler", i,
                             eul[i], eps_rot, True, 1.0))
        channels.append((*yaw_target[:2], yaw, eps_rot, True, yaw_target[2]))
        channels.append((*ant_r_target[:2], ant_r, eps_rot, True,
                         ant_r_target[2]))
        channels.append((*ant_l_target[:2], ant_l, eps_rot, True,
                         ant_l_target[2]))

        cb = _ensure_channelbag(arm_obj, pathlib.Path(filepath).stem)

        # A DOF can be keyed through several rig elements (a slider, the
        # driven bone, a chain's tail bone). This import writes exactly
        # one of them per DOF, so stale fcurves on the alternates (an
        # older import, hand keys) must go too - rig.read() sums chains,
        # and leftovers would double-drive the robot.
        stale = [(f'pose.bones["{m.body_yaw_bone}"].rotation_euler',
                  m.body_yaw_axis)]
        for chain in (m.antenna_r_bones, m.antenna_l_bones):
            stale += [(f'pose.bones["{name}"].rotation_euler',
                       m.antenna_axis) for name in chain]
        for path, index in stale:
            for fc in [f for f in cb.fcurves
                       if f.data_path == path and f.array_index == index]:
                cb.fcurves.remove(fc)

        for data_path, index, values, eps, angular, value_scale in channels:
            vals = motionclean.unwrap(values) if angular else values
            vals = motionclean.gaussian_smooth(times, vals, smooth_sigma)
            samples = [(frame0 + t * fps, v * value_scale)
                       for t, v in zip(times, vals)]
            fc = _solve_channel(cb, data_path, index, samples,
                                eps * abs(value_scale), snap_to_frames)
            stats["keys"] += len(fc.keyframe_points)

        _zero_free_antenna_bones(arm_obj, m)

        if set_scene_range:
            scene.frame_end = max(
                frame0 + 1, frame0 + int(math.ceil(times[-1] * fps)))

    if load_audio:
        sidecar = find_audio_sidecar(filepath)
        if sidecar:
            _add_sound_strip(scene, sidecar, frame0)
            stats["audio"] = sidecar

    return stats


def _solve_channel(cb, data_path, index, samples, eps, snap):
    """Write one channel's fcurve, keeping the fewest keys within `eps`.

    RDP picks the initial key set, but RDP's guarantee is for *linear*
    interpolation and Blender draws auto-clamped Beziers between keys,
    which drift from the chord on sparse keys. So the bound is enforced
    against the real thing: evaluate the actual fcurve at every recorded
    sample and re-insert the worst offender until the curve is within
    eps everywhere it still can be.

    When snapping, a key moved to a whole frame carries the *signal's
    value at that frame* (interpolated between the neighbouring samples),
    not the nearest sample's own value - the sample's value at the wrong
    time is exactly how fast gestures pick up half-frame wobble. Samples
    whose frame slot is already keyed are marked unfixable instead of
    aborting the loop: on a whole-frame grid that residual cannot be
    keyed away.
    """
    frames = [s[0] for s in samples]
    values = [s[1] for s in samples]

    def key_for(i):
        if not snap:
            return frames[i], values[i]
        f = float(round(frames[i]))
        return f, _value_at(frames, values, f)

    keep = (motionclean.rdp(frames, values, eps) if eps > 0.0
            else range(len(samples)))
    keyed = {}
    for i in keep:
        f, v = key_for(i)
        keyed[f] = v

    fc = _write_fcurve(cb, data_path, index, sorted(keyed.items()))
    if eps <= 0.0:
        return fc

    fixable = set(range(len(samples)))
    while fixable:
        worst_i = max(fixable,
                      key=lambda i: abs(fc.evaluate(frames[i]) - values[i]))
        if abs(fc.evaluate(frames[worst_i]) - values[worst_i]) <= eps:
            break
        f, v = key_for(worst_i)
        if f in keyed:
            fixable.discard(worst_i)
            continue
        keyed[f] = v
        fc = _write_fcurve(cb, data_path, index, sorted(keyed.items()))
    return fc


def _value_at(frames, values, f):
    """Linear interpolation of the sample polyline at frame f."""
    i = bisect.bisect_right(frames, f)
    if i <= 0:
        return values[0]
    if i >= len(frames):
        return values[-1]
    span = frames[i] - frames[i - 1]
    if span <= 0.0:
        return values[i]
    a = (f - frames[i - 1]) / span
    return values[i - 1] + a * (values[i] - values[i - 1])


def _ensure_channelbag(arm_obj, action_name):
    """The channelbag imported fcurves go into (Blender 5 slotted actions).

    Reuses the armature's current action when there is one - importing must
    not silently discard keys the animator has on other bones - otherwise
    creates an action named after the move file.
    """
    ad = arm_obj.animation_data_create()
    if ad.action is None:
        act = bpy.data.actions.new(action_name)
        slot = act.slots.new(id_type="OBJECT", name=arm_obj.name)
        ad.action = act
        ad.action_slot = slot
    act = ad.action
    slot = ad.action_slot
    if slot is None:
        slot = act.slots[0] if len(act.slots) else act.slots.new(
            id_type="OBJECT", name=arm_obj.name)
        ad.action_slot = slot
    layer = act.layers[0] if len(act.layers) else act.layers.new("Base")
    strip = layer.strips[0] if len(layer.strips) else layer.strips.new(
        type="KEYFRAME")
    return strip.channelbag(slot, ensure=True)


def _write_fcurve(cb, data_path, index, keys):
    """Replace the fcurve at (data_path, index) with the given keys."""
    for fc in [f for f in cb.fcurves
               if f.data_path == data_path and f.array_index == index]:
        cb.fcurves.remove(fc)
    fc = cb.fcurves.new(data_path, index=index)
    if keys:
        fc.keyframe_points.add(len(keys))
        for kp, (frame, value) in zip(fc.keyframe_points, keys):
            kp.co = (frame, value)
            kp.interpolation = "BEZIER"
            kp.handle_left_type = "AUTO_CLAMPED"
            kp.handle_right_type = "AUTO_CLAMPED"
    fc.update()
    return fc


def _add_sound_strip(scene, filepath, frame_start):
    """Drop the sidecar on a free sequencer channel at the move's start.

    Strips added here are tagged, and a later import removes the tagged
    ones first: imported motion replaces the previous move's fcurves, so
    its audio replaces the previous sidecar too instead of stacking
    into a chorus. Strips the user added by hand are left alone.
    """
    se = scene.sequence_editor_create()
    # Blender 5 renamed SequenceEditor.sequences to strips.
    strips = se.strips if hasattr(se, "strips") else se.sequences
    all_strips = (se.strips_all if hasattr(se, "strips_all")
                  else se.sequences_all)
    for strip in [s for s in all_strips if s.get("reachy_mini_import")]:
        strips.remove(strip)
    channel = max((s.channel for s in all_strips), default=0) + 1
    strip = strips.new_sound(name=pathlib.Path(filepath).stem,
                             filepath=filepath, channel=channel,
                             frame_start=int(frame_start))
    strip["reachy_mini_import"] = True
