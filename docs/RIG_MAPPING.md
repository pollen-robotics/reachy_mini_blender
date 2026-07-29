# Reachy Mini rig → robot DOF mapping

Traceability record for `reachy_mini.blend`'s armature and how it maps onto the robot's
degrees of freedom. **Nothing in the rig has been renamed** — this file exists so the
mapping is documented rather than encoded only in code.

- **Rig:** `reachy_mini.blend`, object `Armature`, 59 bones, all `XYZ` Euler mode.
- **Measured with:** Blender 5.1.0
- **Robot reference:** `reachy_mini` SDK 1.8.3 + `descriptions/reachy_mini/mjcf/reachy_mini.xml`
- **Date:** 2026-07-28

## The animator's control surface

Only these bones are meant to be touched. Everything else is mechanism that follows.

| Bone | Bone collection | Custom shape | What it does |
|---|---|---|---|
| `Head.001` | `IK CTRL` | `Plane. tete` | 6-DoF head control; drives the 6 Stewart legs via IK |
| `Slider.Rot.Core` | `SLIDERS` | `Circle` | Slide to yaw the body (drives `Core` via a driver) |
| `Slider.Rot.Antenna.L` | `SLIDERS` | `Circle` | Slide to sweep the left antenna |
| `Slider.Rot.Antenna.R` | `SLIDERS` | `Circle` | Slide to sweep the right antenna |
| `Antenna.L.003` | `FK CTRL` | `Circle` | Direct FK sweep of the left antenna |
| `Antenna.R.003` | `FK CTRL` | `Circle` | Direct FK sweep of the right antenna |
| `GLOBAL` | `MAIN` / `FK CTRL` | `Plane.001` | Places the whole robot in the scene — **not** a robot DOF |

## DOF mapping

| Robot DOF | Units | Rig source | Extraction |
|---|---|---|---|
| `head` (4×4 pose) | m + rotation | `Head.001` relative to `Base` | `R = C @ (R_cur @ R_rest⁻¹) @ Cᵀ` ; `t = C @ (p_cur − p_rest) × 0.4575`, `C = BASE_TO_ROBOT` (see below) |
| `body_yaw` | rad | `Core` | `rotation_euler[1]` |
| `antennas[0]` (right) | rad | `Antenna.R.002` + `Antenna.R.003` | sum of `rotation_euler[2]` |
| `antennas[1]` (left) | rad | `Antenna.L.002` + `Antenna.L.003` | sum of `rotation_euler[2]` |

Antenna order on the wire is **`[right, left]`**.

### Why `body_yaw` reads local Y

The robot yaws about world +Z, but the extraction reads `Core.rotation_euler[1]` (local
Y). These agree: `Core` runs from `z=0.0676` to `z=0.1728`, so its along-bone axis
(Blender's local Y) *is* world +Z. The driver writes index `[1]` and `Core`'s
`LIMIT_ROTATION` is on Y, both consistent.

### Why `Base` and not `Core` or `GLOBAL`

- **Not `Core`:** the robot's head pose is a *base-frame* target. `placo_kinematics.py:345`
  assigns `self.head_frame.T_world_frame = _pose` while `body_yaw` is a separate
  simultaneous IK joint task (`:347`). Reading the head relative to `Core` would cancel
  the body yaw out of the head pose.
- **Not `GLOBAL`:** `Base` is the robot's fixed base (parented to `GLOBAL`, no
  constraints, not driven). Referencing `Base` lets an artist move or animate `GLOBAL` to
  place the robot in a scene without perturbing the streamed pose.

`Head.001` is parented under `Core`, so yawing the body carries the head and the
base-frame head pose correctly *includes* that rotation — which is physically what the
robot does, and hands the IK a consistent head+yaw pair.

### `BASE_TO_ROBOT`: `Base`'s bone-local frame is not the robot frame

