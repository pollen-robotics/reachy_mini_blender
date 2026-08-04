"""Panel, operators and properties. The only module that touches UI state.

Layout is the 3D viewport sidebar (N) under a "Reachy Mini" tab.
"""

import math
import pathlib
import webbrowser

import bpy

from . import audio, bake, discover, hub, import_move, play, rig, sync

# The rigged model ships inside the add-on so installing the zip is the
# whole setup - no separate .blend download.
_ASSET_BLEND = pathlib.Path(__file__).parent / "assets" / "reachy_mini.blend"


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


class REACHY_MINI_OT_load_rig(bpy.types.Operator):
    """Add the bundled Reachy Mini scene (rig + model) to this file
and switch to it. Your other scenes are untouched"""

    bl_idname = "reachy_mini.load_rig"
    bl_label = "Load Reachy Mini Rig"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if not _ASSET_BLEND.exists():
            self.report({"ERROR"},
                        f"Reachy Mini: bundled rig not found at {_ASSET_BLEND}")
            return {"CANCELLED"}

        # Append (not link) the whole scene: the armature, its widgets and
        # the skinned model span several collections, so cherry-picking one
        # would arrive broken. Appending also means saving stays in the
        # user's own file - the bundled asset is never written to.
        try:
            with bpy.data.libraries.load(str(_ASSET_BLEND)) as (src, dst):
                if "Scene" not in src.scenes:
                    raise RuntimeError("no 'Scene' in the bundled rig file")
                dst.scenes = ["Scene"]
        except (RuntimeError, OSError) as exc:
            self.report({"ERROR"}, f"Reachy Mini: {exc}")
            return {"CANCELLED"}

        scene = dst.scenes[0]
        scene.name = "Reachy Mini"
        context.window.scene = scene
        self.report({"INFO"}, "Reachy Mini rig loaded - you are now in its scene")
        return {"FINISHED"}


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
    dataset_name: bpy.props.StringProperty(
        name="Dataset", default=hub.DATASET_DEFAULT,
        description=("Dataset name Publish to Hub pushes into, created "
                     "under your namespace on first use"))

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "hf_token")
        layout.label(text="Checked only if no hf CLI login or HF_TOKEN "
                          "env var is found.")
        layout.prop(self, "dataset_name")


def _hf_prefs_token(context):
    addon = context.preferences.addons.get(__package__)
    return addon.preferences.hf_token if addon else ""


def _redraw_later():
    """Worker-thread callback: schedule a viewport redraw on the main thread.

    Workers must not touch bpy; a one-shot timer gets us back onto the
    main thread. Shared by every async operation that reports through
    the panel (HF auth, publish, robot playback, discovery).
    """
    def do_redraw():
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
        return None

    bpy.app.timers.register(do_redraw, first_interval=0.0)


def _hf_recheck(context):
    """Kick the auth ladder check; redraw the viewport when it lands."""
    hub.check_async(prefs_token=_hf_prefs_token(context), on_done=_redraw_later)


class REACHY_MINI_OT_find_robot(bpy.types.Operator):
    """Probe 127.0.0.1 and reachy-mini.local for a running daemon.
If neither answers, type the IP shown in the mobile app into Host"""

    bl_idname = "reachy_mini.find_robot"
    bl_label = "Find Robot"

    def execute(self, context):
        port = context.scene.reachy_mini_link.port

        def apply_later():
            # Worker thread: get back on the main thread to touch bpy.
            def apply():
                if discover.state["status"] == "found":
                    bpy.context.scene.reachy_mini_link.host = \
                        discover.state["host"]
                return None

            bpy.app.timers.register(apply, first_interval=0.0)
            _redraw_later()

        discover.find_async(port=port, on_done=apply_later)
        return {"FINISHED"}


