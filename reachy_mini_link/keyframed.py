"""The animator's source of truth: exact fcurve keys as a JSON sidecar.

The dense RecordedMove JSON is the compiled artifact - what the daemon
plays - and re-importing it *reconstructs* keys (very well, but
approximately: the salient selection re-chooses key times and refits
tangents). This module writes and reads `<move>.keys.json`, a verbatim
dump of the move's fcurves, so a move edited in Blender, published, and
re-imported comes back with exactly the keys and handles the animator
left: a lossless roundtrip instead of a reconstruction.

Format (version 1):

    {
      "format": "reachy_mini_keyframed_move",
      "version": 1,
      "fps": 24.0,            # authoring scene fps (informative)
      "channels": [
        {
          "data_path": "pose.bones[\"Head.001\"].location",
          "array_index": 0,
          "keys": [
            # time_s, value, left handle (time_s, value),
            # right handle (time_s, value), interpolation,
            # left handle type, right handle type
            [0.0, 0.1, -0.05, 0.1, 0.05, 0.1,
             "BEZIER", "FREE", "FREE"],
            ...
          ]
        },
        ...
      ]
    }

Key times are seconds from the scene start frame, not frame numbers, so
the sidecar survives a change of scene fps (keys then land on
fractional frames, which Blender is fine with). Only the channels the
move semantics own (head pose, body yaw and antenna write targets, as
resolved by import_move) are dumped: other fcurves on the armature are
the animator's own business and never part of a move.
"""

import json
import math
import pathlib

import bpy

from . import import_move, rig

FORMAT = "reachy_mini_keyframed_move"
VERSION = 1

SUFFIX = ".keys.json"


class KeysFormatError(ValueError):
    """The JSON file is not a keyframed move sidecar."""


def sidecar_path(move_json_path):
    """`<dir>/<stem>.keys.json` for a move at `<dir>/<stem>.json`."""
    base = pathlib.Path(move_json_path)
    return str(base.with_name(base.stem + SUFFIX))


def find_sidecar(move_json_path):
    """The move's keys sidecar if one exists next to it, else None."""
    cand = pathlib.Path(sidecar_path(move_json_path))
    return str(cand) if cand.exists() else None


def _move_channels(arm_obj, mapping):
    """[(data_path, array_index)] the move semantics own on this rig."""
    m = mapping or rig.Mapping()
    head = f'pose.bones["{m.head_bone}"]'
    channels = [(f"{head}.location", i) for i in range(3)]
    channels += [(f"{head}.rotation_euler", i) for i in range(3)]
    channels.append(import_move._driven_write_target(
        arm_obj, m.body_yaw_bone, m.body_yaw_axis)[:2])
    channels.append(import_move._driven_write_target(
        arm_obj, m.antenna_r_bones[0], m.antenna_axis)[:2])
    channels.append(import_move._driven_write_target(
        arm_obj, m.antenna_l_bones[0], m.antenna_axis)[:2])
    return channels


def _channelbag(arm_obj):
    """The armature's current action channelbag, or None (no keys yet)."""
    ad = arm_obj.animation_data
    if ad is None or ad.action is None or ad.action_slot is None:
        return None
    layers = ad.action.layers
    if not len(layers) or not len(layers[0].strips):
        return None
    return layers[0].strips[0].channelbag(ad.action_slot)


def collect(context, mapping=None):
    """The current move keys as a format dict, or None if nothing keyed."""
    m = mapping or rig.Mapping()
    arm_obj = bpy.data.objects.get(m.armature)
    if arm_obj is None:
        raise rig.RigError(f"armature object {m.armature!r} not found")
    cb = _channelbag(arm_obj)
    if cb is None:
        return None

    scene = context.scene
    fps = scene.render.fps / scene.render.fps_base
    frame0 = scene.frame_start

    channels = []
    for data_path, array_index in _move_channels(arm_obj, m):
        fc = next((f for f in cb.fcurves
                   if f.data_path == data_path
                   and f.array_index == array_index), None)
        if fc is None or not len(fc.keyframe_points):
            continue
        keys = []
        for kp in fc.keyframe_points:
            keys.append([
                (kp.co[0] - frame0) / fps, kp.co[1],
                (kp.handle_left[0] - frame0) / fps, kp.handle_left[1],
                (kp.handle_right[0] - frame0) / fps, kp.handle_right[1],
                kp.interpolation, kp.handle_left_type, kp.handle_right_type,
            ])
        channels.append({"data_path": data_path,
                         "array_index": array_index, "keys": keys})
    if not channels:
        return None
    return {"format": FORMAT, "version": VERSION, "fps": fps,
            "channels": channels}


