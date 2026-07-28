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
