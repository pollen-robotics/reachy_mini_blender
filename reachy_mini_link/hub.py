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

import json
import os
import pathlib
import ssl
import threading
import urllib.error
import urllib.request

WHOAMI_URL = "https://huggingface.co/api/whoami-v2"
TOKEN_PAGE_URL = "https://huggingface.co/settings/tokens"

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