def dumps(data):
    """Compact-but-diffable serialization (one key per line)."""
    return json.dumps(data, indent=None, separators=(",", ":"))


def load(path):
    """Read and structurally validate a keys sidecar."""
    with open(path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except ValueError as exc:
            raise KeysFormatError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise KeysFormatError("not a keyframed move sidecar")
    if data.get("version", 0) > VERSION:
        raise KeysFormatError(
            f"sidecar version {data['version']} is newer than this add-on")
    channels = data.get("channels")
    if not isinstance(channels, list) or not channels:
        raise KeysFormatError("no channels")
    for ch in channels:
        if not isinstance(ch.get("keys"), list) or not ch["keys"]:
            raise KeysFormatError("channel without keys")
        for key in ch["keys"]:
            if len(key) != 9:
                raise KeysFormatError("malformed key entry")
    return data


def apply(context, filepath, mapping=None, load_audio=True):
    """Restore the sidecar's fcurves verbatim. Returns a stats dict.

    The exact inverse of collect(): keys, handles and interpolation come
    back untouched (times converted through the current scene fps). The
    same hygiene as a dense import applies: stale fcurves on alternate
    write targets are purged, the antennas' free tail bones zeroed, and
    the move's audio sidecar (found via its dense JSON's stem) added as
    a sequencer strip.
    """
    m = mapping or rig.Mapping()
    arm_obj = bpy.data.objects.get(m.armature)
    if arm_obj is None:
        raise rig.RigError(f"armature object {m.armature!r} not found")
    data = load(filepath)

    scene = context.scene
    fps = scene.render.fps / scene.render.fps_base
    frame0 = scene.frame_start

    own = set(_move_channels(arm_obj, m))
    for ch in data["channels"]:
        if (ch["data_path"], ch["array_index"]) not in own:
            raise rig.RigError(
                f"sidecar channel {ch['data_path']}[{ch['array_index']}] "
                "does not match this rig (recorded on a different rig "
                "version? import the dense .json instead)")

    stem = pathlib.Path(filepath).name[:-len(SUFFIX)]
    cb = import_move._ensure_channelbag(arm_obj, stem)

    stale = [(f'pose.bones["{m.body_yaw_bone}"].rotation_euler',
              m.body_yaw_axis)]
    for chain in (m.antenna_r_bones, m.antenna_l_bones):
        stale += [(f'pose.bones["{name}"].rotation_euler',
                   m.antenna_axis) for name in chain]
    for path, index in stale:
        for fc in [f for f in cb.fcurves
                   if f.data_path == path and f.array_index == index]:
            cb.fcurves.remove(fc)

    stats = {"keys": 0, "channels": 0, "duration": 0.0}
    last_t = 0.0
    for ch in data["channels"]:
        for fc in [f for f in cb.fcurves
                   if f.data_path == ch["data_path"]
                   and f.array_index == ch["array_index"]]:
            cb.fcurves.remove(fc)
        fc = cb.fcurves.new(ch["data_path"], index=ch["array_index"])
        fc.keyframe_points.add(len(ch["keys"]))
        for kp, key in zip(fc.keyframe_points, ch["keys"]):
            (t, v, hlt, hlv, hrt, hrv, ipo, hl_type, hr_type) = key
            kp.co = (frame0 + t * fps, v)
            kp.interpolation = ipo
            kp.handle_left_type = hl_type
            kp.handle_right_type = hr_type
            kp.handle_left = (frame0 + hlt * fps, hlv)
            kp.handle_right = (frame0 + hrt * fps, hrv)
            last_t = max(last_t, t)
        fc.update()
        stats["keys"] += len(ch["keys"])
        stats["channels"] += 1

    import_move._zero_free_antenna_bones(arm_obj, m)
    stats["duration"] = last_t
    scene.frame_end = max(frame0 + 1, frame0 + int(math.ceil(last_t * fps)))

    stats["audio"] = None
    if load_audio:
        dense = pathlib.Path(filepath).with_name(stem + ".json")
        sidecar = import_move.find_audio_sidecar(str(dense))
        if sidecar:
            import_move._add_sound_strip(scene, sidecar, frame0)
            stats["audio"] = sidecar
    return stats
