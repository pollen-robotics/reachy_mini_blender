"""Panel, operators and properties. The only module that touches UI state.

Layout is the 3D viewport sidebar (N) under a "Reachy Mini" tab.
"""

import math
import time
import webbrowser

import bpy

from . import bake, bridge, hub, play, rig, sync


# Module-level state for test_pose timer-driven sequence.
_test_state = {"client": None, "steps": [], "index": 0, "label": ""}


def _test_pose_in_flight():
    """True while the test-pose sequence owns an open socket.

    Sync and the test sequence must be mutually exclusive: the daemon
    silently drops a streamed set_full_target while any move (including a
    goto_target step of the test sequence) is running, and silently drops a
    goto_target while another move is already running. Without this guard
    each path would silently eat the other.
    """
    return _test_state["client"] is not None


class ReachyMiniLinkPrefs(bpy.types.AddonPreferences):
    """Add-on preferences: only the HF token fallback lives here.

    The token is per-user, not per-scene, so it belongs in preferences
    rather than in the scene PropertyGroup (which is saved in .blends
    that people share).
    """

    bl_idname = __package__

    hf_token: bpy.props.StringProperty(
        name="HF Token", subtype="PASSWORD",
        description=("Hugging Face access token (write scope). Only needed "
                     "if you are not signed in with the hf CLI and have no "
                     "HF_TOKEN environment variable"))

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "hf_token")
        layout.label(text="Checked only if no hf CLI login or HF_TOKEN "
                          "env var is found.")


def _hf_prefs_token(context):
    addon = context.preferences.addons.get(__package__)
    return addon.preferences.hf_token if addon else ""


def _hf_recheck(context):
    """Kick the auth ladder check; redraw the viewport when it lands."""

    def redraw_later():
        # Worker thread: not allowed to touch bpy. A one-shot timer gets
        # us back on the main thread for the redraw.
        def do_redraw():
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == "VIEW_3D":
                        area.tag_redraw()
            return None

        bpy.app.timers.register(do_redraw, first_interval=0.0)

    hub.check_async(prefs_token=_hf_prefs_token(context), on_done=redraw_later)


class REACHY_MINI_OT_hf_check(bpy.types.Operator):
    """Look for a Hugging Face token and verify it against the Hub"""

    bl_idname = "reachy_mini.hf_check"
    bl_label = "Check Sign-in"

    def execute(self, context):
        _hf_recheck(context)
        return {"FINISHED"}


class REACHY_MINI_OT_hf_token_page(bpy.types.Operator):
    """Open the Hugging Face token settings page in your browser"""

    bl_idname = "reachy_mini.hf_token_page"
    bl_label = "Get a Token"

    def execute(self, _context):
        webbrowser.open(hub.TOKEN_PAGE_URL)
        return {"FINISHED"}


class ReachyMiniLinkProps(bpy.types.PropertyGroup):
    """Per-scene settings, so a .blend remembers its host and output path."""

    # 127.0.0.1 rather than "localhost": on macOS localhost resolves to
    # ::1 first, where something may accept TCP but never answer the WS
    # handshake, and the connection times out instead of falling back.
    host: bpy.props.StringProperty(
        name="Host", default="127.0.0.1",
        description="Daemon host. Use the robot's address for a remote robot")
    port: bpy.props.IntProperty(
        name="Port", default=8000, min=1, max=65535,
        description="Daemon SDK WebSocket port")
    rate_hz: bpy.props.FloatProperty(
        name="Rate", default=50.0, min=1.0, max=120.0,
        description="Streaming rate in Hz. Takes effect on the next Start")

    bridge_port: bpy.props.IntProperty(
        name="Bridge port", default=9877, min=1024, max=65535,
        description=("Local port The Animator web app connects to. "
                     "Loopback only; nothing leaves this machine"))

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
        min=0.0, max=2.0, soft_max=2.0, precision=4,
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


def _stop_test_pose():
    """Unregister the tick, disconnect and clear _test_state.

    Shared by the cancel operator and unregister() so both tear the
    sequence down the same way.
    """
    global _test_state
    if bpy.app.timers.is_registered(_test_pose_tick):
        bpy.app.timers.unregister(_test_pose_tick)
    if _test_state["client"] is not None:
        _test_state["client"].disconnect()
    _test_state = {"client": None, "steps": [], "index": 0, "label": ""}


