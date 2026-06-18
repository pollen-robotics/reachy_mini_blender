# Reachy Mini — Blender model

Rigged 3D model of [Reachy Mini](https://www.pollen-robotics.com/) for visualization,
and a script to export it to glTF.

3D model and rig by **Clément Plays** — https://clementplays.artstation.com/

## Contents

- `reachy_mini.blend` — the rigged model (Stewart-platform neck IK, antennas, body).
- `export_gltf.py` — headless Blender script that exports the `MiniReachyRetopo`
  collection + armature to a `.glb`, baking the procedural materials to flat PBR.

## Exporting

```bash
blender --background reachy_mini.blend --python export_gltf.py
```

Default output is `reachy_mini_viz.glb` (Y-up, Draco) next to the blend. Flags
(after `--`):

| flag | effect |
|------|--------|
| `--zup` | keep Blender Z-up (the robot's native frame) instead of glTF Y-up |
| `--no-draco` | skip Draco compression |
| `--no-uv` | drop UVs (the flat materials don't use them) |
| `--no-color` | drop vertex colours |
| `--out PATH` | output path |

Example (lightweight, app-ready copy):

```bash
blender --background reachy_mini.blend --python export_gltf.py -- \
  --zup --no-uv --no-color --out /path/to/reachy_mini_viz.glb
```

## Git LFS

3D assets (`.blend`, `.glb`, `.gltf`, `.fbx`, `.obj`, `.stl`, `.ply`, `.dae`,
`.abc`, `.usd*`) are tracked with [Git LFS](https://git-lfs.com/). Install it
before cloning so the files come down as real assets, not pointers:

```bash
git lfs install
git clone <repo-url>
```
