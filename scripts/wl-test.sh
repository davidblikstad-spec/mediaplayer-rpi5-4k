#!/usr/bin/env bash
# Concept test: does 1080p play smoothly under a Wayland kiosk compositor (cage)
# with hardware decode on this Pi 3? Run with sudo. It temporarily stops the
# mediaplayer service, runs cage+mpv on seat0/tty1 (like the real service does),
# measures CPU + playback rate + whether the HW decoder is engaged, then ALWAYS
# restarts the mediaplayer service.
#
# Usage:  sudo scripts/wl-test.sh [VO] [HWDEC] [FILE]
#   VO    default: dmabuf-wayland   (try also: gpu)
#   HWDEC default: v4l2m2m          (non-copy; try also: v4l2m2m-copy, no)
set -u

VO="${1:-dmabuf-wayland}"
HWDEC="${2:-v4l2m2m}"
EXTRA="${3:-}"   # extra mpv options, e.g. "--video-sync=display-resample"
FILE="${4:-/home/david/mediaplayer/media/01._Origo_Solutions_Grand_Opening_4K_1.mp4}"
SOCK=/tmp/wltest-mpv.sock
LOG=/tmp/wl-mpv.log
UNIT=wltest
PY=/home/david/mediaplayer/venv/bin/python

cleanup() {
  echo "--- cleanup: stopping test, restoring service ---"
  systemctl stop "$UNIT" 2>/dev/null
  systemctl reset-failed "$UNIT" 2>/dev/null
  rm -f "$SOCK"
  systemctl start mediaplayer
  echo "mediaplayer service restarted."
}
trap cleanup EXIT

if [ "$(id -u)" -ne 0 ]; then echo "run with sudo"; exit 1; fi
# Mirror all output to a file so results can be read back without copy-paste.
exec > >(tee /tmp/wl-test-result.txt) 2>&1
command -v cage >/dev/null || { echo "cage not installed: sudo apt install -y cage"; exit 1; }
[ -f "$FILE" ] || { echo "file not found: $FILE"; exit 1; }

echo "=== test: cage + mpv  VO=$VO HWDEC=$HWDEC ==="
echo "file: $FILE"
systemctl stop mediaplayer
sleep 2
rm -f "$SOCK" "$LOG"

# Run cage+mpv as a transient unit with the same seat/tty acquisition the real
# service uses (login session on tty1 -> active session on seat0 -> DRM master).
systemd-run --unit="$UNIT" --collect \
  -p User=david -p PAMName=login -p TTYPath=/dev/tty1 \
  -p StandardInput=tty -p StandardOutput=journal -p StandardError=journal \
  -p "SupplementaryGroups=video render input audio tty" \
  --setenv=XDG_RUNTIME_DIR=/run/user/1000 \
  cage -s -- mpv --no-config --really-quiet --log-file="$LOG" \
       --vo="$VO" --hwdec="$HWDEC" --loop-file=inf --no-osc $EXTRA \
       --input-ipc-server="$SOCK" "$FILE"

echo "waiting for compositor + playback to start..."
for i in $(seq 1 25); do [ -S "$SOCK" ] && break; sleep 0.5; done
[ -S "$SOCK" ] || { echo "!! mpv IPC socket never appeared; cage/mpv likely failed. Log:"; tail -20 "$LOG" 2>/dev/null; exit 1; }
sleep 4

echo "=== measurements ==="
echo -n "mpv CPU%: "; ps -eo pcpu,comm | awk '$2=="mpv"{print $1; f=1} END{if(!f)print "(no mpv)"}'
echo -n "/dev/video open: "; for f in 10 11 12; do fuser /dev/video$f 2>/dev/null >/dev/null && printf "video%s " "$f"; done; echo
"$PY" - "$SOCK" <<'PY'
import socket, json, sys, time
sock=sys.argv[1]
def q(n):
    s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.connect(sock)
    s.sendall((json.dumps({"command":["get_property",n]})+"\n").encode()); s.settimeout(2); b=b""
    try:
        while b"\n" not in b: b+=s.recv(4096)
    except OSError: pass
    s.close()
    for l in b.split(b"\n"):
        if l.strip():
            m=json.loads(l)
            if "data" in m: return m["data"]
print("  current-vo   :", q("current-vo"))
print("  hwdec-current:", q("hwdec-current"))
print("  video size   : %sx%s @ %s fps" % (q("width"), q("height"), q("container-fps")))
print("  display-fps  :", q("display-fps"), " video-sync:", q("video-sync"))
rates=[]; vodrops=[]; decdrops=[]
d0=q("frame-drop-count") or 0; e0=q("decoder-frame-drop-count") or 0
for i in range(6):
    t1=q("time-pos"); w=time.time(); time.sleep(1.5); t2=q("time-pos")
    if t1 and t2 and t2>t1: rates.append((t2-t1)/(time.time()-w))
d1=q("frame-drop-count") or 0; e1=q("decoder-frame-drop-count") or 0
if rates:
    rates.sort(); print("  PLAYBACK RATE: %.2fx  (1.00 = smooth realtime)" % rates[len(rates)//2])
print("  VO frame drops over ~9s   : %s  (dropped at output -> real drops)" % (d1-d0))
print("  decoder frame drops ~9s   : %s" % (e1-e0))
PY
echo "=== mpv log: hwdec / vo / dmabuf / errors ==="
grep -iE "hwdec|hardware|software|dmabuf|wayland|interop|drmprime|error|fail|using|VO:" "$LOG" 2>/dev/null | grep -viE "EGL_|CONFIG_ID" | head -20
echo "=== done (service will be restored on exit) ==="
