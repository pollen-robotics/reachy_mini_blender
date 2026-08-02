"""Upload a baked move to the daemon and start daemon-side playback.

Extracted from play_move.py so the add-on's "Play on Robot" button and
the CLI share one implementation. Must never import bpy: the upload is
pure client.py protocol work, unit-testable outside Blender.

The daemon owns the whole playback - interpolation, the 100 Hz tick,
Stewart IK - so nothing streams frames at it and playback survives the
caller disconnecting.

Two layers here:

  - upload_and_play(): the synchronous protocol sequence, on a caller-
    provided connection. Used by play_move.py.
  - play_async() / cancel_async(): the add-on's worker-thread wrapper.
    It owns its own connection, keeps it open to watch the daemon's
    play_uploaded_move broadcasts, and mirrors everything into `state`
    (a plain dict, same pattern as hub.publish) for the panel to render.
    Blender's UI thread never blocks on the network.
"""

import base64
import json
import threading
import time
import uuid

# The protocol caps a chunk at 16 KiB; stay under it with room for the
# JSON envelope the chunk is wrapped in.
CHUNK = 12 * 1024


def _upload(conn, upload_id, kind, payload_str, start_extra):
    """One upload_{kind}_start / chunk* / finish sequence, in order.

    Chunks must arrive in order; the daemon drops the whole slot
    otherwise.
    """
    chunks = [payload_str[i:i + CHUNK]
              for i in range(0, len(payload_str), CHUNK)]
    conn._send_json({
        "type": f"upload_{kind}_start",
        "upload_id": upload_id,
        "total_chunks": len(chunks),
        **start_extra,
    })
    for i, c in enumerate(chunks):
        conn._send_json({
            "type": f"upload_{kind}_chunk",
            "upload_id": upload_id,
            "chunk_index": i,
            "chunk": c,
        })
    conn._send_json({"type": f"upload_{kind}_finish", "upload_id": upload_id})


def upload_and_play(conn, move, freq=100.0, ease_in=1.0,
                    audio=None, audio_lead_ms=0.0):
    """Upload `move` (and optional OGG `audio` bytes) and request playback.

    Returns the upload id. Fire-and-forget: the caller may disconnect
    once the socket has flushed; the daemon keeps playing. Audio shares
    the move's upload_id; the daemon pairs the two at play time and
    starts its GStreamer playback in lockstep with the motion loop.
    """
    upload_id = str(uuid.uuid4())

    _upload(conn, upload_id, "move", json.dumps(move), {
        "description": move.get("description", ""),
        "estimated_duration_s": float(move["time"][-1]),
    })
    if audio:
        _upload(conn, upload_id, "audio", base64.b64encode(audio).decode(), {
            "encoding": "ogg-base64",
            "description": move.get("description", ""),
        })
    conn._send_json({
        "type": "play_uploaded_move",
        "upload_id": upload_id,
        "play_frequency": freq,
        "initial_goto_duration": ease_in,
        "audio_lead_ms": float(audio_lead_ms),
    })
    return upload_id


# ─── Async playback for the panel ───────────────────────────────────────

# Rendered by the panel. status is one of:
# "idle" | "sending" | "playing" | "done" | "stopped" | "error"
state = {"status": "idle", "detail": None, "upload_id": None}

_lock = threading.Lock()
_ACTIVE = ("sending", "playing")


def play_async(host, port, move, freq=100.0, ease_in=1.0,
               audio=None, on_update=None):
    """Upload and play `move` from a worker thread, tracking progress.

    The connection stays open for the whole playback so the daemon's
    started/finished/cancelled/error broadcasts land in `state`; a Stop
    press reaches the same daemon through its own short-lived socket
    (see cancel_async), so nothing writes to this thread's socket.

    `on_update` fires on the worker thread at every state change; the
    caller schedules its own hop back onto Blender's main thread.
    """
    with _lock:
        if state["status"] in _ACTIVE:
            return
        state.update(status="sending", detail=None, upload_id=None)

    def set_state(**kw):
        with _lock:
            state.update(kw)
        if on_update is not None:
            on_update()

    def run():
        from . import client as client_mod
        conn = client_mod.WSClient()

        outcome = {}                 # terminal daemon event, set by on_msg
        terminal = threading.Event()
        started = threading.Event()
        watched = {"id": None}

        def on_msg(payload):
            try:
                msg = json.loads(payload.decode())
            except ValueError:
                return
            if (msg.get("type") != "play_uploaded_move"
                    or msg.get("upload_id") != watched["id"]):
                return
            if msg.get("started"):
                started.set()
            if msg.get("finished") or msg.get("cancelled") or msg.get("error"):
                outcome.update(msg)
                terminal.set()

        conn.on_message = on_msg
        try:
            conn.connect(host, port)
            # The daemon picks its own body yaw unless told otherwise,
            # which would override the yaw baked into the move.
            conn.set_automatic_body_yaw(False)
            conn.set_torque(True)
            time.sleep(0.3)
            watched["id"] = upload_and_play(conn, move, freq=freq,
                                            ease_in=ease_in, audio=audio)
            set_state(upload_id=watched["id"])

            duration = float(move["time"][-1])
            with_audio = " + audio" if audio else ""
            set_state(status="playing",
                      detail=f"{duration:.1f}s{with_audio}")

            # A silent timeout usually means the daemon rejected the
            # upload: it drops a bad slot without complaining.
            deadline = ease_in + duration + 10.0
            if not terminal.wait(deadline):
                if started.is_set():
                    set_state(status="done")      # events can get lost
                else:
                    set_state(status="error",
                              detail="no playback event from the daemon "
                                     "(move rejected?)")
            elif outcome.get("error"):
                set_state(status="error", detail=str(outcome["error"]))
            elif outcome.get("cancelled"):
                set_state(status="stopped", detail=None)
            else:
                set_state(status="done", detail=None)
        except ConnectionError as exc:
            set_state(status="error", detail=str(exc))
        finally:
            conn.disconnect()

    threading.Thread(target=run, daemon=True, name="reachy-play").start()


def cancel_async(host, port, on_update=None):
    """Ask the daemon to stop the move play_async is tracking.

    Own short-lived connection: cancel_move is idempotent and keyed by
    upload_id, and the playing worker's socket stays single-writer. The
    worker sees the daemon's `cancelled` broadcast and settles `state`.
    """
    with _lock:
        upload_id = state["upload_id"]
    if not upload_id:
        return

    def run():
        from . import client as client_mod
        conn = client_mod.WSClient()
        try:
            conn.connect(host, port)
            conn._send_json({"type": "cancel_move", "upload_id": upload_id})
            time.sleep(0.2)          # let the frame flush before closing
        except ConnectionError as exc:
            with _lock:
                state.update(status="error", detail=f"stop failed: {exc}")
            if on_update is not None:
                on_update()
        finally:
            conn.disconnect()

    threading.Thread(target=run, daemon=True, name="reachy-play-cancel").start()
