"""Durable application log.

The service's stdout does not reach the journal. The unit sets
`StandardOutput=journal`, but combined with `PAMName=login` and
`TTYPath=/dev/tty1` the process's stdout lands somewhere else — most likely
tty1 itself, which `GstPlayer._blank_console()` then clears at startup — so
`journalctl -u mediaplayer` shows only systemd's and PAM's own lines, not even
the "serving on http://..." banner run.py prints with flush=True.

That made every log() call in the app effectively write-only, which matters most
for the events nobody is watching for: PlayerEngine's watchdog recovering a
wedged pipeline would otherwise fix the screen and leave no trace that it ever
had to.

So write to a file we control instead of relying on the service's stdout. data/
is already the app's writable state directory, and rotation keeps the log
bounded on the SD card. Output still goes to stdout as well, so running run.py
by hand stays readable and the journal picks it up if the unit is ever fixed.
"""
import logging
import os
from logging.handlers import RotatingFileHandler

from . import config

LOG_PATH = os.path.join(config.DATA_DIR, "mediaplayer.log")
MAX_BYTES = 1024 * 1024
BACKUPS = 3

_LOGGER_NAME = "mediaplayer"


def make_log():
    """Return a log(msg) callable, the shape the rest of the app expects.

    Safe to call more than once: handlers are attached only on first use, so a
    second call returns another callable onto the same single set of handlers
    rather than duplicating every line.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        logger.propagate = False
        fmt = logging.Formatter("%(asctime)s [mediaplayer] %(message)s",
                                "%Y-%m-%d %H:%M:%S")
        try:
            fh = RotatingFileHandler(LOG_PATH, maxBytes=MAX_BYTES,
                                     backupCount=BACKUPS)
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError as e:  # read-only SD card, full disk, bad perms
            print("[mediaplayer] cannot open log %s: %s" % (LOG_PATH, e),
                  flush=True)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    def log(msg):
        # Never let logging take the caller down — several call sites are
        # background threads (timers, the bus watcher, the watchdog) whose
        # death would be silent and permanent.
        try:
            logger.info("%s", msg)
        except Exception:  # noqa
            pass

    return log
