#!/usr/bin/env python3
"""Replay a baked Reachy Mini move JSON on the robot (or its simulator).

The counterpart to `bake_move.py`: that writes a move file from the Blender
timeline, this plays one back. Neither needs the other's output format
explained — both speak `RecordedMove` JSON.

Dependency-free on purpose, in the same spirit as the add-on: it uploads the
move to the daemon over the plain `/ws/sdk` WebSocket and asks the daemon to
play it, so no `reachy_mini` SDK install (and no compiled Rust wheels) is
required. It reuses the add-on's own WebSocket client.

The daemon does the whole playback itself — interpolation, the 100 Hz tick,
Stewart IK — so nothing has to stream frames at it. That also means playback
survives this script exiting, which is why `--wait` is the default.

Usage:
  python3 play_move.py moves/look_around.json
  python3 play_move.py moves/look_around.json --host 192.168.1.42
  python3 play_move.py moves/look_around.json --ease-in 1.0 --no-wait

Flags:
  --host HOST     daemon host (default localhost)
  --port PORT     daemon port (default 8000)
  --ease-in SECS  smoothly interpolate to the move's first frame before
                  playing (default 1.0; 0 starts abruptly from wherever the
                  robot happens to be)
  --freq HZ       daemon playback tick rate (default 100)
  --no-wait       return as soon as playback is requested
"""

import argparse
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reachy_mini_link import client  # noqa: E402  (must follow the sys.path edit)

# The protocol caps a chunk at 16 KiB; stay under it with room for the JSON
# envelope the chunk is wrapped in.
CHUNK = 12 * 1024


def upload_and_play(conn, move, freq, ease_in):
    """Upload `move` to the daemon and start playback. Returns the upload id."""
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("move_file")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--ease-in", type=float, default=1.0)
    ap.add_argument("--freq", type=float, default=100.0)
    ap.add_argument("--no-wait", action="store_true")
    a = ap.parse_args()

    try:
        with open(a.move_file) as fh:
            move = json.load(fh)
    except OSError as exc:
        sys.exit(f"ERROR: cannot read {a.move_file}: {exc.strerror}")
    except ValueError as exc:
        sys.exit(f"ERROR: {a.move_file} is not valid JSON: {exc}")
    for key in ("time", "set_target_data"):
        if key not in move:
            sys.exit(f"ERROR: {a.move_file} is not a move file (no {key!r} key)")
    if len(move["time"]) < 2:
        sys.exit(f"ERROR: {a.move_file} has {len(move['time'])} frame(s); "
                 "a playable move needs at least 2")
    duration = float(move["time"][-1])

    # Collect the daemon's playback events so we can report the real outcome
    # rather than assuming the upload worked.
    events = []
    watched = {"id": None}

    class Tap(client.WSClient):
        def _drain(self):
            while not self._stop.is_set():
                sock = self._sock
                if sock is None:
                    return
                try:
                    op, payload = client.read_frame(sock)
                except (client.WSError, OSError):
                    return
                self._last_msg = time.monotonic()
                if op == client.OP_TEXT:
                    try:
                        m = json.loads(payload.decode())
                    except ValueError:
                        continue
                    if (m.get("type") == "play_uploaded_move"
                            and m.get("upload_id") == watched["id"]):
                        events.append(m)
                elif op == client.OP_PING:
                    self._raw_send(payload, client.OP_PONG)

    conn = Tap()
    try:
        conn.connect(a.host, a.port)
    except ConnectionError as exc:
        sys.exit(f"ERROR: {exc}")

    try:
        # The daemon picks its own body yaw unless told otherwise, which would
        # override the yaw recorded in the move.
        conn.set_automatic_body_yaw(False)
        conn.set_torque(True)
        time.sleep(0.3)

        watched["id"] = upload_and_play(conn, move, a.freq, a.ease_in)
        print(f"playing {a.move_file}: {len(move['time'])} frames, "
              f"{duration:.2f}s{'' if not a.ease_in else f' (+{a.ease_in:.1f}s ease-in)'}")

        if a.no_wait:
            time.sleep(0.5)   # let the upload flush before we close the socket
            return

        deadline = time.time() + a.ease_in + duration + 10.0
        started = False
        while time.time() < deadline:
            for m in list(events):
                if m.get("started") and not started:
                    started = True
                    print(f"  started (daemon reports {m.get('duration_s', duration):.2f}s)")
                if m.get("finished"):
                    print("  finished"); return
                if m.get("cancelled"):
                    print("  cancelled"); return
                if m.get("error"):
                    sys.exit(f"ERROR from daemon: {m['error']}")
            time.sleep(0.05)
        # A silent timeout usually means the upload was rejected: the daemon
        # drops a bad slot without complaining, so play finds nothing to play.
        sys.exit("ERROR: timed out waiting for playback events — the daemon may "
                 "have rejected the move (malformed shape?)")
    finally:
        conn.disconnect()


if __name__ == "__main__":
    main()