The head pose above is computed *relative to `Base`'s bone-local frame* — i.e. in the
coordinate system where `Base`'s own local X/Y/Z axes are the basis vectors. That frame is
**not** the robot's REP-103 convention (+X forward, +Y left, +Z up), because a Blender
bone's local Y always runs *along the bone*, and `Base` itself runs along world +Z, not
world +X. Concretely:

```
Base localX -> world +X
Base localY -> world +Z
Base localZ -> world -Y
```

So a raw bone-local delta must be rotated into the robot frame before it is sent, via:

```
(vx, vy, vz)_bonelocal -> (-vz, -vx, vy)_robot
```

`rig.py` encodes this as the constant `BASE_TO_ROBOT` (a 3×3 change-of-basis matrix,
`det = +1`, orthonormal) and applies it to **both** the rotation and the translation
halves of the head pose — the rotation via conjugation (`BASE_TO_ROBOT @ R @
BASE_TO_ROBOT.transposed()`, the correct way to re-express a rotation in a new basis, not
`BASE_TO_ROBOT @ R` alone) and the translation via a plain multiply.

**Evidence this is the right basis, not merely a plausible one:**

- The rig's face is visible only when looking from rig +Y toward −Y — i.e. rig +Y is the
  model's forward direction, which the robot calls +X.
- `Antenna.L` sits at rig −X — the robot's left antenna, so rig −X is the robot's +Y
  (left).
- All three rotation axes independently confirm the same mapping (bone-local X → world
  X → robot pitch axis at −Y; bone-local Y → world Z → robot yaw axis at +Z; bone-local Z
  → world −Y → robot roll axis at −X), consistent with the single change-of-basis matrix
  above.

Without this correction, a head move that is physically **forward** (rig +Y) is reported
to the robot as **down** (`z = -0.01373` for a 3 cm move), and a move that is physically
**up** is reported as **left** (`y = +0.01373`) — the rotation and translation deltas were
being shipped verbatim in the wrong basis.

