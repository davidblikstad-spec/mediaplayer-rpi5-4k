#!/usr/bin/env python3
"""Headless self-test for the gapless player (app/mixer.py).

Runs the real MixerPlayer + PlayerEngine with *capturing sinks* in place of
kmssink/alsasink, so it needs no display, no DRM master and no sound card — it
works over SSH, in a container, anywhere GStreamer + PyGObject are installed.
It generates its own test media with ffmpeg (solid-colour clips with a tone, and
a PNG) and then checks, from what actually reached the output pipeline's sinks:

  * no black frame between items (hard cuts) or during fades,
  * the cut lands exactly where the previous item ends (no repeated/held frame),
  * loops of a single item are gapless,
  * a cross-fade really blends (alpha ramps monotonically through mixed colours),
  * audio never drops out across a cut, and cross-fades the two tones.

    python3 scripts/mixer_selftest.py            # all scenarios
    python3 scripts/mixer_selftest.py cut fade   # just those

Exit status is non-zero if any check fails. It does NOT exercise kmssink/ALSA —
for that, run the real service (or scripts/gst_player_test.py) on the Pi.
"""
import os
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi  # noqa: E402
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from app import gst as gstmod, mixer  # noqa: E402

W, H, FPS = 320, 180, 25

# Y,U,V of the test colours as they come out of the (BT.709) canvas
COLOURS = {"black": (16, 128, 128), "red": (81, 90, 240), "green": (145, 54, 34),
           "blue": (31, 239, 118), "yellow": (219, 16, 137)}


def classify(yuv):
    best, bd = None, 1e9
    for name, c in COLOURS.items():
        d = sum((a - b) ** 2 for a, b in zip(yuv, c))
        if d < bd:
            best, bd = name, d
    return best, bd ** 0.5


