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

try:                                    # same malloc setting as run.py, so memory figures match
    import ctypes
    ctypes.CDLL("libc.so.6").mallopt(-8, 2)
except Exception:
    pass

import gi  # noqa: E402
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from app import gst as gstmod, mixer  # noqa: E402

W, H, FPS = 320, 180, 25

# Y,U,V of the test colours as they come out of the (BT.709) canvas
COLOURS = {"black": (16, 128, 128), "red": (81, 90, 240), "green": (145, 54, 34),
           "blue": (31, 239, 118), "yellow": (219, 16, 137), "white": (235, 128, 128)}


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
    # a white flash and a 1 kHz beep, both from t=1.0 s to t=1.4 s: A/V sync probe
    ff("-f", "lavfi", "-i", "color=c=red:s=320x180:r=25:d=4,drawbox=x=0:y=0:w=iw:h=ih:"
       "color=white:t=fill:enable='between(t,1,1.4)'", "-f", "lavfi", "-i",
       "sine=f=1000:d=4:r=48000,volume='if(between(t,1,1.4),1,0)':eval=frame",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
       os.path.join(d, "sync.mp4"))
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
        """Common end-of-scenario checks, then tear down."""
        a = [x[0] for x in self.cap.audio]
        gaps = [(round(a[i], 2), round(a[i + 1] - a[i], 2)) for i in range(len(a) - 1)
                if a[i + 1] - a[i] > 0.05]
        check(len(a) > 20 and not gaps,
              "the audio output never stalls, even under silent clips (%d blocks, gaps: %s)"
              % (len(a), gaps[:3]))
        warns = [m for m in self.log_lines if "warning" in m.lower() or "error" in m.lower()
                 or "fail" in m.lower()]
        check(not warns, "nothing warned or failed in the log %s" % warns[:2])
        t0 = time.time()
        self.player.stop()
        if self.player.out is not None:
            self.player.out.stop()
        check(time.time() - t0 < 3, "tears down promptly (%.1f s)" % (time.time() - t0))


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


def scenario_sync(media):
    """A white flash and a beep that start together in the file must reach the
    output within a frame or two of each other."""
    print("scenario: A/V sync")
    r = Rig()
    r.play([item(media, "sync.mp4", **{"_test_dur": 4})])
    time.sleep(5.5)
    flash = next((p for p, yuv, _c in r.cap.frames if classify(yuv)[0] == "white"), None)
    beep = next((p for p, rms, zc in r.cap.audio if rms > 0.05), None)
    check(flash is not None and beep is not None, "flash and beep both reached the sinks")
    if flash is not None and beep is not None:
        d = (beep - flash) * 1000
        check(abs(d) < 60, "beep is %+.0f ms from the flash (limit 60)" % d)
    r.stop()


def scenario_letterbox(media):
    """4:3 video on the 16:9 canvas gets pillarbox bars; a 16:9 still fills it."""
    print("scenario: letterboxing")
    r = Rig()
    r.play([item(media, "yellow.mp4", **{"in": 0, "out": 1.5, "_test_dur": 4}),
            item(media, "blue.png", duration=1.5)])
    time.sleep(5.0)
    fr = r.cap.frames
    yel = [c for p, yuv, c in fr if classify(yuv)[0] == "yellow"]
    blu = [c for p, yuv, c in fr if classify(yuv)[0] == "blue"]
    check(bool(yel) and all(c <= 20 for c in yel), "4:3 clip: black bar at the edge, yellow in the middle")
    check(bool(blu) and all(c > 25 for c in blu), "16:9 still fills the whole frame")
    r.stop()


def scenario_skip(media):
    """Skipping and replacing the playlist must not flash black either, and
    stop() must blank the screen."""
    print("scenario: skip / replace / stop")
    r = Rig()
    items = [item(media, "red.mp4", **{"_test_dur": 6}), item(media, "green.mp4", **{"_test_dur": 6}),
             item(media, "blue.png", duration=3)]
    r.play(items)
    time.sleep(1.5)
    with r.engine.lock:                                  # what POST /api/next does
        r.engine.loops_left = 1
        r.engine.index += 1
        r.engine._load_current()
    time.sleep(1.5)
    r.play([item(media, "yellow.mp4", **{"_test_dur": 4})])   # replaces the playlist
    time.sleep(1.5)
    r.engine.stop()                                      # no default item: black
    time.sleep(1.0)
    runs = r.cap.runs()
    print("  " + describe(runs))
    names = [x[0] for x in runs]
    check(names[1:5] == ["red", "green", "yellow", "black"], "red -> (skip) green -> (replace) yellow -> (stop) black: %s" % names)
    lit = [x for x in runs if x[0] in ("red", "green", "yellow")]
    for a, b in zip(lit, lit[1:]):
        check(b[1] - a[2] < 4.5 / FPS, "%s -> %s without a hold (%.0f ms)" % (a[0], b[0], (b[1] - a[2]) * 1000))
    check(not black_frames_after_start(r.cap) or r.cap.runs()[-1][0] == "black",
          "no black frame before stop()")
    r.stop()


