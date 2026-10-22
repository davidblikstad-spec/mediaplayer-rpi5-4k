"""Gapless, cross-fading playback: one output pipeline that never stops.

The original player (GstPlayer in app/gst.py) gives every item its own pipeline
with its own kmssink, so between two items the DRM plane is released and the
screen goes black until the next item has been set up. This player instead owns
one *persistent* output pipeline and feeds it:

    source pipelines (one per item)             output pipeline (never stopped)

    playbin3 ! ... ! appsink --\\
    playbin3 ! ... ! appsink ---+--> appsrc -> compositor -> videoconvert -> kmssink
    image / splash pipeline  --/
    playbin3 ! ... ! appsink (audio) -> appsrc -> audiomixer -> ... -> alsasink

kmssink therefore sees one fixed-size stream for as long as the service runs.
It never renegotiates and never releases the plane, and when an item ends the
compositor simply keeps showing the last frame until the next item's first
frame arrives. There is no black frame, only (in the worst case, when the next
item could not be prepared in time) a frozen one.

Every item is still decoded by a playbin3 of its own, so trimming, seeking,
looping, live streams and subtitles behave exactly as before. Its decoded
frames are scaled/letterboxed to the canvas, re-stamped onto the output
pipeline's timeline and pushed into the mixers. The next item is *preloaded*
(prerolled to PAUSED) a few seconds early, so switching to it costs nothing.

A cross-fade is two items on screen at once: the incoming item's compositor
pad ramps alpha 0 -> 1 while its audio pad ramps up and the outgoing one ramps
down. Nothing is re-encoded or copied for it.

Timing. Every buffer from an item is mapped onto the output timeline with

    out_pts = origin + running_time

where `origin` is picked when the item's first buffer arrives (see _anchor): the
moment the previous item ends (gapless), `previous end - fade` (cross-fade) or
simply "now" (a manual skip). Item pipelines run their appsinks with sync=false,
so how far they decode ahead is bounded only by the appsrc queues.

Events delivered to the PlayerEngine (same contract as GstPlayer, plus two):

    {"event": "end-file", "reason": "eof" | "error"}   current item finished
    {"event": "preload"}                                next item should be preloaded
    {"event": "about-to-end"}                           cross-fade should start now
"""
import itertools
import math
import os
import queue
import re
import threading
import time
import glob

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from . import config, gst as _gst  # noqa: E402

CLOCK_NONE = Gst.CLOCK_TIME_NONE
MSEC = Gst.MSECOND
SEC = Gst.SECOND

# A slot's first frame is stamped at least this far in the future, so it is
# queued and ready by the time the output pipeline wants it.
START_LEAD_NS = 120 * MSEC
# After a pause, resume this far ahead (audio and video queues differ a little).
RESUME_LEAD_NS = 400 * MSEC
# Items that end on a timer (images, live streams with a duration) report "eof"
# this long before their nominal end so the next item can start exactly on it.
HANDOFF_NS = 250 * MSEC
# How far before the cross-fade point to tell the engine to start the next item.
TRIGGER_AHEAD_NS = 300 * MSEC
# Preload the next item this long before the cross-fade (or the end).
PRELOAD_AHEAD_S = 4.0
# How long to keep an outgoing item's pads after its successor's first frame
# is on screen (a hard cut) — covers the compositor's own latency.
RETIRE_MARGIN_NS = 150 * MSEC
# How long to wait before reporting "can't show anything" (no output pipeline).
OUT_RETRY_S = 3.0
# Output timeline tick: ramps, retiring old items, delivering timed events.
TICK_S = 0.02
# Compositor / audiomixer extra latency (ns): tolerance for late upstream data.
MIXER_LATENCY_NS = 40 * MSEC
# Live appsrcs report max-latency 0 unless told otherwise, which the mixers
# (whose live sources report a few ms of min latency) reject — they then fall
# back to waiting for every pad forever, so one silent video stalls the audio.
APPSRC_MAX_LATENCY_NS = 5 * SEC
# How often to log the output's frame counters (shown / dropped / late).
FRAME_STATS_S = 300
# Appsrc queue sizes
VIDEO_QUEUE_FRAMES = 6
AUDIO_QUEUE_S = 0.3

# The canvas is BT.709, what displays expect from 720p up: HD video then passes
# through untouched, and only SD material and stills pay for a conversion.
CANVAS_COLOR = "colorimetry=bt709"

AUDIO_RATE = 48000
AUDIO_CAPS = ("audio/x-raw,format=F32LE,layout=interleaved,rate=%d,channels=2"
              % AUDIO_RATE)


def detect_output_size(log=print):
    """Native resolution of the connected HDMI display (the connector's first,
    i.e. preferred, mode), or 1920x1080 if it can't be read."""
    for conn in sorted(glob.glob("/sys/class/drm/*-HDMI-A-*")):
        try:
            with open(os.path.join(conn, "status")) as f:
                if f.read().strip() != "connected":
                    continue
            with open(os.path.join(conn, "modes")) as f:
                m = re.match(r"(\d+)x(\d+)", f.readline().strip())
            if m:
                return int(m.group(1)), int(m.group(2))
        except OSError:
            continue
    return 1920, 1080


