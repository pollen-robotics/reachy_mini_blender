# Reachy Mini Live Link — Blender Add-on Design

**Date:** 2026-07-28
**Status:** Design (approved, pre-implementation)
**Branch:** `feat/reachy-mini-live-link`

## Goal

A Blender add-on for `reachy_mini_blender` that does two things with one rig-reading
path:

1. **Live mirror** — while sync is ON the robot continuously follows the rig in real
   time, whether the artist hand-poses a control, scrubs the timeline, or plays it.
2. **Bake a move** — export the timeline to the robot's native `RecordedMove` JSON so
   the animation can be replayed later on the robot, with no robot or daemon needed to
   produce the file.

Prior art (a 2026-06-17 spec and a `reachy_live_link.py` prototype, both in
`~/Pollen/reachy-mini`) was reviewed and **deliberately discarded**. Only facts
independently re-verified against SDK 1.8.3 and this rig are carried forward.

## Verified facts

### Transport / wire protocol (SDK 1.8.3)

- Endpoint: plain WebSocket `ws://<host>:<port>/ws/sdk`, default port `8000`, no auth.
  Source: `io/ws_server.py:3`, `io/ws_client.py:74`.
- Command, fire-and-forget, one JSON text frame — all fields optional:
  ```json
  {"type": "set_full_target",
   "head": [<16 floats, row-major>] | null,
   "antennas": [right, left] | null,
   "body_yaw": <float> | null}
  ```
  Source: `io/protocol.py:141-152` (`SetFullTargetCmd`).
- **Head flatten order is row-major**: client does `head.flatten().tolist()`
  (`reachy_mini.py:539`), server does `np.array(req.head).reshape(4, 4)`
  (`io/ws_server.py:182`).
- The daemon streams `joint_positions` / `head_pose` at ~50 Hz, so **silence is
  meaningful** — no server message for >1 s while syncing means the link is gone.
- `set_automatic_body_yaw` must be sent `false` on connect. `ReachyMini.__init__`
  defaults `automatic_body_yaw=True` (`reachy_mini.py:103`), which lets the daemon
  choose its own body yaw to help reach head poses — that fights our explicit channel.
  Source: `io/protocol.py:212` (`SetAutomaticBodyYawCmd`).

### Robot control convention

- **Head pose:** 4×4 homogeneous, right-handed, **+X forward, +Y left, +Z up, metres**.
  **Neutral = identity** (`INIT_HEAD_POSE = np.eye(4)`, `reachy_mini.py:47`). The
  kinematics adds a **+0.177 m Z offset internally** (`placo_kinematics.py:130,344`), so
  the client sends plain identity for neutral and must **not** bake the offset in.
- **Head pose is expressed in the fixed base frame, not the yaw-rotated body frame.**
  `placo_kinematics.py:345` assigns `self.head_frame.T_world_frame = _pose` while
  `body_yaw` is set as a *separate simultaneous* IK joint task (`:347`). This resolves
  the prior spec's open question: the rig reference must be a fixed bone (`Base`), not
  `Core`.
- **Body yaw:** about +Z, 0 = aligned, range **±2.7925 rad (±160.0°)**.
  Source: MJCF `yaw_body range="-2.792526803190975 2.792526803190879"`.
- **Antennas `[right, left]`, radians,** 0 = vertical, range ±π. Init
  `[-0.1745, 0.1745]`, sleep `[-3.05, 3.05]` (`reachy_mini.py:60-61`). The two antennas
  have **different mounting quats** in the MJCF, so per-side sign is confirmed in sim,
  never assumed.

### Move file format (`RecordedMove`)

```json
{"description": "wave_hello",
 "time": [0.0, 0.0417, ...],
 "set_target_data": [{"head": [[4x4 nested]], "antennas": [r, l], "body_yaw": 0.0}, ...]}
```

Source: `motion/recorded_move.py:78-86`. Note the **format asymmetry**: the wire
protocol wants `head` **flat-16**, the move file wants it **nested 4×4**
(`head.tolist()`, `reachy_mini.py:552`). `RecordedMove` computes
`dt = (time[-1] - time[0]) / len(time)`, so **uniform sampling is required** — frame
stepping satisfies this.

### Environment constraint

