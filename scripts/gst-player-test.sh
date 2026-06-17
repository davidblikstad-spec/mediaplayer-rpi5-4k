#!/usr/bin/env bash
# Exercise the new GstPlayer backend on real hardware, bypassing the Flask app.
# Stops the mediaplayer service (to free the DRM master), runs the Python driver
# on seat0/tty1 as the service user, prints the result, then ALWAYS restores the
# service. Run with sudo.
#
# Usage: sudo scripts/gst-player-test.sh [VIDEO] [IMAGE]
set -u
REPO=/home/david/mediaplayer
PY="$REPO/venv/bin/python"
DRIVER="$REPO/scripts/gst_player_test.py"
VIDEO="${1:-$REPO/media/01._Origo_Solutions_Grand_Opening_4K_1.mp4}"
IMAGE="${2:-}"
UNIT=gstplayertest
LOG=/tmp/gst-player-test.log

cleanup() {
  echo "--- cleanup: restoring mediaplayer service ---"
  systemctl stop "$UNIT" 2>/dev/null
  systemctl reset-failed "$UNIT" 2>/dev/null
  systemctl start mediaplayer
  echo "mediaplayer restarted."
}
trap cleanup EXIT

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
"$PY" -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst" 2>/dev/null || {
  echo "PyGObject not visible to the venv. Install deps first:"
  echo "  sudo apt install -y python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0"
  echo "  then set 'include-system-site-packages = true' in $REPO/venv/pyvenv.cfg"
  exit 1; }
[ -f "$VIDEO" ] || { echo "video not found: $VIDEO"; exit 1; }

echo "=== stopping mediaplayer service ==="
systemctl stop mediaplayer
sleep 2
: > "$LOG"

systemd-run --unit="$UNIT" --collect --wait --pty \
  -p User=david -p PAMName=login -p TTYPath=/dev/tty1 \
  -p StandardInput=tty -p "StandardOutput=append:$LOG" -p "StandardError=append:$LOG" \
  -p "SupplementaryGroups=video render input audio tty" \
  --setenv=XDG_RUNTIME_DIR=/run/user/1000 \
  "$PY" "$DRIVER" "$VIDEO" "$IMAGE"

echo "=== result ==="
cat "$LOG"
echo "=== done (service restored on exit) ==="
