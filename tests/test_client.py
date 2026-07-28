"""Tests for reachy_mini_link.client. Run with system python3, not Blender."""
import os
import socket
import struct
import sys
import time
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


if __name__ == "__main__":
    unittest.main()
