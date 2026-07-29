# Working on this repo

Notes for anyone — human or agent — changing code here.

## Never revert `reachy_mini.blend`

`reachy_mini.blend` is a 17 MB LFS-tracked source asset and the rig everything
else depends on. Add-on code must not modify it.

**But "the blend must be clean" is not the check to enforce.** A dirty blend
usually means the *user saved their own animation*, not that something went
wrong. Acting on a clean-tree assertion by restoring the file destroys work.

This has already happened once. A task was told the blend must be unmodified,
found it dirty because the user had saved 189 keyframes into it, ran
`git checkout -- reachy_mini.blend`, and deleted the `reachy_mini.blend1`
backup. The animation was recoverable only from Blender autosaves in `/tmp`
and a stray LFS object.

So:

- **Never** run `git checkout -- reachy_mini.blend`, `git restore`, `git clean`,
  or `git stash` against the blend to satisfy a cleanliness check.
- **Never** delete `reachy_mini.blend1` — that is Blender's only backup of the
  state before the last save.
- If the blend is unexpectedly dirty, **stop and ask**. It is a human's call.

Use the guard instead, which compares against a baseline you record yourself
rather than against HEAD, and which never writes to the file:

```bash
python3 tools/blend_guard.py baseline     # before your changes
# ... do the work ...
python3 tools/blend_guard.py check        # after; exits 1 if the blend moved
```

Without a baseline, `check` can only compare against HEAD and will say so —
that result is *inconclusive*, never grounds for a revert.

## Author your animation outside the repo

Keep `reachy_mini.blend` as a pristine rig asset. When you animate, **File >
Save As** to a working file outside the repo (e.g. `~/reachy_mini_dev.blend`).
That keeps a 17 MB binary out of your diffs and removes the whole class of
problem above.

The add-on works identically either way — it reads whatever rig is open.

## Don't let an installed add-on copy shadow the working tree

If the add-on is installed into Blender's config *and* enabled, Blender imports
it at startup from `~/.config/blender/<ver>/scripts/addons/reachy_mini_link/`.
Anything importing `reachy_mini_link` afterwards gets that copy from
`sys.modules`, no matter what is on `sys.path` — so a test suite can silently
validate a stale installed copy instead of your edits.

`tests/run_blender_tests.py` handles this: it evicts cached `reachy_mini_link`
modules, then asserts the package resolved to the repo and refuses to run
otherwise. It prints which copy it tested on every run — check that line.

For live iteration, replace the installed directory with a symlink so the two
can never diverge:

```bash
ln -sfn "$PWD/reachy_mini_link" \
        ~/.config/blender/5.1/scripts/addons/reachy_mini_link
```

## Blender API note: actions are layered since 4.4

`Action.fcurves` no longer exists in Blender 5.1. Curves live under
`action.layers[].strips[].channelbags[].fcurves`. Code that walks fcurves must
handle the layered form (or both, if it needs to run on older Blender).

## Running the tests

```bash
python3 -m unittest tests.test_client         # transport; no Blender needed
python3 -m unittest tests.test_move_roundtrip # skips without the SDK installed

blender --background reachy_mini.blend --python tests/run_blender_tests.py
```

The Blender suite must pass in **either** module order — `-- test_rig test_bake`
and `-- test_bake test_rig`. Order dependence here has bitten before: a teardown
using `animation_data_clear()` deleted the rig's three calibration drivers, which
silently made five rig tests meaningless. Clear `animation_data.action` instead.
