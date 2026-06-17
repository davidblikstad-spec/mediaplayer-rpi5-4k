#!/usr/bin/env python3
"""Entry point: serve the media-player web app with a production WSGI server."""
import os
import tempfile

# Large uploads (e.g. multi-GB 4K video) are spooled to a temp file by both
# waitress (request body) and Werkzeug (multipart parsing). On this Pi /tmp is
# a small RAM disk (tmpfs), so big uploads fill RAM and fail. Redirect temp
# files to the SD card instead. Must run before anything caches tempfile.tempdir.
_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tmp")
os.makedirs(_TMP, exist_ok=True)
for _v in ("TMPDIR", "TEMP", "TMP"):
    os.environ[_v] = _TMP
tempfile.tempdir = _TMP

from app import create_app
from app import config

app = create_app()

if __name__ == "__main__":
    cfg = config.load()
    host = cfg["settings"].get("host", "0.0.0.0")
    port = int(cfg["settings"].get("port", 8080))
    try:
        from waitress import serve
        print("[mediaplayer] serving on http://%s:%d (waitress)" % (host, port), flush=True)
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        print("[mediaplayer] serving on http://%s:%d (flask dev server)" % (host, port), flush=True)
        app.run(host=host, port=port, threaded=True)
