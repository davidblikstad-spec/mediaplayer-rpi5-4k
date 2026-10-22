# Video playback on the Raspberry Pi 5 — 4K, codecs & transcoding

This documents how playback works on this machine (Raspberry Pi 5 / Debian 13,
**console — no desktop**, GStreamer on DRM/KMS) and what changed moving up from
the Pi 3 / 1080p build.

## TL;DR

- The Pi 5 outputs **4K** (dual 4Kp60 HDMI) and has a **hardware HEVC/H.265
  decoder** (the `rpi-hevc-dec` V4L2 block) — but we **deliberately don't use
  it**: it miscodes some streams into a green screen (see *Hardware HEVC decode
  is disabled* below). `app/gst.py` demotes it to rank 0, so **every codec is
  software-decoded** and **1080p is the comfortable ceiling** for all of them.
  The player uses GStreamer `playbin3 ! kmssink`, scanned out onto a DRM plane.
- The Pi 5 has **no hardware H.264 decoder** (the Pi 4's `bcm2835-codec` block
  was dropped). H.264 and other codecs are **software-decoded** on the
  Cortex-A76 cores: comfortable at 1080p, heavy at 4K.
- The Pi 5 has **no hardware video encoder** either, so transcoding is software
  (ffmpeg `libx265` / `libx264`) — correct but slow.
