# Media Player — 4K HDMI digital signage for Raspberry Pi 5

A self-hosted video/image player for the Pi's HDMI output, configured from a
web interface. Built for the **Raspberry Pi 5 / Debian 13**, console (no
desktop), playing directly to DRM/KMS via **GStreamer** — **native 4K** with
**hardware HEVC decode** and **optional transcoding**.

> This is the Pi 5 / 4K adaptation of
> [`mediaplayer-rpi3-1080p`](https://github.com/davidblikstad-spec/mediaplayer-rpi3-1080p).
> On the Pi 3, 4K was impossible so every oversized upload was force-transcoded
> down to 1080p. The Pi 5 plays 4K natively, so transcoding becomes an **option**
> you choose — including converting H.264 to **HEVC** to gain hardware decode.

## What's different from the Pi 3 build

- **4K playback.** HEVC/H.265 is decoded in **hardware** (`rpi-hevc-dec`), smooth
  to 4Kp60 with the CPU near idle. H.264 and other codecs are **software**-decoded
  on the Cortex-A76 cores (fine at 1080p, heavy at 4K).
- **Transcoding is optional and policy-driven**, not automatic. The new headline
  option re-encodes to **HEVC at the original resolution** so 4K H.264 footage
  moves onto the hardware decoder. (The Pi 5 has no hardware *encoder*, so the
  encode is software — slow but one-time.)
- Per-file **playability badges** in the Media Library (`hardware 4K` /
  `software ≤1080p` / `software >1080p — may stutter`).

See [`docs/playback-on-pi5.md`](docs/playback-on-pi5.md) for the hardware detail.

## Features

- **Web UI with login** (username + password, stored hashed; session cookie).
- **Fullscreen HDMI playback** of videos and images via GStreamer on DRM/KMS,
  zero-copy onto a hardware plane — **native 4K** for HEVC.
- **Transcoding (optional):** per-file **→ HEVC** (keep resolution, gain hardware
  decode) or **→ 1080p** (shrink to H.264), plus an auto-on-upload policy. Runs
  in the background with a progress bar.
- **Live TV channels** (NRK1/2/3/Super) as playlist items — resolved fresh from
  NRK's public psapi at play time; optional play-duration before advancing.
- **Playlists** of video + image items, with per-item:
  - **In / out trim** (videos) and **display duration** (images),
  - **Loop count** — a fixed number of times or **always**,
  - **Volume** (0–130) with **fade-in** and **fade-out** (videos),
- **Loop the whole playlist** on/off.
- **Default content** — an item looped forever whenever nothing else is playing.
- **Scheduling** (day-of-week + time) of playing a playlist, stopping, or
  **HDMI-CEC** display **On / Off / Set-as-source**.
- **Manual HDMI-CEC** buttons + adapter detection.
- **Preview files** in the browser, and a **periodic snapshot of the live HDMI
  output**.

## Install (autostart on boot)

```bash
git clone https://github.com/davidblikstad-spec/mediaplayer-rpi5-4k.git
cd mediaplayer-rpi5-4k
sudo ./install.sh
```

`install.sh` installs the GStreamer HEVC HW-decode stack + PyGObject + ffmpeg,
creates the Python venv (with system site-packages so `gi` is importable) and
installs the Python deps, then a systemd service that takes over **tty1** (the
HDMI console) so the player can drive the display, and starts it on boot. The
service is generated with the actual install path and user, so you can clone it
anywhere. Then browse to `http://<pi-ip>:8080` and create your admin account on
first visit.

> The service runs a login session on tty1 (`PAMName=login`) so it becomes the
> active seat session — required to get DRM master and output to HDMI.
> `getty@tty1` is disabled by the installer so the two don't fight over tty1.

For native 4K, make sure the KMS driver (`vc4-kms-v3d`, the Pi 5 default) is
active and your cable/display negotiate 2160p.

## Run manually (for testing)

```bash
cd mediaplayer-rpi5-4k
./venv/bin/python run.py
```
Note: run from the **active console**, not over SSH — the player needs DRM
master to show video, so video output and the live snapshot only work on the
console (or via the systemd service).

## Transcoding, in practice

- Upload your media in the **Media Library**. Each file is badged with how it
  will play on the Pi 5.
- A **4K HEVC** file just plays (hardware) — nothing to do.
- A **4K H.264** file is badged *"software >1080p — may stutter"*. Click
  **→ HEVC** to re-encode it (keeping 4K) onto the hardware decoder, or **→
  1080p** to shrink it. Either runs in the background.
- To do this automatically on upload, set **Settings → Transcoding → Auto-transcode
  on upload** to *To HEVC* or *To 1080p* (default is *Off* — keep originals).

> Software HEVC encoding of 4K is slow on the Pi (well below realtime). It's a
> one-time cost; afterwards playback is hardware-decoded and smooth. For bulk
> jobs, transcode on a faster machine and upload the result.

## Usage notes

- Build a **playlist**, add files, set in/out, loops, volume, fades, **Save**,
  then **▶ Play now** or schedule it.
- **Default content** and **CEC** are configured in *Settings*. Use *Detect* to
  read the HDMI physical address for CEC "set source".
- **In-browser preview** plays the original file; the browser must support the
  codec (mp4/H.264 and webm preview best; HEVC/mkv/avi may not preview in-browser
  but still play fine on HDMI via GStreamer).

## Layout

```
app/            Flask app: config, media, gst player engine, transcode, cec, scheduler, routes
app/templates/  login / setup / index pages
app/static/     app.js + style.css
data/config.json   all configuration (created on first run)
media/          uploaded video/image files
thumbs/         generated thumbnails
previews/       live HDMI snapshot
venv/           Python environment (flask, apscheduler, waitress)
```

## Service control

```bash
journalctl -u mediaplayer -f        # live logs
sudo systemctl restart mediaplayer
sudo systemctl stop mediaplayer
```

## Security

Login is username + password (hashed with Werkzeug PBKDF2). Traffic is plain
HTTP — fine on a trusted LAN. For exposure beyond the LAN, put it behind a
reverse proxy (e.g. Caddy/nginx) with TLS.