Blender 5.1 bundles **Python 3.13** with numpy 2.3.4 but **no scipy**. The SDK needs
≥3.10 (installed under 3.12) and pulls two compiled Rust wheels
(`reachy_mini_rust_kinematics`, `reachy_mini_motor_controller`). Importing `reachy_mini`
into Blender is therefore not viable — hence talking `/ws/sdk` directly.

## Architecture

```
reachy_mini_blender/
├── reachy_mini_link/
│   ├── __init__.py   bl_info, register/unregister, AddonPreferences
│   ├── client.py     WS client — NO bpy import → unit-testable headless
│   ├── rig.py        evaluated depsgraph → RigState
│   ├── sync.py       bpy.app.timers loop: rig.read() → client.send()
│   ├── bake.py       frame range → move JSON (no robot, no daemon)
│   └── ui.py         N-panel, operators, PropertyGroup
├── tests/
│   ├── test_client.py
│   ├── test_rig.py
│   └── test_bake.py
├── docs/RIG_MAPPING.md
├── README.md          (new "Live Link" + "Exporting a move" sections)
├── export_gltf.py
└── reachy_mini.blend
```

**Deliverables:** the six `reachy_mini_link/` modules, three test files,
`docs/RIG_MAPPING.md`, and README sections covering installing the add-on, running
against `--sim`, and baking a move.

**The core structural decision:** `rig.read(depsgraph) → RigState(head, body_yaw,
antennas)` is a pure function with **two consumers** — `sync` streams it, `bake` writes
it to a file. One mapping, two sinks. A baked move and the live stream cannot drift
apart, `rig.py` is testable without a socket, and `client.py` is testable without
Blender.

`bake.py` also runs headless, in the existing `export_gltf.py` idiom:

```bash
blender --background reachy_mini.blend --python -m reachy_mini_link.bake -- \
    --out moves/wave_hello.json --description wave_hello
```

### Transport: hand-rolled client (decided)

`client.py` implements RFC 6455 directly: handshake, client masking, text-frame send,
and a real receive parser. Receive is **not optional** — the daemon streams at ~50 Hz,
so the socket must be drained or the TCP buffer backs up, and pings must be answered.
Roughly 120 lines, zero dependencies, nothing vendored, no setup step for artists.

