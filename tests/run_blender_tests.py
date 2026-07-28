"""Run the Blender-side test suites inside Blender's Python.

Usage:
  blender --background reachy_mini.blend --python tests/run_blender_tests.py
  blender --background reachy_mini.blend --python tests/run_blender_tests.py -- test_rig

Exits non-zero on failure so CI and shell && chains behave.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
names = argv or ["test_rig", "test_bake"]

loader = unittest.TestLoader()
suite = unittest.TestSuite(loader.loadTestsFromName(f"tests.{n}") for n in names)
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
