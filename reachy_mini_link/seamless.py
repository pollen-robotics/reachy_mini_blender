"""Close a move into a seamless loop.

Recorded captures (Marionette's especially) almost never end exactly
where they started, so looping or chaining them on the robot pops at
the seam - and fixing that by hand means matching nine channels' end
values and tangents against their first keys. This closes the loop in
one operation: every move channel gets one final key holding its first
key's value, eased in over a short return window appended after the
current end, and the junction's incoming slope copies the first key's
outgoing slope. The result is continuous in both pose and velocity:
f(end) == f(start) and f'(end) == f'(start) on every channel, so a
player that repeats or chains the exported file shows no seam.

Only the move's own channels (head pose, body yaw and antenna write
targets) are touched; anything else keyed on the armature is the
animator's business.
"""

import bpy

from . import keyframed, rig

# Auto blend duration bounds (seconds): never shorter than a beat the
# eye can read, never so long the loop drags.
_BLEND_MIN = 0.15
_BLEND_MAX = 2.0

# A smooth ease peaks around 1.5x its average speed, so returning in
# gap/v_max seconds would overshoot the move's own top speed at the
# middle of the ease. Budget for it.
_EASE_PEAK_FACTOR = 1.5


def _auto_blend(fcurves, frame_start, frame_end, fps):
    """Return time sized to the pose gap: gap / the move's own top speed.

    Each channel's return time is how long the move itself would take to
    travel its end-to-start gap at its own peak velocity (with an ease
    factor, since the return eases in and out). The slowest channel
    wins, clamped to sane bounds - a move ending near its start pose
    gets a blink of a return, one ending far away gets the time it
    actually needs.
    """
    need = _BLEND_MIN
    for fc in fcurves:
        first = fc.keyframe_points[0].co[1]
        gap = abs(fc.evaluate(frame_end) - first)
        if gap < 1e-9:
            continue
        prev = fc.evaluate(frame_start)
        v_max = 0.0
        for f in range(frame_start + 1, frame_end + 1):
            cur = fc.evaluate(f)
            v_max = max(v_max, abs(cur - prev) * fps)
            prev = cur
        if v_max < 1e-9:
            continue  # constant-but-offset cannot happen; guard anyway
        need = max(need, _EASE_PEAK_FACTOR * gap / v_max)
    return min(need, _BLEND_MAX)


def close_loop(context, blend=None, mapping=None):
    """Append a return-to-start. Returns a stats dict.

    `blend` is the return duration in seconds; None (the default) sizes
    it automatically from the pose gap and the move's own peak speed.
    Uses the scene's end frame as the loop's last moving frame: the
    return segment is appended after it and the scene range extended to
    match, so nothing already animated is compressed or discarded
    (keys at or past the new end, if any, are replaced by the closing
    key).
    """
    m = mapping or rig.Mapping()
    arm_obj = bpy.data.objects.get(m.armature)
    if arm_obj is None:
        raise rig.RigError(f"armature object {m.armature!r} not found")
    cb = keyframed._channelbag(arm_obj)
    if cb is None:
        raise rig.RigError("no keyed move on the rig - import or key one first")

    scene = context.scene
    fps = scene.render.fps / scene.render.fps_base

    fcurves = []
    for data_path, array_index in keyframed._move_channels(arm_obj, m):
        fc = next((f for f in cb.fcurves
                   if f.data_path == data_path
                   and f.array_index == array_index), None)
        if fc is not None and len(fc.keyframe_points) >= 2:
            fcurves.append(fc)  # unkeyed or constant: nothing to close

    if blend is None:
        blend = _auto_blend(fcurves, scene.frame_start, scene.frame_end, fps)
    end = scene.frame_end + max(1, int(round(blend * fps)))

    channels = 0
    for fc in fcurves:
        first = fc.keyframe_points[0]
        first_value = first.co[1]
        # The loop's outgoing slope, to be mirrored at the junction so
        # velocity is continuous across the seam.
        dx = first.handle_right[0] - first.co[0]
        slope = (first.handle_right[1] - first_value) / dx if dx else 0.0

        for kp in [k for k in fc.keyframe_points if k.co[0] >= end]:
            fc.keyframe_points.remove(kp)
        kp = fc.keyframe_points.insert(end, first_value)
        kp.interpolation = "BEZIER"
        kp.handle_right_type = "AUTO_CLAMPED"
        kp.handle_left_type = "FREE"
        prev = fc.keyframe_points[-2]
        span = max((end - prev.co[0]) / 3.0, 1e-3)
        kp.handle_left = (end - span, first_value - slope * span)
        fc.update()
        channels += 1

    if channels:
        scene.frame_end = end
    return {"channels": channels, "end": end, "blend": blend}
