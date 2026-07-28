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
| `head` (4×4 pose) | m + rotation | `Head.001` relative to `Base` | `R = R_cur @ R_rest⁻¹` ; `t = (p_cur − p_rest) × 0.4575` |
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

The rig is authored to the robot's real limits, so no clamping is needed in code.

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