class REACHY_MINI_OT_publish_move(bpy.types.Operator):
    """Bake the timeline and push it to your Hugging Face dataset"""

    bl_idname = "reachy_mini.publish_move"
    bl_label = "Publish to Hub"

    def execute(self, context):
        props = context.scene.reachy_mini_link
        start = None if props.use_scene_range else props.frame_start
        end = None if props.use_scene_range else props.frame_end
        try:
            move = bake.bake(
                context.scene, mapping=_mapping(props),
                description=props.description,
                frame_start=start, frame_end=end,
            )
        except (rig.RigError, ValueError) as exc:
            self.report({"ERROR"}, f"Reachy Mini: {exc}")
            return {"CANCELLED"}
        audio_bytes = _bake_audio(self, context.scene, start, end)

        prefs = context.preferences.addons[__package__].preferences
        hub.publish_move_async(
            move,
            dataset_name=prefs.dataset_name or hub.DATASET_DEFAULT,
            prefs_token=prefs.hf_token,
            audio=audio_bytes,
            on_done=_redraw_later,
        )
        self.report({"INFO"}, "Publishing to the Hub…")
        return {"FINISHED"}


class REACHY_MINI_OT_open_dataset(bpy.types.Operator):
    """Open the published dataset in your browser"""

    bl_idname = "reachy_mini.open_dataset"
    bl_label = "Open Dataset"

    def execute(self, _context):
        if hub.publish["url"]:
            webbrowser.open(hub.publish["url"])
        return {"FINISHED"}


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
        description=("Robot address: 127.0.0.1 for a Lite plugged into this "
                     "machine, or the robot's IP / reachy-mini.local for a "
                     "wireless one on your network"))
    port: bpy.props.IntProperty(
        name="Port", default=8000, min=1, max=65535,
        description="Daemon port (8000 unless you changed it)")
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
        min=0.0, max=2.0, soft_max=2.0, precision=4,
        description=("Blender units to metres for head translation. The rig is "
                     "~2.19x oversized; see docs/RIG_MAPPING.md"))


def _mapping(props):
    return rig.Mapping(head_scale=props.head_scale)


def _bake_audio(operator, scene, start, end):
    """WAV bytes for the scene's sequencer audio, or None.

    Sound is opt-out by muting the strip, not by a setting: a sound
    strip in the sequencer means the artist animated against it. A
    failed mixdown degrades to motion-only with a warning rather than
    blocking the move.
    """
    if not audio.scene_has_audio(scene):
        return None
    try:
        return audio.mixdown_wav(scene, frame_start=start, frame_end=end)
    except RuntimeError as exc:
        operator.report({"WARNING"},
                        f"Reachy Mini: audio mixdown failed ({exc}); "
                        "continuing without sound")
        return None


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
    bl_label = "Start Live Sync"

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
    bl_label = "Stop Live Sync"

    def execute(self, context):
        sync.stop()
        return {"FINISHED"}


class REACHY_MINI_OT_send_test_pose(bpy.types.Operator):
    """Send a known sequence to confirm axis signs and head scale in sim"""

    bl_idname = "reachy_mini.send_test_pose"
    bl_label = "Test Pose"

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


class REACHY_MINI_OT_reset_rig(bpy.types.Operator):
    """Return every rig control to its rest pose (clears location,
rotation and scale on all bones; undoable). If Live Sync is running,
the robot follows back to neutral"""

    bl_idname = "reachy_mini.reset_rig"
    bl_label = "Reset Rig"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        mapping = _mapping(context.scene.reachy_mini_link)
        arm = bpy.data.objects.get(mapping.armature)
        if arm is None or arm.type != "ARMATURE":
            self.report({"ERROR"},
                        f"Reachy Mini: armature {mapping.armature!r} not found")
            return {"CANCELLED"}

        # Clear every bone, not just the ones rig.read() samples: the
        # artist-facing sliders drive those bones through constraints,
        # so a partial reset would be immediately re-overridden.
        for pb in arm.pose.bones:
            pb.location = (0.0, 0.0, 0.0)
            pb.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
            pb.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
            pb.rotation_euler = (0.0, 0.0, 0.0)
            pb.scale = (1.0, 1.0, 1.0)

        self.report({"INFO"}, "Rig reset to rest pose")
        return {"FINISHED"}


