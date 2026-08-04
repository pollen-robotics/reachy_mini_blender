# Reachy Mini — Blender model

Rigged 3D model of [Reachy Mini](https://www.pollen-robotics.com/) for visualization,
and a script to export it to glTF.

3D model and rig by **Clément Plays** — https://clementplays.artstation.com/

## Contents

- `reachy_mini_link/assets/reachy_mini.blend` — the rigged model (Stewart-platform neck
  IK, antennas, body). It lives inside the add-on package so the installed zip is
  self-contained: the panel's **Load Reachy Mini Rig** button appends it into any file.
- `export_gltf.py` — headless Blender script that exports the `MiniReachyRetopo`
  collection + armature to a `.glb`, baking the procedural materials to flat PBR.
- `reachy_mini_link/` — the "Reachy Mini Live Link" add-on: mirror the rig to a running
  Reachy Mini daemon and bake the timeline to a move file.
- `bake_move.py` — headless equivalent of the add-on's Export Move button.
- `play_move.py` — replay a baked move on the robot, without needing the SDK installed.
- `tools/blend_guard.py` — checks whether a run modified `reachy_mini.blend`, without
  ever reverting it. See `docs/WORKING_ON_THIS_REPO.md`.

## Installing

### Requirements

| | |
|---|---|
| Blender | **5.1** is the only tested version. Earlier 4.x releases probably work — nothing here uses a 5.x-only API — but are unverified. |
| Git LFS | **required before cloning**, see below |
| Python packages | none for the add-on or the CLI scripts — standard library only |
| `reachy_mini` SDK | only if you want a local simulator, or the optional SDK playback route |

### 1. Clone — install Git LFS first

3D assets (`.blend`, `.glb`, `.gltf`, `.fbx`, `.obj`, `.stl`, `.ply`, `.dae`, `.abc`,
`.usd*`) are tracked with [Git LFS](https://git-lfs.com/). Install it **before** cloning,
or `reachy_mini.blend` arrives as a ~130-byte pointer file that Blender cannot open:

```bash
sudo apt install git-lfs        # or: brew install git-lfs
git lfs install                 # once per user

git clone git@github.com:pollen-robotics/reachy_mini_blender.git
cd reachy_mini_blender
git checkout feat/reachy-mini-live-link
```

The `git checkout` is **required until that branch is merged**: `main` currently holds only
the rig and `export_gltf.py`, with no add-on.

Check the blend is a real file, not a pointer — it should be about 17 MB:

```bash
ls -lh reachy_mini_link/assets/reachy_mini.blend
```

If you cloned before installing LFS, fix it in place rather than re-cloning:

```bash
git lfs install && git lfs pull
```

### 2. Install the add-on

The add-on is the `reachy_mini_link/` folder. Build a zip whose top level contains
`reachy_mini_link/` (not its contents directly):

```bash
zip -r reachy_mini_link.zip reachy_mini_link -x "*__pycache__*" "*.pyc"
```

Then in Blender: `Edit > Preferences > Add-ons > Install from Disk`, pick the zip, tick
**"Reachy Mini Live Link"**, and Save Preferences. A "Reachy Mini" tab appears in the 3D
viewport sidebar (`N`).

The zip carries the rigged model with it, so it is the only thing an animator needs:
in a file with no Reachy rig, the panel shows a **Load Reachy Mini Rig** button that
appends the bundled scene (rig + model) and switches to it. Existing scenes are left
untouched, and saving stays in the user's own file.

There is nothing to `pip install`. The add-on speaks the daemon's `/ws/sdk` WebSocket
directly using only the Python standard library (see `reachy_mini_link/client.py`) —
which is deliberate, because Blender's isolated interpreter cannot have the SDK's
compiled wheels installed into it.

**If you are working on the add-on**, symlink instead of installing, so the installed
copy can never drift from your checkout:

```bash
ln -s "$PWD/reachy_mini_link" ~/.config/blender/5.1/scripts/addons/reachy_mini_link
```

Two caveats with the symlink: `ln -s` will not replace an existing directory, so remove
any previously-installed copy first; and Python caches imported modules, so after editing
code you still need to disable/re-enable the add-on (or restart Blender) for changes to
take effect. Adjust `5.1` to your Blender version.

### 3. Something to talk to

The add-on drives no hardware itself — it only talks to a Reachy Mini daemon. Either run
one locally with the SDK:

```bash
pip install reachy-mini
reachy-mini-daemon --sim          # MuJoCo simulator
```

…or leave the SDK out entirely and point the panel's Host field at a daemon running
elsewhere — a real robot, or another machine.

### 4. Check it works

```bash
python3 -m unittest tests.test_client                                        # 20, no Blender
blender --background reachy_mini_link/assets/reachy_mini.blend --python tests/run_blender_tests.py   # 41, in Blender
```

Worth running on a new machine: it is the quickest way to confirm the LFS pull, the rig
and the driver constants are all intact before trusting anything to hardware. The Blender
run prints which copy of the package it tested — check that line says your checkout, not
an installed copy.

## Exporting

```bash
blender --background reachy_mini_link/assets/reachy_mini.blend --python export_gltf.py
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
blender --background reachy_mini_link/assets/reachy_mini.blend --python export_gltf.py -- \
  --zup --no-uv --no-color --out /path/to/reachy_mini_viz.glb
```

## Reachy Mini Live Link (add-on)

An add-on that streams the rig live to a running Reachy Mini daemon, and bakes the
timeline to a move file the robot's SDK can replay.

See [Installing](#installing) above for setup.

### Running a live mirror

1. Start a daemon. For the simulator: `reachy-mini-daemon --sim`.
2. In the 3D viewport, open the sidebar (`N`) and select the "Reachy Mini" tab. No rig
   in the file yet? Click **Load Reachy Mini Rig** first.
3. Set Host/Port in the "Robot" section and click **Start Live Sync**. The
   magnifier button probes `127.0.0.1` (Lite, local daemon) then
   `reachy-mini.local` (wireless, mDNS) and fills Host for you; if neither
   answers, type the IP shown in the mobile app.

While syncing, drive the robot by posing the animator's control surface — everything
else in the rig is mechanism that follows:

| Bone | Drives |
|---|---|
| `Head.001` | 6-DoF head pose |
| `Slider.Rot.Core` | body yaw |
| `Slider.Rot.Antenna.L` / `Slider.Rot.Antenna.R` | antenna sweep (slider) |
| `Antenna.L.003` / `Antenna.R.003` | antenna sweep (direct FK, adds to the slider) |

See `docs/RIG_MAPPING.md` for the full mapping, units and provenance of every constant.

**Verify in the simulator before touching real hardware.** Use **Test Pose** to run
a known sequence (translations, a head roll, antenna sweeps, body yaw) and confirm the
robot moves the way you expect — direction, sign and scale — before pointing Start Live
Sync at a physical robot.

**Reset Rig** (next to Test Pose) returns every control to its rest pose — the same as
selecting all bones and clearing their transforms, and undoable with `Ctrl+Z`. If Live
Sync is running, the robot follows back to neutral.

Note that `Head.001` translation has only a small usable range — about ±0.05 BU in
Blender, ~23 mm on the robot vertically — before the robot silently saturates: it stops
following while Blender keeps moving. Large head translations in Blender will not be
reproduced on the robot; see `docs/RIG_MAPPING.md` for the confirmed numbers.

**Only one client should stream to the daemon at a time.** If a second client streams
simultaneously — a forgotten Blender session with Start Sync still on, or a desktop app —
the daemon applies whichever target arrived last, so the two fight and the robot appears
to track sluggishly or drift back toward a neutral pose. That looks exactly like a
saturation or calibration problem but isn't one. Press **Stop Live Sync** when you're done, and
if the robot ever behaves like this, check for a stale connection with
`ss -tnp | grep :8000`. Separately, the daemon ignores a `goto_target` that arrives while
another move is still playing, which is why the add-on spaces its test-pose steps by the
move duration plus a margin — worth knowing if you're scripting the daemon directly.

### Playing the timeline on the robot

**Play on Robot** (in the "Timeline" section) bakes the timeline and uploads it to the
daemon, which plays it back on its own 100 Hz clock — frame-accurate, no network jitter,
and playback survives Blender closing. The upload runs in the background: the panel
shows the status (Sending, then Playing with the duration) and a **Stop** button that
cancels the move mid-play. Works with the Host/Port set in "Robot": `127.0.0.1` for a
Lite plugged into this machine, the robot's address for a wireless one on your network.
The same thing from the shell is `play_move.py`.

### Sound

Drop a sound strip into Blender's Video Sequencer and animate against it. When
one exists (and isn't muted), the panel says so, and every path carries the
audio along automatically:

- **Play on Robot** uploads it with the move; the daemon starts its own
  playback (GStreamer, on the robot's speaker) in lockstep with the motion.
- **Publish to Hub** pushes it as a `data/<slug>.wav` sidecar next to the
  move JSON — the same convention Marionette and the daemon's move folders
  use (Marionette's loader only looks for `.wav`).
- **Export Move** writes a `.wav` next to the output JSON, which
  `play_move.py` picks up automatically.

The mixdown is rendered by Blender itself over the exported frame range, so
whatever you hear when scrubbing is what the robot plays. Mute the strip to
export motion-only.

### Importing a recorded move

**Import Move** (bottom of the "Timeline" section) does the reverse trip: a
`RecordedMove` JSON — recorded with
[Marionette](https://huggingface.co/spaces/RemiFabre/marionette), downloaded
from a community dataset on the Hub, or exported by this add-on — becomes
editable keyframes on the rig. Raw captures are 30–100 Hz and noisy, so they
are cleaned on the way in: a light low-pass takes out capture jitter, then
one set of key times is chosen for the whole pose (Salient-Poses-style
greedy selection against free-tangent Bézier reconstruction), and each
channel keeps only the key columns its own motion needs. Keys line up in
columns across channels at the points where the motion actually turns —
where an animator would have put them — and the fitted free tangents let a
single Bézier segment cover a whole arc, so a 12 s / 50 Hz capture lands
around ~30 key columns instead of 600 samples. The result is a normal
hand-editable animation: polish it in the Graph Editor, then play, export,
or publish it like anything else. The move's **Name** in the Share section
is set to the imported file's, so a later export or publish keeps the same
identity without retyping it.

Keys land where an animator would put them: head pose on `Head.001`, body
yaw and antennas on their sliders. An audio sidecar
(`.wav` or `.ogg` next to the JSON) is added as a sequencer strip so the
sound survives the trip too.

The file-browser sidebar has the knobs:

| Option | Meaning |
|---|---|
| **Smoothing** | low-pass width in seconds. `0` keeps the raw signal |
| **Simplify** | how far the cleaned curves may drift from the recording. `1.0` ≈ 0.5 mm / 0.3°, invisible on the robot; `0` keys every sample |
| **Snap Keys to Frames** | round keys to whole frames (costs at most half a frame of timing) |
| **Load Audio** | add the sidecar as a sequencer strip |
| **Set Scene Range** | fit the scene frame range to the move |
| **Prefer Exact Keys** | when a `.keys.json` sidecar exists, restore its exact keys instead of reconstructing (see below) |

### Lossless re-editing: the keys sidecar

The dense JSON is the *compiled* move; reconstruction from it is very
good but approximate. So **Export Move** and **Publish to Hub** also
write the *source*: a `<move>.keys.json` sidecar with the exact fcurve
keys — positions, tangent handles, interpolation modes. When importing
a move that has one (next to the file, or `sources/<move>.keys.json` in
a Hub dataset), the add-on restores those exact keys instead: a move
authored in Blender, published, and re-imported months later comes back
precisely as its animator left it. Moves recorded outside Blender
(Marionette, the official libraries) have no sidecar and go through the
usual reconstruction. Note the sidecar snapshots the whole authored
curves, not just the exported frame range.

### Importing straight from the Hub

**From Hub** (next to Import Move) lists every public community move in one
place — Marionette recordings, the official
[emotions](https://huggingface.co/datasets/pollen-robotics/reachy-mini-emotions-library)
and dances libraries, and moves published by this add-on (anything in a
dataset tagged `reachy_mini_community_moves`). The list shows one
collapsible folder per dataset (with its move count); click a folder to
unfold its moves — the note icon marks moves that ship audio, and the
search box filters rows by name. Select a move and **Import Selected**
downloads it and runs the same import/cleanup pipeline as a local file. No
sign-in needed; a token is used only if one is already around (it also
lets your own private datasets show up).

Downloads are cached per user (`~/Library/Caches/reachy_mini_link` on
macOS, `~/.cache/reachy_mini_link` on Linux, `%LOCALAPPDATA%` on Windows)
so the sound strip still resolves after a restart. Re-importing a move
replaces the audio strip a previous import created; strips you added by
hand are never touched.

### Publishing to the Hub

**Publish to Hub** (next to Export Move) bakes the timeline and pushes it to a
Hugging Face dataset under your namespace — `<you>/reachy-mini-moves` by
default, created with a datacard on first use. The layout and tag are
Marionette's community-dataset conventions, so published moves show up in its
community browser and import cleanly: the move lands at
`data/<slug-of-description>.json` (canonicalized to ≤50 Hz / 6 decimals),
audio beside it as `data/<slug>.wav`, the editable keyframe source at
`sources/<slug>.keys.json` (outside `data/`, which players treat as a
flat move list), and the datacard carries the
`reachy_mini_community_moves` tag. Publishing the same description again
overwrites all of them. Sign-in comes from the hf CLI token, the `HF_TOKEN`
env var, or a token pasted in the add-on preferences (the panel shows which
account it found).

### Exporting a move

Use the panel — the frame range lives in the "Timeline" section, the
move's **Name**, the output **Folder** and the buttons in "Share":

| Field | Meaning |
|---|---|
| **Name** | the move's one identity everywhere: stored in the file's `description` (what the robot's move libraries index), and its slug is the filename both locally and on the Hub |
| **Folder** | where the files land — a Blender path, so `//` means *relative to the .blend*. Created if missing. A preview line shows exactly what one click will write (`<slug>.json`, plus `.wav` and `.keys.json` sidecars) |
| **Use scene frame range** | on: use the scene's `frame_start`/`frame_end`. Off: exposes explicit **Start** / **End** fields for exporting a slice |

Then click **Export Move**. The status bar reports the resolved absolute path and the
frame count. Your current frame is restored afterwards, so exporting is invisible to the
rest of your session — and no robot or daemon is involved, since it reads the rig.

Two things it will refuse rather than write something broken:

- A range of fewer than 2 frames. A single-frame or reversed range produces a file the
  robot's loader cannot play, so it errors instead.
- A `//` output folder while the .blend is **unsaved**. `//` has nothing to resolve against
  before the first save, and the path would silently fall through to the process working
  directory. Save the .blend, or give an absolute path. The panel warns about this before
  you click.

Headlessly, from the shell:

```bash
blender --background reachy_mini_link/assets/reachy_mini.blend --python bake_move.py -- \
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
| `--audio PATH` | audio file to play with the move (default: the `.wav` or `.ogg` sidecar next to the JSON, when present) |
| `--no-audio` | skip the sidecar even if one exists |

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
blender --background reachy_mini_link/assets/reachy_mini.blend --python tests/run_blender_tests.py
```

## Working on this repo

If you are changing the code, read `docs/WORKING_ON_THIS_REPO.md` first. It covers three
traps this project has already hit: never revert `reachy_mini.blend` to satisfy a
clean-tree check (it destroys saved animation work), an installed add-on copy silently
shadowing your checkout in tests, and `Action.fcurves` no longer existing in Blender 5.1.

Keep your animation work in a file **outside** the repo (`File > Save As`) so
`reachy_mini.blend` stays a pristine rig asset and a 17 MB binary stays out of your diffs.

Git LFS setup is covered under [Installing](#installing).
