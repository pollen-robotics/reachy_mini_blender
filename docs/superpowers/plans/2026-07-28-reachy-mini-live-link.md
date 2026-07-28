# Reachy Mini Live Link Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Blender add-on that live-mirrors the `reachy_mini.blend` rig to a Reachy Mini daemon over WebSocket, and bakes the timeline to the robot's native `RecordedMove` JSON for later replay.

**Architecture:** `rig.read(depsgraph) → RigState` is a pure function with two consumers — `sync.py` streams it over a hand-rolled WebSocket client, `bake.py` writes it to a JSON file. One mapping, two sinks, so a baked move and the live stream cannot drift apart. `client.py` imports no `bpy` (unit-testable outside Blender); `rig.py` never writes to `bpy.data`.

**Tech Stack:** Python 3.13 (Blender 5.1 bundled), `bpy` + `mathutils`, stdlib only (`socket`, `struct`, `hashlib`, `threading`, `json`, `unittest`). No third-party dependencies, nothing vendored, no pip install.

## Global Constraints

- **Spec of record:** `docs/superpowers/specs/2026-07-28-reachy-mini-live-link-design.md`. Rig provenance: `docs/RIG_MAPPING.md`.
- **No third-party dependencies.** Stdlib only. Do not add `websockets`, `numpy`, `scipy`, or `reachy_mini` as imports. Blender 5.1 ships Python 3.13 with numpy but no scipy; the SDK needs compiled Rust wheels and cannot be imported into Blender.
- **`client.py` must never `import bpy`.** It is tested with system `python3`.
- **`rig.py` is read-only.** It must never assign to `bpy.data` or pose channels.
- **Tests use stdlib `unittest`,** never pytest — Blender's bundled Python has no pytest.
- **`__init__.py` must be import-safe:** no `bpy.utils.register_class` or property assignment at module import time; submodule imports go inside `register()`. Tests do `import reachy_mini_link.rig`, which executes `__init__.py` first.
- **Wire format:** `head` is **flat 16 floats, row-major**. **Move-file format:** `head` is a **nested 4×4**. Do not confuse them.
- **Antenna order is `[right, left]`** everywhere.
- **Units:** `body_yaw` and `antennas` in radians; head translation in metres after `× HEAD_TRANSLATION_SCALE`.
- **`HEAD_TRANSLATION_SCALE = 0.4575`** (Blender units → metres).
- **Never send the +0.177 m Z offset.** The robot's kinematics adds it internally; neutral is plain identity.
- **`set_automatic_body_yaw: false` on every connect.** The SDK defaults it `true`, which makes the daemon pick its own body yaw and fight our explicit channel.
- **Do not modify `reachy_mini.blend`.** No bone renames. Bone names live in `rig.Mapping`.
- **Blender binary for all commands:** `/home/simsim/Blender/blender-5.1.0-linux-x64/blender`
- **Daemon for Task 7:** `/home/simsim/Pollen/reachy-mini/reachy_mini_env/bin/reachy-mini-daemon --sim`

## File Structure

| File | Responsibility |
|---|---|
| `reachy_mini_link/__init__.py` | `bl_info`, `register()` / `unregister()`. Import-safe; no side effects. |
| `reachy_mini_link/client.py` | RFC 6455 client + daemon command surface. **No `bpy`.** |
| `reachy_mini_link/rig.py` | `Mapping`, `RigState`, `read(depsgraph, mapping)`. Read-only. |
| `reachy_mini_link/sync.py` | `bpy.app.timers` loop, start/stop, status. |
| `reachy_mini_link/bake.py` | `bake(scene, mapping, description)` → move dict; `write_move()`. |
| `reachy_mini_link/ui.py` | `PropertyGroup`, 4 operators, 1 panel. Only module touching UI state. |
| `bake_move.py` | Headless CLI entry, `export_gltf.py` idiom. |
| `tests/fake_daemon.py` | Threaded WS server test double. |
| `tests/test_client.py` | System-python tests for framing + commands. |
| `tests/test_rig.py` | Blender tests for the DOF mapping. |
| `tests/test_bake.py` | Blender tests for the move-file schema. |
| `tests/run_blender_tests.py` | Runner: puts repo root on `sys.path`, runs the Blender-side suites. |
| `README.md` | Modify: add "Live Link" and "Exporting a move" sections. |

---

### Task 1: WebSocket framing primitives

Pure functions first, no sockets, no threads. This is the piece most worth testing in isolation because a framing bug looks like a daemon bug.

**Files:**
- Create: `reachy_mini_link/client.py`
- Create: `tests/test_client.py`
- Create: `tests/__init__.py` (empty — makes `tests.test_client` and `tests.fake_daemon` importable as a package rather than relying on namespace-package resolution)
- Create: `reachy_mini_link/__init__.py` (minimal, so the package imports)

**Interfaces:**
- Consumes: nothing.
- Produces: `WSError(ConnectionError)`; `accept_key(key: str) -> str`; `encode_frame(payload: bytes, opcode: int = 0x1, mask_key: bytes | None = None) -> bytes`; `read_frame(sock) -> tuple[int, bytes]`; opcode constants `OP_TEXT = 0x1`, `OP_CLOSE = 0x8`, `OP_PING = 0x9`, `OP_PONG = 0xA`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_client.py`:

```python
"""Tests for reachy_mini_link.client. Run with system python3, not Blender."""
import os
import socket
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reachy_mini_link import client


class TestAcceptKey(unittest.TestCase):
    def test_rfc6455_vector(self):
        # The worked example from RFC 6455 section 1.3.
        self.assertEqual(
            client.accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )


class TestEncodeFrame(unittest.TestCase):
    def test_small_payload_is_masked_with_7bit_length(self):
        frame = client.encode_frame(b"hi", mask_key=b"\x00\x00\x00\x00")
        # FIN + text opcode, then MASK bit + length 2, then the zero mask, then
        # the payload XORed with a zero mask (i.e. unchanged).
        self.assertEqual(frame, b"\x81\x82\x00\x00\x00\x00hi")

    def test_mask_is_actually_applied(self):
        frame = client.encode_frame(b"AB", mask_key=b"\x01\x02\x03\x04")
        self.assertEqual(frame[-2:], bytes([0x41 ^ 0x01, 0x42 ^ 0x02]))

    def test_126_bytes_switches_to_16bit_length(self):
        frame = client.encode_frame(b"x" * 126, mask_key=b"\x00\x00\x00\x00")
        self.assertEqual(frame[1] & 0x7F, 126)
        self.assertEqual(struct.unpack(">H", frame[2:4])[0], 126)

    def test_65536_bytes_switches_to_64bit_length(self):
        frame = client.encode_frame(b"x" * 65536, mask_key=b"\x00\x00\x00\x00")
        self.assertEqual(frame[1] & 0x7F, 127)
        self.assertEqual(struct.unpack(">Q", frame[2:10])[0], 65536)


class TestReadFrame(unittest.TestCase):
    def _roundtrip(self, payload, opcode=client.OP_TEXT):
        a, b = socket.socketpair()
        try:
            # Server->client frames are unmasked, so hand-build one.
            n = len(payload)
            head = bytearray([0x80 | opcode])
            if n < 126:
                head.append(n)
            elif n < 65536:
                head.append(126)
                head += struct.pack(">H", n)
            else:
                head.append(127)
                head += struct.pack(">Q", n)
            a.sendall(bytes(head) + payload)
            return client.read_frame(b)
        finally:
            a.close()
            b.close()

    def test_reads_unmasked_text(self):
        self.assertEqual(self._roundtrip(b"hello"), (client.OP_TEXT, b"hello"))

    def test_reads_16bit_length(self):
        op, data = self._roundtrip(b"y" * 300)
        self.assertEqual((op, len(data)), (client.OP_TEXT, 300))

    def test_reads_masked_frame(self):
        a, b = socket.socketpair()
        try:
            a.sendall(client.encode_frame(b"masked", mask_key=b"\x09\x08\x07\x06"))
            self.assertEqual(client.read_frame(b), (client.OP_TEXT, b"masked"))
        finally:
            a.close()
            b.close()

    def test_closed_socket_raises_wserror(self):
        a, b = socket.socketpair()
        a.close()
        with self.assertRaises(client.WSError):
            client.read_frame(b)
        b.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/simsim/Blender/reachy_mini_blender && python3 -m unittest tests.test_client -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'reachy_mini_link'`

- [ ] **Step 3: Write minimal implementation**

Create `reachy_mini_link/__init__.py`:

```python
"""Reachy Mini Live Link — stream the Blender rig to a Reachy Mini daemon.

This module must stay import-safe: no class registration or property
assignment at import time, because the test suite imports submodules
directly. Registration happens in register(), which imports lazily.
"""

