"""Bake the Blender timeline into the robot's RecordedMove JSON.

No robot and no daemon are involved: this reads the rig frame by frame and
writes a file the SDK can replay. The shape is what
reachy_mini.motion.recorded_move.RecordedMove expects:

    {"description": str,
     "time": [seconds, ...],
     "set_target_data": [{"head": <nested 4x4>, "antennas": [r, l],
                          "body_yaw": float}, ...]}

RecordedMove derives dt as (time[-1] - time[0]) / len(time), so sampling must
be uniform — stepping whole frames satisfies that.

Note the format asymmetry against the live path: the move file stores head as
a nested 4x4, while the /ws/sdk wire format wants a flat 16.
"""

import json
import os

import bpy

from . import rig


def bake(scene, mapping=None, description="", frame_start=None, frame_end=None):
    """Sample every frame in the range and return a move dict.

    Restores scene.frame_current before returning, so baking is invisible to
    the rest of the session.
    """
    start = scene.frame_start if frame_start is None else frame_start
    end = scene.frame_end if frame_end is None else frame_end

    # RecordedMove derives dt as (time[-1] - time[0]) / len(time): fewer than
    # 2 frames gives an empty file (RecordedMove.__init__ raises IndexError
    # on timestamps[-1]) or a single frame with dt == duration == 0.0
    # (RecordedMove.evaluate raises for every t). Both would otherwise write
    # a file that "succeeds" but is unusable.
    if end - start + 1 < 2:
        raise ValueError(
            f"bake range must cover at least 2 frames, got start={start} "
            f"end={end}")

    # Blender stores a rational frame rate; fps_base is 1.001 for 23.976 etc.
    fps = scene.render.fps / scene.render.fps_base

    original_frame = scene.frame_current
    times = []
    samples = []
    try:
        for frame in range(start, end + 1):
            scene.frame_set(frame)
            # frame_set triggers evaluation, but fetch the depsgraph after it
            # so the read is unambiguously against the new frame.
            state = rig.read(bpy.context.evaluated_depsgraph_get(), mapping)
            times.append((frame - start) / fps)
            samples.append({
                "head": state.head_nested(),
                "antennas": [float(state.antennas[0]), float(state.antennas[1])],
                "body_yaw": float(state.body_yaw),
            })
    finally:
        scene.frame_set(original_frame)

    return {"description": description, "time": times, "set_target_data": samples}


def write_move(path, move):
    """Write a move dict as JSON, creating parent directories as needed.

    Resolves Blender's // relative-to-blend prefix, so the UI's default
    "//moves/untitled.json" lands next to the .blend -- but only once the
    .blend has been saved. Before the first save, bpy.data.filepath is ''
    and bpy.path.abspath cannot resolve //, so a // path raises ValueError
    instead of silently writing next to the process's working directory.
    Returns the resolved absolute path, so callers can report where the
    file actually landed.
    """
    if isinstance(path, str) and path.startswith("//"):
        if not bpy.data.filepath:
            raise ValueError(
                f"output path '{path}' is relative to the .blend, but this "
                ".blend has not been saved yet. Save the .blend first, or "
                "set an absolute output path.")
        path = bpy.path.abspath(path)
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(move, fh, indent=1)
    return path
