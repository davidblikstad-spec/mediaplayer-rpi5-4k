"""GStreamer playback backend: hardware-decoded, zero-copy DRM-plane video.

We drive a GStreamer pipeline directly via PyGObject, letting playbin3 auto-plug
the right decoder for the codec and scan the frames out via kmssink onto a DRM
hardware plane (the vc4 HVS) — a zero-copy scanout that holds for 4K.

On the Pi 5 the decode path depends on the codec (see app/transcode.py):

  * HEVC / H.265 -> the dedicated hardware decoder (`rpi-hevc-dec`), smooth up
    to 4Kp60 with the CPU near idle. playbin3 auto-plugs it and kmssink puts the
    decoded frames straight onto a hardware plane (no CPU videoconvert).
  * H.264 and everything else -> **software** decode on the Cortex-A76 cores
    (the Pi 5 dropped the Pi 4's hardware H.264 block). Comfortable at 1080p;
    heavy at 4K — which is why the UI offers transcoding to HEVC.

  * Video + audio: playbin3 with video-sink=kmssink. playbin gives us native
    volume, seeking (used for in/out trim), position/duration queries and EOS.
  * Still images: a small `... ! imagefreeze ! videoconvert ! kmssink` pipeline
    held for the item's display duration.

Only one pipeline holds the DRM plane at a time; the other is forced to NULL
(which releases it) before the active one starts. End-of-stream on the bus is
surfaced to listeners as {"event": "end-file", "reason": "eof"}, the contract
PlayerEngine consumes, so the playlist/loop/fade engine is codec-agnostic.
"""
import os
import subprocess
import threading
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

from . import config, media, nrk  # noqa: E402

Gst.init(None)

# Live streams start clock-synced (sync=true) so GStreamer aligns audio/video
# from the first frames, then flip to sync=false after this many seconds to
# free-float smoothly (the live clock is too jittery to keep syncing to without
# dropouts). The first few seconds are drop-free, so the flip happens before any
# starvation would begin.
STREAM_SYNC_HOLD_S = 3.0
# Optional extra audio delay (ms) applied after the flip, as a manual nudge if
# the auto-alignment leaves a residual offset. Overridden by stream_av_delay_ms.
STREAM_AV_DELAY_MS = 0
# Once free-floating, a live stream's audio can slowly drift from video as the
# ALSA hardware clock and the stream clock diverge. Every this-many seconds we
# briefly re-engage clock sync (the same STREAM_SYNC_HOLD_S dance as startup) to
# re-align, bounding that drift. 0 disables. Overridden by
# stream_resync_interval_s. Each cycle costs a tiny audio hiccup as the sink
# re-engages the clock — negligible once an hour.
STREAM_RESYNC_INTERVAL_S = 3600