class REACHY_MINI_OT_cancel_test_pose(bpy.types.Operator):
    """Cancel the running test pose sequence and disconnect"""

    bl_idname = "reachy_mini.cancel_test_pose"
    bl_label = "Cancel Test Pose"

    def execute(self, context):
        _stop_test_pose()
        self.report({"INFO"}, "Test sequence cancelled")
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
        if play.state["status"] in ("sending", "playing"):
            self.report({"INFO"}, "Already playing - stop it first")
            return {"CANCELLED"}

        props = context.scene.reachy_mini_link
        scene = context.scene
        start = None if props.use_scene_range else props.frame_start
        end = None if props.use_scene_range else props.frame_end
        # Bake and mixdown need bpy, so they stay on the main thread;
        # everything network moves to play_async's worker so the UI
        # never freezes on a slow link or a long move.
        try:
            move = bake.bake(
                scene, mapping=_mapping(props),
                description=props.description,
                frame_start=start, frame_end=end,
            )
        except (rig.RigError, ValueError) as exc:
            self.report({"ERROR"}, f"Reachy Mini: {exc}")
            return {"CANCELLED"}
        audio_bytes = _bake_audio(self, scene, start, end)

        play.play_async(props.host, props.port, move, freq=100.0,
                        ease_in=1.0, audio=audio_bytes,
                        on_update=_redraw_later)
        return {"FINISHED"}


class REACHY_MINI_OT_stop_robot_play(bpy.types.Operator):
    """Stop the move currently playing on the robot"""

    bl_idname = "reachy_mini.stop_robot_play"
    bl_label = "Stop"

    def execute(self, context):
        props = context.scene.reachy_mini_link
        play.cancel_async(props.host, props.port, on_update=_redraw_later)
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
        # Audio goes next to the JSON as a .wav sidecar, the one layout
        # every player (daemon, Marionette, this add-on) understands.
        audio_bytes = _bake_audio(self, scene, start, end)
        if audio_bytes:
            sidecar = str(pathlib.Path(resolved_path).with_suffix(".wav"))
            try:
                with open(sidecar, "wb") as fh:
                    fh.write(audio_bytes)
            except OSError as exc:
                self.report({"ERROR"}, f"Reachy Mini: {exc}")
                return {"CANCELLED"}
        # Report the resolved absolute path, not props.out_path's unresolved
        # "//" form, so the artist is shown a path that actually exists.
        with_audio = " (+ audio sidecar)" if audio_bytes else ""
        self.report({"INFO"},
                    f"Wrote {len(move['time'])} frames to "
                    f"{resolved_path}{with_audio}")
        return {"FINISHED"}