bl_info = {
    "name": "Reachy Mini Live Link",
    "author": "Pollen Robotics",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar (N) > Reachy Mini",
    "description": "Stream the rig to a Reachy Mini daemon; bake the timeline to a move.",
    "category": "Animation",
}


def register():
    from . import ui
    ui.register()


def unregister():
    from . import ui
    ui.unregister()
```

Create `reachy_mini_link/client.py`:

```python
"""Minimal WebSocket client for the Reachy Mini daemon's /ws/sdk endpoint.

Deliberately hand-rolled and dependency-free: Blender 5.1 ships Python 3.13
with no `websockets`, and the SDK itself needs compiled Rust wheels that
cannot be installed into Blender's isolated interpreter.

Only what the link needs from RFC 6455 is implemented:
  - the client handshake, with Sec-WebSocket-Accept validated
  - masked text frames out (clients MUST mask; servers MUST NOT)
  - a full frame parser in, because the daemon streams joint_positions at
    ~50 Hz and the socket must be drained or the kernel buffer backs up
  - ping -> pong, so the server does not time us out

This module must never import bpy: it is unit-tested outside Blender.
"""

import base64
import hashlib
import os
import struct

# RFC 6455 section 1.3 magic value.
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WSError(ConnectionError):
    """Handshake or framing failure. Subclasses ConnectionError so callers
    can catch the whole family of connection problems in one except."""