def _test_pose_tick():
    """Issue one test pose per tick.

    The daemon ignores a goto_target while another move is playing, so
    steps must be spaced by at least the move duration. Returning the
    delay (rather than sleeping) keeps Blender's UI responsive.
    """
    st = _test_state
    client_obj = st["client"]
    if client_obj is None:
        return None
    if st["index"] >= len(st["steps"]):
        client_obj.disconnect()
        st["client"] = None
        print("[reachy-mini] test sequence complete")
        return None
    label, head, antennas, body_yaw, duration = st["steps"][st["index"]]
    st["index"] += 1
    st["label"] = label   # for the panel's progress line
    try:
        client_obj.send_goto_target(head=head, antennas=antennas,
                                    body_yaw=body_yaw, duration=duration)
    except ConnectionError as exc:
        print(f"[reachy-mini] test sequence aborted: {exc}")
        client_obj.disconnect()
        st["client"] = None
        return None
    print(f"[reachy-mini] test pose: {label}")
    return duration + 0.5   # margin so the daemon has finished the move


class REACHY_MINI_OT_sync_start(bpy.types.Operator):
    """Connect to the daemon and start mirroring the rig"""

    bl_idname = "reachy_mini.sync_start"
    bl_label = "Start Sync"

    def execute(self, context):
        if _test_pose_in_flight():
            self.report({"ERROR"},
                        "Reachy Mini: cannot start sync while a test pose "
                        "sequence is running")
            return {"CANCELLED"}

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
        global _test_state

        # Sync and the test sequence must be mutually exclusive (see
        # _test_pose_in_flight's docstring): each silently eats the other's
        # commands on the daemon side.
        if sync.is_running():
            self.report({"ERROR"},
                        "Reachy Mini: cannot start a test pose sequence "
                        "while sync is running")
            return {"CANCELLED"}

        # Guard: do not start a second sequence if one is already running.
        if _test_pose_in_flight():
            self.report({"INFO"}, "Test sequence already running")
            return {"CANCELLED"}

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

        def head_roll_x(deg):
            # Rotation about the head's local X axis, flat-16 row-major,
            # translation left at zero (indices 3/7/11).
            a = math.radians(deg)
            c, s = math.cos(a), math.sin(a)
            return [1.0, 0.0, 0.0, 0.0,
                    0.0, c, -s, 0.0,
                    0.0, s, c, 0.0,
                    0.0, 0.0, 0.0, 1.0]

        # Each step is 2 s so a human can see which way the robot moved.
        steps = [
            ("neutral", head(), [0.0, 0.0], 0.0, 2.0),
            ("+Z 20mm", head(dz=0.02), [0.0, 0.0], 0.0, 2.0),
            ("+X 20mm", head(dx=0.02), [0.0, 0.0], 0.0, 2.0),
            ("roll +15deg", head_roll_x(15.0), [0.0, 0.0], 0.0, 2.0),
            ("right antenna +45", head(), [math.radians(45.0), 0.0], 0.0, 2.0),
            ("left antenna +45", head(), [0.0, math.radians(45.0)], 0.0, 2.0),
            ("body yaw +30", head(), [0.0, 0.0], math.radians(30.0), 2.0),
            ("neutral", head(), [0.0, 0.0], 0.0, 2.0),
        ]

        conn = client.WSClient()
        try:
            conn.connect(props.host, props.port)
            conn.set_automatic_body_yaw(False)
            conn.set_torque(True)
        except ConnectionError as exc:
            self.report({"ERROR"}, f"Reachy Mini: {exc}")
            conn.disconnect()
            return {"CANCELLED"}

        # Store client and steps in module state for the timer to drive.
        _test_state["client"] = conn
        _test_state["steps"] = steps
        _test_state["index"] = 0

        # Register timer if not already registered.
        if not bpy.app.timers.is_registered(_test_pose_tick):
            bpy.app.timers.register(_test_pose_tick, first_interval=0.0)

        self.report({"INFO"}, "Test sequence queued; watch the simulator console")
        return {"FINISHED"}


