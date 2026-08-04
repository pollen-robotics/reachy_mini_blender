"""Browse and download community moves from the Hugging Face Hub.

The other direction from hub.py's publishing: list every dataset tagged
reachy_mini_community_moves (Marionette's tag, which Publish to Hub also
sets), collect the RecordedMove files inside them, and fetch a chosen
move (plus its audio sidecar) so import_move can turn it into keyframes.

Same rules as the rest of the add-on: stdlib only, no bpy. Network runs
in worker threads and only mutates the module-level `state`/`download`
dicts; the panel renders them on its next redraw, and the UI hops back
onto Blender's main thread itself. A token is optional - the datasets
are public - but is sent when one exists (kinder rate limits, and the
user's own private datasets list too).

Downloads land in a per-user cache directory rather than Blender's
session tempdir: the sound strip the import creates points at the
downloaded file, and a strip pointing into a tempdir would come back
silent after a restart.
"""

import json
import os
import pathlib
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import hub

COMMUNITY_TAG = "reachy_mini_community_moves"

_TIMEOUT = 15.0
_MAX_DATASETS = 100
_AUDIO_SUFFIXES = (".wav", ".ogg", ".oga", ".mp3", ".flac")

# Rendered by the panel. status is one of:
# "idle" | "working" | "done" | "error"
# moves: [{"repo_id", "path", "name", "label", "audio_path"}]
state = {"status": "idle", "detail": None, "moves": []}

# status is one of: "idle" | "working" | "fetched" | "done" | "error"
# "fetched" means files are on disk and the UI still has to import them;
# the UI flips it to "done" (or "error") once import_move has run.
download = {"status": "idle", "detail": None, "files": None}

_lock = threading.Lock()


def _get(url, token=None, raw=False):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(
            req, timeout=_TIMEOUT, context=hub._ssl_context()) as resp:
        data = resp.read()
    return data if raw else json.loads(data)


def moves_from_tree(repo_id, entries):
    """The move list for one dataset, from its data/ tree listing.

    A move is any data/*.json; its audio sidecar is the same stem with
    an audio extension, preferring the order of _AUDIO_SUFFIXES (.wav
    first - what Marionette and this add-on write).
    """
    files = {e["path"] for e in entries if e.get("type") == "file"}
    moves = []
    for path in sorted(files):
        if not path.endswith(".json"):
            continue
        stem = path[:-len(".json")]
        audio = next((stem + ext for ext in _AUDIO_SUFFIXES
                      if stem + ext in files), None)
        name = pathlib.PurePosixPath(path).stem
        moves.append({
            "repo_id": repo_id,
            "path": path,
            "name": name,
            # What the list shows; repo included so filtering by author
            # or dataset works in the same search box.
            "label": f"{name} \u00b7 {repo_id}",
            "audio_path": audio,
        })
    return moves


def sort_moves(moves):
    """Official pollen-robotics libraries first, then alphabetical."""
    return sorted(moves, key=lambda m: (
        not m["repo_id"].startswith("pollen-robotics/"), m["label"].lower()))


def repo_moves(repo_id, get):
    """All moves in one dataset, trying both layouts in use.

    Marionette and Publish to Hub write data/<move>.json; the official
    pollen-robotics libraries (emotions, dances) predate that and keep
    <move>.json at the repo root. `get` is a url -> parsed-JSON callable
    so the network stays injectable for tests.
    """
    for suffix in ("/tree/main/data", "/tree/main"):
        try:
            entries = get(f"{hub.HUB_URL}/api/datasets/{repo_id}{suffix}")
        except (urllib.error.URLError, OSError, ValueError):
            continue
        moves = moves_from_tree(repo_id, entries)
        if moves:
            return moves
    return []


def list_moves_async(prefs_token="", on_done=None):
    """Fill state["moves"] with every community move on the Hub.

    One listing call plus one tree call per dataset, fanned out over a
    small thread pool (~30 datasets exist today; sequential would take
    tens of seconds on a slow link). A dataset without a data/ dir, or
    that 404s mid-listing, is skipped rather than failing the browse.
    """
    with _lock:
        if state["status"] == "working":
            return
        state.update(status="working", detail=None)

    def run():
        token, _ = hub.find_token(prefs_token)
        try:
            repos = _get(
                f"{hub.HUB_URL}/api/datasets"
                f"?filter={COMMUNITY_TAG}&limit={_MAX_DATASETS}", token)
            ids = [r["id"] for r in repos if r.get("id")]

            def tree(repo_id):
                return repo_moves(repo_id, lambda url: _get(url, token))

            moves = []
            with ThreadPoolExecutor(max_workers=8) as pool:
                for chunk in pool.map(tree, ids):
                    moves.extend(chunk)
            with _lock:
                state.update(status="done", detail=None,
                             moves=sort_moves(moves))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            detail = getattr(exc, "reason", None) or exc
            with _lock:
                state.update(status="error", detail=str(detail), moves=[])
        if on_done is not None:
            on_done()

    threading.Thread(target=run, daemon=True, name="reachy-hub-browse").start()


def cache_dir():
    """Per-user cache for downloaded moves (survives Blender restarts)."""
    home = pathlib.Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Caches"
    elif os.name == "nt":
        base = pathlib.Path(os.environ.get("LOCALAPPDATA", str(home)))
    else:
        base = pathlib.Path(os.environ.get("XDG_CACHE_HOME",
                                           str(home / ".cache")))
    return base / "reachy_mini_link" / "hub_moves"


def local_dir(repo_id):
    """Where a dataset's files land. Flat: repo separator becomes '~'."""
    return cache_dir() / repo_id.replace("/", "~")


def download_move_async(move, prefs_token="", on_done=None):
    """Fetch a move's JSON (and sidecar) into the cache.

    On success download["files"] holds (json_path, audio_path_or_None)
    and status is "fetched" - importing needs bpy, so that last step is
    the caller's job on the main thread.
    """
    with _lock:
        if download["status"] == "working":
            return
        download.update(status="working", detail=move["name"], files=None)

    def run():
        token, _ = hub.find_token(prefs_token)
        try:
            dest = local_dir(move["repo_id"])
            dest.mkdir(parents=True, exist_ok=True)

            def fetch(path):
                data = _get(f"{hub.HUB_URL}/datasets/{move['repo_id']}"
                            f"/resolve/main/{path}", token, raw=True)
                local = dest / pathlib.PurePosixPath(path).name
                local.write_bytes(data)
                return str(local)

            json_path = fetch(move["path"])
            audio_path = fetch(move["audio_path"]) if move.get("audio_path") \
                else None
            with _lock:
                download.update(status="fetched",
                                files=(json_path, audio_path))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            detail = getattr(exc, "reason", None) or exc
            with _lock:
                download.update(status="error", detail=str(detail),
                                files=None)
        if on_done is not None:
            on_done()

    threading.Thread(target=run, daemon=True,
                     name="reachy-hub-download").start()