Rejected: vendoring `websockets` (~15k lines of third-party code plus update/licence
surface, for features we don't use); `pip install` into Blender's Python (a manual
per-install step that may need elevated permissions).

Public surface:

- `connect(host, port, timeout)` → raises `ConnectionError` on failure
- `send_full_target(head=None, antennas=None, body_yaw=None)` — fire-and-forget
- `send_goto_target(head, antennas, body_yaw, duration)` — the ease-in
- `is_connected()` → bool, from the receive thread's last-message time
- `last_error` → `str | None`
- `disconnect()`

### Threading

`bpy` is touched **only on the main thread**. The sync loop is a `bpy.app.timers`
callback (default 50 Hz): read evaluated depsgraph (main thread, safe) → non-blocking
send. The client's background receive thread tracks liveness and answers pings, and
**never touches `bpy`**.

## Rig mapping

**All reads go through `context.evaluated_depsgraph_get()`** — but not for the reason one
might assume, and the assumption was tested rather than trusted.

Measured on this rig (Blender 5.1): after a `view_layer.update()`, the **raw `bpy.data`
pose channels and matrices are identical to the evaluated ones**, to 0.000000, for both
driver-driven rotations (`Core`, `Antenna.*.002`) and constraint-clamped matrices
(`Head.001` pushed to `(0.9, -0.9, 0.9)` clamps to `(0.234, -0.234, 0.278)` in *both*
reads). Blender flushes evaluated pose results back to the original object's pose, so a
raw read is not stale here.

Evaluated reads are still the design choice, for reasons that survive that finding:

1. **Correct by construction**, independent of write-back behaviour, which is an
   implementation detail rather than a documented contract.
2. **The bake path needs a guaranteed-current evaluation** immediately after
   `scene.frame_set(f)`; going through the depsgraph makes that explicit rather than
   relying on flush ordering.
3. **It stays correct if the rig gains layers later** — NLA tracks, actions, or drivers
   targeting other objects.

The cost is nil, so the robust form is used. What this does mean is that the add-on must
never read pose without an up-to-date depsgraph; that requirement is real even though
raw-vs-evaluated happens to agree today.

| Robot DOF | Rig source | Extraction |
|---|---|---|
| `head` 4×4 | `Head.001` rel. `Base` | `R = R_cur @ R_rest⁻¹` ; `t = (p_cur − p_rest) × SCALE` |
| `body_yaw` | `Core` | evaluated `rotation_euler[1]` (driven by `Slider.Rot.Core`) |
| `antennas` `[r, l]` | `Antenna.{R,L}.002` + `.003` | sum of evaluated `rotation_euler[2]` |

Two things that look like inconsistencies but are not:

- **`body_yaw` reads local Y, yet the robot's yaw is about +Z.** The `Core` bone runs
  from `z=0.0676` to `z=0.1728`, i.e. its bone axis (local Y, Blender's along-bone axis)
  *is* world +Z. So a local-Y rotation on `Core` is a rotation about world up — exactly
  body yaw. The driver writes `rotation_euler[1]`, and `Core`'s `LIMIT_ROTATION` is on Y,
  both consistent with this.
- **Reading `rotation_euler` is legitimate because every bone involved is Euler mode.**
  The rig is 59/59 `XYZ`, so the channel is always meaningful. (On a quaternion-mode bone
  `rotation_euler` is ignored by Blender and returns a stale value — the failure mode to
  watch for if the rig ever gains one.) Driver results are present in this channel; see
  the depsgraph note above for why the read is still routed through the evaluated copy.

**Reference bone is `Base`, not `GLOBAL`.** `Base` is the robot's fixed base: parented to
`GLOBAL`, no constraints, not driven. Using it means an artist can move or animate
`GLOBAL` to place the robot in a scene without perturbing the streamed head pose.

### Head pose construction

**Correction (found after the per-task reviews below, via a simulator screencast):**
the text originally here computed `R` and `t` in `Base`'s bone-local axes and sent them
as if that *were* the robot frame. It is not. A Blender bone's local Y runs *along the
bone*, and `Base` runs along world +Z (not world +X), so `Base` bone-local axes relate to
world axes as `localX -> world +X`, `localY -> world +Z`, `localZ -> world -Y` — a
different basis from the robot's REP-103 convention (+X forward, +Y left, +Z up). Sending
the bone-local delta unrotated meant a physically-forward head move (rig +Y) was reported
to the robot as **down**, and a physically-up move was reported as **left** — confirmed
by driving the rig from known axes in the `--sim` MuJoCo viewer and watching the robot
move the wrong way.

The fix is a change-of-basis matrix `BASE_TO_ROBOT` (`rig.py`), applied to *both* halves
of the independently-assembled pose:

```
C = BASE_TO_ROBOT                        # Base bone-local -> robot frame; det=+1, orthonormal
R = C @ (R_cur @ R_rest.inverted()) @ C.transposed()   # rotation delta, conjugated into robot axes
t = C @ ((p_cur - p_rest) * SCALE)       # origin delta, rotated into robot axes, metres
pose = Matrix.Translation(t) @ R.to_4x4()
```

Rotation and translation are still assembled **independently** — that part of the
original design was correct and unaffected by this fix. Explicitly **not** `M_cur @
M_rest.inverted()`, whose translation would be `p_cur − R·p_rest`. The robot defines the
pose as a rotation about the neutral head origin plus a translation offset from it (the
kinematics adds +0.177 m Z internally), which is what the independent assembly produces.
Both forms give identity at rest, so this only diverges once the head is simultaneously
rotated and translated — a case the tests must cover. See `docs/RIG_MAPPING.md` for the
full derivation and evidence for `BASE_TO_ROBOT` (face visible only from rig +Y;
`Antenna.L` at rig −X = robot +Y).

`Head.001` is parented under `Core`, so yawing the body carries the head and the
base-frame head pose correctly includes that rotation. This is physically what the robot
does, and it hands the IK a consistent head+yaw pair by construction.

### `HEAD_TRANSLATION_SCALE = 0.4575`

The rig is **not** at robot metric scale despite the scene being set METRIC/METERS. Two
independent anchors:

| Anchor | Rig | Robot | Scale |
|---|---|---|---|
| Neutral head origin | 0.3869 BU | 0.177 m | 0.45748 |
| Overall body width | 0.3554 BU | 0.160 m | 0.45020 |

They agree to **1.6%**. Default is **0.4575** (head-origin anchor, the one directly
governing head translation); the width anchor is corroboration. Confirmed in sim via
`send_test_pose` and exposed as an AddonPreference so it is tunable without a code
change. (A third anchor — MJCF antenna geom offset 0.0588 m vs bone length 0.1097 BU,
giving 0.536 — is **not** comparable: that offset is a visual mesh placement, not a
kinematic length.)

Rotations need **no** scaling: they are already exact (see below).

### No clamping, sign, or calibration code

The rig's own constraints are applied in the pose the add-on reads (verified above:
pushing `Head.001` to `(0.9, -0.9, 0.9)` yields a clamped `(0.234, -0.234, 0.278)`) — and
the rig is authored to the robot's ranges:

| Rig constraint | Value | Robot |
|---|---|---|
| `Core` `LIMIT_ROTATION` Y | ±160.0° | MJCF `yaw_body` ±2.7925 rad = ±160.0° |
| `Slider.Rot.Core` `LIMIT_LOCATION` Y | ±0.11 → ×25.386 = ±2.792 rad | same |
| `Slider.Rot.Antenna.{L,R}` | ±0.11 → ×28.56 = ±π | antennas ±π |
| `Head.001` `LIMIT_DISTANCE` | 0.3633 inside, vs `Core` | head workspace |

All four have `use_transform_limit=True`, so interactive posing is clamped in the
channel too. There is therefore nothing left to clamp and **no sign/scale calibration UI
to build**. `HEAD_TRANSLATION_SCALE` is the single exception.

### Antennas

`Antenna.*.002` and `Antenna.*.003` are **collinear with identical rest frames**
(`boneY = (-0.002, 0.121, 0.993)`, same localX/localZ), so `.003` is an FK continuation
of the same hinge; both rotate about local Z, which is perpendicular to the shaft and
therefore sweeps the antenna exactly as the robot's hinge does (MJCF `axis="0 0 1"` with
the antenna geom offset along −Y). The robot has one hinge per antenna, so the value is
the **sum** of the two bones' local-Z rotations. `.002` is slider-driven, `.003` is a
direct FK control (bone collection `FK CTRL`, Circle custom shape) — summing lets both
contribute.

