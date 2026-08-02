"""Upload a baked move to the daemon and start daemon-side playback.

Extracted from play_move.py so the add-on's "Play on Robot" button and
the CLI share one implementation. Must never import bpy: the upload is
pure client.py protocol work, unit-testable outside Blender.

The daemon owns the whole playback - interpolation, the 100 Hz tick,
Stewart IK - so nothing streams frames at it and playback survives the
caller disconnecting. LAN path (Lite over USB, or a wireless robot on
the same network); the remote path is The Animator web app over WebRTC.
"""

import json
import uuid

# The protocol caps a chunk at 16 KiB; stay under it with room for the
# JSON envelope the chunk is wrapped in.
CHUNK = 12 * 1024


def upload_and_play(conn, move, freq=100.0, ease_in=1.0):
    """Upload `move` over an open WSClient and request playback.

    Returns the upload id. Fire-and-forget: the caller may disconnect
    once the socket has flushed; the daemon keeps playing.
    """
    payload = json.dumps(move)
    chunks = [payload[i:i + CHUNK] for i in range(0, len(payload), CHUNK)]
    upload_id = str(uuid.uuid4())

    conn._send_json({
        "type": "upload_move_start",
        "upload_id": upload_id,
        "total_chunks": len(chunks),
        "description": move.get("description", ""),
        "estimated_duration_s": float(move["time"][-1]),
    })
    # Chunks must arrive in order; the daemon drops the whole slot otherwise.
    for i, c in enumerate(chunks):
        conn._send_json({
            "type": "upload_move_chunk",
            "upload_id": upload_id,
            "chunk_index": i,
            "chunk": c,
        })
    conn._send_json({"type": "upload_move_finish", "upload_id": upload_id})
    conn._send_json({
        "type": "play_uploaded_move",
        "upload_id": upload_id,
        "play_frequency": freq,
        "initial_goto_duration": ease_in,
    })
    return upload_id
