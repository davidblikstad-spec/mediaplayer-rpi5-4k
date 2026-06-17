"""APScheduler-based scheduling of playlist playback and CEC display actions."""
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config, cec

# 0=Monday .. 6=Sunday  (matches APScheduler day_of_week numbering)
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class Scheduler:
    def __init__(self, engine, log=print):
        self.engine = engine
        self.log = log
        self.sched = BackgroundScheduler()
        self.sched.start()

    def _trigger(self, sch):
        hh, mm = (sch.get("time") or "00:00").split(":")
        days = sch.get("days") or list(range(7))
        dow = ",".join(DAY_NAMES[d] for d in days if 0 <= d < 7) or "*"
        return CronTrigger(day_of_week=dow, hour=int(hh), minute=int(mm))

    def _make_job(self, sch):
        kind = sch.get("kind")
        if kind == "play_playlist":
            pid = sch.get("playlist_id")

            def job():
                cfg = config.load()
                pl = config.get_playlist(cfg, pid)
                if pl:
                    self.log("schedule: play playlist %s" % pl.get("name"))
                    self.engine.play_playlist(pl)
                else:
                    self.log("schedule: playlist %s not found" % pid)
            return job
        if kind == "stop":
            def job():
                self.log("schedule: stop -> default")
                self.engine.stop()
            return job
        if kind == "cec":
            action = sch.get("cec_action", "on")

            def job():
                r = cec.run_action(action)
                self.log("schedule: cec %s -> %s" % (action, "ok" if r.get("ok") else r.get("output")))
            return job
        return None

    def reload(self):
        self.sched.remove_all_jobs()
        cfg = config.load()
        for sch in cfg.get("schedules", []):
            if not sch.get("enabled", True):
                continue
            job = self._make_job(sch)
            if not job:
                continue
            try:
                self.sched.add_job(job, self._trigger(sch), id=sch["id"],
                                   replace_existing=True, misfire_grace_time=60)
            except Exception as e:  # noqa
                self.log("failed to schedule %s: %s" % (sch.get("id"), e))
        self.log("scheduler reloaded: %d job(s)" % len(self.sched.get_jobs()))

    def _last_occurrence(self, sch, now):
        """Most recent datetime <= now this weekly schedule fired (within 7 days)."""
        try:
            hh, mm = (sch.get("time") or "00:00").split(":")
            hh, mm = int(hh), int(mm)
        except (ValueError, AttributeError):
            return None
        days = sch.get("days") or list(range(7))
        for delta in range(0, 8):           # today, then back up to 7 days
            d = now - timedelta(days=delta)
            if d.weekday() in days:
                occ = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if occ <= now:
                    return occ
        return None

    def catch_up(self):
        """On boot, resume whatever the schedule says should be active right now.

        Applies the most recent past CEC action (to restore display power) and
        the most recent past content event: if it was a 'play_playlist' that
        playlist resumes, otherwise we fall back to the default item.
        """
        now = datetime.now()
        cfg = config.load()
        content_best = None     # (occurrence_dt, schedule)
        cec_best = None
        for s in cfg.get("schedules", []):
            if not s.get("enabled", True):
                continue
            occ = self._last_occurrence(s, now)
            if not occ:
                continue
            kind = s.get("kind")
            if kind in ("play_playlist", "stop"):
                if not content_best or occ > content_best[0]:
                    content_best = (occ, s)
            elif kind == "cec":
                if not cec_best or occ > cec_best[0]:
                    cec_best = (occ, s)

        # restore display power first, so the panel is on before content loads
        if cec_best:
            action = cec_best[1].get("cec_action", "on")
            self.log("boot catch-up: cec %s (last scheduled %s)"
                     % (action, cec_best[0].strftime("%a %H:%M")))
            try:
                cec.run_action(action)
            except Exception as e:  # noqa
                self.log("boot catch-up cec failed: %s" % e)

        # restore content
        if content_best and content_best[1].get("kind") == "play_playlist":
            pl = config.get_playlist(cfg, content_best[1].get("playlist_id"))
            if pl:
                self.log("boot catch-up: resume playlist '%s' (last scheduled %s)"
                         % (pl.get("name"), content_best[0].strftime("%a %H:%M")))
                self.engine.play_playlist(pl)
                return
            self.log("boot catch-up: scheduled playlist not found -> default")
        else:
            self.log("boot catch-up: no active playlist scheduled -> default")
        self.engine.play_default()

    def next_runs(self):
        out = {}
        for j in self.sched.get_jobs():
            out[j.id] = j.next_run_time.isoformat() if j.next_run_time else None
        return out
