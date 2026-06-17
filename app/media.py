"""Media helpers: type detection, ffprobe metadata, thumbnail generation."""
import json
import os
import subprocess

from . import config

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".m4v", ".webm", ".ts", ".mpg", ".mpeg", ".wmv", ".flv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
AUDIO_EXT = {".mp3", ".aac", ".wav", ".flac", ".ogg", ".m4a"}


def media_type(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    return "other"


def abs_path(rel):
    """Resolve a media-relative path safely inside MEDIA_DIR."""
    p = os.path.normpath(os.path.join(config.MEDIA_DIR, rel))
    if not p.startswith(config.MEDIA_DIR):
        raise ValueError("path escapes media dir")
    return p


def probe(rel):
    """Return {duration, width, height, has_audio} for a media-relative file."""
    path = abs_path(rel)
    info = {"duration": None, "width": None, "height": None,
            "has_audio": False, "codec": None}
    if media_type(rel) == "image":
        info["duration"] = None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(out.stdout or "{}")
        fmt = data.get("format", {})
        if fmt.get("duration"):
            info["duration"] = float(fmt["duration"])
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                info["width"] = s.get("width")
                info["height"] = s.get("height")
                info["codec"] = s.get("codec_name")
                if s.get("duration") and not info["duration"]:
                    info["duration"] = float(s["duration"])
            if s.get("codec_type") == "audio":
                info["has_audio"] = True
    except Exception:
        pass
    return info


def thumbnail(rel):
    """Generate (if needed) a thumbnail and return its thumbs-relative name."""
    safe = rel.replace("/", "__")
    name = safe + ".jpg"
    out_path = os.path.join(config.THUMB_DIR, name)
    src = abs_path(rel)
    if os.path.exists(out_path) and os.path.getmtime(out_path) >= os.path.getmtime(src):
        return name
    t = media_type(rel)
    try:
        if t == "image":
            cmd = ["ffmpeg", "-y", "-i", src, "-vf", "scale=320:-1", out_path]
        elif t == "video":
            cmd = ["ffmpeg", "-y", "-ss", "3", "-i", src, "-frames:v", "1",
                   "-vf", "scale=320:-1", out_path]
        else:
            return None
        subprocess.run(cmd, capture_output=True, timeout=60)
        if os.path.exists(out_path):
            return name
    except Exception:
        pass
    return None


def list_media():
    """List files in the media directory with metadata."""
    items = []
    for root, _dirs, files in os.walk(config.MEDIA_DIR):
        for fn in sorted(files):
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, config.MEDIA_DIR)
            t = media_type(rel)
            if t == "other":
                continue
            items.append({
                "file": rel,
                "type": t,
                "size": os.path.getsize(full),
            })
    return items