class REACHY_MINI_OT_cancel_test_pose(bpy.types.Operator):
    """Cancel the running test pose sequence and disconnect"""

    bl_idname = "reachy_mini.cancel_test_pose"
    bl_label = "Cancel Test Pose"

    def execute(self, context):
        _stop_test_pose()
        self.report({"INFO"}, "Test sequence cancelled")
        return {"FINISHED"}


class REACHY_MINI_OT_bridge_start(bpy.types.Operator):
    """Expose the rig to The Animator web app (ws://127.0.0.1, local only)"""

    bl_idname = "reachy_mini.bridge_start"
    bl_label = "Start Bridge"

    def execute(self, context):
        props = context.scene.reachy_mini_link
        settings = sync.Settings(
            host="127.0.0.1",
            port=props.bridge_port,
            rate_hz=props.rate_hz,
            mapping=_mapping(props),
        )
        try:
            bridge.start(settings)
        except OSError as exc:
            self.report({"ERROR"},
                        f"Reachy Mini: port {props.bridge_port} unavailable "
                        f"({exc}). Another Blender running?")
            return {"CANCELLED"}
        self.report({"INFO"},
                    f"Bridge listening on ws://127.0.0.1:{props.bridge_port}")
        return {"FINISHED"}


class REACHY_MINI_OT_bridge_stop(bpy.types.Operator):
    """Stop the local bridge and disconnect the web app"""

    bl_idname = "reachy_mini.bridge_stop"
    bl_label = "Stop Bridge"

    def execute(self, context):
        bridge.stop()
        return {"FINISHED"}


