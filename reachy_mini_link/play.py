"""Upload a baked move to the daemon and start daemon-side playback.

Extracted from play_move.py so the add-on's "Play on Robot" button and
the CLI share one implementation. Must never import bpy: the upload is
pure client.py protocol work, unit-testable outside Blender.

The daemon owns the whole playback - interpolation, the 100 Hz tick,
Stewart IK - so nothing streams frames at it and playback survives the
caller disconnecting. LAN path (Lite over USB, or a wireless robot on
the same network); the remote path is The Animator web app over WebRTC.
"""

import base64
import json
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
