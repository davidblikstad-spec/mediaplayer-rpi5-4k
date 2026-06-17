"""Exercise the GstPlayer backend without the Flask app or the live service.

Run via scripts/gst-player-test.sh (which grabs DRM master on tty1). Plays the
test video for a few seconds, prints whether playback advanced and which
elements playbin actually plugged: for HEVC you want the hardware decoder
(v4l2sl*dec) feeding kmssink with no CPU videoconvert (zero-copy 4K); for H.264
the software decoder (avdec_h264) is expected instead. Then shows a still image
and stops.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import gst as gstmod  # noqa: E402
from gi.repository import Gst  # noqa: E402

VIDEO = sys.argv[1] if len(sys.argv) > 1 else \
    "/home/david/mediaplayer/media/sample-4k-hevc.mp4"
IMAGE = sys.argv[2] if len(sys.argv) > 2 else None


def dump_pipeline(pb):
    """Print the factory name of every element playbin plugged."""
    names = []

    def walk(bin_):
        it = bin_.iterate_elements()
        while True:
            ok, el = it.next()
            if ok != Gst.IteratorResult.OK:
                break
            fac = el.get_factory()
            names.append(fac.get_name() if fac else el.get_name())
            if isinstance(el, Gst.Bin):
                walk(el)
    walk(pb)
    print("plugged elements:", ", ".join(sorted(set(names))))
    hw = any("v4l2" in n and "dec" in n for n in names)
    conv = any(n in ("videoconvert", "videoscale") for n in names)
    print("  hardware HEVC decoder present:", hw, "(expected for H.265 files)")
    print("  CPU videoconvert/scale present:", conv,
          "(want False for zero-copy; expected True for software H.264)" if conv else "")


def main():
    print("=== GstPlayer test ===")
    print("video:", VIDEO)
    p = gstmod.GstPlayer(log=lambda m: print("[gst]", m))
    p.start()

    events = []
    p.event_handlers.append(lambda ev: events.append(ev))

    p.load(VIDEO, kind="video")
    time.sleep(3)
    dump_pipeline(p.playbin)

    t0 = p.get_time_pos()
    dur = p.get_duration()
    print("after 3s: time-pos=%s duration=%s volume=%s" % (t0, dur, p.get_volume()))
    print("WATCH THE TV — playing ~12s...")
    time.sleep(6)
    t1 = p.get_time_pos()
    print("after 9s: time-pos=%s (advanced: %s)"
          % (t1, (t0 is not None and t1 is not None and t1 > t0)))

    print("testing pause/resume...")
    p.toggle_pause()
    time.sleep(2)
    tp = p.get_time_pos()
    p.toggle_pause()
    time.sleep(2)
    tr = p.get_time_pos()
    print("  paused at %s, resumed to %s (resumed: %s)"
          % (tp, tr, (tp is not None and tr is not None and tr > tp)))

    print("testing screenshot...")
    shot = "/tmp/gst-player-shot.jpg"
    ok = p.screenshot(shot)
    print("  screenshot ok=%s exists=%s size=%s" %
          (ok, os.path.exists(shot),
           os.path.getsize(shot) if os.path.exists(shot) else 0))

    if IMAGE:
        print("showing image %s for 4s..." % IMAGE)
        p.load(IMAGE, kind="image", hold=4)
        time.sleep(5)
        print("  image events so far:", events[-1:] if events else "none")

    print("stopping (screen should blank)...")
    p.stop()
    time.sleep(1)
    print("events seen:", events)
    print("=== done ===")


if __name__ == "__main__":
    main()
