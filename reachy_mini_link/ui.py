"""Panel, operators and properties. The only module that touches UI state.

Layout is the 3D viewport sidebar (N) under a "Reachy Mini" tab.
"""

import math

import bpy

from . import bake, rig, sync


class ReachyMiniLinkProps(bpy.types.PropertyGroup):
    """Per-scene settings, so a .blend remembers its host and output path."""

    host: bpy.props.StringProperty(
        name="Host", default="localhost",
        description="Daemon host. Use the robot's address for a remote robot")
    port: bpy.props.IntProperty(
        name="Port", default=8000, min=1, max=65535,
        description="Daemon SDK WebSocket port")
    rate_hz: bpy.props.FloatProperty(
        name="Rate", default=50.0, min=1.0, max=120.0,
        description="Streaming rate in Hz. Takes effect on the next Start")

    description: bpy.props.StringProperty(
        name="Description", default="untitled",
        description="Description stored in the baked move file")
    out_path: bpy.props.StringProperty(
        name="Output", default="//moves/untitled.json", subtype="FILE_PATH",
        description="Where to write the baked move JSON")
    use_scene_range: bpy.props.BoolProperty(
        name="Use scene frame range", default=True)
    frame_start: bpy.props.IntProperty(name="Start", default=1, min=0)
    frame_end: bpy.props.IntProperty(name="End", default=48, min=0)

    head_scale: bpy.props.FloatProperty(
        name="Head translation scale", default=rig.HEAD_TRANSLATION_SCALE,
        min=0.0, soft_max=2.0, precision=4,
        description=("Blender units to metres for head translation. The rig is "
                     "~2.19x oversized; see docs/RIG_MAPPING.md"))


def _mapping(props):
    return rig.Mapping(head_scale=props.head_scale)


def _settings(props):
    return sync.Settings(
        host=props.host,
        port=props.port,
        rate_hz=props.rate_hz,
        mapping=_mapping(props),
    )


class REACHY_MINI_OT_sync_start(bpy.types.Operator):
    """Connect to the daemon and start mirroring the rig"""

    bl_idname = "reachy_mini.sync_start"
    bl_label = "Start Sync"

    def execute(self, context):
        props = context.scene.reachy_mini_link
        try:
            sync.start(_settings(props))
        except (ConnectionError, rig.RigError) as exc:
            self.report({"ERROR"}, f"Reachy Mini: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Syncing to {props.host}:{props.port}")
        return {"FINISHED"}


class REACHY_MINI_OT_sync_stop(bpy.types.Operator):
    """Stop mirroring and disconnect. The robot holds its last pose"""

    bl_idname = "reachy_mini.sync_stop"
    bl_label = "Stop Sync"

    def execute(self, context):
        sync.stop()
        return {"FINISHED"}


class REACHY_MINI_OT_send_test_pose(bpy.types.Operator):
    """Send a known sequence to confirm axis signs and head scale in sim"""

    bl_idname = "reachy_mini.send_test_pose"
    bl_label = "Send test pose"

    def execute(self, context):
        props = context.scene.reachy_mini_link
        from . import client

        identity = [1.0, 0.0, 0.0, 0.0,
                    0.0, 1.0, 0.0, 0.0,
                    0.0, 0.0, 1.0, 0.0,
                    0.0, 0.0, 0.0, 1.0]

        def head(dx=0.0, dy=0.0, dz=0.0):
            m = list(identity)
            m[3], m[7], m[11] = dx, dy, dz
            return m

        # Each step is 2 s so a human can see which way the robot moved.
        steps = [
            ("neutral", head(), [0.0, 0.0], 0.0),
            ("+Z 20mm", head(dz=0.02), [0.0, 0.0], 0.0),
            ("+X 20mm", head(dx=0.02), [0.0, 0.0], 0.0),
            ("right antenna +45", head(), [math.radians(45.0), 0.0], 0.0),
            ("left antenna +45", head(), [0.0, math.radians(45.0)], 0.0),
            ("body yaw +30", head(), [0.0, 0.0], math.radians(30.0)),
            ("neutral", head(), [0.0, 0.0], 0.0),
        ]

        conn = client.WSClient()
        try:
            conn.connect(props.host, props.port)
            conn.set_automatic_body_yaw(False)
            conn.set_torque(True)
            for label, h, ant, yaw in steps:
                print(f"[reachy-mini] test pose: {label}")
                conn.send_goto_target(head=h, antennas=ant, body_yaw=yaw, duration=2.0)
                # goto_target is fire-and-forget; the daemon interpolates. A
                # blocking wait here would freeze the UI, so the sequence is
                # queued and the console log names each step for the observer.
        except ConnectionError as exc:
            self.report({"ERROR"}, f"Reachy Mini: {exc}")
            return {"CANCELLED"}
        finally:
            conn.disconnect()
        self.report({"INFO"}, "Test sequence sent; watch the simulator")
        return {"FINISHED"}


