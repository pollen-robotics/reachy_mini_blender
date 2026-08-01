"""Minimal WebSocket *server* for the local bridge (The Animator web app).

Where client.py dials OUT to a daemon, this module listens on
127.0.0.1 and lets a browser page connect IN. The browser (The
Animator Space) owns the robot connection over WebRTC; Blender only
exposes its rig on the loopback interface. This process never talks
to a robot.

Deliberately dependency-free for the same reason as client.py:
Blender's isolated interpreter. Only what RFC 6455 requires of a
server is implemented:
  - the server side of the handshake (Sec-WebSocket-Accept)
  - unmasked text frames out (servers MUST NOT mask)
  - the client.py frame parser in, which already unmasks

Threading contract: this module must never import bpy. The accept
loop and each client's reader run on daemon threads; incoming
messages are handed to `on_message(client, dict)` FROM THOSE THREADS.
The caller (bridge.py) enqueues them and drains the queue on
Blender's main thread. Outbound sends are serialized per-client with
a lock so the broadcast tick and a bake reply cannot interleave a
frame.
"""

import json
import socket
import struct
import threading

from .client import OP_CLOSE, OP_PING, OP_PONG, OP_TEXT, WSError, accept_key, read_frame


def encode_server_frame(payload, opcode=OP_TEXT):
    """Encode one final, UNMASKED frame (server->client direction)."""
    n = len(payload)
    out = bytearray([0x80 | opcode])          # FIN set, no RSV bits
    if n < 126:
        out.append(n)
    elif n < 65536:
        out.append(126)
        out += struct.pack(">H", n)
    else:
        out.append(127)
        out += struct.pack(">Q", n)
    out += payload
    return bytes(out)


def _read_handshake(sock):
    """Read the client's HTTP upgrade request; return its WS key.

    Raises WSError on anything that is not a well-formed upgrade. We do
    not validate Origin: the socket is bound to 127.0.0.1, so every
    peer is a process on the user's own machine already.
    """
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(1024)
        if not chunk:
            raise WSError("client closed during handshake")
        head += chunk
        if len(head) > 16384:
            raise WSError("handshake request too large")
    text = head.decode("latin-1")
    key = None
    for line in text.split("\r\n"):
        if ":" in line:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-key":
                key = value.strip()
    if not key:
        raise WSError("not a WebSocket upgrade (no Sec-WebSocket-Key)")
    return key


class BridgeClient:
    """One connected browser tab. Send access is lock-serialized."""

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self._send_lock = threading.Lock()
        self.alive = True

    def send_json(self, obj):
        """Send one text frame. Marks the client dead on failure rather
        than raising: senders iterate over many clients and one broken
        tab must not abort the broadcast."""
        payload = json.dumps(obj).encode()
        frame = encode_server_frame(payload)
        with self._send_lock:
            try:
                self.sock.sendall(frame)
                return True
            except OSError:
                self.alive = False
                return False

    def close(self):
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class BridgeServer:
    """Accept loop + per-client readers, all on daemon threads.

    Callbacks (called from server threads, never the main thread):
      on_connect(client)          after a successful handshake
      on_message(client, dict)    per decoded JSON text frame
      on_disconnect(client)       socket closed or errored
    """

    def __init__(self, port=9877, host="127.0.0.1",
                 on_connect=None, on_message=None, on_disconnect=None):
        self.host = host
        self.port = port
        self.on_connect = on_connect
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._clients = []
        self._clients_lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------

    def start(self):
        """Bind and start accepting. Raises OSError if the port is taken
        (surfaced to the UI as 'port already in use')."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
            sock.listen(4)
        except OSError:
            sock.close()
            raise
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()   # unblocks accept()
            except OSError:
                pass
        with self._clients_lock:
            clients, self._clients = self._clients, []
        for c in clients:
            c.close()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    @property
    def running(self):
        return self._sock is not None

    def client_count(self):
        with self._clients_lock:
            return sum(1 for c in self._clients if c.alive)

    # -- outbound --------------------------------------------------------

    def broadcast(self, obj):
        """Send to every live client; reap the dead ones. Safe to call
        from any thread (per-client locks serialize the actual writes)."""
        with self._clients_lock:
            clients = list(self._clients)
        dead = [c for c in clients if not (c.alive and c.send_json(obj))]
        if dead:
            with self._clients_lock:
                self._clients = [c for c in self._clients if c not in dead]
            for c in dead:
                c.close()
                if self.on_disconnect:
                    self.on_disconnect(c)

    # -- accept / read ---------------------------------------------------

    def _accept_loop(self):
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                conn, addr = sock.accept()
            except OSError:
                return   # stop() closed the listener
            threading.Thread(
                target=self._client_session, args=(conn, addr), daemon=True,
            ).start()

    def _client_session(self, conn, addr):
        try:
            conn.settimeout(5.0)
            key = _read_handshake(conn)
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept_key(key).encode() + b"\r\n\r\n"
            )
            conn.settimeout(None)
        except (WSError, OSError):
            try:
                conn.close()
            except OSError:
                pass
            return

        client = BridgeClient(conn, addr)
        with self._clients_lock:
            self._clients.append(client)
        if self.on_connect:
            self.on_connect(client)

        try:
            while not self._stop.is_set() and client.alive:
                op, payload = read_frame(conn)
                if op == OP_TEXT:
                    if self.on_message:
                        try:
                            msg = json.loads(payload.decode())
                        except ValueError:
                            continue   # not our protocol; ignore
                        self.on_message(client, msg)
                elif op == OP_PING:
                    with client._send_lock:
                        conn.sendall(encode_server_frame(payload, OP_PONG))
                elif op == OP_CLOSE:
                    break
        except (WSError, OSError):
            pass
        finally:
            client.close()
            with self._clients_lock:
                if client in self._clients:
                    self._clients.remove(client)
            if self.on_disconnect:
                self.on_disconnect(client)
