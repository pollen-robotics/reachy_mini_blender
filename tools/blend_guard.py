#!/usr/bin/env python3
"""Guard the rig .blend against accidental modification — WITHOUT ever reverting it.

Why this exists
---------------
`reachy_mini.blend` is a 17 MB LFS-tracked source asset, and the add-on must
never modify it. The obvious way to enforce that is "assert git status is
clean", but that check is dangerously ambiguous: it cannot tell an agent's
accidental write apart from the user deliberately saving their own animation.
Acting on it by restoring the file destroys real work — which is exactly what
happened once here: a task found the .blend dirty because the user had saved
189 keyframes into it, ran `git checkout --`, and deleted the .blend1 backup.

So this tool answers a narrower and safer question: *did the thing I just ran
change the file?* — by comparing against a baseline recorded beforehand, rather
than against HEAD.

It NEVER writes to, reverts, stages, or deletes the .blend. If it finds an
unexpected change it reports and exits non-zero, and leaves the file alone for
a human to decide about. Reverting a binary asset is not a decision a script
gets to make.

Usage
-----
    python3 tools/blend_guard.py baseline          # before the work
    ...do the work...
    python3 tools/blend_guard.py check             # after the work

    python3 tools/blend_guard.py check --quiet     # exit code only

Baseline is stored under .superpowers/ (git-ignored scratch). If no baseline
exists, `check` falls back to comparing against HEAD and says so explicitly,
because that comparison cannot distinguish your saves from a stray write.
"""

import argparse
import hashlib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Repo-relative path of the rig asset; it ships inside the add-on package.
BLEND_REL = "reachy_mini_link/assets/reachy_mini.blend"
BLEND = os.path.join(ROOT, BLEND_REL)
STATE_DIR = os.path.join(ROOT, ".superpowers")
STATE = os.path.join(STATE_DIR, "blend_baseline")


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def head_digest():
    """sha256 of the committed blend, or None if it can't be determined.

    Under git-lfs `git show HEAD:file` yields the LFS pointer, not the asset,
    so compare against the pointer's own recorded oid instead.
    """
    try:
        blob = subprocess.run(["git", "-C", ROOT, "show", f"HEAD:{BLEND_REL}"],
                              capture_output=True, check=True).stdout
    except (subprocess.CalledProcessError, OSError):
        return None
    text = blob[:200].decode("utf-8", "replace")
    if "git-lfs" in text:
        for line in text.splitlines():
            if line.startswith("oid sha256:"):
                return line.split(":", 1)[1].strip()
        return None
    return hashlib.sha256(blob).hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=["baseline", "check"])
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(BLEND):
        sys.exit(f"ERROR: {BLEND} not found")
    now = digest(BLEND)

    if a.mode == "baseline":
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE, "w") as fh:
            fh.write(now)
        if not a.quiet:
            print(f"baseline recorded: {now[:16]}…  ({BLEND})")
        return 0

    # --- check ---
    if os.path.exists(STATE):
        with open(STATE) as fh:
            before = fh.read().strip()
        if now == before:
            if not a.quiet:
                print(f"OK: reachy_mini.blend unchanged since baseline ({now[:16]}…)")
            return 0
        print("BLEND CHANGED since the baseline was recorded.", file=sys.stderr)
        print(f"  baseline: {before}", file=sys.stderr)
        print(f"  now     : {now}", file=sys.stderr)
        print("", file=sys.stderr)
        print("This file is NOT being reverted. If the change was yours (you saved", file=sys.stderr)
        print("your own work), keep it — consider File > Save As to a file outside", file=sys.stderr)
        print("the repo so the tracked asset stays pristine. If it was not yours,", file=sys.stderr)
        print("restore it deliberately with:", file=sys.stderr)
        print("    git checkout -- reachy_mini.blend", file=sys.stderr)
        print("and check for a Blender backup at reachy_mini.blend1 first.", file=sys.stderr)
        return 1

    committed = head_digest()
    if committed is None:
        print("WARNING: no baseline recorded and the committed hash could not be "
              "read; cannot tell whether the blend changed.", file=sys.stderr)
        return 1
    if now == committed:
        if not a.quiet:
            print("OK: reachy_mini.blend matches HEAD (no baseline was recorded)")
        return 0
    print("BLEND DIFFERS FROM HEAD, and no baseline was recorded, so it is "
          "impossible to say whether this run changed it.", file=sys.stderr)
    print("  Treat this as inconclusive, NOT as something to revert. A dirty "
          "blend is most often the user's own saved work.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
