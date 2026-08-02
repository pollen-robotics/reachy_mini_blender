"""Hugging Face Hub sign-in for the add-on. Stdlib only, like the rest.

Auth is a ladder of places a token may already exist, so most users
never type anything:

  1. the HF_TOKEN environment variable,
  2. the hf CLI token on disk (~/.cache/huggingface/token),
  3. a token pasted into the add-on preferences.

The local daemon knows whether the user is signed in but (rightly)
never hands its token out over HTTP, so it can't be an auth source.

Network calls run in a worker thread and only mutate `state` (plain
dict, no bpy access), which the panel renders on its next redraw. The
UI schedules that redraw itself; this module never touches Blender.
"""

import base64
import json
import os
import pathlib
import re
import ssl
import threading
import unicodedata
import urllib.error
import urllib.request

HUB_URL = "https://huggingface.co"
WHOAMI_URL = f"{HUB_URL}/api/whoami-v2"
TOKEN_PAGE_URL = f"{HUB_URL}/settings/tokens"

_TOKEN_PATHS = (
    pathlib.Path.home() / ".cache" / "huggingface" / "token",
    pathlib.Path.home() / ".huggingface" / "token",  # legacy location
)

# Rendered by the panel. status is one of:
# "unchecked" | "checking" | "ok" | "no_token" | "error"
state = {"status": "unchecked", "user": None, "source": None, "error": None}

_lock = threading.Lock()


def find_token(prefs_token=""):
    """Return (token, human-readable source) or (None, None)."""
    token = os.environ.get("HF_TOKEN", "").strip()
    if token:
        return token, "HF_TOKEN env"
    for path in _TOKEN_PATHS:
        try:
            token = path.read_text().strip()
        except OSError:
            continue
        if token:
            return token, "hf CLI login"
    token = (prefs_token or "").strip()
    if token:
        return token, "add-on preferences"
    return None, None


def _ssl_context():
    """A verified TLS context that works on Pythons without system CAs.

    Some Python builds (python.org on macOS, occasionally Blender's own)
    load zero CA certificates by default, which fails every https call
    with CERTIFICATE_VERIFY_FAILED. When that's the case and certifi is
    importable (Blender bundles it), use its CA bundle instead.
    """
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca", 0) == 0:
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            pass
    return ctx


