"""/admin - owner-only page: set the bot's activity, see every server the bot is in,
open any server's dashboard, make the bot leave, or get an invite.

Switched off unless ALL of these are set in the environment (Railway variables):
    ADMIN_USERNAME, ADMIN_PASSWORD, DASHBOARD_SECRET_KEY (a real, fixed secret)
The login is stored in Flask's signed session cookie, so a weak/default secret key
would let someone forge it - that's why the page refuses to run with one.
"""
import hmac
import secrets
import threading
import time
from datetime import datetime, timezone
from functools import wraps

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)

from config import settings
from dashboard import botguilds
from dashboard import db as dashboard_db

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

SESSION_HOURS = 12
MAX_FAILS = 5
LOCK_SECONDS = 10 * 60
MIN_SECRET_LEN = 16
PRESENCE_TASK = "bot_presence_apply"

ACTIVITY_TYPES = [("playing", "Playing"), ("watching", "Watching"),
                  ("listening", "Listening to"), ("competing", "Competing in")]
STATUSES = [("online", "Online"), ("idle", "Idle"),
            ("dnd", "Do not disturb"), ("invisible", "Invisible")]
MAX_ACTIVITY_LEN = 128

_fail_lock = threading.Lock()
_fails: dict = {}  # ip -> [timestamps of recent failures]


# ---------------------------------------------------------------------------
# On/off + session helpers
# ---------------------------------------------------------------------------

def admin_disabled_reason():
    """None if the admin page may run, else a human-readable reason."""
    if not settings.ADMIN_USERNAME or not settings.ADMIN_PASSWORD:
        return "ADMIN_USERNAME and ADMIN_PASSWORD are not both set."
    key = settings.DASHBOARD_SECRET_KEY or ""
    if key == "change-me" or len(key) < MIN_SECRET_LEN:
        return (f"DASHBOARD_SECRET_KEY must be set to a long random value "
                f"(at least {MIN_SECRET_LEN} characters).")
    return None


def admin_logged_in() -> bool:
    if admin_disabled_reason():
        return False
    if not session.get("admin_ok"):
        return False
    if time.time() - float(session.get("admin_at") or 0) > SESSION_HOURS * 3600:
        session.pop("admin_ok", None)
        session.pop("admin_at", None)
        return False
    return True


