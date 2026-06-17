"""Flask application: auth, web UI, REST API, and wiring of player/scheduler."""
import functools
import os
import socket
import threading
import time

from flask import (Flask, jsonify, redirect, request, send_file,
                   send_from_directory, session, url_for, render_template, abort)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from . import cec, config, media, transcode, nrk, gst as gstmod
from .scheduler import Scheduler

_snap_last = {"t": 0.0}

BOOT_SPLASH_SECONDS = 5


def _primary_ip():
    """Best-effort primary LAN IP of this host."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))      # no traffic; just picks the route's src IP
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "this-pi"


def _public_config(cfg):
    """Config copy safe to send to the browser (no secrets)."""
    import copy
    c = copy.deepcopy(cfg)
    c.get("auth", {}).pop("password_hash", None)
    c.get("settings", {}).pop("secret_key", None)
    return c


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        cfg = config.load()
        if not cfg["auth"].get("password_hash"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "setup required"}), 401
            return redirect(url_for("setup"))
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "auth required"}), 401
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return wrapper


def create_app():
    app = Flask(__name__)
    cfg = config.load()
    app.secret_key = cfg["settings"]["secret_key"]
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024 * 1024  # 16 GB uploads
    # re-read templates from disk on each request so UI edits show on refresh
    # without a service restart (cheap on a single-user admin UI)
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True

    # ---- player + scheduler singletons -----------------------------------
    log = lambda m: print("[mediaplayer]", m, flush=True)
    gstmod.player = gstmod.GstPlayer(log=log)
    gstmod.player.start()
    gstmod.engine = gstmod.PlayerEngine(gstmod.player, log=log)
    app.scheduler = Scheduler(gstmod.engine, log=log)
    app.scheduler.reload()

    # resume whatever the schedule says should be active now (falls back to the
    # default item if nothing is scheduled)
    def _boot_resume():
        try:
            app.scheduler.catch_up()
        except Exception as e:
            log("boot catch-up failed: %s" % e)
            try:
                gstmod.engine.play_default()
            except Exception as e2:
                log("play_default failed: %s" % e2)

    # On boot, show the web-interface URL on screen for a while, then resume.
    try:
        url = "http://%s:%s" % (_primary_ip(), cfg["settings"].get("port", 8080))
        gstmod.player.splash("The Event AS - Media scheduler\n\nOpen in a browser:\n%s" % url)
        log("boot splash: %s" % url)
        t = threading.Timer(BOOT_SPLASH_SECONDS, _boot_resume)
        t.daemon = True
        t.start()
    except Exception as e:
        log("boot splash failed: %s" % e)
        _boot_resume()

    # ================= auth / pages =======================================
    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        cfg = config.load()
        if cfg["auth"].get("password_hash"):
            return redirect(url_for("login"))
        if request.method == "POST":
            u = (request.form.get("username") or "").strip()
            p = request.form.get("password") or ""
            if not u or len(p) < 4:
                return render_template("setup.html", error="Username required, password min 4 chars")

            def m(c):
                c["auth"]["username"] = u
                c["auth"]["password_hash"] = generate_password_hash(p)
            config.update(m)
            session["user"] = u
            return redirect(url_for("index"))
        return render_template("setup.html", error=None)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        cfg = config.load()
        if not cfg["auth"].get("password_hash"):
            return redirect(url_for("setup"))
        if request.method == "POST":
            u = (request.form.get("username") or "").strip()
            p = request.form.get("password") or ""
            if u == cfg["auth"]["username"] and check_password_hash(cfg["auth"]["password_hash"], p):
                session["user"] = u
                return redirect(url_for("index"))
            return render_template("login.html", error="Invalid credentials")
        return render_template("login.html", error=None)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def index():
        return render_template("index.html", username=session.get("user"))

    # ================= state / config =====================================
    @app.route("/api/state")
    @login_required
    def api_state():
        return jsonify({
            "player": gstmod.engine.status(),
            "next_runs": app.scheduler.next_runs(),
            "player_alive": gstmod.player.is_alive(),
        })

    @app.route("/api/config")
    @login_required
    def api_config():
        return jsonify(_public_config(config.load()))

    # ================= media ==============================================
    @app.route("/api/media")
    @login_required
    def api_media():
        items = media.list_media()
        for it in items:
            it["thumb"] = media.thumbnail(it["file"])
            info = media.probe(it["file"])
            it.update(info)
            # How this will play on the Pi 5: "hw" (hardware HEVC), "sw"
            # (software <=1080p), or "heavy" (software >1080p — may stutter,
            # transcoding offered). Non-videos always play.
            it["play_mode"] = (
                transcode.playback_mode(info.get("width"), info.get("height"),
                                        info.get("codec"))
                if it["type"] == "video" else "ok")
        return jsonify(items)

    @app.route("/api/upload", methods=["POST"])
    @login_required
    def api_upload():
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "no file"}), 400
        name = secure_filename(f.filename)
        if not name:
            return jsonify({"error": "bad filename"}), 400
        dest = os.path.join(config.MEDIA_DIR, name)
        f.save(dest)
        resp = {"ok": True, "file": name, "type": media.media_type(name),
                "transcode": {"needed": False}}
        if media.media_type(name) == "video":
            info = media.probe(name)
            w, h, dur = info.get("width"), info.get("height"), info.get("duration")
            # Auto-transcode only if the configured policy asks for it; otherwise
            # the file plays natively (the Pi 5 can decode 4K).
            policy = config.load()["settings"].get("transcode_policy", "off")
            target = transcode.auto_target(w, h, info.get("codec"), policy)
            if target:
                transcode.start(name, target, w or 0, h or 0, dur or 0, log=log)
                frm = "%dx%d" % (w, h) if w and h else (info.get("codec") or "?")
                resp["transcode"] = {"needed": True, "from": frm, "target": target}
        return jsonify(resp)

    @app.route("/api/transcode")
    @login_required
    def api_transcode():
        return jsonify(transcode.jobs_snapshot())

    @app.route("/api/transcode/start", methods=["POST"])
    @login_required
    def api_transcode_start():
        body = request.get_json(force=True)
        rel = body.get("file")
        if not rel:
            return jsonify({"error": "no file"}), 400
        try:
            ap = media.abs_path(rel)
        except ValueError:
            return jsonify({"error": "bad path"}), 400
        if not os.path.exists(ap):
            return jsonify({"error": "not found"}), 404
        if media.media_type(rel) != "video":
            return jsonify({"error": "not a video"}), 400
        target = body.get("target", "hevc")   # "hevc" (keep res) | "1080p"
        info = media.probe(rel)
        transcode.start(rel, target, info.get("width") or 0,
                        info.get("height") or 0, info.get("duration") or 0,
                        log=log)
        return jsonify({"ok": True, "target": target})

    @app.route("/api/transcode/<path:rel>", methods=["DELETE"])
    @login_required
    def api_transcode_abort(rel):
        return jsonify({"ok": transcode.cancel(rel)})

    # ================= live streams =======================================
    @app.route("/api/streams")
    @login_required
    def api_streams():
        return jsonify(nrk.channels())

    @app.route("/api/media/<path:rel>", methods=["DELETE"])
    @login_required
    def api_media_delete(rel):
        try:
            p = media.abs_path(rel)
        except ValueError:
            return jsonify({"error": "bad path"}), 400
        if os.path.exists(p):
            os.remove(p)
        return jsonify({"ok": True})

    @app.route("/media/<path:rel>")
    @login_required
    def serve_media(rel):
        return send_from_directory(config.MEDIA_DIR, rel, conditional=True)

    @app.route("/thumb/<path:name>")
    @login_required
    def serve_thumb(name):
        return send_from_directory(config.THUMB_DIR, name)

    # ================= playlists ==========================================
    @app.route("/api/playlists", methods=["POST"])
    @login_required
    def api_playlist_create():
        body = request.get_json(force=True)

        def m(c):
            pl = {"id": config.new_id(),
                  "name": body.get("name", "Playlist"),
                  "items": [], "loop_playlist": True}
            c["playlists"].append(pl)
            return pl
        return jsonify(config.update(m))

    @app.route("/api/playlists/<pid>", methods=["PUT"])
    @login_required
    def api_playlist_update(pid):
        body = request.get_json(force=True)

        def m(c):
            pl = config.get_playlist(c, pid)
            if not pl:
                return None
            for k in ("name", "items", "loop_playlist"):
                if k in body:
                    pl[k] = body[k]
            return pl
        res = config.update(m)
        if res is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(res)

    @app.route("/api/playlists/<pid>", methods=["DELETE"])
    @login_required
    def api_playlist_delete(pid):
        def m(c):
            c["playlists"] = [p for p in c["playlists"] if p["id"] != pid]
        config.update(m)
        return jsonify({"ok": True})

    # ================= schedules ==========================================
    @app.route("/api/schedules", methods=["POST"])
    @login_required
    def api_schedule_create():
        body = request.get_json(force=True)

        def m(c):
            sch = {"id": config.new_id(), "enabled": True,
                   "name": body.get("name", "Schedule"),
                   "kind": body.get("kind", "play_playlist"),
                   "playlist_id": body.get("playlist_id"),
                   "cec_action": body.get("cec_action", "on"),
                   "time": body.get("time", "08:00"),
                   "days": body.get("days", list(range(7)))}
            c["schedules"].append(sch)
            return sch
        res = config.update(m)
        app.scheduler.reload()
        return jsonify(res)

    @app.route("/api/schedules/<sid>", methods=["PUT"])
    @login_required
    def api_schedule_update(sid):
        body = request.get_json(force=True)

        def m(c):
            for sch in c["schedules"]:
                if sch["id"] == sid:
                    for k in ("enabled", "name", "kind", "playlist_id",
                              "cec_action", "time", "days"):
                        if k in body:
                            sch[k] = body[k]
                    return sch
            return None
        res = config.update(m)
        app.scheduler.reload()
        if res is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(res)

    @app.route("/api/schedules/<sid>", methods=["DELETE"])
    @login_required
    def api_schedule_delete(sid):
        def m(c):
            c["schedules"] = [s for s in c["schedules"] if s["id"] != sid]
        config.update(m)
        app.scheduler.reload()
        return jsonify({"ok": True})

    # ================= settings ===========================================
    @app.route("/api/settings", methods=["PUT"])
    @login_required
    def api_settings():
        body = request.get_json(force=True)

        def m(c):
            for k in ("cec_device", "cec_phys_addr", "default_item",
                      "audio_out", "screenshot_interval", "stream_av_delay_ms",
                      "transcode_policy"):
                if k in body:
                    c["settings"][k] = body[k]
        config.update(m)
        if "stream_av_delay_ms" in body:
            gstmod.player.set_av_delay(body["stream_av_delay_ms"])
        # changing the audio output device means rebuilding the pipeline with a
        # new sink, then resuming whatever was playing
        if "audio_out" in body:
            gstmod.player.set_audio_device(body["audio_out"] or "auto")
            gstmod.player.restart()
            gstmod.engine.reapply()
        return jsonify(_public_config(config.load()))

    @app.route("/api/password", methods=["PUT"])
    @login_required
    def api_password():
        body = request.get_json(force=True)
        cfg = config.load()
        if not check_password_hash(cfg["auth"]["password_hash"], body.get("old", "")):
            return jsonify({"error": "wrong current password"}), 403
        new = body.get("new", "")
        if len(new) < 4:
            return jsonify({"error": "password too short"}), 400

        def m(c):
            if body.get("username"):
                c["auth"]["username"] = body["username"].strip()
            c["auth"]["password_hash"] = generate_password_hash(new)
        config.update(m)
        return jsonify({"ok": True})

    # ================= playback control ===================================
    @app.route("/api/play/<pid>", methods=["POST"])
    @login_required
    def api_play(pid):
        cfg = config.load()
        pl = config.get_playlist(cfg, pid)
        if not pl:
            return jsonify({"error": "not found"}), 404
        gstmod.engine.play_playlist(pl)
        return jsonify({"ok": True})

    @app.route("/api/stop", methods=["POST"])
    @login_required
    def api_stop():
        gstmod.engine.stop()
        return jsonify({"ok": True})

    @app.route("/api/pause", methods=["POST"])
    @login_required
    def api_pause():
        gstmod.player.toggle_pause()
        return jsonify({"ok": True})

    @app.route("/api/next", methods=["POST"])
    @login_required
    def api_next():
        with gstmod.engine.lock:
            if gstmod.engine.items:
                gstmod.engine.loops_left = 1
                gstmod.engine.index += 1
                gstmod.engine._load_current()
        return jsonify({"ok": True})

    # ================= cec ================================================
    @app.route("/api/cec", methods=["POST"])
    @login_required
    def api_cec():
        body = request.get_json(force=True)
        return jsonify(cec.run_action(body.get("action", "on")))

    @app.route("/api/cec/info")
    @login_required
    def api_cec_info():
        return jsonify(cec.info())

    # ================= live HDMI snapshot =================================
    @app.route("/api/snapshot")
    @login_required
    def api_snapshot():
        path = os.path.join(config.PREVIEW_DIR, "live.jpg")
        now = time.time()
        # refresh at the configured interval (>=2s); the grab itself is
        # single-flight and runs in the background (see GstPlayer.screenshot)
        interval = max(2, int(config.load()["settings"].get("screenshot_interval") or 5))
        if now - _snap_last["t"] > interval:
            _snap_last["t"] = now
            try:
                gstmod.player.screenshot(path)
            except Exception:
                pass
        if not os.path.exists(path):
            abort(404)
        resp = send_file(path, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    return app
