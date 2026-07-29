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
- `play_move.py` — replay a baked move on the robot, without needing the SDK installed.
- `tools/blend_guard.py` — checks whether a run modified `reachy_mini.blend`, without
  ever reverting it. See `docs/WORKING_ON_THIS_REPO.md`.

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

**Only one client should stream to the daemon at a time.** If a second client streams
simultaneously — a forgotten Blender session with Start Sync still on, or a desktop app —
the daemon applies whichever target arrived last, so the two fight and the robot appears
to track sluggishly or drift back toward a neutral pose. That looks exactly like a
saturation or calibration problem but isn't one. Press **Stop Sync** when you're done, and
if the robot ever behaves like this, check for a stale connection with
`ss -tnp | grep :8000`. Separately, the daemon ignores a `goto_target` that arrives while
another move is still playing, which is why the add-on spaces its test-pose steps by the
move duration plus a margin — worth knowing if you're scripting the daemon directly.

### Exporting a move

Use the panel — the "Export Move" section of the "Reachy Mini" sidebar tab:

| Field | Meaning |
|---|---|
| **Description** | stored in the file's `description`; this is what identifies the move to the robot's move libraries, not the filename |
| **Output** | a Blender path, so `//` means *relative to the .blend*. Missing directories are created |
| **Use scene frame range** | on: use the scene's `frame_start`/`frame_end`. Off: exposes explicit **Start** / **End** fields for exporting a slice |

Then click **Export Move**. The status bar reports the resolved absolute path and the
frame count. Your current frame is restored afterwards, so exporting is invisible to the
rest of your session — and no robot or daemon is involved, since it reads the rig.

Two things it will refuse rather than write something broken:

- A range of fewer than 2 frames. A single-frame or reversed range produces a file the
  robot's loader cannot play, so it errors instead.
- A `//` output path while the .blend is **unsaved**. `//` has nothing to resolve against
  before the first save, and the path would silently fall through to the process working
  directory. Save the .blend, or give an absolute path. The panel warns about this before
  you click.

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

### Replaying a move on the robot

`play_move.py` is the counterpart to `bake_move.py`. Blender is not involved:

```bash
python3 play_move.py moves/wave.json
python3 play_move.py moves/wave.json --host 192.168.1.42   # a real robot
```

```
playing moves/wave.json: 49 frames, 2.00s (+1.0s ease-in)
  started (daemon reports 2.00s)
  finished
```

| flag | effect |
|------|--------|
| `--host HOST` | daemon host (default `localhost`) |
| `--port PORT` | daemon port (default `8000`) |
| `--ease-in SECS` | interpolate to the move's first frame before playing (default `1.0`; `0` starts abruptly from wherever the head is) |
| `--freq HZ` | daemon playback tick rate (default `100`) |
| `--no-wait` | return as soon as playback is requested |

It uploads the move to the daemon and asks the daemon to play it, so the daemon owns the
playback loop — interpolation, the tick, the Stewart IK. Nothing streams frames at it,
which also means playback outlives the script (hence `--no-wait`). Like the add-on it
needs no SDK install: plain `python3`, standard library only, reusing
`reachy_mini_link/client.py`.

It waits on the daemon's own playback events rather than assuming the upload landed. That
matters because a rejected upload slot is dropped *silently* server-side, so a naive
upload-and-play would look successful while doing nothing.

**Stop Sync first** if Blender is live-mirroring — otherwise its 50 Hz stream fights the
playback (see the warning above).

#### Or with the SDK

If you want the camera, audio, IMU or face tracking in the same script, use the SDK
instead. It must run on the SDK's own interpreter, not system `python3`:

```python
import asyncio, json, sys
from reachy_mini import ReachyMini
from reachy_mini.motion.recorded_move import RecordedMove

move = RecordedMove(json.load(open(sys.argv[1])))
with ReachyMini(media_backend="no_media") as mini:
    mini.set_automatic_body_yaw(False)
    mini.enable_motors()
    asyncio.run(mini.async_play_move(move, initial_goto_duration=1.0))
```

Three non-obvious points: `async_play_move` is the only playback entry point, so
`asyncio.run` is required; `media_backend="no_media"` skips the WebRTC/GStreamer stack,
which otherwise floods the terminal with audio-device errors irrelevant to motion; and
`set_automatic_body_yaw(False)` is needed or the daemon overrides the body yaw your move
recorded. Both routes go through the same daemon playback path, so the motion is identical.

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
