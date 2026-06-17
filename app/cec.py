"""HDMI-CEC control via cec-ctl (v4l-utils, kernel CEC at /dev/cecN).

We register as a Playback device on every call (idempotent) so a logical
address is claimed, then issue the requested message in the same invocation.
"""
import re
import subprocess

from . import config

OSD_NAME = "MediaPlayer"


def _device():
    return config.load()["settings"].get("cec_device", "/dev/cec0")


def _phys():
    return config.load()["settings"].get("cec_phys_addr", "").strip()


def _run(extra_args, timeout=15):
    cmd = ["cec-ctl", "-d", _device(), "--playback", "-o", OSD_NAME] + extra_args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        return {"ok": p.returncode == 0, "cmd": " ".join(cmd), "output": out.strip()}
    except FileNotFoundError:
        return {"ok": False, "cmd": " ".join(cmd), "output": "cec-ctl not installed"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "cmd": " ".join(cmd), "output": "timed out"}


def power_on():
    args = ["--to", "0", "--image-view-on"]
    phys = _phys()
    if phys:
        args += ["--active-source", "phys-addr=" + phys]
    return _run(args)


def power_off():
    return _run(["--to", "0", "--standby"])


def set_source():
    phys = _phys() or detect_phys_addr()
    if not phys:
        return {"ok": False, "cmd": "", "output": "no physical address configured/detected"}
    return _run(["--active-source", "phys-addr=" + phys])


def detect_phys_addr():
    """Parse the adapter's physical address from cec-ctl output."""
    try:
        p = subprocess.run(["cec-ctl", "-d", _device()],
                           capture_output=True, text=True, timeout=10)
        m = re.search(r"Physical Address\s*:\s*([0-9a-fA-F.]+)", p.stdout or "")
        if m:
            val = m.group(1).strip()
            # cec-ctl prints like 1.0.0.0 -> convert to 0x1000
            if "." in val:
                parts = val.split(".")
                if len(parts) == 4:
                    return "0x" + "".join(parts)
            return val
    except Exception:
        pass
    return ""


def info():
    """Return adapter info / topology for the UI."""
    try:
        p = subprocess.run(["cec-ctl", "-d", _device()],
                           capture_output=True, text=True, timeout=10)
        return {"ok": p.returncode == 0,
                "output": ((p.stdout or "") + (p.stderr or "")).strip(),
                "phys_addr": detect_phys_addr()}
    except FileNotFoundError:
        return {"ok": False, "output": "cec-ctl not installed", "phys_addr": ""}
    except Exception as e:
        return {"ok": False, "output": str(e), "phys_addr": ""}


def run_action(action):
    if action == "on":
        return power_on()
    if action == "off":
        return power_off()
    if action == "source":
        return set_source()
    return {"ok": False, "output": "unknown action: %s" % action}
