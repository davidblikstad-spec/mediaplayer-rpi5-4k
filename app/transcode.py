"""Optional background transcoding of uploads, with progress.

Unlike the Pi 3 (which *had* to shrink everything to 1080p because it could not
decode 4K at all), the Pi 5 plays 4K natively — but only HEVC/H.265 is decoded
in **hardware** (the `rpi-hevc-dec` block, smooth up to 4Kp60). H.264 and
everything else are decoded in **software** on the Cortex-A76 cores: fine up to
1080p, but heavy at 4K. The Pi 5 has **no hardware video encoder**, so any
transcode here is software (libx265 / libx264) — correct but slow.

So on the Pi 5 transcoding is an *option*, not a requirement. Two targets:

  * ``hevc``  — re-encode to HEVC at the **original resolution** (keep 4K),
                so playback moves onto the hardware HEVC decoder. Encode is slow
                (libx265 software), but you encode once and play smoothly many
                times. This is the headline Pi-5 option.
  * ``1080p`` — re-encode to H.264 scaled to fit 1920x1080 (the legacy Pi-3
                behaviour): smaller/lower-bandwidth files that software-decode
                comfortably.

A transcode is started either manually (a button per file in the web UI) or
automatically on upload, depending on the ``transcode_policy`` setting.
"""
import os
import subprocess
import threading

from . import media

# "1080p" downscale target, and the 4K ceiling of the hardware HEVC decoder.
MAX_W, MAX_H = 1920, 1080
UHD_W, UHD_H = 3840, 2160

# The Pi 5 hardware-decodes HEVC (H.265) only; everything else is software.
HW_DECODE_CODECS = {"hevc", "h265"}

# job registry, keyed by the uploaded file's media-relative name
_jobs = {}
_lock = threading.Lock()


def playback_mode(width, height, codec):
    """How a video will play on the Pi 5:

      'hw'    — hardware-decoded (HEVC, up to 4K): smooth, CPU near idle.
      'sw'    — software-decoded at <=1080p: comfortable on the A76 cores.
      'heavy' — software-decoded above 1080p (e.g. 4K H.264 / VP9 / AV1): the
                CPU may not keep up; transcoding to HEVC is offered as an option.
    """
    c = (codec or "").lower()
    if c in HW_DECODE_CODECS:
        return "hw"
    over_1080 = bool(width and height and (width > MAX_W or height > MAX_H))
    return "heavy" if over_1080 else "sw"


def auto_target(width, height, codec, policy):
    """The transcode target to apply to a freshly uploaded file given the auto
    policy, or None to leave it untouched (play natively).

      policy 'off'   -> never transcode automatically (default; play natively).
      policy 'hevc'  -> convert anything that would software-decode at >1080p
                        ('heavy') to HEVC at its original resolution, so it
                        hardware-decodes. Leaves HEVC and <=1080p files alone.
      policy '1080p' -> convert anything larger than 1080p down to 1080p H.264
                        (the legacy Pi-3 behaviour).
    """
    over_1080 = bool(width and height and (width > MAX_W or height > MAX_H))
    if policy == "1080p":
        return "1080p" if over_1080 else None
    if policy == "hevc":
        return "hevc" if playback_mode(width, height, codec) == "heavy" else None
    return None


def jobs_snapshot():
    # private keys (the Popen handle, abort flag) start with "_" and are dropped
    with _lock:
        return {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                for k, v in _jobs.items()}


def _set(rel, **kw):
    with _lock:
        j = _jobs.setdefault(rel, {"file": rel})
        j.update(kw)


def _pick_output(rel, suffix):
    """Choose a non-colliding output name (media-relative) for the transcode.
    `suffix` distinguishes the target (e.g. '_hevc', '_1080p')."""
    root, _ext = os.path.splitext(rel)
    base = root + ".mp4"
    # source is already a plain .mp4 -> replace it in place (the original is the
    # heavy file we're superseding)
    if base == rel:
        return base
    if not os.path.exists(media.abs_path(base)):
        return base
    i = 1
    while True:
        cand = "%s%s%s.mp4" % (root, suffix, "" if i == 1 else "_%d" % i)
        if not os.path.exists(media.abs_path(cand)):
            return cand
        i += 1