`body_yaw` and the two antennas are **not** affected: they are scalar rotations read
directly off a single bone axis (`Core` local Y = world Z, `Antenna.*` local Z = the
antenna's own hinge axis), not vectors or matrices expressed in `Base`'s frame, so there
is no basis to convert.

## Driver constants

Three drivers, all `SCRIPTED` with a single `TRANSFORMS` variable reading the slider's
`LOC_Y` in `LOCAL_SPACE`:

| Driven channel | Expression | Driver source |
|---|---|---|
| `Core.rotation_euler[1]` | `var * 25.386` | `Slider.Rot.Core` `LOC_Y` |
| `Antenna.L.002.rotation_euler[2]` | `var * 28.56` | `Slider.Rot.Antenna.L` `LOC_Y` |
| `Antenna.R.002.rotation_euler[2]` | `var * 28.56` | `Slider.Rot.Antenna.R` `LOC_Y` |

Each slider is clamped by `LIMIT_LOCATION` to `Y ∈ [−0.11, +0.11]`
(`use_transform_limit=True`). The constants are therefore exact range calibrations:

```
body yaw : 0.11 × 25.386 = 2.7925 rad = ±160.0°
antennas : 0.11 × 28.56  = 3.1416 rad = ±180.0°  (= ±π)
```

**Verified end-to-end**, not just read off the driver:

| Input | Measured output |
|---|---|
| `Slider.Rot.Core.location.y = 0.05` | `Core.rotation_euler[1] = 1.269300` rad (72.73°) |
| `Slider.Rot.Antenna.L.location.y = 0.05` | `Antenna.L.002.rotation_euler[2] = 1.428000` rad (81.82°) |

## Rig ranges vs robot ranges

The rig is authored to the robot's real limits for the **rotational/scalar channels** —
`body_yaw` and the antennas — so no clamping is needed in code for those. Head
translation is the exception; see "Head translation workspace" below.

| Rig constraint | Rig value | Robot source | Robot value |
|---|---|---|---|
| `Core` `LIMIT_ROTATION` Y | ±160.0° | MJCF `yaw_body range` | ±2.792526 rad = ±160.0° |
| `Slider.Rot.Core` `LIMIT_LOCATION` Y | ±0.11 → ±2.7925 rad | same | same |
| `Slider.Rot.Antenna.{L,R}` `LIMIT_LOCATION` Y | ±0.11 → ±π | SDK antennas | ±π |
| `Head.001` `LIMIT_DISTANCE` | 0.3633 `INSIDE`, vs `Core`, world | Stewart workspace | — |
| `Head.001` `LIMIT_LOCATION` | Y ≥ −0.109 | — | — |

All have `use_transform_limit=True`, so interactive posing is clamped in the channel too.
Verified: pushing `Head.001` to `(0.9, −0.9, 0.9)` yields a pose translation of
`(0.234, −0.234, 0.278)`.

`Head.001`'s `LIMIT_DISTANCE` row above, unlike the other three, is **not** a robot-range
calibration — it is roughly 7× looser than the Stewart platform's confirmed workspace, so
it does not keep head translation inside what the robot can actually reach. See "Head
translation workspace" below for the confirmed numbers and practical guidance.

## Scale

**The rig is not at robot metric scale**, despite the scene being set METRIC / METERS
with `scale_length = 1.0`. Rotations are exact and need no scaling; **translation needs
`× 0.4575`** (Blender units → metres).

| Anchor | Rig | Robot | Implied scale |
|---|---|---|---|
| Neutral head origin | `Head.001` head_local z = 0.3869 BU | `head_z_offset` = 0.177 m | 0.45748 |
| Overall body width | `MiniReachyRetopo` bbox X = 0.3554 BU | ~0.160 m | 0.45020 |

The two agree to 1.6%. `0.4575` is taken from the head-origin anchor as it directly
governs head translation. The rig is thus ~2.19× oversized.

Not a valid anchor: MJCF antenna geom offset 0.0588 m vs `Antenna.*.002` bone length
0.1097 BU (would give 0.536) — that offset is visual mesh placement, not a kinematic
length.

The robot's kinematics adds the **+0.177 m Z offset internally**
(`placo_kinematics.py:130,344`), so neutral is plain identity and the offset must **not**
be baked into what we send.

## Simulator-confirmed values

Measured 2026-07-29 against a live `reachy-mini-daemon --sim` and a screencast of the
MuJoCo viewer.

| Channel | Sign | Evidence |
|---|---|---|
| `body_yaw` | **+1** (default correct, no flip) | Commanding +30° gave `head_joint_positions[0]` delta `+0.5223` rad (+29.9°); commanding −30° gave `−0.5223` rad. `head_joint_positions[0]` is the body-yaw joint, matching the MJCF actuator order (`yaw_body`, then `stewart_1..6`). |
| `antennas[0]` (right) | **+1**, no swap | Commanding `[45°, 0]` gave `antennas_joint_positions [0.7856, 0.0]`. Independently, the screencast shows this step moves the viewer-**left** antenna, which is the robot's own right since the robot faces the camera. |
| `antennas[1]` (left) | **+1**, no swap | Commanding `[0, 45°]` gave `[-0.0, 0.7857]`; the screencast shows the viewer-**right** antenna moving. |

**`HEAD_TRANSLATION_SCALE = 0.4575` retained**, but honestly: it is derived from the two
geometric anchors above (neutral head origin `0.177 / 0.3869 = 0.45748`; body width
`0.160 / 0.3554 = 0.45020`, agreeing to 1.6%) and is **not independently confirmed on
hardware** — the readback commands synthetic metres, so it cannot distinguish the
rig-to-metres factor from a correctly-scaled identity. What *is* confirmed is that the
wire's units themselves are 1:1 metres: commanding 20 mm on each axis reported
`+0.0198` / `+0.0198` / `+0.0199` m back on the matching axis, with negligible cross-talk.

Also confirmed: neutral reports `t ≈ (0, 0, −0.0001)` m, consistent with the "+0.177 m Z
offset removed on output" claim above — it must not be baked into what we send.

## Head translation workspace

Measured saturation of the Stewart platform (commanded vs. achieved, sim), 2026-07-29:

| Axis | Achieved max | Equivalent rig movement at scale 0.4575 |
|---|---|---|
| +Z | 0.0231 m (23 mm) | 0.050 BU |
| +X | 0.0364 m (36 mm) | 0.080 BU |
| +Y | 0.0482 m (48 mm) | 0.105 BU |

Raw tracking data for +Z: commanded `0.010 -> 0.0098`; `0.020 -> 0.0199`; `0.030 -> 0.0231`
(saturated); `0.050 -> 0.0231`; `0.080 -> 0.0231`.

`Head.001`'s `LIMIT_DISTANCE` permits **0.3633 BU** — roughly **7×** beyond what the robot
can reach vertically. So the rig's constraint does **not** confine head translation to the
robot's workspace: an artist moving `Head.001` freely will silently saturate the robot —
it simply stops following while Blender keeps moving, with nothing surfaced as an error.

**Practical guidance:** keep `Head.001` translation within roughly **±0.05 BU** for motion
the robot can actually reproduce. The limit is axis-dependent — Z is the tightest, X and Y
allow somewhat more (see table above).

## Mechanism bones (read by nothing; they follow)

| Group | Bones | Mechanism |
|---|---|---|
| Stewart legs | `Neck.{A–F}.{001–004}` (24) | `.004` carries an `IK` constraint |
| Stewart IK targets | `Neck.loc.IK.{A–F}` + `.001` (12) | `COPY_LOCATION`, `LIMIT_DISTANCE` |
| Head platform anchors | `Head.{A–F}` (6) | `LIMIT_DISTANCE` |
| Head-skin parent | `Core.001` | `COPY_LOCATION` + `COPY_ROTATION` from `Core`; parents the antennas |
| Antenna mid | `Antenna.{L,R}.{001,002}` | `.002` is driver-driven |
| Slider rails | `Bone`, `Border.Slider.Rot.Antenna.{L,R}` | slider parents |
| Root / base | `GLOBAL`, `Base`, `Core` | `Core`: `LIMIT_DISTANCE` + `LIMIT_ROTATION` |

47 of 59 bones carry constraints. The rig is cleanly layered: a thin control surface the
add-on reads, and a constraint/IK mechanism layer it ignores.

## Antenna geometry note

`Antenna.*.002` and `Antenna.*.003` are **collinear with identical rest frames**
(bone axis `(−0.002, 0.121, 0.993)`, same local X and Z), so `.003` is an FK continuation
of the same hinge. Both rotate about local Z, which is *perpendicular* to the antenna
shaft and therefore sweeps it — matching the robot, whose antenna hinge is
`axis="0 0 1"` with the antenna geom offset along −Y. Since the robot has one hinge per
antenna, the mapped value is the **sum** of the two bones' local-Z rotations.

**Sign caveat:** the MJCF gives the two antennas different mounting quaternions
(`right_antenna` at `reachy_mini.xml:498`, `left_antenna` at `:512`), so "positive" may
look visually opposite between left and right. Per-side sign is confirmed against the
MuJoCo simulator, never assumed.

## Not mapped

| Rig element | Why |
|---|---|
| `GLOBAL` | scene placement, not a robot DOF |
| `Antenna.{L,R}.001` | not a control; different axis from the hinge |
| Individual `Neck.*` / `Head.{A–F}` | mechanism; the robot solves its own Stewart IK from the head pose |
| Actions | the .blend ships with none (`bpy.data.actions` is empty) |

## Reproducing these measurements

```bash
blender --background reachy_mini.blend --python-expr '
import bpy, math
arm = bpy.data.objects["Armature"]
for d in arm.animation_data.drivers:
    print(d.data_path, d.driver.expression,
          [(t.bone_target, t.transform_type) for v in d.driver.variables for t in v.targets])
for n in ("Base","Core","Head.001"):
    print(n, [round(v,4) for v in arm.data.bones[n].head_local])
'
```
