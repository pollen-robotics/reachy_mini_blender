"""Tests for reachy_mini_link.hub_import (pure parts, no network/bpy)."""

import unittest

from reachy_mini_link import hub_import


def _tree(*paths):
    return [{"type": "file", "path": p} for p in paths]


class MovesFromTreeTest(unittest.TestCase):
    def test_pairs_json_with_wav_sidecar(self):
        moves = hub_import.moves_from_tree(
            "user/set", _tree("data/wave.json", "data/wave.wav"))
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["path"], "data/wave.json")
        self.assertEqual(moves[0]["audio_path"], "data/wave.wav")
        self.assertEqual(moves[0]["name"], "wave")
        self.assertEqual(moves[0]["label"], "wave \u00b7 user/set")

    def test_wav_preferred_over_ogg(self):
        moves = hub_import.moves_from_tree(
            "u/d", _tree("data/a.json", "data/a.ogg", "data/a.wav"))
        self.assertEqual(moves[0]["audio_path"], "data/a.wav")

    def test_ogg_only_sidecar_found(self):
        moves = hub_import.moves_from_tree(
            "u/d", _tree("data/a.json", "data/a.ogg"))
        self.assertEqual(moves[0]["audio_path"], "data/a.ogg")

    def test_no_sidecar(self):
        moves = hub_import.moves_from_tree("u/d", _tree("data/a.json"))
        self.assertIsNone(moves[0]["audio_path"])

    def test_ignores_non_json_and_directories(self):
        entries = _tree("data/a.json", "data/readme.txt", "data/b.wav")
        entries.append({"type": "directory", "path": "data/sub"})
        moves = hub_import.moves_from_tree("u/d", entries)
        self.assertEqual([m["name"] for m in moves], ["a"])

    def test_keys_sidecar_next_to_move(self):
        moves = hub_import.moves_from_tree(
            "u/d", _tree("data/a.json", "data/a.keys.json"))
        self.assertEqual([m["name"] for m in moves], ["a"])
        self.assertEqual(moves[0]["keys_path"], "data/a.keys.json")

    def test_keys_sidecar_under_sources(self):
        moves = hub_import.moves_from_tree(
            "u/d", _tree("data/a.json", "sources/a.keys.json"))
        self.assertEqual(moves[0]["keys_path"], "sources/a.keys.json")

    def test_keys_sidecar_is_not_a_move(self):
        moves = hub_import.moves_from_tree(
            "u/d", _tree("data/a.keys.json"))
        self.assertEqual(moves, [])

    def test_deep_json_is_not_a_move(self):
        moves = hub_import.moves_from_tree(
            "u/d", _tree("sources/a.keys.json", "configs/x.json"))
        self.assertEqual(moves, [])

    def test_root_layout_move(self):
        moves = hub_import.moves_from_tree(
            "u/d", _tree("amazed1.json", "amazed1.ogg", "README.md"))
        self.assertEqual(moves[0]["path"], "amazed1.json")
        self.assertEqual(moves[0]["audio_path"], "amazed1.ogg")


class SortMovesTest(unittest.TestCase):
    def test_pollen_first_then_alphabetical(self):
        moves = [
            {"repo_id": "zz/set", "label": "aaa \u00b7 zz/set"},
            {"repo_id": "pollen-robotics/lib", "label": "zzz \u00b7 pollen-robotics/lib"},
            {"repo_id": "aa/set", "label": "bbb \u00b7 aa/set"},
        ]
        out = hub_import.sort_moves(moves)
        self.assertEqual([m["repo_id"] for m in out],
                         ["pollen-robotics/lib", "zz/set", "aa/set"])


class RepoMovesTest(unittest.TestCase):
    def test_single_recursive_call_covers_both_layouts(self):
        calls = []

        def get(url):
            calls.append(url)
            return _tree("data/a.json", "root_move.json",
                         "sources/a.keys.json")
        moves = hub_import.repo_moves("u/d", get)
        self.assertEqual(len(calls), 1)
        self.assertIn("recursive=true", calls[0])
        self.assertEqual([m["path"] for m in moves],
                         ["data/a.json", "root_move.json"])
        self.assertEqual(moves[0]["keys_path"], "sources/a.keys.json")

    def test_network_error_gives_empty(self):
        def get(url):
            raise OSError("404")
        self.assertEqual(hub_import.repo_moves("u/d", get), [])


class GroupMovesTest(unittest.TestCase):
    def test_groups_by_repo_pollen_first(self):
        moves = [
            {"repo_id": "zz/set", "name": "b"},
            {"repo_id": "pollen-robotics/lib", "name": "z"},
            {"repo_id": "zz/set", "name": "a"},
        ]
        groups = hub_import.group_moves(moves)
        self.assertEqual([g[0] for g in groups],
                         ["pollen-robotics/lib", "zz/set"])
        self.assertEqual([m["name"] for m in groups[1][1]], ["a", "b"])

    def test_empty(self):
        self.assertEqual(hub_import.group_moves([]), [])


class CachePathTest(unittest.TestCase):
    def test_local_dir_flattens_repo_id(self):
        d = hub_import.local_dir("someone/their-moves")
        self.assertEqual(d.name, "someone~their-moves")
        self.assertEqual(d.parent, hub_import.cache_dir())

    def test_cache_dir_is_per_user(self):
        import pathlib
        self.assertTrue(str(hub_import.cache_dir()).startswith(
            str(pathlib.Path.home())))


if __name__ == "__main__":
    unittest.main()