class REACHY_MINI_OT_export_move(bpy.types.Operator):
    """Bake the timeline to a Reachy Mini move JSON"""

    bl_idname = "reachy_mini.export_move"
    bl_label = "Export Move"

    def execute(self, context):
        props = context.scene.reachy_mini_link
        scene = context.scene
        start = None if props.use_scene_range else props.frame_start
        end = None if props.use_scene_range else props.frame_end
        try:
            move = bake.bake(
                scene, mapping=_mapping(props),
                description=props.description,
                frame_start=start, frame_end=end,
            )
            bake.write_move(props.out_path, move)
        except (rig.RigError, OSError) as exc:
            self.report({"ERROR"}, f"Reachy Mini: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"},
                    f"Wrote {len(move['time'])} frames to {props.out_path}")
        return {"FINISHED"}


class REACHY_MINI_PT_link(bpy.types.Panel):
    bl_label = "Reachy Mini"
    bl_idname = "REACHY_MINI_PT_link"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Reachy Mini"

    def draw(self, context):
        layout = self.layout
        props = context.scene.reachy_mini_link
        phase, message = sync.get_status()

        box = layout.box()
        box.label(text="Connection")
        col = box.column(align=True)
        col.enabled = not sync.is_running()
        col.prop(props, "host")
        col.prop(props, "port")
        col.prop(props, "rate_hz")

        if sync.is_running():
            box.operator("reachy_mini.sync_stop", text="Stop Sync", icon="PAUSE")
        else:
            box.operator("reachy_mini.sync_start", text="Start Sync", icon="PLAY")

        if phase == "syncing":
            box.label(text=f"Syncing ({message})", icon="REC")
        elif phase == "error":
            box.label(text=message or "error", icon="ERROR")
        else:
            box.label(text="Idle", icon="RADIOBUT_OFF")

        box.operator("reachy_mini.send_test_pose", icon="EXPORT")

        box = layout.box()
        box.label(text="Export Move")
        box.prop(props, "description")
        box.prop(props, "out_path")
        box.prop(props, "use_scene_range")
        if not props.use_scene_range:
            row = box.row(align=True)
            row.prop(props, "frame_start")
            row.prop(props, "frame_end")
        box.operator("reachy_mini.export_move", icon="FILE_TICK")

        box = layout.box()
        box.label(text="Advanced")
        box.prop(props, "head_scale")


_classes = (
    ReachyMiniLinkProps,
    REACHY_MINI_OT_sync_start,
    REACHY_MINI_OT_sync_stop,
    REACHY_MINI_OT_send_test_pose,
    REACHY_MINI_OT_export_move,
    REACHY_MINI_PT_link,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.reachy_mini_link = bpy.props.PointerProperty(
        type=ReachyMiniLinkProps)


def unregister():
    # Stop the loop before tearing down the classes it reports status through.
    sync.stop()
    if hasattr(bpy.types.Scene, "reachy_mini_link"):
        del bpy.types.Scene.reachy_mini_link
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
