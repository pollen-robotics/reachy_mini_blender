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

    def __init__(self, bad_handshake=False, bad_accept=False):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self.received = []
        self.control = []
        self.conn = None
        self._bad_handshake = bad_handshake
        self._bad_accept = bad_accept
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
            accept_value = client.accept_key(key)
            if self._bad_accept:
                accept_value = "AAAAAAAAAAAAAAAAAAAAAA=="  # Wrong value
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept_value.encode() + b"\r\n\r\n"
            )
            while not self._stop.is_set():
                try:
                    op, payload = client.read_frame(conn)
                except (client.WSError, OSError):
                    return
                if op == client.OP_TEXT:
                    self.received.append(json.loads(payload.decode()))
                elif op == client.OP_PONG:
                    self.control.append("pong")
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