def _ffmpeg_cmd(target, src, tmp_abs):
    """Build the ffmpeg command for the given transcode target. Both targets
    encode in software (the Pi 5 has no hardware encoder)."""
    common_tail = ["-c:a", "aac", "-b:a", "160k",
                   "-movflags", "+faststart", "-f", "mp4",
                   "-progress", "pipe:1", "-nostats", tmp_abs]
    if target == "hevc":
        # Keep the original resolution (e.g. 4K); only force even dimensions for
        # yuv420p. The hvc1 tag keeps the .mp4 broadly compatible. CRF 28 is the
        # libx265 default-quality knob (~visually equivalent to x264 CRF 23).
        vf = "scale='trunc(iw/2)*2':'trunc(ih/2)*2'"
        return ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", src,
                "-vf", vf, "-c:v", "libx265", "-preset", "medium", "-crf", "28",
                "-tag:v", "hvc1", "-pix_fmt", "yuv420p"] + common_tail
    # "1080p": fit inside 1920x1080 preserving aspect, even dimensions.
    vf = ("scale='min(%d,iw)':'min(%d,ih)':force_original_aspect_ratio=decrease,"
          "scale='trunc(iw/2)*2':'trunc(ih/2)*2'" % (MAX_W, MAX_H))
    return ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", src,
            "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p"] + common_tail


def start(rel, target, width, height, duration, log=print):
    """Kick off a background transcode of media file `rel`.

    target: 'hevc'  -> HEVC at original resolution (gain hardware decode), or
            '1080p' -> H.264 scaled to fit 1080p (legacy)."""
    if target not in ("hevc", "1080p"):
        target = "hevc"
    with _lock:
        cur = _jobs.get(rel)
        if cur and cur.get("status") == "running":
            return
        _jobs[rel] = {"file": rel, "status": "running", "percent": 0,
                      "target": target,
                      "from": "%dx%d" % (width, height) if width and height else "?",
                      "result": None, "error": None}
    threading.Thread(target=_run, args=(rel, target, duration, log),
                     daemon=True).start()


def _run(rel, target, duration, log):
    suffix = "_hevc" if target == "hevc" else "_1080p"
    try:
        src = media.abs_path(rel)
        out_rel = _pick_output(rel, suffix)
        out_abs = media.abs_path(out_rel)
    except Exception as e:  # noqa
        _set(rel, status="error", error=str(e))
        return
    tmp_abs = out_abs + ".transcoding.part"
    errf = out_abs + ".transcoding.log"
    cmd = _ffmpeg_cmd(target, src, tmp_abs)
    log("transcode start (%s): %s -> %s" % (target, rel, out_rel))
    try:
        with open(errf, "wb") as ef:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef,
                                    text=True)
            _set(rel, _proc=proc)
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_us=") and duration:
                    try:
                        us = int(line.split("=", 1)[1])
                        pct = int(us / 1e6 / duration * 100)
                        _set(rel, percent=max(0, min(99, pct)))
                    except (ValueError, ZeroDivisionError):
                        pass
            proc.wait()
        with _lock:
            aborted = bool(_jobs.get(rel, {}).get("_aborted"))
        if aborted:
            _set(rel, status="aborted", percent=0, error=None)
            log("transcode aborted: %s" % rel)
            _rm(tmp_abs)
            return
        if proc.returncode != 0:
            tail = _tail(errf)
            _set(rel, status="error",
                 error=tail or ("ffmpeg exited %d" % proc.returncode))
            log("transcode failed: %s: %s" % (rel, tail))
            _rm(tmp_abs)
            return
        os.replace(tmp_abs, out_abs)
        # drop the superseded original if we wrote to a different file
        if os.path.abspath(src) != os.path.abspath(out_abs) and os.path.exists(src):
            _rm(src)
        try:
            media.thumbnail(out_rel)
        except Exception:  # noqa
            pass
        _set(rel, status="done", percent=100, result=out_rel)
        log("transcode done: %s" % out_rel)
    except FileNotFoundError:
        _set(rel, status="error", error="ffmpeg not installed")
        _rm(tmp_abs)
    except Exception as e:  # noqa
        _set(rel, status="error", error=str(e))
        _rm(tmp_abs)
    finally:
        _rm(errf)


def cancel(rel):
    """Abort a running transcode for `rel`. Returns True if one was running."""
    with _lock:
        j = _jobs.get(rel)
        if not j or j.get("status") != "running":
            return False
        j["_aborted"] = True
        p = j.get("_proc")
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(5)
        except Exception:  # noqa
            try:
                p.kill()
            except Exception:  # noqa
                pass
    return True


def _rm(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _tail(path, n=400):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()[-n:].strip()
    except OSError:
        return ""
