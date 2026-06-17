"""Resolve NRK live-TV channels to a playable HLS URL via NRK's public psapi.

The manifest endpoint returns JSON describing the current live stream; we pull
the HLS (.m3u8) asset URL out of it. NRK live is HLS with AES-128 ("statickey")
encryption, which GStreamer's hlsdemux decrypts itself — no DRM module needed.
The manifest URL is request-specific and time-limited, so callers should resolve
fresh at play time (a short cache here avoids hammering the API on retries).
"""
import json
import threading
import time
import urllib.request

MANIFEST = "https://psapi.nrk.no/playback/manifest/channel/%s"

# channels we expose in the UI (id -> display name)
CHANNELS = [
    {"id": "nrk1", "name": "NRK1"},
    {"id": "nrk2", "name": "NRK2"},
    {"id": "nrk3", "name": "NRK3"},
    {"id": "nrksuper", "name": "NRK Super"},
]
_VALID = {c["id"] for c in CHANNELS}

_CACHE_TTL = 30.0
_cache = {}            # channel -> (url, expiry)
_lock = threading.Lock()


def channels():
    return CHANNELS


def resolve(channel):
    """Return the current HLS manifest URL for an NRK channel, or None."""
    if channel not in _VALID:
        return None
    now = time.time()
    with _lock:
        hit = _cache.get(channel)
        if hit and hit[1] > now:
            return hit[0]
    try:
        req = urllib.request.Request(MANIFEST % channel,
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        assets = (data.get("playable") or {}).get("assets") or []
        url = next((a.get("url") for a in assets if a.get("url")), None)
    except Exception:
        url = None
    if url:
        with _lock:
            _cache[channel] = (url, now + _CACHE_TTL)
    return url