class REACHY_MINI_OT_import_move(bpy.types.Operator):
    """Load a recorded move (Marionette, Hub dataset...) as keyframes
on the rig, cleaned up for hand editing"""

    bl_idname = "reachy_mini.import_move"
    bl_label = "Import Move"
    bl_options = {"REGISTER", "UNDO"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.json",
                                          options={"HIDDEN"})

    smooth_sigma: bpy.props.FloatProperty(
        name="Smoothing", default=0.02, min=0.0, max=0.5, step=1,
        precision=2, subtype="TIME_ABSOLUTE",
        description=("Gaussian low-pass width in seconds. Softens capture "
                     "jitter before keyframes are placed; 0 keeps the raw "
                     "signal"))
    tolerance: bpy.props.FloatProperty(
        name="Simplify", default=1.0, min=0.0, max=10.0,
        description=("How far the cleaned curves may drift from the "
                     "recording (1.0 \u2248 0.5 mm / 0.3\u00b0, invisible on "
                     "the robot). Higher keeps fewer keys; 0 keys every "
                     "sample"))
    snap_to_frames: bpy.props.BoolProperty(
        name="Snap Keys to Frames", default=True,
        description=("Round keys to whole frames so they are draggable in "
                     "the dope sheet. Costs at most half a frame of timing"))
    load_audio: bpy.props.BoolProperty(
        name="Load Audio", default=True,
        description=("Add the move's audio sidecar (.wav/.ogg next to the "
                     "JSON) as a sequencer strip"))
    set_scene_range: bpy.props.BoolProperty(
        name="Set Scene Range", default=True,
        description="Fit the scene frame range to the imported move")

    def invoke(self, context, _event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        try:
            stats = import_move.apply(
                context, self.filepath, mapping=_mapping(
                    context.scene.reachy_mini_link),
                smooth_sigma=self.smooth_sigma, tolerance=self.tolerance,
                snap_to_frames=self.snap_to_frames,
                load_audio=self.load_audio,
                set_scene_range=self.set_scene_range)
        except (import_move.MoveFormatError, rig.RigError, OSError) as exc:
            self.report({"ERROR"}, f"Reachy Mini: {exc}")
            return {"CANCELLED"}
        if stats["samples"]:
            with_audio = " + audio" if stats["audio"] else ""
            self.report(
                {"INFO"},
                f"Imported {stats['duration']:.1f}s: {stats['samples']} "
                f"samples \u2192 {stats['keys']} keys{with_audio}")
        elif stats["audio"]:
            self.report({"INFO"}, "Audio-only move: added a sound strip")
        else:
            self.report({"WARNING"}, "Move had no motion and no audio")
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
        syncing = sync.is_running()
        test_running = _test_pose_in_flight()
        busy = syncing or test_running

        # First contact: the file has no rig yet, so nothing below can do
        # anything useful. Offer the bundled scene and keep the panel short.
        if bpy.data.objects.get(_mapping(props).armature) is None:
            box = layout.box()
            box.label(text="No Reachy Mini rig in this file", icon="INFO")
            row = box.row()
            row.scale_y = 1.3
            row.operator("reachy_mini.load_rig", icon="OUTLINER_OB_ARMATURE")

        # ── Robot: where it is, and the live link to it ────────────────
        box = layout.box()
        box.label(text="Robot", icon="TOOL_SETTINGS")
        col = box.column(align=True)
        col.enabled = not busy
        row = col.row(align=True)
        row.prop(props, "host")
        row.operator("reachy_mini.find_robot", text="", icon="VIEWZOOM")
        col.prop(props, "port")

        dstate = discover.state["status"]
        if dstate == "searching":
            box.label(text="Looking for a robot…", icon="TIME")
        elif dstate == "none":
            box.label(text="No robot found - enter the IP from the mobile app",
                      icon="INFO")

        row = box.row()
        row.scale_y = 1.3
        if syncing:
            row.operator("reachy_mini.sync_stop", icon="PAUSE")
        else:
            row.enabled = not test_running
            row.operator("reachy_mini.sync_start", icon="PLAY")

        if phase == "syncing":
            box.label(text=f"Live · {message}", icon="REC")
        elif phase == "error":
            box.label(text=message or "error", icon="ERROR")

        if test_running:
            st = _test_state
            row = box.row(align=True)
            row.label(
                text=f"Test {st['index']}/{len(st['steps'])}: {st['label']}",
                icon="TIME")
            row.operator("reachy_mini.cancel_test_pose", text="", icon="X")
        else:
            row = box.row(align=True)
            sub = row.row(align=True)
            sub.enabled = not syncing
            sub.operator("reachy_mini.send_test_pose", icon="OUTLINER_OB_ARMATURE")
            row.operator("reachy_mini.reset_rig", icon="LOOP_BACK")

        # ── Timeline: what the move is, and playing it on the robot ────
        box = layout.box()
        box.label(text="Timeline", icon="SEQUENCE")
        col = box.column(align=True)
        col.prop(props, "description")
        col.prop(props, "use_scene_range")
        if not props.use_scene_range:
            row = col.row(align=True)
            row.prop(props, "frame_start")
            row.prop(props, "frame_end")
        if audio.scene_has_audio(context.scene):
            box.label(text="Audio plays with the move", icon="SOUND")

        pl = play.state
        if pl["status"] == "playing":
            row = box.row(align=True)
            row.scale_y = 1.3
            row.label(text=f"Playing · {pl['detail']}", icon="PLAY")
            row.operator("reachy_mini.stop_robot_play", icon="SNAP_FACE")
        else:
            row = box.row()
            row.scale_y = 1.3
            row.enabled = not busy and pl["status"] != "sending"
            row.operator("reachy_mini.play_on_robot", icon="PLAY")
            if pl["status"] == "sending":
                box.label(text="Sending to the robot…", icon="TIME")
            elif pl["status"] == "stopped":
                box.label(text="Stopped", icon="SNAP_FACE")
            elif pl["status"] == "error":
                box.label(text=f"Play failed: {pl['detail']}", icon="ERROR")

        box.operator("reachy_mini.import_move", icon="IMPORT")

        # ── Share: export to disk, publish to the Hub ───────────────────
        box = layout.box()
        box.label(text="Share", icon="EXPORT")
        col = box.column(align=True)
        col.prop(props, "out_path")
        if not bpy.data.filepath and props.out_path.startswith("//"):
            col.label(text="Save the .blend first, or use an absolute path",
                      icon="ERROR")

        box.separator(factor=0.5)

        row = box.row(align=True)
        row.operator("reachy_mini.export_move", icon="FILE_TICK")
        sub = row.row(align=True)
        sub.enabled = hub.state["status"] == "ok" \
            and hub.publish["status"] != "working"
        sub.operator("reachy_mini.publish_move", icon="URL")

        pub = hub.publish
        if pub["status"] == "working":
            box.label(text="Publishing…", icon="TIME")
        elif pub["status"] == "done":
            row = box.row(align=True)
            row.label(text=f"Published {pub['detail']}", icon="CHECKMARK")
            row.operator("reachy_mini.open_dataset", text="", icon="URL")
        elif pub["status"] == "error":
            box.label(text=f"Publish failed: {pub['detail']}", icon="ERROR")

        # Sign-in state lives with publishing, the only thing that needs it.
        st = hub.state
        if st["status"] == "ok":
            box.label(text=f"Signed in as {st['user']}", icon="CHECKMARK")
        elif st["status"] == "checking":
            box.label(text="Hugging Face: checking…", icon="TIME")
        elif st["status"] == "error":
            row = box.row(align=True)
            row.label(text=f"Hugging Face: {st['error'] or 'error'}",
                      icon="ERROR")
            row.operator("reachy_mini.hf_check", text="", icon="FILE_REFRESH")
        elif st["status"] == "no_token":
            box.label(text="Hugging Face: not signed in", icon="RADIOBUT_OFF")
            row = box.row(align=True)
            row.operator("reachy_mini.hf_token_page", icon="URL")
            row.operator("reachy_mini.hf_check", text="Check Again",
                         icon="FILE_REFRESH")
        else:  # unchecked
            box.operator("reachy_mini.hf_check",
                         text="Check Hugging Face Sign-in",
                         icon="FILE_REFRESH")

        # ── Advanced: rarely touched knobs ──────────────────────────────
        header, panel = layout.panel("reachy_mini_advanced",
                                     default_closed=True)
        header.label(text="Advanced")
        if panel:
            panel.prop(props, "rate_hz")
            panel.prop(props, "head_scale")


_classes = (
    REACHY_MINI_OT_load_rig,
    ReachyMiniLinkPrefs,
    REACHY_MINI_OT_find_robot,
    REACHY_MINI_OT_publish_move,
    REACHY_MINI_OT_open_dataset,
    REACHY_MINI_OT_hf_check,
    REACHY_MINI_OT_hf_token_page,
    ReachyMiniLinkProps,
    REACHY_MINI_OT_sync_start,
    REACHY_MINI_OT_sync_stop,
    REACHY_MINI_OT_send_test_pose,
    REACHY_MINI_OT_reset_rig,
    REACHY_MINI_OT_cancel_test_pose,
    REACHY_MINI_OT_play_on_robot,
    REACHY_MINI_OT_stop_robot_play,
    REACHY_MINI_OT_export_move,
    REACHY_MINI_OT_import_move,
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

    # Stop the sync loop before tearing down the classes it reports
    # status through.
    sync.stop()
    if hasattr(bpy.types.Scene, "reachy_mini_link"):
        del bpy.types.Scene.reachy_mini_link
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