### Not renamed

The rig is **left untouched**; the mapping is documented in `docs/RIG_MAPPING.md`
instead. Renaming is safe *inside* the .blend (Blender remaps 383 bone-parented objects,
19 constraint subtargets, 3 driver `bone_target`s + RNA paths, and 4 bone-named vertex
groups) but glTF node names come from bone names, and `export_gltf.py:96` notes the
desktop app "drive[s] bone nodes live in-app" — so a rename would break that app until
updated in lockstep. Bone names live in the mapping config, so the add-on tolerates a
future rename as a settings change.

## UI

N-panel tab **"Reachy Mini"**, three boxes:

```
▸ Connection     Host [localhost]  Port [8000]  Rate [50] Hz
                 [ ● Start Sync ]        ○ Idle
                 [ Send test pose ]
▸ Export Move    Description [wave_hello]
                 Range [scene 1–146]   Out [//moves/wave_hello.json]
                 [ Export Move ]
▸ Advanced       Head translation scale [0.4575]   Bone mapping ▸
```

Operators: `reachy_mini.sync_start` / `sync_stop`, `reachy_mini.send_test_pose`,
`reachy_mini.export_move`. Start connects implicitly, Stop disconnects — one toggle, two
states, rather than a separate connect-then-sync three-state machine.

Status line: grey `○ Idle` · green `● Syncing (<host>)` · red
`✕ Connection failed: <reason>` / `✕ Connection lost` / `✕ Rig: <bone>`.

### Start sequence

1. `connect()`
2. `set_automatic_body_yaw: false`
3. `set_torque: on`
4. **one interpolated `goto_target` (~1 s)** to ease in from the robot's current pose
5. stream `set_target` at the configured rate

Step 4 is deliberate. `set_target` has no interpolation, so without it the first frame
after Start snaps the robot from wherever it is to whatever pose Blender holds. This is
a defect observed in the reachy2 add-on and is designed out here.