class GstPlayer:
    """Owns the GStreamer pipelines and exposes mpv-style playback primitives."""

    def __init__(self, log=print):
        self.log = log
        self.event_handlers = []          # list of callables(event_dict)
        self._lock = threading.RLock()
        self.playbin = None               # reused playbin3 for video/audio
        self.imgpipe = None               # per-image pipeline (rebuilt each time)
        self._active_bus = None           # bus the watcher polls
        self._gen = 0                     # cancels stale image timers
        self._img_timer = None
        self._cur_path = None             # source file currently shown
        self._cur_start = 0.0             # trim-in offset, for screenshots
        self._cur_kind = None
        self._volume = 100                # last requested volume (0..100)
        self._audio_device = None         # ALSA device, or None = playbin default
        self._paused = False
        self._stop = False
        self._watcher = None
        self._asink = None                   # alsasink element (clock-sync toggle)
        self._adelay = None                  # queue before the sink (A/V delay)
        self._av_delay_ms = STREAM_AV_DELAY_MS
        self._resync_interval_s = STREAM_RESYNC_INTERVAL_S
        self._sync_timer = None              # flips stream audio to free-float
        self._resync_timer = None            # periodic re-sync of stream audio
        self._shot_lock = threading.Lock()   # single-flight HDMI snapshots

    # ---- lifecycle --------------------------------------------------------
    def start(self):
        self._stop = False
        self._blank_console()
        try:
            cfg = config.load()["settings"]
            self.set_audio_device(cfg.get("audio_out"))
            self.set_av_delay(cfg.get("stream_av_delay_ms"))
            self.set_resync_interval(cfg.get("stream_resync_interval_s"))
        except Exception:
            pass
        self._build_playbin()
        self._watcher = threading.Thread(target=self._watch_bus, daemon=True)
        self._watcher.start()

    def _blank_console(self):
        """Clear the text console and hide its cursor so the bare framebuffer
        shows black, not boot/login text. kmssink briefly releases the DRM plane
        between items; with the console buffer emptied, that gap shows black
        instead of flashing the terminal. Runs as the tty1 session owner."""
        for dev in ("/dev/tty1", "/dev/tty0"):
            try:
                with open(dev, "w") as t:
                    # home, clear screen, clear scrollback, hide cursor
                    t.write("\033[H\033[2J\033[3J\033[?25l")
                    t.flush()
                return
            except Exception:
                continue

    def _build_playbin(self):
        pb = Gst.ElementFactory.make("playbin3", "player")
        if pb is None:
            self.log("playbin3 unavailable")
            return
        vsink = Gst.ElementFactory.make("kmssink", "vsink")
        if vsink is not None:
            vsink.set_property("enable-last-sample", True)  # for cheap snapshots
            pb.set_property("video-sink", vsink)
        asink = self._make_audio_sink()
        if asink is not None:
            pb.set_property("audio-sink", asink)
        self.playbin = pb

    def _make_audio_sink(self):
        """Build the audio sink as `queue ! alsasink` so load() can toggle
        clock-sync and an A/V delay per item (streams play sync=false, which
        needs a small audio delay to match video). Falls back to playbin's
        default sink if alsasink is absent."""
        sink = Gst.ElementFactory.make("alsasink", None)
        if sink is None:
            self.log("alsasink unavailable (install gstreamer1.0-alsa); "
                     "using default audio output")
            self._asink = self._adelay = None
            return None
        if self._audio_device:
            sink.set_property("device", self._audio_device)
        q = Gst.ElementFactory.make("queue", None)
        q.set_property("max-size-time", 10 * Gst.SECOND)
        q.set_property("max-size-bytes", 0)
        q.set_property("max-size-buffers", 0)
        binn = Gst.Bin.new("asinkbin")
        binn.add(q)
        binn.add(sink)
        q.link(sink)
        binn.add_pad(Gst.GhostPad.new("sink", q.get_static_pad("sink")))
        self._asink = sink
        self._adelay = q
        return binn

    def restart(self):
        """Tear down and rebuild the pipelines (e.g. after a wedged sink)."""
        with self._lock:
            self._stop_video()
            self._stop_image()
            if self.playbin is not None:
                self.playbin.set_state(Gst.State.NULL)
            self._build_playbin()

    def is_alive(self):
        return self.playbin is not None

    def splash(self, text):
        """Show `text` (white on black) on the HDMI output until the next load()
        — used at boot to display the web-interface URL. Holds the DRM plane via
        its own pipeline (stored as imgpipe, so the next load() tears it down)."""
        with self._lock:
            self._cancel_image_timer()
            self._stop_video()
            self._stop_image()
            self._cur_kind = "splash"
            self._cur_path = None
            try:
                pipe = Gst.parse_launch(
                    "videotestsrc pattern=black is-live=true ! "
                    "video/x-raw,width=1280,height=720,framerate=10/1 ! "
                    "textoverlay name=t valignment=center halignment=center "
                    'line-alignment=center font-desc="Sans, 20" ! kmssink')
            except Exception as e:  # noqa
                self.log("splash pipeline failed: %s" % e)
                return
            pipe.get_by_name("t").set_property("text", text)
            self.imgpipe = pipe
            self._active_bus = pipe.get_bus()
            pipe.set_state(Gst.State.PLAYING)

    # ---- loading / playback ----------------------------------------------
    def load(self, src, *, kind, start=0.0, end=None, hold=None, subtitles=False):
        """Show `src`. kind: 'image' freezes a frame for `hold` seconds;
        'stream' plays a live URL (optionally auto-advancing after `hold`,
        subtitles on/off); anything else plays a local video/audio file, trimmed
        to [start, end]."""
        with self._lock:
            self._cancel_image_timer()
            self._cur_path = src
            self._cur_start = float(start or 0.0)
            self._cur_kind = kind
            self._paused = False
            if kind == "image":
                self._stop_video()
                self._play_image(src, hold)
            elif kind == "stream":
                self._stop_image()
                self._play_video(src, 0.0, None, is_url=True, subtitles=subtitles)
                if hold:                         # live stream, advance after N s
                    self._arm_image_timer(hold)
            else:
                self._stop_image()
                self._play_video(src, self._cur_start, end)

    _TEXT_FLAG = 1 << 2                           # GST_PLAY_FLAG_TEXT

    def _play_video(self, src, start, end, is_url=False, subtitles=False):
        pb = self.playbin
        if pb is None:
            return
        if is_url:                                # subtitles on/off for streams
            flags = pb.get_property("flags")
            flags = (flags | self._TEXT_FLAG) if subtitles else (flags & ~self._TEXT_FLAG)
            pb.set_property("flags", flags)
        # Start clock-synced so A/V aligns from the first frames. Streams then
        # flip to sync=false after STREAM_SYNC_HOLD_S (see _arm_sync_flip): a
        # live clock is too jittery to keep syncing to (causes dropouts), but by
        # then the alignment is established and audio free-floats from it. Local
        # files stay synced for the whole playback.
        if self._asink is not None:
            self._asink.set_property("sync", True)
        if self._adelay is not None:
            self._adelay.set_property(
                "min-threshold-time", self._av_delay_ms * Gst.MSECOND if is_url else 0)
        pb.set_state(Gst.State.READY)            # flush any previous stream
        pb.set_property("uri", src if is_url else Gst.filename_to_uri(src))
        pb.set_state(Gst.State.PAUSED)
        pb.get_state((20 if is_url else 5) * Gst.SECOND)   # wait for preroll
        if not is_url and (start > 0 or end is not None):
            flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE
            stop_type = Gst.SeekType.SET if end is not None else Gst.SeekType.NONE
            stop_ns = int(float(end) * Gst.SECOND) if end is not None else -1
            pb.seek(1.0, Gst.Format.TIME, flags,
                    Gst.SeekType.SET, int(start * Gst.SECOND), stop_type, stop_ns)
        pb.set_property("volume", self._volume / 100.0)
        self._active_bus = pb.get_bus()
        pb.set_state(Gst.State.PLAYING)
        if is_url:
            self._arm_sync_flip()
            self._arm_periodic_resync()

    def _arm_sync_flip(self):
        """After a short synced hold, flip the audio sink to free-float so a live
        stream plays smoothly without re-syncing to its jittery clock."""
        gen = self._gen
        def flip():
            if gen != self._gen or self._asink is None:
                return
            self._asink.set_property("sync", False)
            self.log("stream audio: free-floating (sync off)")
        t = threading.Timer(STREAM_SYNC_HOLD_S, flip)
        t.daemon = True
        self._sync_timer = t
        t.start()

    def _arm_periodic_resync(self):
        """Every _resync_interval_s, briefly re-engage clock sync to re-align a
        long-running stream's audio with video, then free-float again — bounding
        the drift that builds up under sync=false. 0 disables. Reschedules itself
        for as long as the same stream keeps playing (guarded by _gen)."""
        interval = self._resync_interval_s
        if not interval or interval <= 0:
            return
        gen = self._gen
        def resync():
            if gen != self._gen or self._cur_kind != "stream" or self._asink is None:
                return
            self._asink.set_property("sync", True)   # re-align A/V
            self.log("stream audio: periodic re-sync")
            def release():
                if gen != self._gen or self._asink is None:
                    return
                self._asink.set_property("sync", False)
            rt = threading.Timer(STREAM_SYNC_HOLD_S, release)
            rt.daemon = True
            rt.start()
            self._arm_periodic_resync()              # schedule the next cycle
        t = threading.Timer(interval, resync)
        t.daemon = True
        self._resync_timer = t
        t.start()

    def _play_image(self, path, dur):
        pipe = Gst.Pipeline.new("imgpipe")
        src = Gst.ElementFactory.make("filesrc", None)
        dec = Gst.ElementFactory.make("decodebin", None)
        freeze = Gst.ElementFactory.make("imagefreeze", None)
        conv = Gst.ElementFactory.make("videoconvert", None)
        sink = Gst.ElementFactory.make("kmssink", None)
        if not all([pipe, src, dec, freeze, conv, sink]):
            self.log("image pipeline: missing element")
            return
        src.set_property("location", path)
        for e in (src, dec, freeze, conv, sink):
            pipe.add(e)
        src.link(dec)
        freeze.link(conv)
        conv.link(sink)
        # decodebin exposes its src pad only once the image type is known
        dec.connect("pad-added",
                    lambda _dbin, pad: pad.link(freeze.get_static_pad("sink")))
        self.imgpipe = pipe
        self._active_bus = pipe.get_bus()
        pipe.set_state(Gst.State.PLAYING)
        # images don't EOS (imagefreeze loops); advance via a timer instead
        self._arm_image_timer(dur)

    def _arm_image_timer(self, dur):
        try:
            dur = float(dur) if dur else 0.0
        except (TypeError, ValueError):
            dur = 0.0
        if dur <= 0:
            return
        gen = self._gen
        t = threading.Timer(dur, self._image_elapsed, args=(gen,))
        t.daemon = True
        self._img_timer = t
        t.start()

    def _image_elapsed(self, gen):
        if gen != self._gen:
            return
        self._emit({"event": "end-file", "reason": "eof"})

    def _cancel_image_timer(self):
        self._gen += 1
        if self._img_timer is not None:
            self._img_timer.cancel()
            self._img_timer = None
        if self._sync_timer is not None:
            self._sync_timer.cancel()
            self._sync_timer = None
        if self._resync_timer is not None:
            self._resync_timer.cancel()
            self._resync_timer = None

    def _stop_video(self):
        if self.playbin is not None:
            self.playbin.set_state(Gst.State.NULL)

    def _stop_image(self):
        if self.imgpipe is not None:
            self.imgpipe.set_state(Gst.State.NULL)
            self.imgpipe = None

    def stop(self):
        """Blank the screen and play nothing (releases the DRM plane)."""
        with self._lock:
            self._cancel_image_timer()
            self._stop_video()
            self._stop_image()
            self._cur_path = None
            self._cur_kind = None
            self._active_bus = None

    # ---- transport / properties ------------------------------------------
    def toggle_pause(self):
        with self._lock:
            if self._cur_kind == "image" or self.playbin is None:
                return  # a frozen image has nothing to pause
            self._paused = not self._paused
            self.playbin.set_state(
                Gst.State.PAUSED if self._paused else Gst.State.PLAYING)

    def get_pause(self):
        return self._paused

    def set_volume(self, vol):
        try:
            vol = max(0, min(100, float(vol)))
        except (TypeError, ValueError):
            return
        self._volume = vol
        if self.playbin is not None:
            self.playbin.set_property("volume", vol / 100.0)

    def get_volume(self):
        return self._volume

    def get_time_pos(self):
        return self._query(Gst.Format.TIME, "position")

    def get_duration(self):
        return self._query(Gst.Format.TIME, "duration")

    def _query(self, fmt, what):
        pb = self.playbin
        if pb is None or self._cur_kind == "image":
            return None
        try:
            if what == "position":
                ok, val = pb.query_position(fmt)
            else:
                ok, val = pb.query_duration(fmt)
        except Exception:
            return None
        if not ok or val < 0:
            return None
        return val / Gst.SECOND

    def set_audio_device(self, name):
        """Select the ALSA output device. 'auto'/blank → playbin default.
        Stored now; takes effect on the next pipeline (re)build (see restart)."""
        if not name or name == "auto":
            self._audio_device = None
        elif name.startswith("alsa/"):     # tolerate legacy mpv-style values
            self._audio_device = name[len("alsa/"):]
        else:
            self._audio_device = name
        self.log("audio device set to %s" % (self._audio_device or "default"))

    def set_av_delay(self, ms):
        """Set the live-stream audio delay (ms) used to match A/V under
        sync=false. Applies live if a stream is currently playing."""
        try:
            ms = max(0, min(2000, int(ms)))
        except (TypeError, ValueError):
            return
        self._av_delay_ms = ms
        if self._cur_kind == "stream" and self._adelay is not None:
            self._adelay.set_property("min-threshold-time", ms * Gst.MSECOND)
        self.log("stream A/V delay set to %d ms" % ms)

    def set_resync_interval(self, seconds):
        """Set how often (s) a playing live stream re-syncs its audio to video.
        0 = off. Re-arms live if a stream is currently playing."""
        try:
            seconds = max(0, int(seconds))
        except (TypeError, ValueError):
            return
        self._resync_interval_s = seconds
        if self._cur_kind == "stream":
            if self._resync_timer is not None:
                self._resync_timer.cancel()
                self._resync_timer = None
            self._arm_periodic_resync()
        self.log("stream re-sync interval set to %d s" % seconds)

    # DRM/raw fourcc -> GStreamer raw format, for packed 4:2:0 frames we can
    # re-wrap and JPEG-encode cheaply
    _PACKED_420 = {"YU12": "I420", "YV12": "YV12", "NV12": "NV12", "NV21": "NV21"}

    def screenshot(self, path):
        """Refresh the live HDMI preview at `path`. Single-flight + background so
        snapshots never pile up and starve the audio thread. Cheap path: grab the
        frame playbin already decoded (no URL re-decode / AES / network). Falls
        back to a one-frame ffmpeg decode for images or odd pixel layouts."""
        if not self._shot_lock.acquire(blocking=False):
            return False                      # one already in flight; skip

        def run():
            try:
                if not self._grab_from_pipeline(path):
                    self._grab_with_ffmpeg(path)
            finally:
                self._shot_lock.release()
        threading.Thread(target=run, daemon=True).start()
        return True

    def _grab_from_pipeline(self, path):
        """Tap playbin's last decoded frame and JPEG-encode it. Returns False
        (so the caller can fall back) if there's no usable frame."""
        pb = self.playbin
        if pb is None or self._cur_kind == "image":
            return False
        try:
            sample = pb.get_property("sample")
        except Exception:
            sample = None
        if sample is None:
            return False
        st = sample.get_caps().get_structure(0)
        ok_w, w = st.get_int("width")
        ok_h, h = st.get_int("height")
        fmt = self._PACKED_420.get(st.get_string("drm-format") or "")
        if fmt is None:                       # plain system-memory raw?
            f2 = st.get_string("format")
            fmt = f2 if f2 in self._PACKED_420.values() else None
        if not (ok_w and ok_h and fmt):
            return False
        buf = sample.get_buffer()
        ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok:
            return False
        try:
            if mi.size != w * h * 3 // 2:     # not tightly packed -> bail
                return False
            data = bytes(mi.data)
        finally:
            buf.unmap(mi)
        tw = 640
        th = max(2, int(round(tw * h / w)) & ~1)
        desc = ("appsrc name=src format=time "
                "caps=video/x-raw,format=%s,width=%d,height=%d,framerate=25/1 "
                "! videoconvert ! videoscale ! video/x-raw,width=%d,height=%d "
                "! jpegenc quality=70 ! appsink name=out max-buffers=1"
                % (fmt, w, h, tw, th))
        try:
            conv = Gst.parse_launch(desc)
        except Exception:
            return False
        try:
            src = conv.get_by_name("src")
            out = conv.get_by_name("out")
            conv.set_state(Gst.State.PLAYING)
            src.emit("push-buffer", Gst.Buffer.new_wrapped(data))
            src.emit("end-of-stream")
            s = out.emit("try-pull-sample", 4 * Gst.SECOND)
            if not s:
                return False
            b = s.get_buffer()
            ok, mi = b.map(Gst.MapFlags.READ)
            if not ok:
                return False
            try:
                jpg = bytes(mi.data)
            finally:
                b.unmap(mi)
            tmp = path + ".tmp.jpg"
            with open(tmp, "wb") as f:
                f.write(jpg)
            os.replace(tmp, path)
            return True
        except Exception:
            return False
        finally:
            conv.set_state(Gst.State.NULL)

    def _grab_with_ffmpeg(self, path):
        """Fallback: decode one frame from the source with ffmpeg (one thread)."""
        with self._lock:
            src = self._cur_path
            kind = self._cur_kind
        is_url = bool(src) and src.startswith(("http://", "https://"))
        if not src or (not is_url and not os.path.exists(src)):
            return False
        pos = self.get_time_pos()
        if pos is None:
            pos = self._cur_start
        cmd = ["ffmpeg", "-y", "-nostdin", "-threads", "1"]
        if kind not in ("image", "stream") and not is_url:
            cmd += ["-ss", "%.3f" % max(0.0, pos)]
        tmp = path + ".tmp.jpg"
        cmd += ["-i", src, "-frames:v", "1", "-q:v", "3",
                "-vf", "scale=640:-2", tmp]
        try:
            subprocess.run(cmd, capture_output=True, timeout=15)
            if os.path.exists(tmp):
                os.replace(tmp, path)
                return True
        except Exception:
            pass
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
        return False

    # ---- bus watching -----------------------------------------------------
    def _watch_bus(self):
        while not self._stop:
            bus = self._active_bus
            if bus is None:
                time.sleep(0.1)
                continue
            msg = bus.timed_pop_filtered(
                100 * Gst.MSECOND,
                Gst.MessageType.EOS | Gst.MessageType.ERROR)
            if msg is None:
                continue
            if msg.type == Gst.MessageType.EOS:
                self._emit({"event": "end-file", "reason": "eof"})
            elif msg.type == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                self.log("gst error: %s (%s)" % (err, dbg))
                self._emit({"event": "end-file", "reason": "error"})

    def _emit(self, ev):
        for h in list(self.event_handlers):
            try:
                h(ev)
            except Exception as e:  # noqa
                self.log("event handler error: %s" % e)


