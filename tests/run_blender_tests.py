"""Run the Blender-side test suites inside Blender's Python.

Usage:
  blender --background reachy_mini_link/assets/reachy_mini.blend --python tests/run_blender_tests.py
  blender --background reachy_mini_link/assets/reachy_mini.blend --python tests/run_blender_tests.py -- test_rig

Exits non-zero on failure so CI and shell && chains behave.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Blender auto-enables add-ons that were left enabled in the user's saved
# preferences at startup — including any copy of this add-on installed under
# ~/.config/blender/.../scripts/addons/. That import happens before this
# script runs, so reachy_mini_link and its submodules may already be sitting
# in sys.modules, pointing at the installed copy. Putting ROOT at the front
# of sys.path is useless against that: `import reachy_mini_link.bake` would
# just return the cached (installed) module. Evict any such cached modules
# so the import below is forced to resolve through sys.path again.
for name in [n for n in list(sys.modules) if n == "reachy_mini_link"
             or n.startswith("reachy_mini_link.")]:
    del sys.modules[name]

# Now import for real and verify it actually came from the repo, not from
# some other installed copy still discoverable on sys.path. Testing the
# wrong copy is worse than a failing test, so refuse to run rather than
# silently do that.
import reachy_mini_link
loaded = os.path.dirname(os.path.abspath(reachy_mini_link.__file__))
expected = os.path.join(ROOT, "reachy_mini_link")
if loaded != expected:
    sys.exit(
        f"ERROR: refusing to run tests: reachy_mini_link resolved to "
        f"{loaded}, not the repo copy at {expected}.\n"
        f"This usually means an installed add-on copy (e.g. under "
        f"~/.config/blender/*/scripts/addons/reachy_mini_link/) is ahead of "
        f"the repo on sys.path. Disable that installed add-on, or replace "
        f"its directory with a symlink to this repo's reachy_mini_link/."
    )
print(f"Testing reachy_mini_link from: {loaded}")

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
names = argv or ["test_rig", "test_bake"]

loader = unittest.TestLoader()
suite = unittest.TestSuite(loader.loadTestsFromName(f"tests.{n}") for n in names)
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
