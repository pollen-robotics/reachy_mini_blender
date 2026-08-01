"""Local bridge: expose the rig to a browser page over ws://127.0.0.1.

The counterpart of sync.py for the WebRTC era. sync.py streams the rig
straight at a daemon on the LAN; this module instead runs a local
WebSocket server (server.py) that a web app on the SAME machine (The
Animator Space) connects to. The web app owns the robot connection
over WebRTC, so Blender needs zero network/auth knowledge here.

Threading rule, same as sync.py: bpy is touched only from the main
thread. server.py delivers connects/messages on its own threads; they
are enqueued and drained by the bpy.app.timers tick, which also
samples the rig and broadcasts one frame per tick.

Wire protocol (versioned JSON text frames):
  out  {"type": "hello", "version": 1, "blender": "...", "scene": {...}}
  out  {"type": "frame", "head": [16], "antennas": [r, l],
        "body_yaw": f, "frame_current": int}
  out  {"type": "scene_info", "scene": {...}}
  out  {"type": "bake_result", "request_id": ..., "move": {...}}
  out  {"type": "error", "request_id": ..., "message": "..."}
  in   {"type": "scene_info"}
  in   {"type": "bake", "request_id": ..., "start": int?, "end": int?,
        "description": str?}
"""

import queue

import bpy

from . import bake, rig, server

PROTOCOL_VERSION = 1

_server = None
_settings = None
_events = queue.Queue()
_phase = "off"           # "off" | "listening" | "connected" | "error"
_message = ""


def get_status():
    """(phase, message) for the UI. Never raises."""
    return _phase, _message


def is_running():
    return _server is not None


def _set_status(phase, message=""):
    global _phase, _message
    _phase, _message = phase, message


def _scene_payload(scene):
    return {
        "fps": scene.render.fps / scene.render.fps_base,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "frame_current": scene.frame_current,
        "blend": bpy.path.basename(bpy.data.filepath) or None,
    }


def start(settings):
    """Bind the local server and start the broadcast tick.

    `settings` is a sync.Settings (host/port/rate/mapping); host is
    ignored — the bridge always binds loopback, by design.

    Raises OSError if the port is already bound (stale Blender, another
    app). The caller surfaces it; nothing is left running.
    """
    global _server, _settings
    if _server is not None:
        return

    srv = server.BridgeServer(
        port=settings.port,
        on_connect=lambda c: _events.put(("connect", c, None)),
        on_message=lambda c, m: _events.put(("message", c, m)),
        on_disconnect=lambda c: _events.put(("disconnect", c, None)),
    )
    srv.start()   # raises OSError on port collision, before any state is set

    _server = srv
    _settings = settings
    _set_status("listening", f"ws://127.0.0.1:{settings.port}")
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=0.0)


def stop():
    """Close the server and every client. The web app sees the socket
    drop and stops mirroring on its side; the robot is not ours to
    touch, so there is nothing else to clean up."""
    global _server
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    srv, _server = _server, None
    if srv is not None:
        srv.stop()
    while not _events.empty():
        try:
            _events.get_nowait()
        except queue.Empty:
            break
    _set_status("off")


def _handle_message(client, msg):
    """Process one browser->Blender request, on the main thread."""
    mtype = msg.get("type")
    if mtype == "scene_info":
        client.send_json({
            "type": "scene_info",
            "scene": _scene_payload(bpy.context.scene),
        })
    elif mtype == "bake":
        request_id = msg.get("request_id")
        scene = bpy.context.scene
        props = scene.reachy_mini_link
        mapping = rig.Mapping(head_scale=props.head_scale)
        try:
            move = bake.bake(
                scene, mapping=mapping,
                description=msg.get("description") or "blender timeline",
                frame_start=msg.get("start"),
                frame_end=msg.get("end"),
            )
        except (rig.RigError, ValueError) as exc:
            client.send_json({
                "type": "error", "request_id": request_id, "message": str(exc),
            })
            return
        client.send_json({
            "type": "bake_result", "request_id": request_id, "move": move,
        })


def _tick():
    """Drain browser events, then broadcast one rig frame."""
    srv = _server
    if srv is None:
        return None

    while True:
        try:
            kind, client, msg = _events.get_nowait()
        except queue.Empty:
            break
        if kind == "connect":
            client.send_json({
                "type": "hello",
                "version": PROTOCOL_VERSION,
                "blender": bpy.app.version_string,
                "scene": _scene_payload(bpy.context.scene),
            })
        elif kind == "message":
            try:
                _handle_message(client, msg)
            except Exception as exc:                    # never kill the tick
                import traceback
                traceback.print_exc()
                client.send_json({
                    "type": "error",
                    "request_id": (msg or {}).get("request_id"),
                    "message": f"bridge error: {exc}",
                })
        # disconnects only matter for the status line, updated below

    if srv.client_count() > 0:
        try:
            state = rig.read(bpy.context.evaluated_depsgraph_get(),
                             _settings.mapping)
        except rig.RigError as exc:
            # Rig missing (wrong .blend open). Keep serving hello/scene so
            # the app can tell the user which file to open, but say so.
            _set_status("error", str(exc))
        else:
            scene = bpy.context.scene
            srv.broadcast({
                "type": "frame",
                "head": state.head_flat(),
                "antennas": [float(state.antennas[0]), float(state.antennas[1])],
                "body_yaw": float(state.body_yaw),
                "frame_current": scene.frame_current,
            })
            _set_status("connected",
                        f"{srv.client_count()} client(s) on :{_settings.port}")
    else:
        _set_status("listening", f"ws://127.0.0.1:{_settings.port}")

    return 1.0 / max(1.0, _settings.rate_hz)