class PlayerEngine:
    """Drives playback of playlist items through GstPlayer with per-item options."""

    def __init__(self, player, log=print):
        self.player = player
        self.log = log
        self.lock = threading.RLock()
        self.items = []           # resolved item dicts currently playing
        self.index = 0
        self.loop_playlist = False
        self.loops_left = 0       # remaining loops for current item
        self.current = None       # the item dict now playing
        self.playlist_name = None
        self.playing_default = False
        self._gen = 0             # generation counter to cancel stale timers
        player.event_handlers.append(self._on_event)

    # ---- public API -------------------------------------------------------
    def play_playlist(self, playlist):
        with self.lock:
            items = [self._resolve(i) for i in playlist.get("items", [])]
            items = [i for i in items if i]
            if not items:
                self.log("playlist empty: %s" % playlist.get("name"))
                self.play_default()
                return
            self.items = items
            self.index = 0
            self.loop_playlist = bool(playlist.get("loop_playlist", True))
            self.playlist_name = playlist.get("name")
            self.playing_default = False
            self._load_current()

    def reapply(self):
        """Resume playback after a player restart."""
        with self.lock:
            if self.items and not self.playing_default:
                self._load_current()
            else:
                self.play_default()

    def play_default(self):
        with self.lock:
            cfg = config.load()
            item = cfg["settings"].get("default_item")
            self.items = []
            self.playlist_name = None
            self.current = None
            if not item:
                self.playing_default = True
                self._bump_gen()
                self.player.stop()
                return
            ritem = self._resolve(item)
            if not ritem:
                self.playing_default = True
                self.player.stop()
                return
            self.playing_default = True
            self._play_item(ritem)

    def stop(self):
        """Stop the active playlist and fall back to the default item."""
        self.play_default()

    def status(self):
        with self.lock:
            cur = self.current
        return {
            "playing": cur is not None,
            "playing_default": self.playing_default,
            "playlist_name": self.playlist_name,
            "index": self.index,
            "count": len(self.items),
            "current": {
                "file": cur.get("file") or cur.get("name"),
                "type": cur.get("type"),
            } if cur else None,
            "time_pos": self.player.get_time_pos(),
            "duration": self.player.get_duration(),
            "volume": self.player.get_volume(),
            "paused": bool(self.player.get_pause()),
        }

    # ---- internals --------------------------------------------------------
    def _resolve(self, item):
        if item.get("type") == "stream":
            r = dict(item)
            r["_type"] = "stream"
            r["_abs"] = None
            return r
        try:
            ap = media.abs_path(item["file"])
        except Exception:
            return None
        if not os.path.exists(ap):
            self.log("missing file: %s" % item.get("file"))
            return None
        info = media.probe(item["file"])
        r = dict(item)
        r["_abs"] = ap
        r["_type"] = item.get("type") or media.media_type(item["file"])
        r["_duration"] = info.get("duration")
        r["_has_audio"] = info.get("has_audio")
        r["_codec"] = info.get("codec")
        return r

    def _bump_gen(self):
        self._gen += 1
        return self._gen

    def _resolve_stream(self, item):
        if item.get("provider") == "nrk":
            return nrk.resolve(item.get("channel"))
        return item.get("url")

    def _retry_current(self, gen):
        """Re-attempt the current item after a delay (e.g. a live stream that
        was momentarily unreachable), unless playback has moved on."""
        def go():
            with self.lock:
                if gen == self._gen and self.current:
                    self._play_item(self.current)
        t = threading.Timer(5, go)
        t.daemon = True
        t.start()

    def _load_current(self):
        if not self.items:
            self.play_default()
            return
        if self.index >= len(self.items):
            if self.loop_playlist:
                self.index = 0
            else:
                self.play_default()
                return
        item = self.items[self.index]
        loop = item.get("loop", 1)
        # "always"/0/None => infinite (sentinel -1); otherwise N total plays
        if loop in (0, "always", None):
            self.loops_left = -1
        else:
            self.loops_left = max(1, int(loop))
        self._play_item(item)

    def _play_item(self, item):
        self.current = item
        gen = self._bump_gen()
        t = item["_type"]
        eff_len = None
        if t == "image":
            dur = float(item.get("duration") or 10)
            self.player.load(item["_abs"], kind="image", hold=dur)
            eff_len = dur
        elif t == "stream":
            url = self._resolve_stream(item)
            hold = float(item.get("duration") or 0) or None
            if not url:
                self.log("stream unavailable: %s; retrying"
                         % (item.get("name") or item.get("channel")))
                self._retry_current(gen)
                return
            self.player.load(url, kind="stream", hold=hold,
                             subtitles=bool(item.get("subtitles")))
            eff_len = hold
        else:
            tin = float(item.get("in") or 0)
            tout = item.get("out")
            end = None
            if tout not in (None, "", 0):
                end = float(tout)
                eff_len = end - tin
            elif item.get("_duration"):
                eff_len = float(item["_duration"]) - tin
            self.player.load(item["_abs"], kind=t, start=tin, end=end)
        # volume + fades (videos with audio only)
        vol = int(item.get("volume", 100)) if t != "image" else 100
        fade_in = float(item.get("fade_in") or 0) if t != "image" else 0
        fade_out = float(item.get("fade_out") or 0) if t != "image" else 0
        if fade_in > 0:
            self.player.set_volume(0)
            self._start_fade(gen, 0, vol, fade_in, delay=0)
        else:
            self.player.set_volume(vol)
        if fade_out > 0 and eff_len and eff_len > fade_out:
            self._start_fade(gen, vol, 0, fade_out, delay=eff_len - fade_out)

    def _start_fade(self, gen, frm, to, dur, delay):
        def run():
            if delay:
                time.sleep(delay)
            if gen != self._gen:
                return
            steps = max(1, int(dur * 15))
            for s in range(1, steps + 1):
                if gen != self._gen:
                    return
                v = frm + (to - frm) * (s / steps)
                self.player.set_volume(round(v, 1))
                time.sleep(dur / steps)
        threading.Thread(target=run, daemon=True).start()

    def _on_event(self, ev):
        if ev.get("event") != "end-file":
            return
        if ev.get("reason") not in ("eof", "error"):
            return  # stop/quit handled elsewhere or manual
        with self.lock:
            if self.playing_default:
                if self.current:        # default item loops forever
                    self._play_item(self.current)
                return
            if not self.items:
                return
            if self.loops_left == -1:   # infinite loop on this item
                self._play_item(self.current)
                return
            if self.loops_left > 1:     # more plays of this item remain
                self.loops_left -= 1
                self._play_item(self.current)
                return
            self.index += 1             # advance to next item
            self._load_current()


# module-level singletons, wired in __init__
player = None
engine = None
