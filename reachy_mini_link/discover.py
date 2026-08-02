"""Find a Reachy Mini daemon on the network. Stdlib only.

No mDNS stack needed: the OS resolver already handles .local names on
macOS (Bonjour) and most Linux desktops (Avahi), so probing the
daemon's HTTP status endpoint on a couple of well-known hosts covers
the common cases. When none answers, the user types the IP shown by
the mobile app into the Host field - same field, no extra path.

Same threading contract as hub.py: the worker only mutates `state`,
the UI applies the result from a timer on the main thread.
"""

import json
import threading
import urllib.error
import urllib.request

# Order matters: a Lite plugged into this machine wins over a wireless
# robot advertising on mDNS.
CANDIDATES = ("127.0.0.1", "reachy-mini.local")

# status is one of: "idle" | "searching" | "found" | "none"
state = {"status": "idle", "host": None}

_lock = threading.Lock()


def probe(host, port=8000, timeout=2.0):
    """True if a Reachy Mini daemon answers on host:port."""
    url = f"http://{host}:{port}/api/daemon/status"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            json.load(resp)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def find_async(port=8000, on_done=None):
    """Probe CANDIDATES off the main thread; first hit wins."""
    with _lock:
        if state["status"] == "searching":
            return
        state.update(status="searching", host=None)

    def run():
        found = None
        for host in CANDIDATES:
            if probe(host, port=port):
                found = host
                break
        with _lock:
            if found:
                state.update(status="found", host=found)
            else:
                state.update(status="none", host=None)
        if on_done is not None:
            on_done()

    threading.Thread(target=run, daemon=True, name="reachy-discover").start()
