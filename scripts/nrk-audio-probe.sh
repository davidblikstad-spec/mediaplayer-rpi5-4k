#!/usr/bin/env bash
# Measure NRK live-audio smoothness on the real HDMI sink at a given pipeline
# latency. Prints audio buffers rendered per 2s (steady ~94 = smooth; 0 then a
# burst = starving). Stops the mediaplayer service and ALWAYS restarts it.
# Usage: sudo scripts/nrk-audio-probe.sh [LATENCY_SECONDS]   (default 6)
set -u
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
REPO=/home/david/mediaplayer
LAT="${1:-6}"
trap 'echo; echo "restarting mediaplayer..."; systemctl start mediaplayer' EXIT
echo "stopping mediaplayer..."; systemctl stop mediaplayer; sleep 1
echo "testing with pipeline latency = ${LAT}s  (LISTEN — smooth or dropping?)"

LAT="$LAT" "$REPO/venv/bin/python" -u - <<'PY'
import time, json, os, urllib.request as U
import gi; gi.require_version("Gst","1.0")
from gi.repository import Gst, GLib
Gst.init(None)
LAT=float(os.environ.get("LAT","6"))
url=[a["url"] for a in json.load(U.urlopen("https://psapi.nrk.no/playback/manifest/channel/nrk1",timeout=10))["playable"]["assets"] if a.get("url")][0]
pb=Gst.ElementFactory.make("playbin3"); pb.set_property("uri",url); pb.set_property("flags",0x13)
pb.set_property("video-sink",Gst.ElementFactory.make("fakesink"))
asink=Gst.ElementFactory.make("alsasink"); asink.set_property("device","plughw:CARD=vc4hdmi,DEV=0")
pb.set_property("audio-sink",asink)
pb.use_clock(Gst.SystemClock.obtain())
try: pb.set_latency(int(LAT*Gst.SECOND))
except Exception as e: print("set_latency failed:",e)
bus=pb.get_bus(); bus.add_signal_watch(); t0=time.time()
def on(b,m):
    if m.type==Gst.MessageType.WARNING: print("%6.2f WARN %s"%(time.time()-t0,m.parse_warning()[0].message),flush=True)
    elif m.type==Gst.MessageType.ERROR: print("%6.2f ERROR %s"%(time.time()-t0,m.parse_error()[0].message),flush=True)
bus.connect("message",on)
pb.set_state(Gst.State.PLAYING)
last=[0]
def tick():
    et=time.time()-t0
    try:
        st=asink.get_property("stats"); ok,rend=st.get_uint64("rendered"); ok2,drop=st.get_uint64("dropped")
    except Exception: rend=drop=0
    d=rend-last[0]; last[0]=rend
    print("%6.2f rendered=%d (+%d in 2s; ~94=smooth) dropped=%d"%(et,rend,d,drop),flush=True)
    return True
GLib.timeout_add_seconds(2, tick)
loop=GLib.MainLoop()
GLib.timeout_add_seconds(28, lambda:(loop.quit(),False)[1])
loop.run(); pb.set_state(Gst.State.NULL); print("done"); os._exit(0)
PY
