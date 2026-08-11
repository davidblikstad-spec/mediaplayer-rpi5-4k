# Media Player — 4K HDMI digital signage for Raspberry Pi 5

A self-hosted video/image player for the Pi's HDMI output, configured from a
web interface. Built for the **Raspberry Pi 5 / Debian 13**, console (no
desktop), playing directly to DRM/KMS via **GStreamer**, with **optional
transcoding**.

> **Note:** the Pi 5's hardware HEVC decoder is **disabled** in this build
> (`config.HW_HEVC_DECODE = False`) — it miscodes some streams into a green
> screen. Everything is software-decoded, so **1080p is the practical ceiling**.
> See [*Hardware HEVC decode is disabled*](docs/playback-on-pi5.md) for the
> detail and how to turn it back on.

> This is the Pi 5 / 4K adaptation of
> [`mediaplayer-rpi3-1080p`](https://github.com/davidblikstad-spec/mediaplayer-rpi3-1080p).
> On the Pi 3, 4K was impossible so every oversized upload was force-transcoded
> down to 1080p. The Pi 5 plays 4K natively, so transcoding becomes an **option**
> you choose — including converting H.264 to **HEVC** to gain hardware decode.

## What's different from the Pi 3 build

- **Decoding is all software** on the Cortex-A76 cores — fine at 1080p, heavy at
  4K. The Pi 5's hardware HEVC block would have made 4K HEVC cheap, but it's
  disabled here (green-screen bug, see above), and the Pi 5 never had a hardware
  H.264 decoder at all.
- **Transcoding is optional and policy-driven**, not automatic. With the
  hardware decoder disabled, the useful target is **→ 1080p** (H.264, fits the
  software decoder comfortably). The **→ HEVC** target was built to move 4K
  H.264 onto the hardware decoder and buys nothing while that decoder is off.
- Per-file **playability badges** in the Media Library — note these still assume
  hardware HEVC and so overstate HEVC files; the honest rule is **≤1080p = fine,
  >1080p = heavy**, whatever the codec.

See [`docs/playback-on-pi5.md`](docs/playback-on-pi5.md) for the hardware detail.

## Features

- **Web UI with login** (username + password, stored hashed; session cookie).
- **Fullscreen HDMI playback** of videos and images via GStreamer on DRM/KMS,
  scanned out onto a hardware plane.
- **Transcoding (optional):** per-file **→ 1080p** (shrink to H.264) or
  **→ HEVC** (keep resolution — only useful with the hardware decoder enabled),
  plus an auto-on-upload policy. Runs in the background with a progress bar.
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
- Anything **over 1080p** is software-decoded above the comfortable ceiling and
  may stutter, whatever the codec. Click **→ 1080p** to shrink it to H.264; it
  runs in the background.
- To do this automatically on upload, set **Settings → Transcoding → Auto-transcode
  on upload** to *To 1080p* (default is *Off* — keep originals).
- **→ HEVC** keeps the original resolution and only paid off via the hardware
  decoder, so leave it alone while that decoder is disabled.

> For bulk work, transcode on a faster machine and upload the result — the Pi 5
> has no hardware *encoder*, so every transcode here is software and slow.

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
