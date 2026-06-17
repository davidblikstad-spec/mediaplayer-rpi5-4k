#!/usr/bin/env bash
# Direct DRM-plane playback test, bypassing our app. GStreamer auto-plugs the
# decoder for the file's codec (the Pi 5's hardware HEVC decoder for H.265,
# software for H.264) and kmssink scans the frames out onto a DRM hardware plane
# (the vc4 HVS) — the zero-copy path that gives smooth 4K when the decoder is the
# hardware one. Run with sudo. Stops the mediaplayer service, runs the pipeline
# on seat0/tty1, prints fps/dropped + which decoder was plugged, then ALWAYS
# restores the service.
#
# Usage: sudo scripts/gst-test.sh [FILE]
set -u
FILE="${1:-/home/david/mediaplayer/media/sample-4k-hevc.mp4}"
UNIT=gsttest
LOG=/tmp/gst-test.log
PIPE_SINK="${SINK:-kmssink}"   # override with SINK=... for variants

cleanup() {
  echo "--- cleanup: restoring mediaplayer service ---"
  systemctl stop "$UNIT" 2>/dev/null
  systemctl reset-failed "$UNIT" 2>/dev/null
  systemctl start mediaplayer
  echo "mediaplayer restarted."
}
trap cleanup EXIT

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
exec > >(tee /tmp/gst-test-result.txt) 2>&1
command -v gst-launch-1.0 >/dev/null || {
  echo "GStreamer not installed. Run:"
  echo "  sudo apt install -y gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad"
  exit 1; }
[ -f "$FILE" ] || { echo "file not found: $FILE"; exit 1; }

echo "=== GStreamer auto-decode + $PIPE_SINK (DRM hardware plane) ==="
echo "file: $FILE"
# Report what decoder GStreamer picks for this file (v4l2sl*dec = Pi 5 hardware
# HEVC; avdec_* = software). GST_DEBUG=GST_ELEMENT_FACTORY:4 logs every plug.
echo "--- decoder GStreamer plugs for this file ---"
GST_DEBUG=GST_ELEMENT_FACTORY:4 gst-launch-1.0 -q filesrc location="$FILE" \
  ! decodebin ! fakesink 2>&1 | grep -oiE "plugged.*(v4l2[a-z0-9]*dec|avdec_[a-z0-9]+|[a-z0-9]+dec)" | sort -u | tail -5

systemctl stop mediaplayer
sleep 2
: > "$LOG"

# decodebin auto-plugs the right decoder (hardware HEVC or software H.264);
# fpsdisplaysink wraps the real sink and reports rendered/dropped frame counts
# so we get an objective smoothness number.
systemd-run --unit="$UNIT" --collect \
  -p User=david -p PAMName=login -p TTYPath=/dev/tty1 \
  -p StandardInput=tty -p "StandardOutput=append:$LOG" -p "StandardError=append:$LOG" \
  -p "SupplementaryGroups=video render input audio tty" \
  --setenv=XDG_RUNTIME_DIR=/run/user/1000 \
  gst-launch-1.0 filesrc location="$FILE" ! decodebin ! \
     fpsdisplaysink video-sink="$PIPE_SINK" text-overlay=false sync=true silent=false

echo "playing ~16s — WATCH THE TV for smoothness..."
sleep 16

echo "=== fps / dropped (fpsdisplaysink; want fps≈framerate, dropped≈0) ==="
grep -oE "rendered: [0-9]+, dropped: [0-9]+, current: [0-9.-]+, average: [0-9.-]+" "$LOG" | tail -8
echo "=== pipeline state / errors ==="
grep -iE "ERROR|not-negotiated|fail|no element|cannot|missing|WARNING|Setting pipeline" "$LOG" | grep -viE "GST_DEBUG" | tail -10
echo "=== done (service restored on exit) ==="