def scenario_pause(media):
    print("scenario: pause / resume")
    r = Rig()
    r.play([item(media, "red.mp4", **{"in": 0, "out": 4, "_test_dur": 6})])
    time.sleep(1.5)
    r.player.toggle_pause()
    n0 = len(r.cap.frames)
    time.sleep(1.5)
    r.player.toggle_pause()
    time.sleep(4.5)
    runs = r.cap.runs()
    print("  " + describe(runs))
    check(not black_frames_after_start(r.cap), "no black while paused or after resuming")
    reds = [x for x in runs if x[0] == "red"]
    total = sum(x[2] - x[1] + 1.0 / FPS for x in reds)
    check(len(reds) == 1 and total > 4.8, "last frame held through the pause; %.2f s of red (2.5 s played + 1.5 s paused + rest)" % total)
    r.stop()


def scenario_still_fade(media):
    """image -> video -> image with 1 s cross-fades."""
    print("scenario: still <-> video cross-fades")
    r = Rig()
    r.play([item(media, "blue.png", duration=3),
            item(media, "red.mp4", **{"in": 0, "out": 3, "_test_dur": 6}),
            item(media, "blue.png", duration=3)], transition=1.0)
    time.sleep(10.5)
    runs = r.cap.runs()
    print("  " + describe(runs))
    names = [x[0] for x in runs if x[0] != "black"]
    check(names[:5] == ["blue", "mix", "red", "mix", "blue"], "blue, fade, red, fade, blue: %s" % names)
    check(not black_frames_after_start(r.cap), "no black frame anywhere")
    mixes = [x for x in runs if x[0] == "mix"]
    check(len(mixes) == 2 and all(0.5 < (m[2] - m[1]) < 1.1 for m in mixes),
          "both fades take ~1 s (%s)" % ["%.2f" % (m[2] - m[1]) for m in mixes])
    r.stop()


def scenario_soak(media):
    """A looping playlist of short items, cut and cross-faded: no leaks."""
    def rss_mb():
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") // (1 << 20)
    print("scenario: soak (leaks)")
    r = Rig()
    items = [item(media, "red.mp4", **{"in": 0, "out": 1.0, "_test_dur": 6}),
             item(media, "blue.png", duration=1.0),
             item(media, "green.mp4", **{"in": 0, "out": 1.0, "_test_dur": 6}),
             item(media, "yellow.mp4", **{"in": 0, "out": 1.0, "_test_dur": 4})]
    r.play(items, transition=0.4, loop=True)
    time.sleep(25)                       # warm-up: the heap settles to its working size
    fds0 = len(os.listdir("/proc/self/fd"))
    th0 = threading.active_count()
    rss0 = rss_mb()
    time.sleep(60)
    fds1 = len(os.listdir("/proc/self/fd"))
    th1 = threading.active_count()
    rss1 = rss_mb()
    nodes = len(r.player.out.pipe.children)
    print("  over 60 s (~60 transitions) after warm-up: fds %d->%d, threads %d->%d, RSS %d->%d MB, out-pipeline elements %d, errors in log: %d"
          % (fds0, fds1, th0, th1, rss0, rss1, nodes,
             len([m for m in r.log_lines if "error" in m.lower() or "fail" in m.lower()])))
    check(fds1 <= fds0 + 6, "file descriptors stable")
    check(th1 <= th0 + 4, "threads stable")
    check(rss1 < rss0 * 1.1 + 15, "memory stable (%d -> %d MB)" % (rss0, rss1))
    check(nodes < 40, "output pipeline does not accumulate elements (%d)" % nodes)
    check(not black_frames_after_start(r.cap), "no black frame in %d frames" % len(r.cap.frames))
    check(not [m for m in r.log_lines if "error" in m.lower() or "fail" in m.lower()], "no errors logged")
    r.stop()


SCENARIOS = {"cut": scenario_cut, "loop": scenario_loop, "fade": scenario_fade,
             "sync": scenario_sync, "letterbox": scenario_letterbox, "skip": scenario_skip,
             "pause": scenario_pause, "stills": scenario_still_fade, "soak": scenario_soak}


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