def _csrf_token() -> str:
    tok = session.get("admin_csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["admin_csrf"] = tok
    return tok


def _csrf_ok() -> bool:
    sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token") or ""
    real = session.get("admin_csrf") or ""
    return bool(real) and hmac.compare_digest(sent.encode(), real.encode())


def require_admin(json_response: bool = False):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if admin_disabled_reason() or not admin_logged_in():
                if json_response:
                    return jsonify(ok=False, error="Not logged in as admin."), 401
                return redirect(url_for("admin.login"))
            if request.method == "POST" and not _csrf_ok():
                if json_response:
                    return jsonify(ok=False, error="Session expired - reload the page."), 400
                return redirect(url_for("admin.admin_page", error="csrf"))
            return fn(*a, **kw)
        return wrapper
    return deco


# ---------------------------------------------------------------------------
# Brute-force lock (per IP, in memory)
# ---------------------------------------------------------------------------

def _client_ip() -> str:
    return request.remote_addr or "unknown"


def _locked_for(ip: str) -> int:
    """Seconds left on the lock for this IP (0 = not locked)."""
    now = time.time()
    with _fail_lock:
        recent = [t for t in _fails.get(ip, []) if now - t < LOCK_SECONDS]
        if recent:
            _fails[ip] = recent
        else:
            _fails.pop(ip, None)
        if len(recent) >= MAX_FAILS:
            return int(LOCK_SECONDS - (now - recent[-1])) + 1
    return 0


def _record_fail(ip: str):
    with _fail_lock:
        _fails.setdefault(ip, []).append(time.time())


def _clear_fails(ip: str):
    with _fail_lock:
        _fails.pop(ip, None)


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    disabled = admin_disabled_reason()
    if disabled:
        return render_template("admin_login.html", disabled=disabled, error=None, locked=0), 503
    if admin_logged_in():
        return redirect(url_for("admin.admin_page"))

    ip = _client_ip()
    locked = _locked_for(ip)
    if request.method == "POST":
        if locked:
            return render_template("admin_login.html", disabled=None, error=None,
                                   locked=locked), 429
        user = request.form.get("username") or ""
        pw = request.form.get("password") or ""
        # evaluate both so timing doesn't reveal which one was wrong
        ok_user = _same(user, settings.ADMIN_USERNAME)
        ok_pw = _same(pw, settings.ADMIN_PASSWORD)
        if ok_user and ok_pw:
            _clear_fails(ip)
            session["admin_ok"] = True
            session["admin_at"] = time.time()
            session["admin_csrf"] = secrets.token_urlsafe(32)
            return redirect(url_for("admin.admin_page"))
        _record_fail(ip)
        locked = _locked_for(ip)
        return render_template("admin_login.html", disabled=None,
                               error="Wrong username or password.", locked=locked), 401

    return render_template("admin_login.html", disabled=None, error=None, locked=locked)


@admin_bp.route("/logout")
def logout():
    for k in ("admin_ok", "admin_at", "admin_csrf"):
        session.pop(k, None)
    return redirect(url_for("admin.login"))


@admin_bp.route("/", strict_slashes=False)
@require_admin()
def admin_page():
    guilds = botguilds.get_bot_guilds(force=True)
    listed = sorted(guilds or [], key=lambda g: (g.get("name") or "").lower())
    doc = dashboard_db.get_db().bot_settings.find_one({"_id": "presence"}) or {}
    return render_template(
        "admin.html",
        guilds=listed,
        guilds_failed=guilds is None,
        presence={
            "type": doc.get("type") or "playing",
            "text": doc.get("text") or "",
            "status": doc.get("status") or "online",
            "last": doc.get("last_result"),
        },
        activity_types=ACTIVITY_TYPES,
        statuses=STATUSES,
        max_len=MAX_ACTIVITY_LEN,
        csrf=_csrf_token(),
        args=request.args,
    )


@admin_bp.route("/activity", methods=["POST"])
@require_admin()
def set_activity():
    a_type = request.form.get("type", "playing")
    status = request.form.get("status", "online")
    text = (request.form.get("text") or "").strip()[:MAX_ACTIVITY_LEN]
    if a_type not in dict(ACTIVITY_TYPES) or status not in dict(STATUSES):
        return redirect(url_for("admin.admin_page", error="badvalue"))

    dashboard_db.get_db().bot_settings.update_one(
        {"_id": "presence"},
        {"$set": {"type": a_type, "text": text, "status": status,
                  "updated_at": datetime.now(timezone.utc)},
         "$unset": {"last_result": ""}},
        upsert=True,
    )
    dashboard_db.enqueue_task(PRESENCE_TASK, "", {})
    return redirect(url_for("admin.admin_page", activity="queued"))


@admin_bp.route("/servers/<guild_id>/leave", methods=["POST"])
@require_admin()
def leave_server(guild_id):
    if not guild_id.isdigit():
        return redirect(url_for("admin.admin_page", error="badid"))
    ok, msg = botguilds.leave_guild(guild_id)
    if ok:
        return redirect(url_for("admin.admin_page", left="1"))
    print(f"[admin] leave {guild_id} failed: {msg}")
    return redirect(url_for("admin.admin_page", error="leave"))


@admin_bp.route("/servers/<guild_id>/invite", methods=["POST"])
@require_admin(json_response=True)
def invite_server(guild_id):
    if not guild_id.isdigit():
        return jsonify(ok=False, error="Bad server id."), 400
    ok, result = botguilds.create_invite(guild_id)
    if ok:
        return jsonify(ok=True, invite_url=result)
    return jsonify(ok=False, error=result), 200
