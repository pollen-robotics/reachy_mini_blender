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


def close_loop(context, blend=0.5, mapping=None):
    """Append a `blend`-seconds return-to-start. Returns a stats dict.

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
    end = scene.frame_end + max(1, int(round(blend * fps)))

    channels = 0
    for data_path, array_index in keyframed._move_channels(arm_obj, m):
        fc = next((f for f in cb.fcurves
                   if f.data_path == data_path
                   and f.array_index == array_index), None)
        if fc is None or len(fc.keyframe_points) < 2:
            continue  # unkeyed or constant: nothing to close
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
