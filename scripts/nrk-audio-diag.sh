#!/usr/bin/env bash
# Diagnose NRK live-audio dropouts. Plays NRK1 audio (video -> fakesink, no DRM
# needed) through candidate alsasink configs in turn; LISTEN and note which stay
# smooth past the first few seconds. Stops the mediaplayer service to free the
# audio device and ALWAYS restarts it. Run with sudo.
#
# Theory: on a live stream alsasink is the clock master ("we are not slaved"),
# so it plays at the HDMI hardware rate while the stream arrives at its own rate
# -> the buffer drains and underruns every few seconds. Forcing the sink to
# slave (provide-clock=false) and resample should track the stream and stay
# smooth.
#
# Usage: sudo scripts/nrk-audio-diag.sh ["alsasink ...full sink desc..."] [SECONDS]
set -u
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
trap 'echo; echo "restarting mediaplayer..."; systemctl start mediaplayer' EXIT

echo "stopping mediaplayer..."; systemctl stop mediaplayer; sleep 1
URL=$(/usr/bin/python3 - <<'PY'
import json, urllib.request as u
d = json.load(u.urlopen("https://psapi.nrk.no/playback/manifest/channel/nrk1", timeout=10))
print([a["url"] for a in d["playable"]["assets"] if a.get("url")][0])
PY
)
[ -n "${URL:-}" ] || { echo "could not resolve NRK url"; exit 1; }

DEV="plughw:CARD=vc4hdmi,DEV=0"
DUR="${2:-30}"
if [ $# -ge 1 ]; then
  SINKS=("$1")
else
  SINKS=(
    "alsasink device=$DEV"
    "alsasink device=$DEV sync=false"
    "alsasink device=$DEV sync=false buffer-time=2000000"
  )
fi

i=0
for SINK in "${SINKS[@]}"; do
  i=$((i+1))
  echo
  echo "############################################################"
  echo "### TEST $i/${#SINKS[@]}: $SINK"
  echo "### LISTEN ~${DUR}s — smooth, or starts dropping after a few s?"
  echo "############################################################"
  GST_DEBUG=1 timeout -k3 "$DUR" gst-launch-1.0 playbin3 uri="$URL" flags=0x13 \
    video-sink=fakesink audio-sink="$SINK" >"/tmp/nrk-test-$i.log" 2>&1
  echo ">>> log: /tmp/nrk-test-$i.log ($(wc -l < /tmp/nrk-test-$i.log) lines)"
  grep -qiE "Unknown PCM|No such device|could not open|cannot find card" "/tmp/nrk-test-$i.log" \
    && { echo ">>> failed to open:"; grep -iE "Unknown PCM|No such|could not open|cannot find" "/tmp/nrk-test-$i.log" | head -1; }
  sleep 1
done
echo
echo "Done. Which TEST number stayed smooth?  (1=baseline, 2=sync=false, 3=sync=false+2s buffer)"
