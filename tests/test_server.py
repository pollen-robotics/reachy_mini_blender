"""Tests for reachy_mini_link.server. Run with system python3, not Blender.

The nicest property of having both halves in the repo: the bridge
server is tested against our own WSClient, so the handshake, masking
direction and framing are exercised end to end without a browser.
"""
import json
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reachy_mini_link import client, server


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class RecordingClient(client.WSClient):
    """WSClient whose drain thread records text payloads instead of
    discarding them."""

    def __init__(self):
        super().__init__()
        self.received = []
        self._recv_lock = threading.Lock()

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
                with self._recv_lock:
                    self.received.append(json.loads(payload.decode()))
            elif op == client.OP_CLOSE:
                return

    def messages(self):
        with self._recv_lock:
            return list(self.received)


class TestBridgeServer(unittest.TestCase):
    def setUp(self):
        self.port = _free_port()
        self.connects = []
        self.messages = []
        self.disconnects = []
        self.srv = server.BridgeServer(
            port=self.port,
            on_connect=lambda c: self.connects.append(c),
            on_message=lambda c, m: self.messages.append((c, m)),
            on_disconnect=lambda c: self.disconnects.append(c),
        )
        self.srv.start()

    def tearDown(self):
        self.srv.stop()

    def _connect(self):
        c = RecordingClient()
        c.connect("127.0.0.1", self.port, path="/")
        self.addCleanup(c.disconnect)
        self.assertTrue(_wait_for(lambda: self.connects))
        return c

    def test_handshake_and_client_count(self):
        c = self._connect()
        self.assertEqual(self.srv.client_count(), 1)
        c.disconnect()
        self.assertTrue(_wait_for(lambda: self.srv.client_count() == 0))
        self.assertTrue(_wait_for(lambda: self.disconnects))

    def test_broadcast_reaches_client(self):
        c = self._connect()
        self.srv.broadcast({"type": "frame", "head": [1.0] * 16})
        self.assertTrue(_wait_for(lambda: c.messages()))
        self.assertEqual(c.messages()[0]["type"], "frame")
        self.assertEqual(len(c.messages()[0]["head"]), 16)

    def test_client_message_reaches_server(self):
        c = self._connect()
        c._send_json({"type": "bake", "request_id": 7, "start": 1, "end": 10})
        self.assertTrue(_wait_for(lambda: self.messages))
        _, msg = self.messages[0]
        self.assertEqual(msg["type"], "bake")
        self.assertEqual(msg["request_id"], 7)

    def test_reply_to_specific_client(self):
        c1 = self._connect()
        c2 = RecordingClient()
        c2.connect("127.0.0.1", self.port, path="/")
        self.addCleanup(c2.disconnect)
        self.assertTrue(_wait_for(lambda: len(self.connects) == 2))

        c2._send_json({"type": "scene_info"})
        self.assertTrue(_wait_for(lambda: self.messages))
        sender, _ = self.messages[0]
        sender.send_json({"type": "scene_info", "scene": {"fps": 24}})

        self.assertTrue(_wait_for(lambda: c2.messages()))
        self.assertEqual(c2.messages()[0]["scene"]["fps"], 24)
        # The other client must NOT have received the targeted reply.
        time.sleep(0.05)
        self.assertEqual(c1.messages(), [])

    def test_large_frame_roundtrip(self):
        # A bake_result for a long timeline easily exceeds 64 KiB, which
        # exercises the 64-bit length path of encode_server_frame.
        c = self._connect()
        big = {"type": "bake_result", "move": {"time": [0.02] * 20000}}
        self.srv.broadcast(big)
        self.assertTrue(_wait_for(lambda: c.messages(), timeout=5.0))
        self.assertEqual(len(c.messages()[0]["move"]["time"]), 20000)

    def test_port_collision_raises(self):
        with self.assertRaises(OSError):
            second = server.BridgeServer(port=self.port)
            second.start()

    def test_non_websocket_request_is_rejected(self):
        raw = socket.create_connection(("127.0.0.1", self.port), timeout=2.0)
        try:
            raw.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            raw.settimeout(2.0)
            data = raw.recv(1024)   # server closes without upgrading
            self.assertEqual(data, b"")
        finally:
            raw.close()
        self.assertEqual(self.srv.client_count(), 0)


if __name__ == "__main__":
    unittest.main()