def accept_key(key):
    """Return the Sec-WebSocket-Accept value a server must echo for `key`."""
    digest = hashlib.sha1((key + _GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def encode_frame(payload, opcode=OP_TEXT, mask_key=None):
    """Encode one final, client-masked frame.

    `mask_key` exists so tests can pin the mask; production callers leave it
    None and get a fresh random mask, as the RFC requires.
    """
    if mask_key is None:
        mask_key = os.urandom(4)
    n = len(payload)
    out = bytearray([0x80 | opcode])          # FIN set, no RSV bits
    if n < 126:
        out.append(0x80 | n)                  # MASK set + 7-bit length
    elif n < 65536:
        out.append(0x80 | 126)
        out += struct.pack(">H", n)
    else:
        out.append(0x80 | 127)
        out += struct.pack(">Q", n)
    out += mask_key
    out += bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return bytes(out)


def _recv_exact(sock, n):
    """Read exactly n bytes or raise. recv() may return short reads."""
    chunks = []
    got = 0
    while got < n:
        chunk = sock.recv(n - got)
        if not chunk:
            raise WSError("connection closed by peer")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def read_frame(sock):
    """Read one frame; return (opcode, payload).

    Handles 7/16/64-bit lengths and unmasks if the peer masked (servers
    should not, but tolerating it costs three lines and makes the function
    usable against our own encode_frame in tests).
    """
    b0, b1 = _recv_exact(sock, 2)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    key = _recv_exact(sock, 4) if masked else None
    payload = _recv_exact(sock, n) if n else b""
    if key:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return opcode, payload
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/simsim/Blender/reachy_mini_blender && python3 -m unittest tests.test_client -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
touch tests/__init__.py
git add reachy_mini_link/__init__.py reachy_mini_link/client.py \
        tests/__init__.py tests/test_client.py
git commit -m "feat(client): RFC 6455 framing primitives with RFC test vector"
```

---

### Task 2: WebSocket connection, commands, and liveness

**Files:**
- Modify: `reachy_mini_link/client.py` (append `WSClient`)
- Create: `tests/fake_daemon.py`
- Modify: `tests/test_client.py` (append connection tests)

**Interfaces:**
- Consumes: `encode_frame`, `read_frame`, `accept_key`, `WSError`, `OP_*` from Task 1.
- Produces: `WSClient` with `connect(host, port, path="/ws/sdk", timeout=5.0) -> None`, `disconnect() -> None`, `is_connected() -> bool`, `last_error: str | None`, `send_full_target(head=None, antennas=None, body_yaw=None) -> None`, `send_goto_target(head=None, antennas=None, body_yaw=None, duration=1.0) -> None`, `set_automatic_body_yaw(enabled: bool) -> None`, `set_torque(on: bool) -> None`. `head` arguments are **flat 16-float lists**. `FakeDaemon` with `.port`, `.start()`, `.stop()`, `.received` (list of decoded dicts), `.wait_for(n, timeout)`.

- [ ] **Step 1: Write the failing test**

Create `tests/fake_daemon.py`:

```python
"""A threaded WebSocket server standing in for the Reachy Mini daemon."""
import json
import re
import socket
import threading
import time

from reachy_mini_link import client


class FakeDaemon:
    """Accepts one connection, completes the handshake, records frames.

    Only what the tests need: no concurrency beyond a single client, no
    close handshake niceties.
    """

    def __init__(self, bad_handshake=False):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self.received = []
        self.conn = None
        self._bad_handshake = bad_handshake
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _serve(self):
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        self.conn = conn
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = conn.recv(1024)
                if not chunk:
                    return
                req += chunk
            if self._bad_handshake:
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            key = re.search(rb"Sec-WebSocket-Key:\s*(\S+)", req, re.I).group(1).decode()
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + client.accept_key(key).encode() + b"\r\n\r\n"
            )
            while not self._stop.is_set():
                try:
                    op, payload = client.read_frame(conn)
                except (client.WSError, OSError):
                    return
                if op == client.OP_TEXT:
                    self.received.append(json.loads(payload.decode()))
                elif op == client.OP_CLOSE:
                    return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def send_text(self, obj):
        """Push a server->client text frame (unmasked, as servers must)."""
        payload = json.dumps(obj).encode()
        n = len(payload)
        head = bytearray([0x80 | client.OP_TEXT])
        if n < 126:
            head.append(n)
        else:
            head.append(126)
            head += n.to_bytes(2, "big")
        self.conn.sendall(bytes(head) + payload)

    def send_ping(self):
        self.conn.sendall(bytes([0x80 | client.OP_PING, 0x00]))

    def wait_for(self, count, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.received) >= count:
                return True
            time.sleep(0.005)
        return False

    def stop(self):
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass
        if self.conn:
            try:
                self.conn.close()
            except OSError:
                pass
```

Append to `tests/test_client.py` (above the `if __name__` block):

```python
from tests.fake_daemon import FakeDaemon


class TestWSClient(unittest.TestCase):
    def setUp(self):
        self.daemon = FakeDaemon().start()
        self.c = client.WSClient()

    def tearDown(self):
        self.c.disconnect()
        self.daemon.stop()

    def test_connect_completes_handshake(self):
        self.c.connect("127.0.0.1", self.daemon.port)
        self.assertTrue(self.c.is_connected())
        self.assertIsNone(self.c.last_error)

    def test_connect_to_closed_port_raises_connectionerror(self):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()
        with self.assertRaises(ConnectionError):
            self.c.connect("127.0.0.1", port, timeout=1.0)
        self.assertIsNotNone(self.c.last_error)

    def test_rejected_handshake_raises(self):
        bad = FakeDaemon(bad_handshake=True).start()
        try:
            with self.assertRaises(client.WSError):
                self.c.connect("127.0.0.1", bad.port, timeout=1.0)
        finally:
            bad.stop()

    def test_send_full_target_payload_is_exact(self):
        self.c.connect("127.0.0.1", self.daemon.port)
        head = [1.0, 0.0, 0.0, 0.0,
                0.0, 1.0, 0.0, 0.0,
                0.0, 0.0, 1.0, 0.0,
                0.0, 0.0, 0.0, 1.0]
        self.c.send_full_target(head=head, antennas=[0.25, -0.25], body_yaw=0.5)
        self.assertTrue(self.daemon.wait_for(1))
        self.assertEqual(self.daemon.received[0], {
            "type": "set_full_target",
            "head": head,
            "antennas": [0.25, -0.25],
            "body_yaw": 0.5,
        })

    def test_omitted_fields_are_null(self):
        self.c.connect("127.0.0.1", self.daemon.port)
        self.c.send_full_target(body_yaw=0.1)
        self.assertTrue(self.daemon.wait_for(1))
        msg = self.daemon.received[0]
        self.assertIsNone(msg["head"])
        self.assertIsNone(msg["antennas"])
        self.assertEqual(msg["body_yaw"], 0.1)

    def test_automatic_body_yaw_and_torque_commands(self):
        self.c.connect("127.0.0.1", self.daemon.port)
        self.c.set_automatic_body_yaw(False)
        self.c.set_torque(True)
        self.assertTrue(self.daemon.wait_for(2))
        self.assertEqual(self.daemon.received[0],
                         {"type": "set_automatic_body_yaw", "enabled": False})
        self.assertEqual(self.daemon.received[1],
                         {"type": "set_torque", "on": True, "ids": None})

    def test_goto_target_carries_duration(self):
        self.c.connect("127.0.0.1", self.daemon.port)
        self.c.send_goto_target(body_yaw=0.2, duration=1.5)
        self.assertTrue(self.daemon.wait_for(1))
        msg = self.daemon.received[0]
        self.assertEqual(msg["type"], "goto_target")
        self.assertEqual(msg["duration"], 1.5)

    def test_ping_is_answered_with_pong(self):
        self.c.connect("127.0.0.1", self.daemon.port)
        self.daemon.send_ping()
        # A pong is a control frame, not JSON, so assert via liveness instead:
        # the client must still be usable and connected afterwards.
        time.sleep(0.1)
        self.c.send_full_target(body_yaw=0.0)
        self.assertTrue(self.daemon.wait_for(1))
        self.assertTrue(self.c.is_connected())

    def test_server_message_refreshes_liveness(self):
        self.c.connect("127.0.0.1", self.daemon.port)
        self.c._last_msg = 0.0                     # simulate a long silence
        self.assertFalse(self.c.is_connected())
        self.daemon.send_text({"type": "joint_positions"})
        time.sleep(0.1)
        self.assertTrue(self.c.is_connected())

    def test_send_after_disconnect_raises(self):
        self.c.connect("127.0.0.1", self.daemon.port)
        self.c.disconnect()
        with self.assertRaises(ConnectionError):
            self.c.send_full_target(body_yaw=0.0)
```

Add `import time` to the imports at the top of `tests/test_client.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/simsim/Blender/reachy_mini_blender && python3 -m unittest tests.test_client -v`
Expected: FAIL — `AttributeError: module 'reachy_mini_link.client' has no attribute 'WSClient'`

- [ ] **Step 3: Write minimal implementation**

Append to `reachy_mini_link/client.py`. **Move these four imports up into the
existing import block at the top of the file** rather than leaving them
mid-module — the file must end up with one import block:

```python
import json
import socket
import threading
import time

# No server message for longer than this while connected means the link is
# gone. The daemon streams joint_positions at ~50 Hz, so silence is a real
# signal rather than an absence of news.
LIVENESS_TIMEOUT = 1.0


class WSClient:
    """A send-mostly WebSocket client with a background drain thread.

    The drain thread exists for three reasons: the daemon streams state at
    ~50 Hz and would otherwise fill the socket buffer; pings must be
    answered; and the arrival time of the last message is our only signal
    that the link is still alive.
    """

    def __init__(self):
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._last_msg = 0.0
        self.last_error = None

    # -- lifecycle -------------------------------------------------------

    def connect(self, host, port, path="/ws/sdk", timeout=5.0):
        """Open the socket and complete the handshake.

        Raises WSError (a ConnectionError) on any failure, after recording
        the reason in last_error for the UI to display.
        """
        self.disconnect()
        self.last_error = None
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            self.last_error = f"cannot reach {host}:{port} ({exc})"
            raise WSError(self.last_error) from exc

        try:
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall(
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n".encode()
            )
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(1024)
                if not chunk:
                    raise WSError("server closed during handshake")
                head += chunk
            status = head.split(b"\r\n", 1)[0]
            if b"101" not in status:
                raise WSError(f"handshake rejected: {status.decode(errors='replace')}")
            expected = accept_key(key).encode()
            if expected not in head:
                raise WSError("bad Sec-WebSocket-Accept")
        except OSError as exc:
            sock.close()
            self.last_error = f"handshake failed ({exc})"
            raise WSError(self.last_error) from exc
        except WSError as exc:
            sock.close()
            self.last_error = str(exc)
            raise

        # Blocking recv in the drain thread; the timeout only guarded connect.
        sock.settimeout(None)
        self._sock = sock
        self._stop.clear()
        self._last_msg = time.monotonic()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def disconnect(self):
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def is_connected(self):
        return (
            self._sock is not None
            and (time.monotonic() - self._last_msg) < LIVENESS_TIMEOUT
        )

    # -- receive ---------------------------------------------------------

    def _drain(self):
        """Read and discard server frames; answer pings; track liveness.

        Never touches bpy — it runs off the main thread.
        """
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                op, payload = read_frame(sock)
            except (WSError, OSError) as exc:
                if not self._stop.is_set():
                    self.last_error = f"connection lost ({exc})"
                return
            self._last_msg = time.monotonic()
            if op == OP_PING:
                self._raw_send(payload, OP_PONG)
            elif op == OP_CLOSE:
                self.last_error = "server closed the connection"
                return

    # -- send ------------------------------------------------------------

    def _raw_send(self, payload, opcode=OP_TEXT):
        sock = self._sock
        if sock is None:
            raise WSError("not connected")
        with self._send_lock:
            try:
                sock.sendall(encode_frame(payload, opcode))
            except OSError as exc:
                self.last_error = f"send failed ({exc})"
                raise WSError(self.last_error) from exc

    def _send_json(self, obj):
        self._raw_send(json.dumps(obj).encode(), OP_TEXT)

    # -- daemon commands -------------------------------------------------

    def send_full_target(self, head=None, antennas=None, body_yaw=None):
        """Immediate target, no interpolation. `head` is flat 16 row-major."""
        self._send_json({
            "type": "set_full_target",
            "head": head,
            "antennas": antennas,
            "body_yaw": body_yaw,
        })

    def send_goto_target(self, head=None, antennas=None, body_yaw=None, duration=1.0):
        """Interpolated target — used once on start to ease in from the
        robot's current pose, so the first streamed frame is not a snap."""
        self._send_json({
            "type": "goto_target",
            "head": head,
            "antennas": antennas,
            "body_yaw": body_yaw,
            "duration": duration,
        })

    def set_automatic_body_yaw(self, enabled):
        """Must be sent False before streaming: the SDK defaults it True,
        which lets the daemon pick its own yaw and fight our channel."""
        self._send_json({"type": "set_automatic_body_yaw", "enabled": bool(enabled)})

    def set_torque(self, on, ids=None):
        self._send_json({"type": "set_torque", "on": bool(on), "ids": ids})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/simsim/Blender/reachy_mini_blender && python3 -m unittest tests.test_client -v`
Expected: PASS, 19 tests.

- [ ] **Step 5: Commit**

```bash
git add reachy_mini_link/client.py tests/fake_daemon.py tests/test_client.py
git commit -m "feat(client): connection, daemon commands, drain thread and liveness"
```

---

### Task 3: Rig reading

The crux of the add-on. Every constant here traces to `docs/RIG_MAPPING.md`.

**Files:**
- Create: `reachy_mini_link/rig.py`
- Create: `tests/test_rig.py`
- Create: `tests/run_blender_tests.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `HEAD_TRANSLATION_SCALE = 0.4575`; `RigError(RuntimeError)`; `Mapping` (frozen dataclass, all fields defaulted); `RigState` with fields `head: Matrix`, `body_yaw: float`, `antennas: tuple[float, float]` and methods `head_flat() -> list[float]` (16, row-major) and `head_nested() -> list[list[float]]` (4×4); `read(depsgraph, mapping=None) -> RigState`.

- [ ] **Step 1: Write the failing test**

Create `tests/run_blender_tests.py`:

```python
"""Run the Blender-side test suites inside Blender's Python.

Usage:
  blender --background reachy_mini.blend --python tests/run_blender_tests.py
  blender --background reachy_mini.blend --python tests/run_blender_tests.py -- test_rig

Exits non-zero on failure so CI and shell && chains behave.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
names = argv or ["test_rig", "test_bake"]

loader = unittest.TestLoader()
suite = unittest.TestSuite(loader.loadTestsFromName(f"tests.{n}") for n in names)
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
```

Create `tests/test_rig.py`:

```python
"""Tests for reachy_mini_link.rig. Must run inside Blender.

  blender --background reachy_mini.blend --python tests/run_blender_tests.py -- test_rig

Expected values come from docs/RIG_MAPPING.md and are verified against the
rig's driver constants, so these tests fail loudly if the rig is recalibrated.
"""
import math
import unittest

import bpy
from mathutils import Matrix

from reachy_mini_link import rig

# The shipped rig's rest pose is exactly zero on every bone, so neutral
# assertions only need to absorb float error from the matrix inverse.
TOL = 1e-6


def reset_pose():
    arm = bpy.data.objects["Armature"]
    for pb in arm.pose.bones:
        pb.location = (0.0, 0.0, 0.0)
        pb.rotation_euler = (0.0, 0.0, 0.0)
        pb.scale = (1.0, 1.0, 1.0)
    bpy.context.view_layer.update()


def read():
    bpy.context.view_layer.update()
    return rig.read(bpy.context.evaluated_depsgraph_get())


class TestNeutral(unittest.TestCase):
    def setUp(self):
        reset_pose()

    def test_head_is_identity_at_rest(self):
        state = read()
        for i in range(4):
            for j in range(4):
                self.assertAlmostEqual(
                    state.head[i][j], 1.0 if i == j else 0.0, delta=TOL,
                    msg=f"head[{i}][{j}]")

    def test_body_yaw_and_antennas_are_zero_at_rest(self):
        state = read()
        self.assertAlmostEqual(state.body_yaw, 0.0, delta=TOL)
        self.assertAlmostEqual(state.antennas[0], 0.0, delta=TOL)
        self.assertAlmostEqual(state.antennas[1], 0.0, delta=TOL)


class TestBodyYaw(unittest.TestCase):
    def setUp(self):
        reset_pose()

    def test_slider_drives_body_yaw_by_driver_constant(self):
        # Slider.Rot.Core LOC_Y * 25.386 -> Core.rotation_euler[1].
        bpy.data.objects["Armature"].pose.bones["Slider.Rot.Core"].location[1] = 0.05
        self.assertAlmostEqual(read().body_yaw, 1.269300, delta=1e-5)

    def test_full_slider_travel_is_the_robot_limit(self):
        bpy.data.objects["Armature"].pose.bones["Slider.Rot.Core"].location[1] = 0.11
        # MJCF yaw_body range is +/-2.792526 rad (+/-160 deg).
        self.assertAlmostEqual(read().body_yaw, 2.792460, delta=1e-4)
        self.assertAlmostEqual(math.degrees(read().body_yaw), 160.0, delta=0.01)


class TestAntennas(unittest.TestCase):
    def setUp(self):
        reset_pose()

    def test_left_slider_drives_left_antenna(self):
        bpy.data.objects["Armature"].pose.bones["Slider.Rot.Antenna.L"].location[1] = 0.05
        state = read()
        self.assertAlmostEqual(state.antennas[1], 1.428000, delta=1e-5)  # left
        self.assertAlmostEqual(state.antennas[0], 0.0, delta=TOL)        # right

    def test_right_slider_drives_right_antenna(self):
        bpy.data.objects["Armature"].pose.bones["Slider.Rot.Antenna.R"].location[1] = 0.05
        state = read()
        self.assertAlmostEqual(state.antennas[0], 1.428000, delta=1e-5)
        self.assertAlmostEqual(state.antennas[1], 0.0, delta=TOL)

    def test_fk_control_adds_to_the_slider(self):
        # .002 (slider-driven) and .003 (FK) are collinear with identical rest
        # frames, so the robot's single hinge value is their sum.
        arm = bpy.data.objects["Armature"]
        arm.pose.bones["Slider.Rot.Antenna.L"].location[1] = 0.05
        arm.pose.bones["Antenna.L.003"].rotation_euler[2] = 0.25
        self.assertAlmostEqual(read().antennas[1], 1.428000 + 0.25, delta=1e-5)


class TestHeadPose(unittest.TestCase):
    def setUp(self):
        reset_pose()

    def test_pure_translation_scales_to_metres(self):
        bpy.data.objects["Armature"].pose.bones["Head.001"].location = (0.0, 0.0, 0.02)
        state = read()
        t = state.head.translation
        # Head.001's local Z is world -Y (matrix_local: localZ = (0,-1,0)), so
        # assert on magnitude rather than guessing which world axis it lands on.
        self.assertAlmostEqual(t.length, 0.02 * rig.HEAD_TRANSLATION_SCALE, delta=1e-6)

    def test_rotation_and_translation_are_assembled_independently(self):
        # The robot wants R about the neutral head origin plus a translation
        # offset from it. The naive composed form M_cur @ M_rest^-1 yields
        # p_cur - R*p_rest instead, which differs once both are non-zero.
        # This test pins the correct form by checking the translation is the
        # scaled origin delta and nothing else.
        arm = bpy.data.objects["Armature"]
        pb = arm.pose.bones["Head.001"]
        pb.location = (0.0, 0.0, 0.03)
        pb.rotation_euler = (0.0, 0.0, math.radians(20.0))
        bpy.context.view_layer.update()
        dg = bpy.context.evaluated_depsgraph_get()
        state = rig.read(dg)

        m = rig.Mapping()
        ev = arm.evaluated_get(dg)
        base_pose = ev.pose.bones[m.base_bone].matrix
        head_pose = ev.pose.bones[m.head_bone].matrix
        base_rest = ev.data.bones[m.base_bone].matrix_local
        head_rest = ev.data.bones[m.head_bone].matrix_local
        cur = base_pose.inverted() @ head_pose
        rst = base_rest.inverted() @ head_rest

        expected_t = (cur.translation - rst.translation) * rig.HEAD_TRANSLATION_SCALE
        self.assertAlmostEqual((state.head.translation - expected_t).length, 0.0,
                               delta=1e-9)

        naive = (cur @ rst.inverted()).translation * rig.HEAD_TRANSLATION_SCALE
        self.assertGreater((naive - expected_t).length, 1e-5,
                           "test is vacuous unless the two forms actually differ")

    def test_rotation_is_recoverable(self):
        pb = bpy.data.objects["Armature"].pose.bones["Head.001"]
        pb.rotation_euler = (0.0, 0.0, math.radians(15.0))
        state = read()
        angle = state.head.to_3x3().to_quaternion().angle
        self.assertAlmostEqual(math.degrees(angle), 15.0, delta=0.01)


class TestSerialisation(unittest.TestCase):
    def setUp(self):
        reset_pose()

    def test_head_flat_is_16_floats_row_major(self):
        flat = read().head_flat()
        self.assertEqual(len(flat), 16)
        self.assertEqual([round(v, 6) for v in flat],
                         [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])

    def test_head_flat_row_major_order_is_not_column_major(self):
        # Put a known translation in so the last column is non-trivial, then
        # assert the translation lands in row-major positions 3, 7, 11.
        m = Matrix.Translation((1.0, 2.0, 3.0))
        state = rig.RigState(head=m, body_yaw=0.0, antennas=(0.0, 0.0))
        self.assertEqual(state.head_flat()[3], 1.0)
        self.assertEqual(state.head_flat()[7], 2.0)
        self.assertEqual(state.head_flat()[11], 3.0)

    def test_head_nested_is_4x4(self):
        nested = read().head_nested()
        self.assertEqual(len(nested), 4)
        self.assertTrue(all(len(row) == 4 for row in nested))


class TestErrors(unittest.TestCase):
    def test_missing_bone_raises_rigerror_naming_the_bone(self):
        reset_pose()
        bad = rig.Mapping(head_bone="NoSuchBone")
        with self.assertRaises(rig.RigError) as ctx:
            rig.read(bpy.context.evaluated_depsgraph_get(), bad)
        self.assertIn("NoSuchBone", str(ctx.exception))

    def test_missing_armature_raises_rigerror(self):
        bad = rig.Mapping(armature="NoSuchArmature")
        with self.assertRaises(rig.RigError):
            rig.read(bpy.context.evaluated_depsgraph_get(), bad)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd /home/simsim/Blender/reachy_mini_blender && \
/home/simsim/Blender/blender-5.1.0-linux-x64/blender --background reachy_mini.blend \
  --python tests/run_blender_tests.py -- test_rig
```
Expected: FAIL — `ModuleNotFoundError: No module named 'reachy_mini_link.rig'`

- [ ] **Step 3: Write minimal implementation**

Create `reachy_mini_link/rig.py`:

```python
"""Read the robot's DOFs out of the Blender rig.

Read-only: this module never assigns to bpy.data or to a pose channel.

Every bone name, axis index and constant here is documented with its
provenance in docs/RIG_MAPPING.md. The short version:

  - head     Head.001 relative to Base, rotation and translation assembled
             independently, translation scaled to metres
  - body_yaw Core.rotation_euler[1] -- local Y, which for that bone is world
             +Z, so it really is yaw
  - antennas Antenna.{R,L}.002 + .003 local Z summed, because the two bones
             are collinear continuations of the robot's single hinge
"""

from dataclasses import dataclass
from typing import Tuple

import bpy
from mathutils import Matrix

# Blender units -> metres. The rig is ~2.19x oversized despite the scene
# being set METRIC/METERS. Two independent anchors agree to 1.6%: the neutral
# head origin (0.177 m / 0.3869 BU = 0.45748) and the overall body width
# (0.160 m / 0.3554 BU = 0.45020). See docs/RIG_MAPPING.md.
HEAD_TRANSLATION_SCALE = 0.4575


class RigError(RuntimeError):
    """A mapped bone or object is missing — usually a renamed rig."""


@dataclass(frozen=True)
class Mapping:
    """Which rig element supplies each robot DOF.

    Defaults match the shipped reachy_mini.blend. Overriding these is how a
    renamed rig is accommodated without a code change.

    The *_sign fields are determined once by the simulator verification step
    and then left alone; they are constants, not user controls. The MJCF gives
    the two antennas different mounting quaternions, so their signs are
    established empirically rather than assumed equal.
    """

    armature: str = "Armature"
    base_bone: str = "Base"
    head_bone: str = "Head.001"

    body_yaw_bone: str = "Core"
    body_yaw_axis: int = 1          # local Y; for Core this is world +Z
    body_yaw_sign: float = 1.0

    antenna_r_bones: Tuple[str, ...] = ("Antenna.R.002", "Antenna.R.003")
    antenna_l_bones: Tuple[str, ...] = ("Antenna.L.002", "Antenna.L.003")
    antenna_axis: int = 2           # local Z, perpendicular to the shaft
    antenna_r_sign: float = 1.0
    antenna_l_sign: float = 1.0

    head_scale: float = HEAD_TRANSLATION_SCALE


@dataclass
class RigState:
    """One sample of the robot's DOFs, in the robot's own conventions."""

    head: Matrix                    # 4x4, base frame, metres, identity at rest
    body_yaw: float                 # radians
    antennas: Tuple[float, float]   # (right, left) radians

    def head_flat(self):
        """Row-major 16 floats — the /ws/sdk wire format."""
        return [float(v) for row in self.head for v in row]

    def head_nested(self):
        """Nested 4x4 — the RecordedMove move-file format."""
        return [[float(v) for v in row] for row in self.head]


def _pose_bone(arm, name):
    pb = arm.pose.bones.get(name)
    if pb is None:
        raise RigError(f"pose bone {name!r} not found in armature {arm.name!r}")
    return pb


def _rest_bone(arm, name):
    b = arm.data.bones.get(name)
    if b is None:
        raise RigError(f"rest bone {name!r} not found in armature {arm.name!r}")
    return b


def _sum_axis(arm, names, axis):
    return sum(_pose_bone(arm, n).rotation_euler[axis] for n in names)


def read(depsgraph, mapping=None):
    """Sample the rig. Returns a RigState.

    `depsgraph` must be current — call bpy.context.evaluated_depsgraph_get()
    after any pose change or scene.frame_set(). Raises RigError if the mapping
    does not match the rig.
    """
    m = mapping or Mapping()

    orig = bpy.data.objects.get(m.armature)
    if orig is None:
        raise RigError(f"armature object {m.armature!r} not found")
    arm = orig.evaluated_get(depsgraph)

    # -- head: Head.001 relative to Base -------------------------------
    cur = _pose_bone(arm, m.base_bone).matrix.inverted() @ _pose_bone(arm, m.head_bone).matrix
    rst = _rest_bone(arm, m.base_bone).matrix_local.inverted() @ _rest_bone(arm, m.head_bone).matrix_local

    # Assembled independently on purpose. The robot defines the head pose as a
    # rotation about the neutral head origin plus a translation offset from it,
    # so the translation must be the plain origin delta. The composed form
    # cur @ rst.inverted() would give p_cur - R*p_rest instead.
    rotation = cur.to_3x3() @ rst.to_3x3().inverted()
    translation = (cur.translation - rst.translation) * m.head_scale
    head = Matrix.Translation(translation) @ rotation.to_4x4()

    # -- body yaw ------------------------------------------------------
    body_yaw = m.body_yaw_sign * _pose_bone(arm, m.body_yaw_bone).rotation_euler[m.body_yaw_axis]

    # -- antennas, [right, left] ---------------------------------------
    right = m.antenna_r_sign * _sum_axis(arm, m.antenna_r_bones, m.antenna_axis)
    left = m.antenna_l_sign * _sum_axis(arm, m.antenna_l_bones, m.antenna_axis)

    return RigState(head=head, body_yaw=body_yaw, antennas=(right, left))
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd /home/simsim/Blender/reachy_mini_blender && \
/home/simsim/Blender/blender-5.1.0-linux-x64/blender --background reachy_mini.blend \
  --python tests/run_blender_tests.py -- test_rig
```
Expected: PASS, 15 tests, exit code 0.

If `test_pure_translation_scales_to_metres` fails on magnitude, do **not** adjust `HEAD_TRANSLATION_SCALE` to make it pass — the scale is confirmed in Task 7 against the simulator. Report the discrepancy instead.

- [ ] **Step 5: Commit**

```bash
git add reachy_mini_link/rig.py tests/test_rig.py tests/run_blender_tests.py
git commit -m "feat(rig): read head/body_yaw/antennas from the evaluated depsgraph"
```

---

### Task 4: Bake the timeline to a move file

**Files:**
- Create: `reachy_mini_link/bake.py`
- Create: `bake_move.py`
- Create: `tests/test_bake.py`

**Interfaces:**
- Consumes: `rig.read`, `rig.Mapping`, `rig.RigState` from Task 3.
- Produces: `bake(scene, mapping=None, description="", frame_start=None, frame_end=None) -> dict` returning `{"description": str, "time": list[float], "set_target_data": list[dict]}`; `write_move(path, move) -> None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_bake.py`:

```python
"""Tests for reachy_mini_link.bake. Must run inside Blender.

  blender --background reachy_mini.blend --python tests/run_blender_tests.py -- test_bake
"""
import json
import math
import os
import tempfile
import unittest

import bpy

from reachy_mini_link import bake


def reset_pose():
    arm = bpy.data.objects["Armature"]
    for pb in arm.pose.bones:
        pb.location = (0.0, 0.0, 0.0)
        pb.rotation_euler = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()


class TestBakeSchema(unittest.TestCase):
    def setUp(self):
        reset_pose()
        self.scene = bpy.context.scene
        self.scene.render.fps = 24
        self.scene.render.fps_base = 1.0

    def test_keys_match_recordedmove(self):
        move = bake.bake(self.scene, description="unit", frame_start=1, frame_end=3)
        self.assertEqual(set(move), {"description", "time", "set_target_data"})
        self.assertEqual(move["description"], "unit")

    def test_one_sample_per_frame(self):
        move = bake.bake(self.scene, frame_start=1, frame_end=3)
        self.assertEqual(len(move["time"]), 3)
        self.assertEqual(len(move["set_target_data"]), 3)

    def test_timestamps_come_from_scene_fps(self):
        move = bake.bake(self.scene, frame_start=1, frame_end=3)
        self.assertAlmostEqual(move["time"][0], 0.0, delta=1e-9)
        self.assertAlmostEqual(move["time"][1], 1.0 / 24.0, delta=1e-9)
        self.assertAlmostEqual(move["time"][2], 2.0 / 24.0, delta=1e-9)

    def test_timestamps_honour_fps_base(self):
        self.scene.render.fps = 24
        self.scene.render.fps_base = 1.001          # 23.976 fps
        move = bake.bake(self.scene, frame_start=1, frame_end=2)
        self.assertAlmostEqual(move["time"][1], 1.0 / (24.0 / 1.001), delta=1e-9)

    def test_sample_fields_and_head_is_nested_4x4(self):
        move = bake.bake(self.scene, frame_start=1, frame_end=2)
        sample = move["set_target_data"][0]
        self.assertEqual(set(sample), {"head", "antennas", "body_yaw"})
        self.assertEqual(len(sample["head"]), 4)
        self.assertTrue(all(len(row) == 4 for row in sample["head"]))
        self.assertEqual(len(sample["antennas"]), 2)
        self.assertIsInstance(sample["body_yaw"], float)

    def test_head_is_nested_not_flat(self):
        # Guards the wire-vs-file format asymmetry.
        sample = bake.bake(self.scene, frame_start=1, frame_end=1)["set_target_data"][0]
        self.assertIsInstance(sample["head"][0], list)

    def test_defaults_to_scene_frame_range(self):
        self.scene.frame_start = 5
        self.scene.frame_end = 9
        move = bake.bake(self.scene)
        self.assertEqual(len(move["time"]), 5)

    def test_original_frame_is_restored(self):
        self.scene.frame_set(42)
        bake.bake(self.scene, frame_start=1, frame_end=3)
        self.assertEqual(self.scene.frame_current, 42)


class TestBakeCapturesAnimation(unittest.TestCase):
    def setUp(self):
        reset_pose()
        self.scene = bpy.context.scene
        self.scene.render.fps = 24
        self.scene.render.fps_base = 1.0
        arm = bpy.data.objects["Armature"]
        slider = arm.pose.bones["Slider.Rot.Core"]
        slider.location[1] = 0.0
        slider.keyframe_insert("location", index=1, frame=1)
        slider.location[1] = 0.05
        slider.keyframe_insert("location", index=1, frame=3)

    def tearDown(self):
        arm = bpy.data.objects["Armature"]
        if arm.animation_data and arm.animation_data.action:
            arm.animation_data_clear()
        reset_pose()

    def test_body_yaw_changes_across_frames(self):
        move = bake.bake(self.scene, frame_start=1, frame_end=3)
        yaws = [s["body_yaw"] for s in move["set_target_data"]]
        self.assertAlmostEqual(yaws[0], 0.0, delta=1e-6)
        self.assertAlmostEqual(yaws[2], 1.269300, delta=1e-5)
        self.assertGreater(yaws[2], yaws[0])


class TestWriteMove(unittest.TestCase):
    def setUp(self):
        reset_pose()

    def test_writes_loadable_json(self):
        move = bake.bake(bpy.context.scene, description="io", frame_start=1, frame_end=2)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "io.json")
            bake.write_move(path, move)
            with open(path) as fh:
                loaded = json.load(fh)
        self.assertEqual(loaded["description"], "io")
        self.assertEqual(len(loaded["time"]), 2)

    def test_is_json_serialisable_with_plain_floats(self):
        move = bake.bake(bpy.context.scene, frame_start=1, frame_end=2)
        json.dumps(move)   # must not raise on mathutils types


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd /home/simsim/Blender/reachy_mini_blender && \
/home/simsim/Blender/blender-5.1.0-linux-x64/blender --background reachy_mini.blend \
  --python tests/run_blender_tests.py -- test_bake
```
Expected: FAIL — `ModuleNotFoundError: No module named 'reachy_mini_link.bake'`

- [ ] **Step 3: Write minimal implementation**

Create `reachy_mini_link/bake.py`:

```python
"""Bake the Blender timeline into the robot's RecordedMove JSON.

No robot and no daemon are involved: this reads the rig frame by frame and
writes a file the SDK can replay. The shape is what
reachy_mini.motion.recorded_move.RecordedMove expects:

    {"description": str,
     "time": [seconds, ...],
     "set_target_data": [{"head": <nested 4x4>, "antennas": [r, l],
                          "body_yaw": float}, ...]}

RecordedMove derives dt as (time[-1] - time[0]) / len(time), so sampling must
be uniform — stepping whole frames satisfies that.

Note the format asymmetry against the live path: the move file stores head as
a nested 4x4, while the /ws/sdk wire format wants a flat 16.
"""

import json
import os

import bpy

from . import rig


def bake(scene, mapping=None, description="", frame_start=None, frame_end=None):
    """Sample every frame in the range and return a move dict.

    Restores scene.frame_current before returning, so baking is invisible to
    the rest of the session.
    """
    start = scene.frame_start if frame_start is None else frame_start
    end = scene.frame_end if frame_end is None else frame_end

    # Blender stores a rational frame rate; fps_base is 1.001 for 23.976 etc.
    fps = scene.render.fps / scene.render.fps_base

    original_frame = scene.frame_current
    times = []
    samples = []
    try:
        for frame in range(start, end + 1):
            scene.frame_set(frame)
            # frame_set triggers evaluation, but fetch the depsgraph after it
            # so the read is unambiguously against the new frame.
            state = rig.read(bpy.context.evaluated_depsgraph_get(), mapping)
            times.append((frame - start) / fps)
            samples.append({
                "head": state.head_nested(),
                "antennas": [float(state.antennas[0]), float(state.antennas[1])],
                "body_yaw": float(state.body_yaw),
            })
    finally:
        scene.frame_set(original_frame)

    return {"description": description, "time": times, "set_target_data": samples}


def write_move(path, move):
    """Write a move dict as JSON, creating parent directories as needed.

    Resolves Blender's // relative-to-blend prefix, so the UI's default
    "//moves/untitled.json" lands next to the .blend rather than in the
    process's working directory.
    """
    if isinstance(path, str) and path.startswith("//"):
        path = bpy.path.abspath(path)
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(move, fh, indent=1)
```

Create `bake_move.py` at the repo root:

```python
#!/usr/bin/env python3
"""Headless CLI: bake the rig's timeline to a Reachy Mini move JSON.

Blender's --python takes a file, not a module, so this file is the entry
point, in the same idiom as export_gltf.py.

Usage:
  blender --background reachy_mini.blend --python bake_move.py -- \
      --out moves/wave_hello.json --description wave_hello

Flags (after --):
  --out PATH          output path (default: <blend dir>/move.json)
  --description TEXT  move description (default: output filename stem)
  --start N           first frame (default: scene frame_start)
  --end N             last frame  (default: scene frame_end)
"""
import os
import sys

import bpy

# The add-on package lives next to this script; make it importable when run
# via --python, which does not put the script's directory on sys.path.
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from reachy_mini_link import bake  # noqa: E402  (must follow the sys.path edit)

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def flag(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


out = flag("--out", os.path.join(os.path.dirname(bpy.data.filepath), "move.json"))
description = flag("--description", os.path.splitext(os.path.basename(out))[0])
start = flag("--start")
end = flag("--end")

scene = bpy.context.scene
move = bake.bake(
    scene,
    description=description,
    frame_start=int(start) if start else None,
    frame_end=int(end) if end else None,
)
bake.write_move(out, move)
print(f"DONE -> {out}  ({len(move['time'])} frames, "
      f"{move['time'][-1]:.3f}s @ {scene.render.fps / scene.render.fps_base:g} fps)")
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd /home/simsim/Blender/reachy_mini_blender && \
/home/simsim/Blender/blender-5.1.0-linux-x64/blender --background reachy_mini.blend \
  --python tests/run_blender_tests.py -- test_bake
```
Expected: PASS, 11 tests.

Then exercise the CLI end to end:
```bash
cd /home/simsim/Blender/reachy_mini_blender && \
/home/simsim/Blender/blender-5.1.0-linux-x64/blender --background reachy_mini.blend \
  --python bake_move.py -- --out /tmp/mini_move.json --start 1 --end 5 && \
python3 -c "
import json; m = json.load(open('/tmp/mini_move.json'))
assert set(m) == {'description','time','set_target_data'}
assert len(m['time']) == 5, m['time']
assert len(m['set_target_data'][0]['head']) == 4
print('CLI move OK:', m['description'], len(m['time']), 'frames')"
```
Expected: `DONE -> /tmp/mini_move.json (5 frames, ...)` then `CLI move OK: mini_move 5 frames`.

- [ ] **Step 5: Commit**

```bash
git add reachy_mini_link/bake.py bake_move.py tests/test_bake.py
git commit -m "feat(bake): timeline -> RecordedMove JSON, plus headless CLI"
```

---

### Task 5: Sync loop

**Files:**
- Create: `reachy_mini_link/sync.py`

**Interfaces:**
- Consumes: `client.WSClient`, `client.WSError`, `rig.read`, `rig.Mapping`, `rig.RigError`.
- Produces: `Settings` (dataclass: `host: str`, `port: int`, `rate_hz: float`, `mapping: rig.Mapping`, `ease_in_duration: float`); `start(settings) -> None` (raises `ConnectionError`); `stop() -> None`; `is_running() -> bool`; `get_status() -> tuple[str, str]` where phase is one of `"idle"`, `"syncing"`, `"error"`.

- [ ] **Step 1: Write the failing test**

There is no unit test for this task: the loop is a `bpy.app.timers` callback whose behaviour is the interaction of Blender's timer scheduler, a live socket, and viewport state. Testing it in isolation would mean mocking all three and would assert only that the mocks were called. It is covered instead by the Task 7 acceptance run against the simulator, which exercises the real loop.

The two pieces of logic in this task that *can* be tested in isolation already are — `rig.read` in Task 3 and the client in Tasks 1–2.

- [ ] **Step 2: Verify the module imports cleanly**

This is the check that stands in for a unit test at this step.

Run:
```bash
cd /home/simsim/Blender/reachy_mini_blender && \
/home/simsim/Blender/blender-5.1.0-linux-x64/blender --background --python-expr "
import sys; sys.path.insert(0, '.')
from reachy_mini_link import sync
print('phase:', sync.get_status())
print('running:', sync.is_running())
assert sync.get_status()[0] == 'idle'
assert sync.is_running() is False
print('IMPORT OK')"
```
Expected: before implementation, FAIL with `ModuleNotFoundError: No module named 'reachy_mini_link.sync'`.

- [ ] **Step 3: Write minimal implementation**

Create `reachy_mini_link/sync.py`:

```python
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

    Raises ConnectionError if the daemon cannot be reached; the caller is
    expected to surface last_error. Leaves state clean on failure.
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
    executing timer is not something to rely on. _tick calls _teardown and
    returns None; stop() — always called from an operator, never from inside
    the tick — unregisters and then calls _teardown.
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

    return 1.0 / max(1.0, _settings.rate_hz)
```

- [ ] **Step 4: Verify it imports and reports idle**

Run the same command as Step 2.
Expected: `phase: ('idle', '')`, `running: False`, `IMPORT OK`.

- [ ] **Step 5: Commit**

```bash
git add reachy_mini_link/sync.py
git commit -m "feat(sync): 50 Hz timer loop with ease-in and status reporting"
```

---

### Task 6: UI — panel, operators, registration

**Files:**
- Create: `reachy_mini_link/ui.py`
- Modify: `reachy_mini_link/__init__.py` (no change needed if Task 1's version was written as specified — verify it delegates to `ui.register`)

**Interfaces:**
- Consumes: `sync.Settings`, `sync.start`, `sync.stop`, `sync.is_running`, `sync.get_status`, `bake.bake`, `bake.write_move`, `rig.Mapping`, `rig.HEAD_TRANSLATION_SCALE`.
- Produces: `register()` / `unregister()`; `ReachyMiniLinkProps` on `bpy.types.Scene.reachy_mini_link`; operators `reachy_mini.sync_start`, `reachy_mini.sync_stop`, `reachy_mini.send_test_pose`, `reachy_mini.export_move`; panel `REACHY_MINI_PT_link`.

- [ ] **Step 1: Write the failing test**

Register/unregister correctness is the thing worth checking here, and it is checkable: registering twice in one session must not raise, which is the defect that made the reachy2 add-on require a Blender restart per edit.

Run this as the test (it fails before implementation):
```bash
cd /home/simsim/Blender/reachy_mini_blender && \
/home/simsim/Blender/blender-5.1.0-linux-x64/blender --background --python-expr "
import sys; sys.path.insert(0, '.')
import bpy, reachy_mini_link as addon

addon.register()
assert hasattr(bpy.types.Scene, 'reachy_mini_link'), 'scene props missing'
for op in ('sync_start', 'sync_stop', 'send_test_pose', 'export_move'):
    assert op in dir(bpy.ops.reachy_mini), 'missing operator: ' + op
p = bpy.context.scene.reachy_mini_link
assert p.host == 'localhost' and p.port == 8000 and p.rate_hz == 50.0
assert abs(p.head_scale - 0.4575) < 1e-9

addon.unregister()
assert not hasattr(bpy.types.Scene, 'reachy_mini_link'), 'props leaked'

# The reload cycle that the reachy2 add-on could not survive.
addon.register(); addon.unregister(); addon.register(); addon.unregister()
print('REGISTER CYCLE OK')"
```
Expected before implementation: FAIL — `ModuleNotFoundError: No module named 'reachy_mini_link.ui'`

- [ ] **Step 2: Run it to confirm the failure**

Run the command from Step 1.
Expected: the `ModuleNotFoundError` above.

- [ ] **Step 3: Write minimal implementation**

Create `reachy_mini_link/ui.py`:

```python
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
```

- [ ] **Step 4: Run the registration test to verify it passes**

Run the command from Step 1.
Expected: `REGISTER CYCLE OK`.

Then confirm the whole non-Blender suite still passes:
```bash
cd /home/simsim/Blender/reachy_mini_blender && python3 -m unittest tests.test_client -v
```
Expected: PASS, 19 tests.

- [ ] **Step 5: Commit**

```bash
git add reachy_mini_link/ui.py reachy_mini_link/__init__.py
git commit -m "feat(ui): sidebar panel, operators, clean register/unregister cycle"
```

---

### Task 7: Simulator verification, sign/scale confirmation, README

The acceptance task. Everything before this was verified against a fake or a static rig; this is the first contact with a real daemon, and it is where `HEAD_TRANSLATION_SCALE` and the antenna signs stop being inferences.

**Files:**
- Modify: `reachy_mini_link/rig.py` (only the `Mapping` sign/scale defaults, only if the simulator disagrees)
- Modify: `README.md`
- Modify: `docs/RIG_MAPPING.md` (record confirmed signs)

**Interfaces:**
- Consumes: everything from Tasks 1–6.
- Produces: no new API. Confirmed constants and user-facing docs.

- [ ] **Step 1: Start the simulator and confirm the transport against it**

```bash
/home/simsim/Pollen/reachy-mini/reachy_mini_env/bin/reachy-mini-daemon --sim &
sleep 15
cd /home/simsim/Blender/reachy_mini_blender && python3 -c "
import sys, time; sys.path.insert(0, '.')
from reachy_mini_link import client
c = client.WSClient()
c.connect('localhost', 8000)
time.sleep(0.5)
assert c.is_connected(), 'no server traffic within 1s: ' + str(c.last_error)
c.set_automatic_body_yaw(False)
c.set_torque(True)
c.send_full_target(body_yaw=0.0)
time.sleep(0.5)
assert c.is_connected(), c.last_error
c.disconnect()
print('LIVE DAEMON OK')"
```
Expected: `LIVE DAEMON OK`. If the daemon needs longer to boot, raise the `sleep`.

If this fails with a handshake error, the daemon's endpoint or port differs from the spec — stop and report rather than editing the client to match a guess.

- [ ] **Step 2: Confirm signs and head scale with the test sequence**

With the simulator's MuJoCo viewer visible, run:
```bash
cd /home/simsim/Blender/reachy_mini_blender && \
/home/simsim/Blender/blender-5.1.0-linux-x64/blender reachy_mini.blend --python-expr "
import sys; sys.path.insert(0, '.')
import bpy
import reachy_mini_link as addon
addon.register()
bpy.context.scene.reachy_mini_link.host = 'localhost'
bpy.ops.reachy_mini.send_test_pose()"
```

Watch the viewer and record, for each step, whether the robot moved as the label says:

| Step | Expected | Observed |
|---|---|---|
| `+Z 20mm` | head rises ~2 cm | |
| `+X 20mm` | head moves forward (nose direction) ~2 cm | |
| `right antenna +45` | **right** antenna sweeps | |
| `left antenna +45` | **left** antenna sweeps | |
| `body yaw +30` | body rotates CCW seen from above | |

- If an antenna moves on the wrong side, swap `antenna_r_bones` and `antenna_l_bones` in `rig.Mapping`.
- If an antenna sweeps the wrong direction, set that side's `antenna_*_sign` to `-1.0`.
- If body yaw goes clockwise, set `body_yaw_sign = -1.0`.
- If `+X` moves the head sideways rather than forward, stop and report: that indicates the rig's base axes do not match the robot's X-forward convention, which is a spec-level issue, not a sign flip.

- [ ] **Step 3: Confirm the head translation scale numerically**

The `+Z 20mm` step commands exactly 0.02 m. Read back what the daemon reports and compare:

```bash
cd /home/simsim/Blender/reachy_mini_blender && python3 -c "
import sys, json, time; sys.path.insert(0, '.')
from reachy_mini_link import client

# Subclass to capture server frames instead of discarding them.
seen = []
class Tap(client.WSClient):
    def _drain(self):
        while not self._stop.is_set():
            sock = self._sock
            if sock is None: return
            try: op, payload = client.read_frame(sock)
            except Exception: return
            self._last_msg = time.monotonic()
            if op == client.OP_TEXT:
                try: seen.append(json.loads(payload.decode()))
                except Exception: pass
            elif op == client.OP_PING: self._raw_send(payload, client.OP_PONG)

c = Tap(); c.connect('localhost', 8000)
c.set_automatic_body_yaw(False); c.set_torque(True)
h = [1,0,0,0, 0,1,0,0, 0,0,1,0.02, 0,0,0,1]   # +20 mm in Z, row-major
c.send_goto_target(head=h, duration=2.0); time.sleep(3.0)
poses = [m for m in seen if m.get('type') == 'head_pose']
print('head_pose messages:', len(poses))
if poses: print('last head_pose:', poses[-1])
c.disconnect()"
```

Expected: the reported head Z settles near `0.02` (the daemon removes its internal 0.177 offset on output, per `placo_kinematics.py:490`). If the reported value is consistently a constant factor off the commanded 0.02, that factor multiplies into `HEAD_TRANSLATION_SCALE`.

Note this step validates the **wire units**, not the rig scale. To check the rig scale, pose `Head.001` in Blender by a known amount, read `rig.read().head.translation`, and confirm the robot's reported head translation matches it. A mismatch means `HEAD_TRANSLATION_SCALE` needs the correcting factor applied to `0.4575`.

- [ ] **Step 4: Record what was confirmed**

Append to `docs/RIG_MAPPING.md`:

```markdown
## Simulator-confirmed values

Confirmed against `reachy-mini-daemon --sim` on <DATE — fill in with the real date>:

| Channel | Sign | Note |
|---|---|---|
| `body_yaw` | <+1 / -1> | <CCW from above as commanded / inverted> |
| `antennas[0]` (right) | <+1 / -1> | <side and direction as labelled / swapped> |
| `antennas[1]` (left) | <+1 / -1> | <side and direction as labelled / swapped> |
| `HEAD_TRANSLATION_SCALE` | <value> | <0.4575 confirmed / corrected from 0.4575 to X because Y> |
```

Replace every `<...>` with the observed result. Leaving a placeholder here defeats the purpose of the file.

- [ ] **Step 5: Write the README sections**

Append to `README.md`:

```markdown
## Live Link add-on

`reachy_mini_link/` streams this rig to a Reachy Mini in real time, and bakes
the timeline to the robot's move format.

### Install

Blender > Edit > Preferences > Add-ons > install from disk, and pick a zip of
the `reachy_mini_link/` folder:

```bash
zip -r reachy_mini_link.zip reachy_mini_link
```

No dependencies and no `pip install` — the add-on speaks the daemon's
WebSocket protocol directly, using only the Python standard library. (The
`reachy_mini` SDK itself cannot be installed into Blender's bundled Python:
it needs compiled Rust wheels and a different Python version.)

### Live mirror

1. Start a daemon. For a simulator: `reachy-mini-daemon --sim`
2. In the 3D viewport press **N** and open the **Reachy Mini** tab.
3. Check the host and port, then hit **Start Sync**.

The robot now follows the rig — hand-pose a control, scrub the timeline, or
play it. The controls to drive are `Head.001` (the head), `Slider.Rot.Core`
(body yaw), and `Slider.Rot.Antenna.L/R` plus `Antenna.L/R.003` (antennas).
See `docs/RIG_MAPPING.md`.

**Verify in the simulator before using a real robot.** **Send test pose**
walks a known sequence so you can confirm the axes behave as labelled.

### Exporting a move

**Export Move** bakes the frame range to `RecordedMove` JSON, replayable on
the robot without Blender. Headless equivalent:

```bash
blender --background reachy_mini.blend --python bake_move.py -- \
    --out moves/wave_hello.json --description wave_hello
```

Flags: `--out PATH`, `--description TEXT`, `--start N`, `--end N`.

### Tests

```bash
# transport, no Blender needed
python3 -m unittest tests.test_client -v

# rig mapping and bake, inside Blender
blender --background reachy_mini.blend --python tests/run_blender_tests.py
```
```

- [ ] **Step 6: Run the full suite and commit**

```bash
cd /home/simsim/Blender/reachy_mini_blender && \
python3 -m unittest tests.test_client -v && \
/home/simsim/Blender/blender-5.1.0-linux-x64/blender --background reachy_mini.blend \
  --python tests/run_blender_tests.py
```
Expected: both suites pass, exit code 0.

```bash
kill %1   # stop the simulator daemon
git add README.md docs/RIG_MAPPING.md reachy_mini_link/rig.py
git commit -m "docs: README usage and simulator-confirmed signs/scale"
```

---

## Verification summary

| Deliverable | Verified by |
|---|---|
| WS framing | `tests.test_client` — RFC 6455 accept vector, 7/16/64-bit lengths, masking, socketpair round-trip |
| WS connection + commands | `tests.test_client` — `FakeDaemon`, exact JSON payloads, liveness, ping/pong |
| Rig mapping | `tests/test_rig.py` — driver constants `1.269300` / `1.428000` rad, `±160°` limit, independent R/t assembly, row-major order |
| Move file | `tests/test_bake.py` — schema, fps and `fps_base` timing, nested 4×4, frame restore, keyframed animation captured |
| Sync loop | Task 7 acceptance run against `--sim` |
| Register cycle | Task 6 Step 1 — register/unregister four times without error |
| Signs and scale | Task 7 Steps 2–3 against the MuJoCo viewer |

## Deliberately out of scope

Per the spec: reading robot state back into Blender (one-way only), velocity/slew limiting, tested remote-host operation, and attaching a `.wav` to a baked move.
