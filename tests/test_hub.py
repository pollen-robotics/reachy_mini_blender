"""Tests for reachy_mini_link.hub (auth ladder only; whoami needs network)."""

import os
import pathlib
import tempfile
import unittest
from unittest import mock

from reachy_mini_link import hub


class FindTokenTest(unittest.TestCase):
    def setUp(self):
        # Isolate from the developer's real environment and token files.
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("HF_TOKEN", None)
        self._tmp = tempfile.TemporaryDirectory()
        missing = pathlib.Path(self._tmp.name) / "nope"
        self._paths = mock.patch.object(hub, "_TOKEN_PATHS", (missing,))
        self._paths.start()

    def tearDown(self):
        self._paths.stop()
        self._env.stop()
        self._tmp.cleanup()

    def test_env_var_wins(self):
        os.environ["HF_TOKEN"] = "hf_env"
        token, source = hub.find_token(prefs_token="hf_prefs")
        self.assertEqual(token, "hf_env")
        self.assertEqual(source, "HF_TOKEN env")

    def test_cli_file_beats_prefs(self):
        path = pathlib.Path(self._tmp.name) / "token"
        path.write_text("hf_cli\n")
        with mock.patch.object(hub, "_TOKEN_PATHS", (path,)):
            token, source = hub.find_token(prefs_token="hf_prefs")
        self.assertEqual(token, "hf_cli")
        self.assertEqual(source, "hf CLI login")

    def test_prefs_fallback(self):
        token, source = hub.find_token(prefs_token="  hf_prefs  ")
        self.assertEqual(token, "hf_prefs")
        self.assertEqual(source, "add-on preferences")

    def test_nothing_found(self):
        self.assertEqual(hub.find_token(), (None, None))

    def test_empty_cli_file_is_skipped(self):
        path = pathlib.Path(self._tmp.name) / "token"
        path.write_text("\n")
        with mock.patch.object(hub, "_TOKEN_PATHS", (path,)):
            token, source = hub.find_token(prefs_token="hf_prefs")
        self.assertEqual(token, "hf_prefs")


class CanonicalizeTest(unittest.TestCase):
    """Mirrors Marionette's canonicalizeMotion so datasets round-trip."""

    @staticmethod
    def _move(times):
        return {"description": "t",
                "time": list(times),
                "set_target_data": [
                    {"body_yaw": 0.123456789} for _ in times]}

    def test_sub_rate_bake_is_kept_whole(self):
        # A 24 fps bake is below 50 Hz: every frame survives.
        times = [i / 24 for i in range(48)]
        out = hub.canonicalize(self._move(times))
        self.assertEqual(len(out["time"]), 48)

    def test_100hz_decimates_to_50(self):
        times = [i / 100 for i in range(200)]
        out = hub.canonicalize(self._move(times))
        self.assertAlmostEqual(len(out["time"]), 101, delta=2)
        # The last frame always survives so the duration is intact.
        self.assertEqual(out["time"][-1], round(times[-1], 6))

    def test_floats_rounded_to_6dp(self):
        out = hub.canonicalize(self._move([0.0, 0.1234567, 1.0]))
        self.assertEqual(out["time"][1], 0.123457)
        self.assertEqual(out["set_target_data"][0]["body_yaw"], 0.123457)

    def test_idempotent(self):
        once = hub.canonicalize(self._move([i / 100 for i in range(200)]))
        self.assertEqual(hub.canonicalize(once), once)

    def test_original_untouched(self):
        move = self._move([0.0, 0.1234567])
        hub.canonicalize(move)
        self.assertEqual(move["time"][1], 0.1234567)


if __name__ == "__main__":
    unittest.main()