class REACHY_MINI_OT_play_on_robot(bpy.types.Operator):
    """Bake the timeline and play it on the robot's own clock (via the daemon)"""

    bl_idname = "reachy_mini.play_on_robot"
    bl_label = "Play on Robot"

    def execute(self, context):
        # A live stream and daemon-side playback fight each other: the
        # daemon drops streamed targets while a move plays and vice
        # versa (see _test_pose_in_flight's docstring).
        if sync.is_running():
            self.report({"ERROR"},
                        "Reachy Mini: stop sync before playing the timeline")
            return {"CANCELLED"}
        if _test_pose_in_flight():
            self.report({"ERROR"},
                        "Reachy Mini: wait for the test pose sequence to end")
            return {"CANCELLED"}

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
        except (rig.RigError, ValueError) as exc:
            self.report({"ERROR"}, f"Reachy Mini: {exc}")
            return {"CANCELLED"}

        from . import client
        conn = client.WSClient()
        try:
            conn.connect(props.host, props.port)
            # The daemon picks its own body yaw unless told otherwise,
            # which would override the yaw baked into the move.
            conn.set_automatic_body_yaw(False)
            conn.set_torque(True)
            time.sleep(0.3)
            play.upload_and_play(conn, move, freq=100.0, ease_in=1.0)
            # Let the upload flush before closing; playback is daemon-side
            # and survives the disconnect.
            time.sleep(0.5)
        except ConnectionError as exc:
            self.report({"ERROR"}, f"Reachy Mini: {exc}")
            return {"CANCELLED"}
        finally:
            conn.disconnect()

        duration = move["time"][-1]
        self.report({"INFO"},
                    f"Playing {len(move['time'])} frames ({duration:.1f}s) "
                    f"on {props.host}:{props.port}")
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
            resolved_path = bake.write_move(props.out_path, move)
        except (rig.RigError, OSError, ValueError) as exc:
            self.report({"ERROR"}, f"Reachy Mini: {exc}")
            return {"CANCELLED"}
        # Report the resolved absolute path, not props.out_path's unresolved
        # "//" form, so the artist is shown a path that actually exists.
        self.report({"INFO"},
                    f"Wrote {len(move['time'])} frames to {resolved_path}")
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

        test_running = _test_pose_in_flight()
        row = box.row()
        row.enabled = not sync.is_running() and not test_running
        row.operator("reachy_mini.send_test_pose", icon="EXPORT")

        if test_running:
            st = _test_state
            box.label(
                text=f"Test pose {st['index']}/{len(st['steps'])}: {st['label']}",
                icon="TIME")
            box.operator("reachy_mini.cancel_test_pose", text="Cancel",
                         icon="X")

        box = layout.box()
        box.label(text="Local Bridge (The Animator)")
        bphase, bmessage = bridge.get_status()
        row = box.row()
        row.enabled = not bridge.is_running()
        row.prop(props, "bridge_port")
        if bridge.is_running():
            box.operator("reachy_mini.bridge_stop", text="Stop Bridge",
                         icon="PAUSE")
        else:
            box.operator("reachy_mini.bridge_start", text="Start Bridge",
                         icon="WORLD")
        if bphase == "connected":
            box.label(text=f"App connected ({bmessage})", icon="REC")
        elif bphase == "listening":
            box.label(text=f"Waiting for the app ({bmessage})",
                      icon="RADIOBUT_ON")
        elif bphase == "error":
            box.label(text=bmessage or "error", icon="ERROR")
        else:
            box.label(text="Off", icon="RADIOBUT_OFF")

        box = layout.box()
        box.label(text="Export Move")
        box.prop(props, "description")
        box.prop(props, "out_path")
        if not bpy.data.filepath and props.out_path.startswith("//"):
            box.label(text="Save the .blend first, or use an absolute path",
                     icon="ERROR")
        box.prop(props, "use_scene_range")
        if not props.use_scene_range:
            row = box.row(align=True)
            row.prop(props, "frame_start")
            row.prop(props, "frame_end")
        row = box.row(align=True)
        row.operator("reachy_mini.export_move", icon="FILE_TICK")
        row.operator("reachy_mini.play_on_robot", icon="PLAY")

        box = layout.box()
        box.label(text="Hugging Face")
        st = hub.state
        if st["status"] == "ok":
            box.label(text=f"Signed in as {st['user']} ({st['source']})",
                      icon="CHECKMARK")
        elif st["status"] == "checking":
            box.label(text="Checking…", icon="TIME")
        elif st["status"] == "error":
            box.label(text=st["error"] or "error", icon="ERROR")
            box.operator("reachy_mini.hf_check", text="Retry", icon="FILE_REFRESH")
        elif st["status"] == "no_token":
            box.label(text="No token found", icon="RADIOBUT_OFF")
            box.label(text="Sign in with the hf CLI, or paste a token in "
                           "the add-on preferences")
            row = box.row(align=True)
            row.operator("reachy_mini.hf_token_page", icon="URL")
            row.operator("reachy_mini.hf_check", text="Check Again",
                         icon="FILE_REFRESH")
        else:  # unchecked
            box.operator("reachy_mini.hf_check", icon="FILE_REFRESH")

        box = layout.box()
        box.label(text="Advanced")
        box.prop(props, "head_scale")


_classes = (
    ReachyMiniLinkPrefs,
    REACHY_MINI_OT_hf_check,
    REACHY_MINI_OT_hf_token_page,
    ReachyMiniLinkProps,
    REACHY_MINI_OT_sync_start,
    REACHY_MINI_OT_sync_stop,
    REACHY_MINI_OT_send_test_pose,
    REACHY_MINI_OT_cancel_test_pose,
    REACHY_MINI_OT_bridge_start,
    REACHY_MINI_OT_bridge_stop,
    REACHY_MINI_OT_play_on_robot,
    REACHY_MINI_OT_export_move,
    REACHY_MINI_PT_link,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.reachy_mini_link = bpy.props.PointerProperty(
        type=ReachyMiniLinkProps)

    # Resolve HF sign-in once at startup so the panel is populated
    # without a click. Deferred to a timer: register() runs in a
    # restricted context where preferences may not be readable yet.
    def initial_hf_check():
        _hf_recheck(bpy.context)
        return None

    bpy.app.timers.register(initial_hf_check, first_interval=0.5)


def unregister():
    # Clean up the test sequence timer and connection before tearing down.
    _stop_test_pose()

    # Stop the sync loop and the local bridge before tearing down the
    # classes they report status through.
    sync.stop()
    bridge.stop()
    if hasattr(bpy.types.Scene, "reachy_mini_link"):
        del bpy.types.Scene.reachy_mini_link
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