def whoami(token, timeout=6.0):
    """Return the Hub username for `token`. Raises on bad token/network."""
    req = urllib.request.Request(
        WHOAMI_URL, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(
                req, timeout=timeout, context=_ssl_context()) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise ValueError("token rejected (expired or revoked?)") from exc
        raise ValueError(f"Hub answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ValueError(f"cannot reach huggingface.co ({exc.reason if hasattr(exc, 'reason') else exc})") from exc
    name = data.get("name")
    if not name:
        raise ValueError("unexpected whoami answer")
    return name


def check_async(prefs_token="", on_done=None):
    """Resolve the auth ladder off the main thread and update `state`.

    `on_done` runs on the worker thread; the caller is responsible for
    getting back onto Blender's main thread (e.g. a one-shot timer).
    """
    with _lock:
        if state["status"] == "checking":
            return
        state.update(status="checking", error=None)

    def run():
        token, source = find_token(prefs_token)
        if token is None:
            result = dict(status="no_token", user=None, source=None, error=None)
        else:
            try:
                user = whoami(token)
                result = dict(status="ok", user=user, source=source, error=None)
            except ValueError as exc:
                result = dict(status="error", user=None, source=source,
                              error=str(exc))
        with _lock:
            state.update(result)
        if on_done is not None:
            on_done()

    threading.Thread(target=run, daemon=True, name="reachy-hf-auth").start()


# ─── Publishing moves to a dataset ─────────────────────────────────────

DATASET_DEFAULT = "reachy-mini-moves"

# First-commit datacard. The reachy_mini_community_moves tag is what
# Marionette's community browser filters on; the data/ layout below is
# its dataset schema, so published moves are importable there as-is.
_DATACARD = """\
---
tags:
- reachy_mini_community_moves
- reachy-mini
- robotics
---

# Reachy Mini moves

Motion clips for [Reachy Mini](https://www.pollen-robotics.com/), baked from
Blender timelines with the
[reachy_mini_link](https://github.com/pollen-robotics/reachy_mini_blender)
add-on. Marionette-compatible layout:

- `data/<move>.json` - the motion (`RecordedMove`: description, time,
  set_target_data), canonicalized to <=50 Hz / 6 decimals.
- `data/<move>.ogg` - optional audio sidecar, played by the daemon in
  lockstep with the motion.
"""

# Rendered by the panel. status is one of:
# "idle" | "working" | "done" | "error"
publish = {"status": "idle", "detail": None, "url": None}


def slugify(text):
    """A safe file stem from a move description."""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "untitled"


def canonicalize(move, hz=50.0, decimals=6):
    """Marionette's canonical compressed motion: <=hz + rounded floats.

    Same algorithm as its canonicalizeMotion (walk a fixed 1/hz grid,
    keep the first frame at/after each tick, always keep the last
    frame), so a round-trip through the community datasets is
    byte-stable. Idempotent; a bake below the target rate only gets
    the rounding.
    """
    time = move.get("time") or []
    frames = move.get("set_target_data") or []
    if len(time) >= 3:
        step = 1.0 / hz
        keep = [0]
        next_tick = time[0] + step
        for i in range(1, len(time)):
            if time[i] >= next_tick - 1e-9:
                keep.append(i)
                next_tick += step
        if keep[-1] != len(time) - 1:
            keep.append(len(time) - 1)
        if len(keep) < len(time):
            time = [time[i] for i in keep]
            frames = [frames[i] for i in keep]

    def rnd(x):
        if isinstance(x, float):
            return round(x, decimals)
        if isinstance(x, list):
            return [rnd(v) for v in x]
        if isinstance(x, dict):
            return {k: rnd(v) for k, v in x.items()}
        return x

    out = dict(move)
    out["time"] = rnd(time)
    out["set_target_data"] = rnd(frames)
    return out


def _api(method, url, token, payload=None,
         content_type="application/json", timeout=20.0):
    if payload is not None and not isinstance(payload, bytes):
        payload = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=payload, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": content_type})
    with urllib.request.urlopen(
            req, timeout=timeout, context=_ssl_context()) as resp:
        return json.load(resp)


def _dataset_exists(token, repo_id):
    try:
        _api("GET", f"{HUB_URL}/api/datasets/{repo_id}", token)
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def _create_dataset(token, name):
    _api("POST", f"{HUB_URL}/api/repos/create", token,
         {"type": "dataset", "name": name, "private": False})


def _commit_files(token, repo_id, files, message):
    """One commit with the given [(path_in_repo, bytes)] files.

    Uses the Hub's NDJSON commit endpoint - the same one
    huggingface_hub drives - so no git and no extra dependency.
    """
    lines = [json.dumps({"key": "header", "value": {"summary": message}})]
    for path, content in files:
        lines.append(json.dumps({"key": "file", "value": {
            "path": path,
            "content": base64.b64encode(content).decode(),
            "encoding": "base64",
        }}))
    _api("POST", f"{HUB_URL}/api/datasets/{repo_id}/commit/main", token,
         "\n".join(lines).encode(), content_type="application/x-ndjson")


def publish_move_async(move, dataset_name=DATASET_DEFAULT, prefs_token="",
                       audio=None, on_done=None):
    """Bundle `move` (and optional OGG `audio` bytes) into
    <user>/<dataset_name> on the Hub, in Marionette's community layout.

    Creates the dataset (with a datacard) on first use. The move lands
    at data/<slug-of-description>.json, the audio next to it as
    data/<slug>.ogg; publishing the same description again overwrites
    both, which is the predictable thing: the description is the
    move's identity across the ecosystem.
    """
    with _lock:
        if publish["status"] == "working":
            return
        publish.update(status="working", detail=None, url=None)

    def run():
        try:
            token, _ = find_token(prefs_token)
            if token is None:
                raise ValueError("no Hugging Face token (see sign-in above)")
            user = whoami(token)
            repo_id = f"{user}/{dataset_name}"
            stem = f"data/{slugify(move.get('description'))}"
            path = f"{stem}.json"
            files = [(path, json.dumps(canonicalize(move)).encode())]
            if audio:
                files.append((f"{stem}.ogg", audio))
            if not _dataset_exists(token, repo_id):
                _create_dataset(token, dataset_name)
                files.append(("README.md", _DATACARD.encode()))
            _commit_files(token, repo_id, files,
                          f"add {path} from Blender")
            result = dict(status="done", detail=path,
                          url=f"{HUB_URL}/datasets/{repo_id}")
        except (ValueError, urllib.error.URLError, OSError) as exc:
            detail = getattr(exc, "reason", None) or str(exc)
            result = dict(status="error", detail=str(detail), url=None)
        with _lock:
            publish.update(result)
        if on_done is not None:
            on_done()

    threading.Thread(target=run, daemon=True, name="reachy-hf-publish").start()
