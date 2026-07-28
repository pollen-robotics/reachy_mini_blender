"""Validate a baked move file against the robot's own RecordedMove loader.

This is the one test in the suite that proves a file written by
reachy_mini_link.bake actually loads in the SDK's own move player, not just
that it matches the schema we believe RecordedMove wants.

The reachy_mini SDK is not importable from Blender's isolated interpreter
(see reachy_mini_link/client.py's module docstring), so this test is run
under a plain CPython interpreter instead -- and skips cleanly wherever the
SDK is not installed, so the suite still passes on a machine without it:

    python3 -m unittest tests.test_move_roundtrip -v

To actually exercise RecordedMove, run it under the SDK's venv:

    /home/simsim/Pollen/reachy-mini/reachy_mini_env/bin/python \
        -m unittest tests.test_move_roundtrip -v
"""
import json
import os
import unittest

try:
    from reachy_mini.motion.recorded_move import RecordedMove
    _HAVE_SDK = True
except ImportError:
    _HAVE_SDK = False

FIXTURE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "sample_move.json")


@unittest.skipUnless(_HAVE_SDK, "reachy_mini SDK not installed in this interpreter")
class TestMoveRoundtrip(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE) as fh:
            self.move = json.load(fh)

    def test_recorded_move_accepts_the_fixture(self):
        rm = RecordedMove(self.move)
        self.assertEqual(rm.description, self.move["description"])

    def test_duration_is_positive(self):
        rm = RecordedMove(self.move)
        self.assertGreater(rm.duration, 0.0)

    def test_evaluate_at_start(self):
        rm = RecordedMove(self.move)
        head, antennas, body_yaw = rm.evaluate(0.0)
        self.assertEqual(head.shape, (4, 4))
        self.assertEqual(len(antennas), 2)
        self.assertIsInstance(float(body_yaw), float)

    def test_evaluate_mid_interval(self):
        rm = RecordedMove(self.move)
        # Halfway between the first and second recorded timestamps.
        t_mid = self.move["time"][1] / 2.0
        head, antennas, body_yaw = rm.evaluate(t_mid)
        self.assertEqual(head.shape, (4, 4))
        self.assertEqual(len(antennas), 2)
        self.assertIsInstance(float(body_yaw), float)


if __name__ == "__main__":
    unittest.main()
