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


if __name__ == "__main__":
    unittest.main()