**Stop** halts the timer and disconnects, leaving the robot holding its last pose with
motors on — cutting torque would drop the head.

## Bake

Walk `scene.frame_start .. frame_end`; per frame `scene.frame_set(f)` then read the
evaluated depsgraph via the same `rig.read()` as the live path.

- `time[i] = (f - frame_start) / fps`, `fps = scene.render.fps / scene.render.fps_base`
- `set_target_data[i] = {"head": <nested 4x4>, "antennas": [r, l], "body_yaw": <float>}`
- `description` from the UI field
- the original frame is restored afterwards

## Error handling

Three failure sources, all surfaced in the status line, never silent:

- **Connect fails** — `client.connect()` raises `ConnectionError`; the Start operator
  catches it, sets red status with the reason, and does not start the timer.
- **Connection lost mid-sync** — receive thread sees >1 s with no server message; the
  loop stops itself and status goes red.
- **Rig read fails** (renamed/missing bone) — caught, status names the offending bone,
  sync stops.

A tick must never die silently: unexpected exceptions are logged with a traceback, sync
stops, and the status reflects it.

## Testing

| Test | Assertion |
|---|---|
| `test_client.py` | Fake WS server: handshake, client masking, frame parse (7/16/64-bit lengths), ping→pong, exact JSON payload. Opt-in live test vs a running `--sim` daemon. |
| `test_rig.py` | `blender --background`: neutral → head identity, `body_yaw == 0`, `antennas == [0, 0]`. `Slider.Rot.Core.location.y = 0.05` → `body_yaw ≈ 1.2693` rad. Antenna slider `0.05` → `1.428` rad. Combined head rotate+translate → asserts the independent R/t assembly, not the composed-matrix form. |
| `test_bake.py` | Bake 3 frames → validate schema → load through `RecordedMove` → assert `evaluate(t)` at `t=0` and mid-interval. |

The numeric expectations are derived from the rig's driver constants (25.386, 28.56), so
`test_rig.py` fails loudly if the rig's calibration ever changes.

**Tolerances:** the shipped rig's rest pose is **exactly zero** — measured max
`|rotation_euler|` across all 59 bones at rest is `0.000000`. Neutral assertions can
therefore be tight; `1e-6` rad / `1e-9` m covers only the floating-point error introduced
by the `R_rest.inverted()` matrix inverse in the head construction.

The two driver constants were verified end-to-end, not just read off the driver:
`Slider.Rot.Core.location.y = 0.05` yields `Core.rotation_euler[1] = 1.269300` rad
(= `0.05 × 25.386`, 72.73°), and the antenna slider at `0.05` yields
`1.428000` rad (= `0.05 × 28.56`, 81.82°). These are the exact figures `test_rig.py`
asserts.

## Safety

- All verification against `reachy-mini-daemon --sim` (MuJoCo) **before any hardware**.
- `send_test_pose` walks a known sequence — neutral, +Z, +X, roll, each antenna, yaw —
  which is how `HEAD_TRANSLATION_SCALE` and the per-side antenna signs are confirmed.
- `set_target` is immediate, so a fast timeline scrub produces step changes. Absolute
  range is bounded by the rig's constraints; the ease-in covers the Start transient.
  A velocity/slew limit is **out of scope for v1** and revisited only if sim testing
  shows it is needed.

## Decisions locked

| Decision | Choice |
|---|---|
| Prior art | Discarded entirely; only re-verified facts kept |
| Sync semantics | Live mirror **and** offline bake to move JSON |
| Recording | Bake timeline → `RecordedMove` JSON; no robot required |
| Packaging | On-disk multi-module add-on package in the repo |
| Transport | Hand-rolled WS client, no dependencies |
| Head translation | Streamed, `SCALE = 0.4575`, sim-confirmed |
| Rig | **Not** renamed; documented in `docs/RIG_MAPPING.md` |
| Reference frame | `Base` (fixed), not `Core` |

## Out of scope for v1

- Reading robot state back into Blender (one-way only).
- Velocity/slew limiting.
- Remote robot over the network — works by changing the host field, untested.
- Sound attached to a baked move (`RecordedMove` supports a sibling `.wav`).
