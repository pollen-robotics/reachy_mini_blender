# Reachy Mini — Blender model

Rigged 3D model of [Reachy Mini](https://www.pollen-robotics.com/) for visualization,
and a script to export it to glTF.

3D model and rig by **Clément Plays** — https://clementplays.artstation.com/

## Contents

- `reachy_mini.blend` — the rigged model (Stewart-platform neck IK, antennas, body).
- `export_gltf.py` — headless Blender script that exports the `MiniReachyRetopo`
  collection + armature to a `.glb`, baking the procedural materials to flat PBR.
- `reachy_mini_link/` — the "Reachy Mini Live Link" add-on: mirror the rig to a running
  Reachy Mini daemon and bake the timeline to a move file.
- `bake_move.py` — headless equivalent of the add-on's Export Move button.

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

## Reachy Mini Live Link (add-on)

An add-on that streams the rig live to a running Reachy Mini daemon, and bakes the
timeline to a move file the robot's SDK can replay.

### Installing

The add-on is the `reachy_mini_link/` folder — no dependencies, no `pip install`. It
speaks the daemon's `/ws/sdk` WebSocket directly with only the Python standard library
(see `reachy_mini_link/client.py`), which matters because Blender's isolated interpreter
cannot have the SDK's compiled wheels installed into it.

1. Zip the `reachy_mini_link/` folder (the zip's top level must contain
   `reachy_mini_link/`, not its contents directly).
2. In Blender: `Edit > Preferences > Add-ons > Install...`, pick the zip, enable
   "Reachy Mini Live Link".

Tested only on **Blender 5.1**. Earlier 4.x releases may work but are unverified.

### Running a live mirror

1. Start a daemon. For the simulator: `reachy-mini-daemon --sim`.
2. In the 3D viewport, open the sidebar (`N`) and select the "Reachy Mini" tab.
3. Set Host/Port (defaults to `localhost:8000`) and click **Start Sync**.

While syncing, drive the robot by posing the animator's control surface — everything
else in the rig is mechanism that follows:

| Bone | Drives |
|---|---|
| `Head.001` | 6-DoF head pose |
| `Slider.Rot.Core` | body yaw |
| `Slider.Rot.Antenna.L` / `Slider.Rot.Antenna.R` | antenna sweep (slider) |
| `Antenna.L.003` / `Antenna.R.003` | antenna sweep (direct FK, adds to the slider) |

See `docs/RIG_MAPPING.md` for the full mapping, units and provenance of every constant.

**Verify in the simulator before touching real hardware.** Use **Send test pose** to run
a known sequence (translations, a head roll, antenna sweeps, body yaw) and confirm the
robot moves the way you expect — direction, sign and scale — before pointing Start Sync
at a physical robot.

Note that `Head.001` translation has only a small usable range — about ±0.05 BU in
Blender, ~23 mm on the robot vertically — before the robot silently saturates: it stops
following while Blender keeps moving. Large head translations in Blender will not be
reproduced on the robot; see `docs/RIG_MAPPING.md` for the confirmed numbers.

### Exporting a move

Set Description and Output path in the "Export Move" section of the panel (or leave
"Use scene frame range" checked to bake the scene's frame range), then click **Export
Move**.

Headlessly, from the shell:

```bash
blender --background reachy_mini.blend --python bake_move.py -- \
    --out moves/wave.json --description wave
```

Flags (after `--`):

| flag | effect |
|------|--------|
| `--out PATH` | output path (default: `move.json` next to the blend) |
| `--description TEXT` | move description (default: the output filename stem) |
| `--start N` | first frame (default: scene `frame_start`) |
| `--end N` | last frame (default: scene `frame_end`) |

### Running the tests

```bash
# Outside Blender — the WebSocket client and move-roundtrip validation:
python3 -m unittest tests.test_client
python3 -m unittest tests.test_move_roundtrip   # skips cleanly without the SDK

# Inside Blender — rig reading and baking:
blender --background reachy_mini.blend --python tests/run_blender_tests.py
```

## Git LFS

3D assets (`.blend`, `.glb`, `.gltf`, `.fbx`, `.obj`, `.stl`, `.ply`, `.dae`,
`.abc`, `.usd*`) are tracked with [Git LFS](https://git-lfs.com/). Install it
before cloning so the files come down as real assets, not pointers:

```bash
git lfs install
git clone <repo-url>
```
