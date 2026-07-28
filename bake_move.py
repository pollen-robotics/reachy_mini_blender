#!/usr/bin/env python3
"""Headless CLI: bake the rig's timeline to a Reachy Mini move JSON.

Blender's --python takes a file, not a module, so this file is the entry
point, in the same idiom as export_gltf.py.

Usage:
  blender --background reachy_mini.blend --python bake_move.py -- \
      --out moves/wave_hello.json --description wave_hello

Flags (after --):
  --out PATH          output path (default: <blend dir>/move.json)
  --description TEXT  move description (default: output filename stem)
  --start N           first frame (default: scene frame_start)
  --end N             last frame  (default: scene frame_end)
"""
import os
import sys

import bpy

# The add-on package lives next to this script; make it importable when run
# via --python, which does not put the script's directory on sys.path.
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from reachy_mini_link import bake  # noqa: E402  (must follow the sys.path edit)

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def flag(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


out = flag("--out", os.path.join(os.path.dirname(bpy.data.filepath), "move.json"))
description = flag("--description", os.path.splitext(os.path.basename(out))[0])
start = flag("--start")
end = flag("--end")

scene = bpy.context.scene
move = bake.bake(
    scene,
    description=description,
    frame_start=int(start) if start else None,
    frame_end=int(end) if end else None,
)
bake.write_move(out, move)
print(f"DONE -> {out}  ({len(move['time'])} frames, "
      f"{move['time'][-1]:.3f}s @ {scene.render.fps / scene.render.fps_base:g} fps)")
