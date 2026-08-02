"""Tests for reachy_mini_link.play's upload protocol framing."""

import base64
import json
import unittest

from reachy_mini_link import play


class FakeConn:
    def __init__(self):
        self.sent = []

    def _send_json(self, msg):
        self.sent.append(msg)


MOVE = {"description": "demo", "time": [0.0, 1.0],
        "set_target_data": [{"body_yaw": 0.0}, {"body_yaw": 0.1}]}


class UploadAndPlayTest(unittest.TestCase):
    def test_motion_only_sequence(self):
        conn = FakeConn()
        upload_id = play.upload_and_play(conn, MOVE)
        types = [m["type"] for m in conn.sent]
        self.assertEqual(types, ["upload_move_start", "upload_move_chunk",
                                 "upload_move_finish", "play_uploaded_move"])
        self.assertTrue(all(m["upload_id"] == upload_id for m in conn.sent))
        # The daemon reassembles the chunks into the exact move JSON.
        payload = "".join(m["chunk"] for m in conn.sent
                          if m["type"] == "upload_move_chunk")
        self.assertEqual(json.loads(payload), MOVE)

    def test_audio_shares_upload_id_and_precedes_play(self):
        conn = FakeConn()
        audio = bytes(range(256)) * 100
        upload_id = play.upload_and_play(conn, MOVE, audio=audio)
        types = [m["type"] for m in conn.sent]
        self.assertEqual(types.index("upload_audio_start"),
                         types.index("upload_move_finish") + 1)
        self.assertLess(types.index("upload_audio_finish"),
                        types.index("play_uploaded_move"))
        start = next(m for m in conn.sent
                     if m["type"] == "upload_audio_start")
        self.assertEqual(start["upload_id"], upload_id)
        self.assertEqual(start["encoding"], "ogg-base64")
        payload = "".join(m["chunk"] for m in conn.sent
                          if m["type"] == "upload_audio_chunk")
        self.assertEqual(base64.b64decode(payload), audio)

    def test_chunks_respect_protocol_cap(self):
        conn = FakeConn()
        play.upload_and_play(conn, MOVE, audio=b"x" * 100_000)
        for m in conn.sent:
            if "chunk" in m:
                self.assertLessEqual(len(m["chunk"]), 16 * 1024)

    def test_chunk_indices_are_ordered(self):
        conn = FakeConn()
        play.upload_and_play(conn, MOVE, audio=b"x" * 100_000)
        indices = [m["chunk_index"] for m in conn.sent
                   if m["type"] == "upload_audio_chunk"]
        self.assertEqual(indices, list(range(len(indices))))


if __name__ == "__main__":
    unittest.main()