class Capture:
    """Capturing stand-ins for kmssink and alsasink."""

    def __init__(self):
        self.frames = []          # (out_pts_s, yuv_at_centre, (alpha-ish), name)
        self.audio = []           # (out_pts_s, rms, zero_crossings_per_s)
        self.lock = threading.Lock()

    def vsink(self):
        s = Gst.ElementFactory.make("appsink", None)
        s.set_property("emit-signals", True)
        s.set_property("sync", True)
        s.set_property("enable-last-sample", True)
        s.set_property("caps", Gst.Caps.from_string("video/x-raw,format=I420"))

        def on(sink):
            smp = sink.emit("pull-sample")
            buf = smp.get_buffer()
            ok, mi = buf.map(Gst.MapFlags.READ)
            d = mi.data
            cx, cy = W // 2, H // 2
            y = d[cy * W + cx]
            u = d[W * H + (cy // 2) * (W // 2) + cx // 2]
            v = d[W * H + (W // 2) * (H // 2) + (cy // 2) * (W // 2) + cx // 2]
            corner = d[2 * W + 2]                    # near the top-left: letterbox bars?
            buf.unmap(mi)
            with self.lock:
                self.frames.append((buf.pts / 1e9, (y, u, v), corner))
            return Gst.FlowReturn.OK
        s.connect("new-sample", on)
        return s

    def asink(self):
        s = Gst.ElementFactory.make("appsink", None)
        s.set_property("emit-signals", True)
        s.set_property("sync", True)
        s.set_property("caps", Gst.Caps.from_string("audio/x-raw,format=F32LE"))
        import array

        def on(sink):
            smp = sink.emit("pull-sample")
            buf = smp.get_buffer()
            ok, mi = buf.map(Gst.MapFlags.READ)
            a = array.array("f")
            a.frombytes(bytes(mi.data))
            buf.unmap(mi)
            left = a[0::2]
            n = len(left)
            if n:
                rms = (sum(x * x for x in left) / n) ** 0.5
                zc = sum(1 for i in range(1, n) if (left[i - 1] < 0) != (left[i] < 0))
                dur = n / 48000.0
                with self.lock:
                    self.audio.append((buf.pts / 1e9, rms, zc / dur / 2))
            return Gst.FlowReturn.OK
        s.connect("new-sample", on)
        return s

    def runs(self):
        """Collapse frames into [(colour, first_pts, last_pts, n)] runs."""
        out = []
        with self.lock:
            fr = list(self.frames)
        for pts, yuv, _c in fr:
            name, dist = classify(yuv)
            if dist > 28:
                name = "mix"
            if out and out[-1][0] == name:
                out[-1][2] = pts
                out[-1][3] += 1
            else:
                out.append([name, pts, pts, 1])
        return out


def make_media(d):
    def ff(*a):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error"] + list(a), check=True)
    ff("-f", "lavfi", "-i", "color=c=red:s=1280x720:r=25:d=6", "-f", "lavfi",
       "-i", "sine=f=440:d=6:r=48000", "-c:v", "libx264", "-preset", "ultrafast",
       "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", os.path.join(d, "red.mp4"))
    ff("-f", "lavfi", "-i", "color=c=0x00FF00:s=1920x1080:r=30:d=6", "-f", "lavfi",
       "-i", "sine=f=880:d=6:r=44100", "-c:v", "libx264", "-preset", "ultrafast",
       "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", os.path.join(d, "green.mp4"))
    ff("-f", "lavfi", "-i", "color=c=yellow:s=640x480:r=25:d=4", "-c:v", "libx264",
       "-preset", "ultrafast", "-pix_fmt", "yuv420p", os.path.join(d, "yellow.mp4"))
    ff("-f", "lavfi", "-i", "color=c=blue:s=3840x2160", "-frames:v", "1",
       os.path.join(d, "blue.png"))
    return {n: os.path.join(d, n) for n in os.listdir(d)}


class Rig:
    """A MixerPlayer + PlayerEngine over capturing sinks."""

    def __init__(self):
        self.cap = Capture()
        self.log_lines = []
        self.player = mixer.MixerPlayer(
            log=lambda m: self.log_lines.append(m), make_vsink=self.cap.vsink,
            make_asink=self.cap.asink, canvas=(W, H), fps=FPS, wait_hdmi=False)
        self.engine = gstmod.PlayerEngine(self.player, log=lambda m: self.log_lines.append(m))
        self.player.start()

    def play(self, items, transition=0.0, loop=False):
        self.engine.play_playlist({"name": "t", "items": items, "loop_playlist": loop,
                                   "transition_s": transition})

    def stop(self):
        self.player.stop()
        if self.player.out is not None:
            self.player.out.stop()


def item(media, name, **kw):
    t = "image" if name.endswith(".png") else "video"
    it = {"file": name, "type": t, "_test_abs": media[name]}
    it.update(kw)
    return it


# ---- engine hook: tests use absolute paths, bypassing media/ffprobe -------------
_orig_resolve = gstmod.PlayerEngine._resolve


def _resolve(self, it):
    if "_test_abs" not in it:
        return _orig_resolve(self, it)
    r = dict(it)
    r["_abs"] = it["_test_abs"]
    r["_type"] = it.get("type")
    r["_duration"] = it.get("_test_dur")
    r["_has_audio"] = True
    return r


gstmod.PlayerEngine._resolve = _resolve

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def describe(runs):
    return "  ".join("%s[%.2f-%.2f,%d]" % (n, a, b, c) for n, a, b, c in runs)


def black_frames_after_start(cap):
    """Black frames between the first and the last coloured frame."""
    fr = [(pts, classify(yuv)) for pts, yuv, _c in cap.frames]
    lit = [i for i, (_p, (n, d)) in enumerate(fr) if n != "black" or d > 28]
    if not lit:
        return [0.0]
    return [fr[i][0] for i in range(lit[0], lit[-1])
            if fr[i][1][0] == "black" and fr[i][1][1] <= 28]


def scenario_cut(media):
    """red(0-2s) -> green(1-3s, seeks) -> blue still(2s) -> yellow(2s); hard cuts."""
    print("scenario: hard cuts (gapless)")
    r = Rig()
    items = [item(media, "red.mp4", **{"in": 0, "out": 2, "_test_dur": 6}),
             item(media, "green.mp4", **{"in": 1, "out": 3, "_test_dur": 6}),
             item(media, "blue.png", duration=2),
             item(media, "yellow.mp4", **{"in": 0, "out": 2, "_test_dur": 4})]
    r.play(items)
    time.sleep(10.5)
    runs = r.cap.runs()
    print("  " + describe(runs))
    names = [x[0] for x in runs if x[0] != "black"]
    check(names[:4] == ["red", "green", "blue", "yellow"], "plays red, green, blue, yellow in order (got %s)" % names)
    check(not black_frames_after_start(r.cap), "no black frame once playback has started")
    body = [x for x in runs if x[0] in ("red", "green", "blue", "yellow")][:3]
    for a, b in zip(body, body[1:]):
        gap = b[1] - a[2]
        check(gap < 1.6 / FPS, "%s -> %s: next frame follows within %.0f ms (no hold)" % (a[0], b[0], gap * 1000))
    for n, a, b, c in body:
        check(1.8 < (b - a + 1.0 / FPS) < 2.25, "%s plays for ~2 s (%.2f s)" % (n, b - a + 1.0 / FPS))
    aud = [x for x in r.cap.audio if x[0] > 0.5]
    quiet = [x for x in aud if x[0] < 4.0 and x[1] < 0.01]
    check(not quiet, "audio never drops out across the red -> green cut (%d silent blocks)" % len(quiet))
    r.stop()


def scenario_loop(media):
    """one clip looped 3x; the seek-based loop must be gapless."""
    print("scenario: loop of one clip")
    r = Rig()
    r.play([item(media, "red.mp4", **{"in": 0, "out": 1.5, "_test_dur": 6, "loop": 3})])
    time.sleep(7.0)
    runs = r.cap.runs()
    print("  " + describe(runs))
    reds = [x for x in runs if x[0] == "red"]
    check(len(reds) == 1, "frames are continuous red across 3 loops (%d run)" % len(reds))
    if reds:
        total = reds[0][2] - reds[0][1] + 1.0 / FPS
        check(4.2 < total < 4.8, "3 x 1.5 s of red = %.2f s" % total)
    check(not black_frames_after_start(r.cap), "no black frame")
    r.stop()


def scenario_fade(media):
    """red -> green with a 1.5 s cross-fade."""
    print("scenario: 1.5 s cross-fade")
    r = Rig()
    items = [item(media, "red.mp4", **{"in": 0, "out": 4, "_test_dur": 6}),
             item(media, "green.mp4", **{"in": 0, "out": 4, "_test_dur": 6})]
    r.play(items, transition=1.5)
    time.sleep(9.5)
    runs = r.cap.runs()
    print("  " + describe(runs[:12]))
    check(not black_frames_after_start(r.cap), "no black frame during or around the fade")
    # the share of green in each frame between the first and last coloured one,
    # from the V channel (red 240 -> green 34)
    lit = [(p, yuv) for p, yuv, _c in r.cap.frames
           if classify(yuv)[0] != "black" or classify(yuv)[1] > 28]
    seq = [(p, min(1.0, max(0.0, (240 - yuv[2]) / (240 - 34.0)))) for p, yuv in lit]
    mixed = [(p, f) for p, f in seq if 0.1 < f < 0.9]
    check(len(mixed) > 8, "a blended (red+green) stretch exists (%d frames)" % len(mixed))
    if mixed:
        dur = mixed[-1][0] - mixed[0][0]
        check(0.9 < dur < 1.7, "the visible blend lasts ~1.2 s of the 1.5 s ramp (%.2f s)" % dur)
        ramp = [f for p, f in seq if mixed[0][0] <= p <= mixed[-1][0]]
        check(all(b >= a - 0.02 for a, b in zip(ramp, ramp[1:])),
              "the blend moves monotonically from red to green")
        half = next(p for p, f in seq if f > 0.5)
        # red = 4 s long, 1.5 s fade: the 50 % point is 0.75 s before red's end
        check(abs(half - (0.2 + 4.0 - 0.75)) < 0.4, "mid-fade (50%%) at %.2f s, expected near 3.45 s" % half)
        steps = sorted({round(f, 2) for p, f in mixed})
        check(len(steps) >= 12, "alpha takes %d distinct values: a smooth ramp, not a jump" % len(steps))
    aud = [x for x in r.cap.audio if 1.0 < x[0] < 6.0]
    silent = [x for x in aud if x[1] < 0.01]
    check(not silent, "audio continuous through the cross-fade (%d silent blocks)" % len(silent))
    zc = [x[2] for x in aud if 3.2 < x[0] < 3.9]
    check(bool(zc), "audio present mid-fade")
    r.stop()


SCENARIOS = {"cut": scenario_cut, "loop": scenario_loop, "fade": scenario_fade}


def main():
    names = sys.argv[1:] or list(SCENARIOS)
    d = tempfile.mkdtemp(prefix="mixer-selftest-")
    media = make_media(d)
    for n in names:
        SCENARIOS[n](media)
    print("\nRESULT: %s" % ("FAILED (%d)" % len(FAILS) if FAILS else "all checks passed"))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
