"""Persistent JSON configuration store with a write lock."""
import json
import os
import threading
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
MEDIA_DIR = os.path.join(BASE_DIR, "media")
THUMB_DIR = os.path.join(BASE_DIR, "thumbs")
PREVIEW_DIR = os.path.join(BASE_DIR, "previews")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

for d in (DATA_DIR, MEDIA_DIR, THUMB_DIR, PREVIEW_DIR):
    os.makedirs(d, exist_ok=True)

_lock = threading.RLock()

# Is the Pi 5's hardware HEVC decoder (`rpi-hevc-dec`) in play?
#
# It is not: it green-screens on some streams (see docs/playback-on-pi5.md), so
# app/gst.py demotes the GStreamer element to rank 0 and everything decodes in
# software. This single flag is what the rest of the app keys off — the playback
# badges, the auto-transcode policy and the "→ HEVC" button all only make sense
# when HEVC actually buys you a hardware decoder. Flip it to True (and restart)
# to put the decoder and all of that back, e.g. on a kernel where it's fixed.
HW_HEVC_DECODE = False


def new_id():
    return uuid.uuid4().hex[:12]


def _defaults():
    return {
        "auth": {"username": None, "password_hash": None},
        "settings": {
            "secret_key": uuid.uuid4().hex,
            "host": "0.0.0.0",
            "port": 8080,
            "cec_device": "/dev/cec0",
            "cec_phys_addr": "",          # e.g. 0x1000; blank = auto/skip
            "default_item": None,          # an item dict (see playlists) or None
            "audio_out": "auto",           # ALSA device: "auto", HDMI or analog
            "screenshot_interval": 5,      # seconds between HDMI snapshots
            "stream_av_delay_ms": 0,       # manual extra delay for stream audio (ms)
            "stream_resync_interval_s": 3600,  # re-sync stream audio every N s (0=off)
            # Auto-transcode policy applied on upload (see app/transcode.py):
            #   "off"   — never; keep and play the original. Default.
            #   "1080p" — auto-shrink anything over 1080p to 1080p H.264.
            #   "hevc"  — auto-convert >1080p non-HEVC uploads to HEVC (keep the
            #             resolution) so they hardware-decode smoothly. Only
            #             meaningful when HW_HEVC_DECODE is on; while it is off
            #             this policy is ignored and hidden from Settings.
            "transcode_policy": "off",
        },
        "playlists": [],
        "schedules": [],
    }


def load():
    with _lock:
        if not os.path.exists(CONFIG_PATH):
            cfg = _defaults()
            _write(cfg)
            return cfg
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        # merge in any new default keys
        d = _defaults()
        for k, v in d.items():
            cfg.setdefault(k, v)
        for k, v in d["settings"].items():
            cfg["settings"].setdefault(k, v)
        return cfg


def _write(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def save(cfg):
    with _lock:
        _write(cfg)


def update(mutator):
    """Load, apply mutator(cfg), save, and return the result of the mutator."""
    with _lock:
        cfg = load()
        result = mutator(cfg)
        _write(cfg)
        return result


def get_playlist(cfg, pid):
    for p in cfg["playlists"]:
        if p["id"] == pid:
            return p
    return None
