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
import json
import os
import socket
import struct
import threading
import time

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
        except WSError as exc:        # must precede OSError — WSError is a subclass
            sock.close()
            self.last_error = str(exc)
            raise
        except OSError as exc:        # genuine socket failure
            sock.close()
            self.last_error = f"handshake failed ({exc})"
            raise WSError(self.last_error) from exc

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
            # shutdown() must come before close(): it is what unblocks the
            # drain thread's blocking recv() (close() alone does not
            # interrupt a recv already in progress on another thread).
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
                try:
                    self._raw_send(payload, OP_PONG)
                except (WSError, OSError) as exc:
                    if not self._stop.is_set():
                        self.last_error = f"pong failed ({exc})"
                    return
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