- Therefore **transcoding is optional**, not mandatory as on the Pi 3 (which
  couldn't do 4K at all and had to shrink everything to 1080p).

## What the hardware actually exposes

On this Pi 5, `/sys/class/video4linux/*/name` shows:

```
video19: rpi-hevc-dec      <- hardware HEVC/H.265 decoder (up to 4Kp60)
video20..35: pispbe-*       <- camera ISP back-end, NOT video codecs
```

There is **no** H.264/H.265 *encoder* node and **no** H.264 *decoder* node. So:

| Codec        | ≤1080p           | 4K (2160p)                       |
|--------------|------------------|----------------------------------|
| HEVC / H.265 | software (fine)  | software (**heavy**) — hardware decoder disabled, see below |
| H.264        | software (fine)  | software (**heavy**, may stutter) |
| VP9 / AV1 /… | software (fine)  | software (heavy)                 |

> `app/transcode.py:playback_mode()` still reports HEVC as `hw`, so the Media
> Library badges overstate HEVC files. With the hardware decoder disabled the
> honest rule is simply **≤1080p = fine, >1080p = heavy**, whatever the codec.

`app/transcode.py:playback_mode()` returns `hw` / `sw` / `heavy` from these
rules, and the Media Library badges each file accordingly.

## Playback path (in this repo)

- **`app/mixer.py`** — the default player since the gapless rework: one
  persistent output pipeline, items fed into it. See *Gapless playback* below.
  Everything in the next two bullets describes the **legacy** `GstPlayer`
  (`"gapless": false`), which is still in `app/gst.py` as the fallback.
- **`app/gst.py`** — `GstPlayer` drives `playbin3` with `video-sink=kmssink`.
  playbin auto-plugs the decoder for the codec (hardware HEVC, or software for
  the rest). kmssink performs the vc4 atomic commit that puts a decoded NV12
  dmabuf straight onto a hardware plane — zero-copy scanout that holds at 4K.
  Nothing in the pipeline caps the resolution; for HEVC there is no
  `videoconvert` in the path.
- Still images: `filesrc ! decodebin ! imagefreeze ! videoconvert ! kmssink`.
- Only one pipeline holds the DRM plane at a time; the other is set to NULL
  (releasing the plane) before the active one starts.

## Gapless playback — `app/mixer.py`

```
playbin3 (item A) -> videoconvert ! videoscale ! I420 canvas ! appsink --\
playbin3 (item B) -> ...                                                  +-> appsrc -> compositor -> videoconvert -> kmssink
image / splash    -> ... ! imagefreeze num-buffers=1 ! appsink -----------/
playbin3 (audio)  -> audioconvert ! audioresample ! F32 48k ! appsink -> appsrc -> audiomixer -> queue(leaky) -> alsasink
```

- **kmssink never renegotiates.** The canvas (size, 30 fps, BT.709) is fixed when
  the output pipeline starts, so the DRM plane is never released or re-modeset
  between items. A one-frame-per-second black layer sits underneath for "nothing
  playing". Cost: video is scaled/blended in system memory instead of being
  scanned out zero-copy — roughly 30 % of a core for 1080p on a 1080p canvas and
  65 % on a 4K canvas on a desktop x86 core; the Pi will be slower. Use
  *Settings → Output resolution* to trade sharpness for CPU.
- **Timing.** Item pipelines run with `sync=false` appsinks, so they decode
  ahead only as far as the bounded appsrc queues (~0.3 s) allow. Each buffer's
  running time is mapped onto the output timeline as `origin + running_time`,
  where the origin is chosen when the item's first buffer arrives: the previous
  item's end (a cut), `end − fade` (a fade), or now (a skip). Audio and video of
  an item share one origin, so they stay in sync.
- **Preloading.** `PlayerEngine` asks the player to `preload()` the next item
  ~4 s before the current one ends (a `playbin3` prerolled to PAUSED); `load()`
  of the same item then only switches it to PLAYING. Live streams are not
  preloaded (they resolve a fresh URL at play time).
- **Fades.** The incoming item's compositor pad ramps `alpha` 0→1 and its
  audiomixer pad `volume` sin(0→π/2); the outgoing item's volume follows cos.
  The outgoing item's pads are released once the incoming item has been on
  screen for the fade length.
- **Item volume / fade-in / fade-out** still drive the item's `playbin3` volume.
- **Audio can't take the picture down:** the audio sink sits behind a leaky
  queue, is not the pipeline clock (see `GstPlayer._make_audio_sink`), and if it
  can't be opened at all the output pipeline falls back to a silent sink.
- **Leaks** that were found and fixed while building it, worth knowing if you
  touch it: PyGObject leaks the `GstBus` (2 fds + memory) of any pipeline that
  has had `bus.set_sync_handler()` called, so buses are polled instead; and the
  appsink callbacks hold the slot, which holds the pipeline, so `_release()`
  disconnects them. `run.py` also sets `M_ARENA_MAX=2`, otherwise glibc's
  per-thread arenas fragment the heap to several hundred MB (it levels off).

`scripts/mixer_selftest.py` is the regression test (see the README).

## Transcoding (optional) — `app/transcode.py`

Two software targets, picked per-file in the UI or applied automatically by the
`transcode_policy` setting:

- **`hevc`** — re-encode to HEVC at the **original resolution** (keep 4K), so
  the file moves onto the hardware decoder. `libx265 -preset medium -crf 28
  -tag:v hvc1`. The headline Pi-5 option: encode once (slowly), then play
  smoothly and cheaply many times. **This is the win 4K H.264 footage wants.**
- **`1080p`** — re-encode to H.264 scaled to fit 1920×1080 (`libx264 -preset
  veryfast -crf 23`): smaller, lower-bandwidth files that software-decode
  comfortably (the legacy Pi-3 behaviour).

`transcode_policy` (Settings → Transcoding): `off` (default — keep originals,
play natively), `hevc` (auto-convert >1080p non-HEVC uploads to 4K HEVC), or
`1080p` (auto-shrink anything over 1080p).

> Software HEVC encoding of 4K on the Pi 5 is **slow** (well below realtime).
> It runs in the background with a progress bar; leave it to finish. The payoff
> is hardware-decoded, butter-smooth 4K playback afterwards.

## Testing

`sudo scripts/gst-test.sh [FILE]` — raw pipeline smoke test. Auto-plugs the
decoder, prints which one (hardware `v4l2sl*dec` for HEVC vs software
`avdec_*`), and reports fps/dropped from `fpsdisplaysink`.

`sudo scripts/gst-player-test.sh [VIDEO] [IMAGE]` — exercises the real
`GstPlayer`: plays, checks position advances, pause/resume, screenshot, image
display, and dumps the plugged elements (confirming hardware HEVC + no
`videoconvert` for H.265 files). Both stop the mediaplayer service to free DRM
master and restore it on exit.

## Hardware HEVC decode is disabled (green screen)

`rpi-hevc-dec` fails on some HEVC streams. Once the driver grows its coefficient
buffer:

```
rpi-hevc-dec 1000800000.codec: phase1_thread: Coeff realloc (600000) OK
```

every frame from then on errors out, once per frame (25/s on a 25 fps file):

```
rpi-hevc-dec 1000800000.codec: phase1_cb: Post wait: 0xffffffff
```

The decoder emits nothing, kmssink scans out an untouched NV12 buffer, and the
HDMI output goes **solid green** (all-zero NV12 renders green). It fails
silently: the pipeline reports no error, and the live snapshot still looks
correct because it decodes from the *source file*, not from the plane. The files
themselves are fine — they software-decode without a single error.

So `config.HW_HEVC_DECODE = False` (in `app/config.py`), which makes `app/gst.py`
set `GST_PLUGIN_FEATURE_RANK=v4l2slh265dec:0` before `Gst.init()` so `playbin3`
auto-plugs `avdec_h265` instead. Software HEVC decode measures **~2.5x realtime
at 1080p** on the four A76 cores — ample for 1080p25/30, not enough for 4K.

That one flag is what the rest of the app keys off, so everything downstream
stays consistent with it:

| | `HW_HEVC_DECODE = False` (default) | `= True` |
|---|---|---|
| HEVC decode | software (`avdec_h265`) | hardware (`v4l2slh265dec`) |
| Media Library badge | by resolution only — `≤1080p` / `>1080p may stutter` | HEVC badged `hardware 4K` |
| **→ HEVC** button | hidden; `/api/transcode/start` refuses the target | shown |
| Auto-transcode *To HEVC* | hidden in Settings, ignored by `auto_target()` | available |

To re-enable the hardware decoder (e.g. on a kernel where it's fixed), flip
`HW_HEVC_DECODE` to `True` and restart. For a one-off test without touching the
flag, set the env var — `os.environ.setdefault` leaves an explicit value alone:

```bash
GST_PLUGIN_FEATURE_RANK=v4l2slh265dec:300 ./venv/bin/python run.py
```

Watch `dmesg -w | grep hevc` while it plays: a per-frame `Post wait: 0xffffffff`
flood means that stream still hits the bug (and it will spam the kernel ring
buffer clean in about a minute).

## Known limitations / follow-ups

- **4K HDMI output** needs the KMS driver (`vc4-kms-v3d`, the Pi 5 default) and
  a cable/display that negotiates 2160p. Check the negotiated mode under
  `/sys/class/drm/card*/modes`.
- **Software HEVC encoding is slow.** There is no hardware encoder on the Pi 5;
  this is inherent. For bulk conversion, do it on a faster machine and upload
  the HEVC result.
- **Live HDMI snapshot** is still captured by decoding one frame from the source
  (kmssink can't be read back) — see `GstPlayer.screenshot`.
- **Audio device selection** is wired through `alsasink` (HDMI / analog),
  selectable in Settings.
