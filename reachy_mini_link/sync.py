"""Timer-driven live mirror: rig.read() -> client.send_full_target().

Threading rule: bpy is touched only from the main thread. That is why the
loop is a bpy.app.timers callback rather than a worker thread — each tick
reads the evaluated pose on the main thread and hands a plain list of floats
to a non-blocking send. The client's drain thread never touches bpy.

Module-level state rather than scene properties, because a live socket is not
something to serialise into a .blend.
"""

from dataclasses import dataclass, field

import bpy

from . import client, rig


@dataclass
class Settings:
    """Everything the loop needs, snapshotted at start.

    Rate and host changes take effect on the next Start rather than mid-run,
    which keeps the tick free of property lookups.
    """

    host: str = "localhost"
    port: int = 8000
    rate_hz: float = 50.0
    mapping: rig.Mapping = field(default_factory=rig.Mapping)
    ease_in_duration: float = 1.0


_client = None
_settings = None
_running = False
_phase = "idle"          # "idle" | "syncing" | "error"
_message = ""


def get_status():
    """(phase, message) for the UI to render. Never raises."""
    return _phase, _message


def is_running():
    return _running


def _set_status(phase, message=""):
    global _phase, _message
    _phase, _message = phase, message


def _read_state():
    return rig.read(bpy.context.evaluated_depsgraph_get(), _settings.mapping)


def start(settings):
    """Connect, arm the robot, ease in, then begin streaming.

    Raises:
        client.WSError: a ConnectionError subclass, if the daemon cannot be
            reached or the handshake fails.
        rig.RigError: if the rig mapping does not match the armature — for
            example a renamed bone — which surfaces on the ease-in read.

    Leaves module state clean and the socket closed on either failure; the
    caller should surface `last_error` / get_status().
    """
    global _client, _settings, _running
    if _running:
        return

    _settings = settings
    conn = client.WSClient()
    try:
        conn.connect(settings.host, settings.port)
        # The SDK defaults automatic_body_yaw True, which would have the
        # daemon choose its own yaw and fight our explicit channel.
        conn.set_automatic_body_yaw(False)
        conn.set_torque(True)

        # Ease in with one interpolated move. set_target has no
        # interpolation, so without this the first streamed frame snaps the
        # robot from wherever it is to whatever pose Blender is holding.
        state = _read_state()
        conn.send_goto_target(
            head=state.head_flat(),
            antennas=[state.antennas[0], state.antennas[1]],
            body_yaw=state.body_yaw,
            duration=settings.ease_in_duration,
        )
    except (client.WSError, rig.RigError) as exc:
        conn.disconnect()
        _set_status("error", str(exc))
        raise

    _client = conn
    _running = True
    _set_status("syncing", f"{settings.host}:{settings.port}")
    if not bpy.app.timers.is_registered(_tick):
        # Start after the ease-in so the first streamed frame does not fight
        # the interpolation.
        bpy.app.timers.register(_tick, first_interval=settings.ease_in_duration)


def _teardown():
    """Drop the connection and clear running state, WITHOUT touching timers.

    Split out from stop() because _tick must never unregister itself: a timer
    callback stops by returning None, and calling unregister on the currently
    executing timer is not something to rely on. _tick calls _fail which calls
    _teardown and returns None; stop() — always called from an operator, never
    from inside the tick — unregisters and then calls _teardown.
    """
    global _client, _running
    _running = False
    if _client is not None:
        _client.disconnect()
        _client = None


def stop():
    """Stop streaming and disconnect, leaving the robot holding its pose.

    Torque is deliberately left on: cutting it would drop the head.
    """
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    _teardown()
    if _phase != "error":
        _set_status("idle")


def _fail(message):
    """Record an error, drop the connection, and stop the timer by returning
    None to Blender. Always `return _fail(...)` from _tick."""
    _set_status("error", message)
    _teardown()
    return None


def _tick():
    """One frame of the mirror. Returns the next delay, or None to stop."""
    if not _running or _client is None:
        return None

    if not _client.is_connected():
        return _fail(_client.last_error or "connection lost")

    try:
        state = _read_state()
    except rig.RigError as exc:
        return _fail(str(exc))
    except Exception as exc:                      # never die silently
        import traceback
        traceback.print_exc()
        return _fail(f"rig read failed: {exc}")

    try:
        _client.send_full_target(
            head=state.head_flat(),
            antennas=[state.antennas[0], state.antennas[1]],
            body_yaw=state.body_yaw,
        )
    except client.WSError as exc:
        return _fail(str(exc))

    # max(1.0, ...) is a divide-by-zero and runaway guard: rates below 1 Hz
    # are clamped to 1 Hz rather than honoured.
    return 1.0 / max(1.0, _settings.rate_hz)