def parse_size(spec):
    m = re.match(r"^\s*(\d{3,5})\s*[x×]\s*(\d{3,5})\s*$", str(spec or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


class _Slot:
    """One item on its way to, on, or off the screen."""
    _ids = itertools.count(1)

    def __init__(self, kind, src, start, end, hold, subtitles):
        self.id = next(self._ids)
        self.kind = kind                      # video | audio | stream | image | splash
        self.src = src
        self.start = float(start or 0.0)
        self.end = None if end in (None, "", 0) else float(end)
        self.hold = float(hold) if hold else None
        self.subtitles = bool(subtitles)
        self.key = (kind, src, round(self.start, 3), self.end, self.subtitles)
        self.pipe = None                      # the source pipeline (playbin3 or custom)
        self.bus = None
        self.pb = None                        # the playbin3, if there is one
        self.vsink = self.asink = None        # appsinks at the end of the source pipeline
        self.sink_handlers = []               # (appsink, handler id): disconnected on dispose
        self.vsrc = self.asrc = None          # appsrcs inside the output pipeline
        self.vpad = self.apad = None          # their compositor / audiomixer pads
        self.dead = False
        self.attached = False
        # timeline (all out-pipeline running time, ns)
        self.mlock = threading.Lock()
        self.origin = None                    # out time of this item's running time 0
        self.reanchor = False                 # pick a new origin at the next buffer
        self.request_at = None                # where the new origin should land
        self.first_out = None                 # out time of the first buffer
        self.last_end = 0                     # end of the last buffer pushed
        self.length_ns = None                 # nominal play length of one pass
        self.E = None                         # nominal out time this pass ends
        # engine signalling, once per pass
        self.eof_sent = self.ate_sent = self.preload_sent = False
        self.eos = False
        # transition bookkeeping
        self.fade_in_ns = 0                   # cross-fade length into this slot
        self.xfade_ns = 0                     # cross-fade length wanted out of it
        self.predecessor = None
        self.successor = None
        self.retire_after_ns = RETIRE_MARGIN_NS
        self.volume = 1.0
        self.audio_delay_ns = 0
        self.vfps = None

    def reset_pass(self):
        self.eof_sent = self.ate_sent = self.preload_sent = False
        self.eos = False


class _Out:
    """The persistent output pipeline: compositor + audiomixer + the real sinks."""

    def __init__(self, width, height, fps, make_vsink, make_asink, log, silent=False):
        self.w, self.h, self.fps = width, height, fps
        self.log = log
        self.silent = silent             # audio goes to a fakesink (device unusable)
        self.ok = False
        self._zorder = itertools.count(1)
        self._warned = set()
        self.late = 0                    # video frames that reached the compositor too late
        self.pipe = Gst.Pipeline.new("out")
        self.bus = self.pipe.get_bus()
        self.pipe.use_clock(Gst.SystemClock.obtain())

        mk = Gst.ElementFactory.make
        self.vmix = mk("compositor", "vmix")
        self.vmix.set_property("background", 1)              # black
        self.vmix.set_property("latency", MIXER_LATENCY_NS)
        vcaps = mk("capsfilter", None)
        vcaps.set_property("caps", Gst.Caps.from_string(
            "video/x-raw,width=%d,height=%d,framerate=%d/1" % (width, height, fps)))
        vconv = mk("videoconvert", None)
        self.vsink = make_vsink()
        # The black layer that is shown when nothing is playing. Not live, and
        # one frame a second: a live source would add its frame duration to the
        # mixer's latency and stall the (live) compositor.
        base = mk("videotestsrc", None)
        base.set_property("pattern", 2)                      # black
        bcaps = mk("capsfilter", None)
        bcaps.set_property("caps", Gst.Caps.from_string(
            "video/x-raw,format=I420,width=%d,height=%d,framerate=1/1,%s"
            % (width, height, CANVAS_COLOR)))

        self.amix = mk("audiomixer", "amix")
        self.amix.set_property("latency", MIXER_LATENCY_NS)
        acaps = mk("capsfilter", None)
        acaps.set_property("caps", Gst.Caps.from_string(AUDIO_CAPS))
        aconv = mk("audioconvert", None)
        ares = mk("audioresample", None)
        # Leaky: if the audio device wedges (see GstPlayer._make_audio_sink) it
        # must not back-pressure into the mixers and from there into the video.
        aq = mk("queue", None)
        aq.set_property("max-size-time", 2 * SEC)
        aq.set_property("max-size-bytes", 0)
        aq.set_property("max-size-buffers", 0)
        aq.set_property("leaky", 2)                          # downstream
        self.asink = mk("fakesink", None) if silent else make_asink()
        if silent:
            self.asink.set_property("sync", True)
        silence = mk("audiotestsrc", None)
        silence.set_property("wave", 4)                      # silence
        silence.set_property("is-live", True)
        scaps = mk("capsfilter", None)
        scaps.set_property("caps", Gst.Caps.from_string(AUDIO_CAPS))

        chain_v = [self.vmix, vcaps, vconv, self.vsink]
        chain_a = [self.amix, acaps, aconv, ares, aq, self.asink]
        for e in chain_v + chain_a + [base, bcaps, silence, scaps]:
            if e is None:
                raise RuntimeError("missing GStreamer element for the output pipeline")
            self.pipe.add(e)
        for chain in (chain_v, chain_a):
            for a, b in zip(chain, chain[1:]):
                if not a.link(b):
                    raise RuntimeError("cannot link %s -> %s" % (a.get_name(), b.get_name()))
        base.link(bcaps)
        bpad = self.vmix.request_pad(self.vmix.get_pad_template("sink_%u"), None, None)
        bcaps.get_static_pad("src").link(bpad)
        bpad.set_property("zorder", 0)
        silence.link(scaps)
        spad = self.amix.request_pad(self.amix.get_pad_template("sink_%u"), None, None)
        scaps.get_static_pad("src").link(spad)


    # -- bus ------------------------------------------------------------
    # Polled, not a sync handler: PyGObject leaks the bus (two file descriptors
    # and its memory) of every pipeline that has had a sync handler set.
    def poll(self):
        """Drain the bus; returns the text of any errors. Warnings are logged once."""
        errors = []
        while True:
            msg = self.bus.pop()
            if msg is None:
                return errors
            if msg.type == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                errors.append("%s: %s (%s)" % (msg.src.get_name() if msg.src else "?", err, dbg))
            elif msg.type == Gst.MessageType.WARNING:
                w, dbg = msg.parse_warning()
                text = str(w.message)
                if text not in self._warned and len(self._warned) < 20:
                    self._warned.add(text)
                    self.log("output pipeline warning: %s (%s)" % (text, dbg))

    # -- lifecycle ----------------------------------------------------
    def start(self, timeout=8):
        self.pipe.set_state(Gst.State.PLAYING)
        ret = self.pipe.get_state(timeout * SEC)[0]
        self.ok = ret != Gst.StateChangeReturn.FAILURE
        return self.ok

    def stop(self):
        self.ok = False
        self.pipe.set_state(Gst.State.NULL)

    def now_ns(self):
        return self.pipe.get_clock().get_time() - self.pipe.get_base_time()

    # -- slot branches ---------------------------------------------------
    def attach(self, slot, with_video=True, with_audio=True):
        """Create the slot's appsrcs and mixer pads inside the output pipeline."""
        mk = Gst.ElementFactory.make
        z = next(self._zorder)
        if with_video:
            vs = mk("appsrc", None)
            vs.set_property("caps", Gst.Caps.from_string(
                "video/x-raw,format=I420,width=%d,height=%d,framerate=0/1,%s"
                % (self.w, self.h, CANVAS_COLOR)))
            vs.set_property("format", Gst.Format.TIME)
            vs.set_property("is-live", True)
            vs.set_property("do-timestamp", False)
            vs.set_property("block", True)
            vs.set_property("min-latency", 0)
            vs.set_property("max-latency", APPSRC_MAX_LATENCY_NS)
            vs.set_property("max-bytes", self.w * self.h * 3 // 2 * VIDEO_QUEUE_FRAMES)
            self.pipe.add(vs)
            pad = self.vmix.request_pad(self.vmix.get_pad_template("sink_%u"), None, None)
            vs.get_static_pad("src").link(pad)
            pad.set_property("zorder", z)
            pad.set_property("alpha", 0.0 if slot.fade_in_ns else 1.0)
            vs.sync_state_with_parent()
            slot.vsrc, slot.vpad = vs, pad
        if with_audio:
            as_ = mk("appsrc", None)
            as_.set_property("caps", Gst.Caps.from_string(AUDIO_CAPS))
            as_.set_property("format", Gst.Format.TIME)
            as_.set_property("is-live", True)
            as_.set_property("do-timestamp", False)
            as_.set_property("block", True)
            as_.set_property("min-latency", 0)
            as_.set_property("max-latency", APPSRC_MAX_LATENCY_NS)
            as_.set_property("max-bytes", int(AUDIO_RATE * 8 * AUDIO_QUEUE_S))
            self.pipe.add(as_)
            pad = self.amix.request_pad(self.amix.get_pad_template("sink_%u"), None, None)
            as_.get_static_pad("src").link(pad)
            pad.set_property("volume", 0.0 if slot.fade_in_ns else 1.0)
            as_.sync_state_with_parent()
            slot.asrc, slot.apad = as_, pad
        slot.attached = True

    def detach(self, slot):
        """Remove the slot's appsrcs and pads. Idempotent."""
        for src, pad, mix in ((slot.vsrc, slot.vpad, self.vmix),
                              (slot.asrc, slot.apad, self.amix)):
            if src is None:
                continue
            src.set_state(Gst.State.NULL)        # also releases a blocked push-buffer
            try:
                src.get_static_pad("src").unlink(pad)
                mix.release_request_pad(pad)
                self.pipe.remove(src)
            except Exception as e:  # noqa
                self.log("detach slot %d: %s" % (slot.id, e))
        slot.vsrc = slot.asrc = slot.vpad = slot.apad = None
        slot.attached = False

    def last_sample(self):
        try:
            return self.vsink.get_property("last-sample")
        except Exception:
            return None

    def frame_counts(self):
        """(shown, dropped, late) since the pipeline started. Shown and dropped
        are the video sink's own counters (kmssink drops a frame it can't put on
        screen in time); late counts source frames the compositor had already
        moved past when they arrived, i.e. the decoder fell behind. None if the
        sink has no statistics."""
        try:
            st = self.vsink.get_property("stats")
            return st.get_value("rendered"), st.get_value("dropped"), self.late
        except Exception:
            return None


class MixerPlayer(_gst.GstPlayer):
    """Drop-in replacement for GstPlayer with gapless playback and cross-fades."""

    supports_transitions = True

    def __init__(self, log=print, make_vsink=None, make_asink=None,
                 canvas=None, fps=None, wait_hdmi=True):
        super().__init__(log=log)
        for factory in ("compositor", "audiomixer", "appsrc", "appsink",
                        "playbin3", "videoscale", "imagefreeze"):
            if Gst.ElementFactory.find(factory) is None:
                raise RuntimeError("GStreamer element '%s' is not installed" % factory)
        self._make_vsink = make_vsink or self._default_vsink
        self._make_asink = make_asink or self._default_asink
        self._canvas_override = canvas
        self._fps_override = fps
        self._wait_hdmi = wait_hdmi
        self.out = None
        self.cur = None                    # slot on screen / most recently started
        self.next = None                   # preloaded slot, prerolled but not started
        self.old = []                      # outgoing slots, waiting to be retired
        self._events = queue.Queue()       # events on their way to the engine
        self._worker = None
        self._out_lock = threading.RLock()
        self._stop_at = None               # out time to blank the screen at (graceful stop)
        self._out_err = {}                 # output-pipeline error text -> when last logged
        self._frame_stats_s = FRAME_STATS_S
        self._frame_mark = None            # (out, monotonic time, frame_counts()) last logged

    # ---- sinks ---------------------------------------------------------
    def _default_vsink(self):
        sink = Gst.ElementFactory.make("kmssink", "vsink")
        if sink is None:
            raise RuntimeError("kmssink is not installed (gstreamer1.0-plugins-bad)")
        sink.set_property("enable-last-sample", True)        # for the HDMI snapshot
        return sink

    def _default_asink(self):
        sink = Gst.ElementFactory.make("alsasink", None)
        if sink is None:
            self.log("alsasink unavailable (install gstreamer1.0-alsa); "
                     "using the default audio output")
            return Gst.ElementFactory.make("autoaudiosink", None)
        if self._audio_device:
            sink.set_property("device", self._audio_device)
        # Not the pipeline clock: see GstPlayer._make_audio_sink.
        sink.set_property("provide-clock", False)
        return sink

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        self._stop = False
        self._blank_console()
        try:
            cfg = config.load()["settings"]
            self.set_audio_device(cfg.get("audio_out"))
            self.set_av_delay(cfg.get("stream_av_delay_ms"))
        except Exception:
            pass
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()
        threading.Thread(target=self._dispatch, daemon=True).start()
        # The output pipeline itself is built on first use (splash / load): it
        # has to wait for the HDMI link, which must not hold up the web UI.

    def _canvas(self):
        if self._canvas_override:
            w, h = self._canvas_override
        else:
            spec = None
            try:
                spec = config.load()["settings"].get("output_resolution")
            except Exception:
                pass
            wh = parse_size(spec) if spec and spec != "auto" else None
            w, h = wh or detect_output_size(self.log)
        return max(2, w) & ~1, max(2, h) & ~1

    def _fps(self):
        if self._fps_override:
            return int(self._fps_override)
        try:
            return max(5, min(120, int(config.load()["settings"].get("output_fps") or 30)))
        except Exception:
            return 30

    def _ensure_out(self):
        with self._out_lock:
            if self.out is not None and self.out.ok:
                return True
            if self.out is not None:
                self.out.stop()
                self.out = None
            if self._wait_hdmi:
                _gst.wait_for_hdmi(self.log)
            w, h = self._canvas()
            fps = self._fps()
            silent = False
            while True:
                try:
                    out = _Out(w, h, fps, self._make_vsink, self._make_asink, self.log,
                               silent=silent)
                except Exception as e:  # noqa
                    self.log("output pipeline: %s" % e)
                    return False
                if out.start():
                    break
                why = "; ".join(out.poll()) or "no error posted"
                out.stop()
                if not silent and ("alsa" in why.lower() or "audio device" in why.lower()):
                    # A sound device that can't be opened (wrong device chosen,
                    # card missing) must not take the picture down with it.
                    self._log_out_error("audio device unusable (%s); playing without sound "
                                        "until the next restart" % why.split(" (")[0])
                    silent = True
                    continue
                self._log_out_error("output pipeline failed to start: %s" % why)
                return False
            self.out = out
            self.log("output pipeline up: %dx%d @ %d fps%s"
                     % (w, h, fps, " (silent)" if silent else ""))
            return True

    def _log_out_error(self, text):
        """With the TV off the output pipeline is retried every few seconds:
        say it once (and again every 5 minutes), not 30 000 times a day."""
        key = re.sub(r"\d+", "#", text.split(" (")[0])
        now = time.monotonic()
        if now - self._out_err.get(key, -1e9) > 300:
            self._out_err[key] = now
            self.log(text)

    def is_alive(self):
        return self._worker is not None and self._worker.is_alive()

    def restart(self):
        """Tear everything down; the output pipeline is rebuilt on next use."""
        with self._lock:
            self._drop_everything()
            with self._out_lock:
                if self.out is not None:
                    self.out.stop()
                    self.out = None
        self._ensure_out()

    def _drop_everything(self):
        self._cancel_image_timer()
        for s in [self.cur, self.next] + self.old:
            if s is not None:
                self._dispose(s)
        self.cur = self.next = None
        self.old = []
        self._cur_path = None
        self._cur_kind = None

    # ---- slot construction -----------------------------------------------
    def _video_bin(self):
        w, h = self.out.w, self.out.h
        return Gst.parse_bin_from_description(
            "videoconvert ! videoscale add-borders=true ! capsfilter "
            "caps=video/x-raw,format=I420,width=%d,height=%d,pixel-aspect-ratio=1/1,%s "
            "! appsink name=vsink emit-signals=true sync=false max-buffers=2"
            % (w, h, CANVAS_COLOR), True)

    def _audio_bin(self):
        return Gst.parse_bin_from_description(
            "audioconvert ! audioresample ! capsfilter caps=\"%s\" "
            "! appsink name=asink emit-signals=true sync=false max-buffers=4"
            % AUDIO_CAPS, True)

    def _build(self, kind, src, start, end, hold, subtitles, length=None, volume=None):
        """Create a slot and preroll its source pipeline to PAUSED (the output
        pipeline must be up: see _ensure_out)."""
        slot = _Slot(kind, src, start, end, hold, subtitles)
        if length:
            slot.length_ns = int(float(length) * SEC)
        elif hold:
            slot.length_ns = int(float(hold) * SEC)
        slot.volume = 1.0 if volume is None else max(0.0, float(volume)) / 100.0
        try:
            if kind in ("image", "splash"):
                self._build_still(slot)
            else:
                self._build_playbin(slot)
        except Exception as e:  # noqa
            self.log("could not build %s pipeline: %s" % (kind, e))
            self._dispose(slot)
            return None
        slot.bus = slot.pipe.get_bus()
        if kind == "stream":
            slot.audio_delay_ns = self._av_delay_ms * MSEC
        slot.pipe.set_state(Gst.State.PAUSED)
        timeout = 20 if kind == "stream" else 5
        ret = slot.pipe.get_state(timeout * SEC)[0]
        if ret == Gst.StateChangeReturn.FAILURE:
            self.log("could not preroll %s %s" % (kind, os.path.basename(str(src))))
            self._dispose(slot)
            return None
        if kind in ("video", "audio") and (slot.start > 0 or slot.end is not None):
            self._seek(slot, slot.start, slot.end)
        return slot

    def _build_playbin(self, slot):
        pb = Gst.ElementFactory.make("playbin3", None)
        if pb is None:
            raise RuntimeError("playbin3 unavailable")
        vbin, abin = self._video_bin(), self._audio_bin()
        pb.set_property("video-sink", vbin)
        pb.set_property("audio-sink", abin)
        if slot.kind == "stream":                       # subtitles on/off for streams
            flags = pb.get_property("flags")
            pb.set_property("flags", (flags | self._TEXT_FLAG) if slot.subtitles
                            else (flags & ~self._TEXT_FLAG))
            pb.set_property("uri", slot.src)
        else:
            pb.set_property("uri", Gst.filename_to_uri(slot.src))
        pb.set_property("volume", slot.volume)
        slot.pipe = slot.pb = pb
        slot.vsink = vbin.get_by_name("vsink")
        slot.asink = abin.get_by_name("asink")
        self._connect_sink(slot, slot.vsink, True)
        self._connect_sink(slot, slot.asink, False)

    def _build_still(self, slot):
        """A single canvas-sized frame (an image, or the boot splash). One
        buffer is enough: the compositor repeats the last frame of a pad."""
        w, h = self.out.w, self.out.h
        scale = ("videoconvert ! videoscale add-borders=true ! capsfilter "
                 "caps=video/x-raw,format=I420,width=%d,height=%d,pixel-aspect-ratio=1/1,%s "
                 "! imagefreeze num-buffers=1 ! appsink name=vsink emit-signals=true "
                 "sync=false max-buffers=1" % (w, h, CANVAS_COLOR))
        if slot.kind == "image":
            desc = "filesrc name=src ! decodebin ! " + scale
        else:
            text = ("textoverlay name=t valignment=center halignment=center "
                    "line-alignment=center font-desc=\"Sans, %d\" ! " % max(12, h // 32)
                    if Gst.ElementFactory.find("textoverlay") else "")   # gstreamer1.0-x
            desc = ("videotestsrc pattern=black num-buffers=1 ! "
                    "video/x-raw,width=%d,height=%d,framerate=1/1 ! " % (w, h) + text + scale)
        pipe = Gst.parse_launch(desc)
        if slot.kind == "image":
            pipe.get_by_name("src").set_property("location", slot.src)
        elif pipe.get_by_name("t") is not None:
            pipe.get_by_name("t").set_property("text", slot.src)
        slot.pipe = pipe
        slot.vsink = pipe.get_by_name("vsink")
        self._connect_sink(slot, slot.vsink, True)

    def _connect_sink(self, slot, sink, is_video):
        slot.sink_handlers.append(
            (sink, sink.connect("new-sample", self._on_sample, slot, is_video)))

    def _dispose(self, slot):
        """Stop a slot's pipelines and release its branches in the output."""
        slot.dead = True
        if slot.vsrc is not None or slot.asrc is not None:
            # unblock any appsink callback stuck in push-buffer first
            for src in (slot.vsrc, slot.asrc):
                if src is not None:
                    src.set_state(Gst.State.NULL)
        if slot.pipe is not None:
            slot.pipe.set_state(Gst.State.NULL)
        if slot.attached and self.out is not None:
            self.out.detach(slot)
        self._release(slot)

    @staticmethod
    def _release(slot):
        """Break the reference cycle (the appsink callbacks hold the slot, the
        slot holds the pipeline) so the GStreamer objects are actually freed —
        each leaked pipeline costs file descriptors and megabytes, which adds
        up over weeks of playlist loops."""
        for sink, hid in slot.sink_handlers:
            try:
                sink.disconnect(hid)
            except Exception:
                pass
        slot.sink_handlers = []
        slot.pipe = slot.pb = slot.bus = slot.vsink = slot.asink = None
        slot.predecessor = slot.successor = None

    # ---- the data path ---------------------------------------------------
    def _anchor(self, slot, rt):
        """Map an item running time to the output timeline, choosing the
        item's origin on its first buffer (and again after a seek or resume)."""
        with slot.mlock:
            if slot.origin is None or slot.reanchor:
                floor = self.out.now_ns() + START_LEAD_NS
                want = slot.request_at if slot.request_at is not None else floor
                target = max(want, floor)
                slot.origin = target - rt
                slot.reanchor = False
                slot.request_at = None
                slot.first_out = target if slot.first_out is None else slot.first_out
                slot.E = slot.origin + slot.length_ns if slot.length_ns else None
            return slot.origin + rt

    def _on_sample(self, sink, slot, is_video):
        sample = sink.emit("pull-sample")
        if sample is None or slot.dead:
            return Gst.FlowReturn.OK
        src = slot.vsrc if is_video else slot.asrc
        if src is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        if buf.pts == CLOCK_NONE:
            return Gst.FlowReturn.OK
        rt = sample.get_segment().to_running_time(Gst.Format.TIME, buf.pts)
        if rt == CLOCK_NONE or rt < 0 or rt > (1 << 62):
            return Gst.FlowReturn.OK              # outside the trim segment
        out_pts = self._anchor(slot, rt)
        if not is_video:
            out_pts += slot.audio_delay_ns
        nb = buf.copy()                            # shares the memory, own metadata
        nb.pts = out_pts
        nb.dts = CLOCK_NONE
        dur = buf.duration if buf.duration != CLOCK_NONE else 0
        if is_video and not dur and slot.kind not in ("image", "splash"):
            dur = SEC // max(1, self.out.fps)
        if dur:
            nb.duration = dur
        end = out_pts + dur
        if end > slot.last_end:
            slot.last_end = end
        if is_video and dur and end + MIXER_LATENCY_NS < self.out.now_ns():
            self.out.late += 1
        ret = src.emit("push-buffer", nb)
        return Gst.FlowReturn.OK if ret in (Gst.FlowReturn.OK, Gst.FlowReturn.FLUSHING,
                                             Gst.FlowReturn.EOS) else ret

    # ---- loading ---------------------------------------------------------
    def load(self, src, *, kind, start=0.0, end=None, hold=None, subtitles=False,
             transition=0.0, length=None, volume=None):
        """Show `src`. Same contract as GstPlayer.load, plus:

        transition  cross-fade length (s) to use if the previous item is
                    ending (i.e. we were told "about-to-end"); a skip is a cut.
        length      how long this item plays, if known (s) — needed to
                    cross-fade out of it.
        volume      initial playback volume, 0..100+ (default: the last one).
        """
        with self._lock:
            self._cancel_image_timer()
            self._stop_at = None
            self._cur_path = src
            self._cur_start = float(start or 0.0)
            self._cur_kind = kind
            self._paused = False
            if not self._ensure_out():
                # No display to show it on (TV off, HDMI not up). Report the
                # failure slowly, or the engine would spin through the playlist.
                self._fail_later(OUT_RETRY_S)
                return
            key = _Slot(kind, src, start, end, hold, subtitles).key
            cur = self.cur
            if (cur is not None and not cur.dead and cur.key == key and kind != "stream"
                    and self._same_again(cur, hold, length, volume, transition)):
                return
            slot = None
            if self.next is not None:
                if self.next.key == key and not self.next.dead:
                    slot, self.next = self.next, None
                    self._retune(slot, hold, length, volume)
                else:
                    self._dispose(self.next)
                    self.next = None
            if slot is None:
                slot = self._build(kind, src, start, end, hold, subtitles, length, volume)
            if slot is None:
                self._fail_later(0.05)                   # this item is the problem: move on
                return
            self._start_slot(slot, transition)

    @property
    def current_slot_id(self):
        """Lets the engine drop events that were queued for an item that has
        since been replaced (e.g. by a manual skip)."""
        cur = self.cur
        return cur.id if cur is not None else None

    def _fail_later(self, delay):
        t = threading.Timer(delay, self._emit, args=({"event": "end-file", "reason": "error"},))
        t.daemon = True
        t.start()

    def _retune(self, slot, hold, length, volume):
        """Apply the per-load details to a slot that was preloaded without them."""
        if hold:
            slot.hold = float(hold)
        slot.length_ns = (int(float(length) * SEC) if length else
                          int(float(hold) * SEC) if hold else slot.length_ns)
        if volume is not None:
            slot.volume = max(0.0, float(volume)) / 100.0
            if slot.pb is not None:
                slot.pb.set_property("volume", slot.volume)

    def preload(self, src, *, kind, start=0.0, end=None, hold=None, subtitles=False,
                length=None, volume=None):
        """Prepare the next item (decoder open, first frame ready) without
        showing it. A following load() with the same arguments is instant."""
        with self._lock:
            if self.cur is None or self.cur.dead or self.out is None or not self.out.ok:
                return
            key = _Slot(kind, src, start, end, hold, subtitles).key
            if self.cur.key == key or (self.next is not None and self.next.key == key):
                return
            if self.next is not None:
                self._dispose(self.next)
                self.next = None
            self.next = self._build(kind, src, start, end, hold, subtitles, length, volume)

    @staticmethod
    def _end_of(prev):
        """Out time at which `prev`'s content really ends."""
        if prev.kind in ("image", "splash") or (prev.kind == "stream" and prev.hold):
            return prev.E
        return prev.last_end or prev.E

    def _start_slot(self, slot, transition):
        prev = self.cur
        xf = 0
        request_at = None
        if prev is not None and not prev.dead:
            if prev.ate_sent and transition and prev.E and slot.length_ns and prev.length_ns:
                xf = int(min(float(transition) * SEC, prev.length_ns / 2, slot.length_ns / 2))
                request_at = prev.E - xf
            elif prev.eos or prev.eof_sent:
                request_at = self._end_of(prev)         # gapless: begin where it ends
            slot.predecessor = prev
            prev.successor = slot
            prev.retire_after_ns = xf + RETIRE_MARGIN_NS
            self.old.append(prev)
        slot.fade_in_ns = xf
        slot.xfade_ns = self._clamp_xfade(transition)
        slot.request_at = request_at
        slot.reset_pass()
        self.cur = slot
        is_still = slot.kind in ("image", "splash")
        self.out.attach(slot, with_video=slot.kind != "audio",
                        with_audio=not is_still)
        slot.pipe.set_state(Gst.State.PLAYING)

    @staticmethod
    def _clamp_xfade(seconds):
        try:
            return int(max(0.0, min(5.0, float(seconds or 0))) * SEC)
        except (TypeError, ValueError):
            return 0

    def _same_again(self, slot, hold, length, volume, transition):
        """The item is already on screen: loop it without touching the pipeline
        (a flushing seek for video, a re-armed timer for a still). Returns False
        if that isn't possible and the caller should start a fresh slot."""
        self._retune(slot, hold, length, volume)
        slot.xfade_ns = self._clamp_xfade(transition)
        if slot.kind in ("image", "splash"):
            now = self.out.now_ns()
            begin = slot.E if slot.E and slot.E > now else now
            slot.reset_pass()
            slot.E = begin + slot.length_ns if slot.length_ns else None
            return True
        if not self._seek(slot, slot.start, slot.end):
            return False
        slot.reset_pass()
        while slot.bus.pop() is not None:                # stale EOS of the pass just ended
            pass
        slot.reanchor = True
        slot.request_at = slot.last_end or None          # continue where the last pass ended
        slot.last_end = 0
        slot.pipe.set_state(Gst.State.PLAYING)           # in case it was paused
        return True

    def _seek(self, slot, start, end):
        flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE
        stop_type = Gst.SeekType.SET if end is not None else Gst.SeekType.NONE
        stop_ns = int(float(end) * SEC) if end is not None else -1
        return slot.pipe.seek(1.0, Gst.Format.TIME, flags,
                              Gst.SeekType.SET, int(start * SEC), stop_type, stop_ns)

    def splash(self, text):
        """Show `text` until the next load(). Built off-thread: the output
        pipeline may first have to wait for the HDMI link."""
        threading.Thread(target=self._splash_now, args=(text,), daemon=True).start()

    def _splash_now(self, text):
        with self._lock:
            self._cancel_image_timer()
            self._stop_at = None
            self._cur_kind = "splash"
            self._cur_path = None
            if not self._ensure_out():
                return
            slot = self._build("splash", text, 0, None, None, False)
            if slot is not None:
                self._start_slot(slot, 0)

    def stop(self, graceful=False):
        """Blank the screen and play nothing. `graceful`: the playlist ran out,
        so let what is already queued for the screen finish first — the source
        reports its end up to a third of a second before it is shown."""
        with self._lock:
            cur = self.cur
            if graceful and cur is not None and not cur.dead and self.out is not None:
                end = self._end_of(cur)
                if end and end > self.out.now_ns():
                    self._stop_at = end + MIXER_LATENCY_NS
                    if self.next is not None:
                        self._dispose(self.next)
                        self.next = None
                    return
            self._drop_everything()

    # ---- transport / properties ------------------------------------------
    def toggle_pause(self):
        with self._lock:
            cur = self.cur
            if cur is None or cur.dead or cur.kind in ("image", "splash"):
                return                                   # a still has nothing to pause
            self._paused = not self._paused
            if self._paused:
                cur.pipe.set_state(Gst.State.PAUSED)
            else:
                cur.reanchor = True                      # out time moved on while paused
                cur.request_at = self.out.now_ns() + RESUME_LEAD_NS
                cur.pipe.set_state(Gst.State.PLAYING)

    def set_volume(self, vol):
        try:
            vol = max(0, min(130, float(vol)))
        except (TypeError, ValueError):
            return
        self._volume = vol
        cur = self.cur
        if cur is not None and cur.pb is not None:
            cur.volume = vol / 100.0
            cur.pb.set_property("volume", cur.volume)

    def _query(self, fmt, what):
        cur = self.cur
        if cur is None or cur.dead or cur.pipe is None or cur.kind in ("image", "splash"):
            return None
        try:
            if what == "position":
                ok, val = cur.pipe.query_position(fmt)
            else:
                ok, val = cur.pipe.query_duration(fmt)
        except Exception:
            return None
        if not ok or val < 0:
            return None
        return val / SEC

    def set_resync_interval(self, seconds):
        """Not needed here: audio and video of a stream share one timeline, and
        the output pipeline is clocked by the system clock."""
        try:
            self._resync_interval_s = max(0, int(seconds))
        except (TypeError, ValueError):
            pass

    def set_av_delay(self, ms):
        try:
            self._av_delay_ms = max(0, min(2000, int(ms)))
        except (TypeError, ValueError):
            return
        cur = self.cur
        if cur is not None and cur.kind == "stream":
            cur.audio_delay_ns = self._av_delay_ms * MSEC
        self.log("stream A/V delay set to %d ms" % self._av_delay_ms)

    # ---- snapshots ---------------------------------------------------------
    def _last_sample(self):
        return self.out.last_sample() if self.out is not None else None

    # ---- worker: bus events, ramps, timed events -----------------------------
    def _run(self):
        while not self._stop:
            try:
                time.sleep(TICK_S)
                with self._lock:
                    events = self._poll_buses() + self._tick()
                for ev in events:
                    self._events.put(ev)
            except Exception as e:  # noqa - the worker must never die
                self.log("player worker error: %s" % e)
                time.sleep(0.2)

    def _poll_buses(self):
        """EOS / errors of the source pipelines (the bus is polled, not
        watched: see _Out.poll)."""
        events = []
        for slot in [self.cur, self.next] + list(self.old):
            if slot is None or slot.dead or slot.bus is None:
                continue
            while True:
                msg = slot.bus.pop()
                if msg is None:
                    break
                if msg.type == Gst.MessageType.EOS:
                    events += self._on_item_event("eos", slot, None)
                elif msg.type == Gst.MessageType.ERROR:
                    err, dbg = msg.parse_error()
                    events += self._on_item_event("error", slot, "%s (%s)" % (err, dbg))
        return events

    def _dispatch(self):
        """Delivers events to the engine on its own thread, so a slow handler
        (it may preload the next item) never stalls the ramps in _tick."""
        while not self._stop:
            try:
                ev = self._events.get(timeout=0.5)
            except queue.Empty:
                continue
            self._emit(ev)

    def _on_item_event(self, kind, slot, text):
        if kind == "eos":
            slot.eos = True
            if slot is not self.cur or slot.kind in ("image", "splash") or slot.eof_sent:
                return []
            slot.eof_sent = True
            return [{"event": "end-file", "reason": "eof", "slot": slot.id}]
        if kind == "error":
            self.log("gst error: %s" % text)
            if slot is self.cur and not slot.eof_sent:
                slot.eof_sent = True
                return [{"event": "end-file", "reason": "error", "slot": slot.id}]
            if slot is self.next:
                self._dispose(slot)
                self.next = None
        return []

    def _tick(self):
        out = self.out
        if out is None:
            return []
        events = []
        for text in out.poll():                          # output pipeline trouble
            self.log("output pipeline error: %s" % text)
            out.ok = False
            events.append({"event": "out-failed"})
        self._log_frame_stats(out)
        now = out.now_ns()
        if self._stop_at is not None and now >= self._stop_at:
            self._stop_at = None
            self._drop_everything()
            return events
        cur = self.cur
        if cur is not None and cur.fade_in_ns and cur.first_out is not None:
            self._ramp(cur, now)
        self._retire_old(now)
        if cur is not None and not cur.dead and not self._paused and cur.E:
            left = cur.E - now
            xf = min(cur.xfade_ns, (cur.length_ns or 0) // 2)
            if not cur.preload_sent and left <= PRELOAD_AHEAD_S * SEC + xf:
                cur.preload_sent = True
                events.append({"event": "preload", "slot": cur.id})
            if xf and not cur.ate_sent and left - xf <= TRIGGER_AHEAD_NS:
                cur.ate_sent = True
                events.append({"event": "about-to-end", "slot": cur.id})
            if (cur.kind in ("image", "splash", "stream") and cur.hold
                    and not cur.eof_sent and left <= HANDOFF_NS):
                cur.eof_sent = True
                events.append({"event": "end-file", "reason": "eof", "slot": cur.id})
        return events

    def _log_frame_stats(self, out):
        """Every few minutes, log how many frames reached the screen and how
        many were lost, so stutter can be told apart from content judder."""
        t = time.monotonic()
        mark = self._frame_mark
        if mark is not None and mark[0] is out and t - mark[1] < self._frame_stats_s:
            return
        counts = out.frame_counts()
        if counts is None:
            return
        self._frame_mark = (out, t, counts)
        if mark is None or mark[0] is not out:
            return                                    # new pipeline: just set the baseline
        shown, dropped, late = (a - b for a, b in zip(counts, mark[2]))
        span = t - mark[1]
        self.log("frames, last %d s: %d shown (%.1f fps of %d), %d dropped at the screen, "
                 "%d late from the decoder" % (span, shown, shown / span, out.fps, dropped, late))

    def _ramp(self, cur, now):
        """Drive the cross-fade: the new item's alpha and volume up, the old one's down."""
        p = (now - cur.first_out) / float(cur.fade_in_ns)
        p = min(1.0, max(0.0, p))
        try:
            if cur.vpad is not None:
                cur.vpad.set_property("alpha", p)
            if cur.apad is not None:
                cur.apad.set_property("volume", math.sin(p * math.pi / 2))
            prev = cur.predecessor
            if prev is not None and prev.apad is not None:
                prev.apad.set_property("volume", math.cos(p * math.pi / 2))
        except Exception as e:  # noqa
            self.log("cross-fade ramp: %s" % e)
        if p >= 1.0:
            cur.fade_in_ns = 0

    def _retire_old(self, now):
        """Remove an outgoing slot once the one that replaced it is on screen."""
        for old in list(self.old):
            succ = old.successor
            if succ is not None and not succ.dead:
                if succ.first_out is None:
                    continue                      # nothing of the successor shown yet: keep holding
                if now < succ.first_out + old.retire_after_ns:
                    continue
            self.old.remove(old)
            if succ is not None:
                succ.predecessor = None
            self._dispose(old)
