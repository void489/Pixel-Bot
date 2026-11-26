"""/admin - owner-only page.
  Main:      live stats (online, uptime, ping, servers, members, open tickets), maintenance mode,
             feature switches, the bot's activity, and the public support link + contact email.
  Servers:   every server the bot is in - search / sort, open its dashboard or its details page,
             get an invite, make the bot leave, or block it (leave + never rejoin).
  Broadcast: send one message to every server (or the ones you pick) and see how each went.
  Errors:    the latest errors the bot logged.
  Audit:     what was changed on this page, and when.
  Pick Winner: see the note further down.

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

from config import bot_features, settings
from dashboard import botguilds
from dashboard import db as dashboard_db
from utils.site_settings import valid_email, valid_support_url

from utils.log import get_logger
logger = get_logger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

SESSION_HOURS = 12
MAX_FAILS = 5
LOCK_SECONDS = 10 * 60
MIN_SECRET_LEN = 16
PRESENCE_TASK = "bot_presence_apply"
BROADCAST_TASK = "admin_broadcast"
BROADCAST_TEXT_MAX = 1800
BROADCAST_TITLE_MAX = 100
MAINTENANCE_MESSAGE_MAX = 300
HEARTBEAT_STALE_SECONDS = 120      # no heartbeat for this long = the bot is offline
AUDIT_KEEP_SECONDS = 180 * 24 * 3600

ACTIVITY_TYPES = [("playing", "Playing"), ("watching", "Watching"),
                  ("listening", "Listening to"), ("competing", "Competing in"),
                  ("streaming", "Streaming")]
STATUSES = [("online", "Online"), ("idle", "Idle"),
            ("dnd", "Do not disturb"), ("invisible", "Offline (invisible)")]
# Discord only shows the purple "Streaming" badge for Twitch and YouTube links.
STREAM_HOSTS = ("twitch.tv", "www.twitch.tv", "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be")


def _valid_stream_url(url: str) -> bool:
    from urllib.parse import urlparse
    u = urlparse(url)
    return u.scheme == "https" and (u.hostname or "").lower() in STREAM_HOSTS and len(url) <= 300
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
# Small helpers: audit log, stats
# ---------------------------------------------------------------------------

_audit_index_done = False


def _audit(action: str, detail: str = ""):
    """Remember what was changed on this page (shown on the Audit tab). Never raises.
    Deliberately not called for Pick Winner (see the note on that block)."""
    global _audit_index_done
    try:
        col = dashboard_db.get_db().admin_audit
        if not _audit_index_done:
            col.create_index("at", expireAfterSeconds=AUDIT_KEEP_SECONDS)
            _audit_index_done = True
        col.insert_one({"at": datetime.now(timezone.utc), "action": action,
                        "detail": str(detail)[:300], "ip": _client_ip()})
    except Exception as e:
        logger.error(f"[admin] audit write failed: {e}")


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _stats() -> dict:
    """Numbers for the top of the Main tab. The bot writes a heartbeat every 30 seconds
    (cogs/admin_tools.py); no recent heartbeat means it is offline."""
    db = dashboard_db.get_db()
    hb = db.bot_settings.find_one({"_id": "heartbeat"}) or {}
    now = datetime.now(timezone.utc)
    updated = _aware(hb.get("updated_at"))
    started = _aware(hb.get("started_at"))
    online = bool(updated and (now - updated).total_seconds() < HEARTBEAT_STALE_SECONDS)
    return {
        "online": online,
        "seen": f"{_fmt_duration((now - updated).total_seconds())} ago" if updated else "never",
        "uptime": _fmt_duration((now - started).total_seconds()) if online and started else "-",
        "latency": hb.get("latency_ms") if online else None,
        "servers": hb.get("guilds"),
        "members": hb.get("members"),
        "open_tickets": db.tickets.count_documents({"status": "open"}),
        "pending_apps": db.application_submissions.count_documents({"status": "Pending"}),
        "running_giveaways": db.giveaways.count_documents({"ended": False}),
    }


def _server_details(guild_id: str, listed: list) -> dict:
    """Everything shown on a server's details page, or None if the bot isn't in it."""
    g = next((x for x in listed if str(x.get("id")) == guild_id), None)
    if g is None:
        return None
    db = dashboard_db.get_db()
    info = next((x for x in ((db.bot_settings.find_one({"_id": "guild_info"}) or {}).get("guilds") or [])
                 if x.get("id") == guild_id), {})
    cfg = db.guild_settings.find_one({"_id": guild_id}) or {}     # read only: never creates defaults
    tickets_cfg, anti = cfg.get("tickets") or {}, cfg.get("anti_nuke") or {}
    return {
        "guild": g,
        "members": g.get("approximate_member_count") if g.get("approximate_member_count") is not None
        else info.get("members"),
        "joined": (info.get("joined_at") or "")[:16].replace("T", " "),
        "counts": [
            ("Open tickets", db.tickets.count_documents({"guild_id": guild_id, "status": "open"})),
            ("Tickets, all time", db.tickets.count_documents({"guild_id": guild_id})),
            ("Running giveaways", db.giveaways.count_documents({"guild_id": guild_id, "ended": False})),
            ("Applications", db.applications.count_documents({"guild_id": guild_id})),
            ("Pending application submissions",
             db.application_submissions.count_documents({"guild_id": guild_id, "status": "Pending"})),
            ("Role panels", db.role_panels.count_documents({"guild_id": guild_id})),
            ("Spawner panels", len((cfg.get("spawners") or {}).get("panels") or [])),
        ],
        "settings": [
            ("Prefix", cfg.get("prefix") or "/"),
            ("Ticket support roles", len(tickets_cfg.get("support_role_ids") or [])),
            ("Ticket category set", "yes" if tickets_cfg.get("category_id") else "no"),
            ("Ticket log channel set", "yes" if tickets_cfg.get("log_channel_id") else "no"),
            ("Moderation log channel set", "yes" if (cfg.get("moderation") or {}).get("log_channel_id") else "no"),
            ("Vouch channels", len((cfg.get("vouches") or {}).get("channel_ids") or [])),
            ("Anti-nuke", "on" if anti.get("enabled") else "off"),
        ],
    }


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
    tab = request.args.get("tab")
    tab = tab if tab in ("servers", "server", "broadcast", "errors", "audit", "pickwinner") else "main"
    db = dashboard_db.get_db()
    guilds = None
    listed = []
    stats, maint, features_off = None, {}, set()
    blocked, detail, bc_servers, broadcasts, errors, audit = [], None, [], [], [], []
    sel_guild = ""
    pick_servers, pick_gws, pick_guild = [], [], ""
    if tab == "pickwinner":     # servers that have a running giveaway, then that server's giveaways
        col = dashboard_db.get_db().giveaways
        ids = [str(x) for x in col.distinct("guild_id", _pickable_filter())]
        names = {str(g.get("id")): g.get("name") for g in (botguilds.get_bot_guilds() or [])}
        pick_servers = sorted(({"id": i, "name": names.get(i) or i} for i in ids),
                              key=lambda s: str(s["name"]).lower())
        pick_guild = (request.args.get("guild") or "").strip()
        if pick_guild in ids:
            pick_gws = list(col.find(_pickable_filter({"guild_id": pick_guild})).sort("end_time", 1))
            for g in pick_gws:
                g["entry_count"] = len(set(g.get("entries") or []))
        else:
            pick_guild = ""
    if tab in ("servers", "server"):    # only ask Discord for the server list when it is shown
        guilds = botguilds.get_bot_guilds(force=True)
        listed = sorted(guilds or [], key=lambda g: (g.get("name") or "").lower())
    if tab == "servers":
        info = {x.get("id"): x for x in ((db.bot_settings.find_one({"_id": "guild_info"}) or {}).get("guilds") or [])}
        for g in listed:        # members (Discord's count, else the bot's) and when the bot joined
            gi = info.get(str(g.get("id")), {})
            n = g.get("approximate_member_count")
            g["members_n"] = n if n is not None else gi.get("members")
            g["joined_iso"] = gi.get("joined_at") or ""
        bdoc = db.bot_settings.find_one({"_id": "blocked_guilds"}) or {}
        blocked = [{"id": i, "name": (bdoc.get("names") or {}).get(i) or ""} for i in (bdoc.get("ids") or [])]
    if tab == "server":
        sel_guild = (request.args.get("guild") or "").strip()
        detail = _server_details(sel_guild, listed) if sel_guild.isdigit() and guilds is not None else None
    if tab == "main":
        stats = _stats()
        maint = db.bot_settings.find_one({"_id": "maintenance"}) or {}
        features_off = set((db.bot_settings.find_one({"_id": "features"}) or {}).get("disabled_keys") or [])
    if tab == "broadcast":
        bc_servers = sorted(({"id": str(g["id"]), "name": g.get("name") or str(g["id"])}
                             for g in (botguilds.get_bot_guilds() or [])), key=lambda x: x["name"].lower())
        broadcasts = list(db.admin_broadcasts.find().sort("created_at", -1).limit(10))
    if tab == "errors":
        errors = list(db.bot_errors.find().sort("created_at", -1).limit(100))
    if tab == "audit":
        audit = list(db.admin_audit.find().sort("at", -1).limit(100))
    doc = dashboard_db.get_db().bot_settings.find_one({"_id": "presence"}) or {}
    return render_template(
        "admin.html",
        tab=tab,
        site=dashboard_db.get_site_settings(),
        guilds=listed,
        guilds_failed=tab == "servers" and guilds is None,
        presence={
            "type": doc.get("type") or "playing",
            "text": doc.get("text") or "",
            "status": doc.get("status") or "online",
            "url": doc.get("url") or "",
            "last": doc.get("last_result"),
        },
        pick_servers=pick_servers, pick_gws=pick_gws, pick_guild=pick_guild,
        stats=stats, maint=maint, features=bot_features.TOGGLEABLE, features_off=features_off,
        blocked=blocked, detail=detail, sel_guild=sel_guild, bc_servers=bc_servers,
        broadcasts=broadcasts, errors=errors, audit=audit,
        bc_text_max=BROADCAST_TEXT_MAX, bc_title_max=BROADCAST_TITLE_MAX,
        maint_max=MAINTENANCE_MESSAGE_MAX,
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
    url = (request.form.get("url") or "").strip()
    if a_type not in dict(ACTIVITY_TYPES) or status not in dict(STATUSES):
        return redirect(url_for("admin.admin_page", error="badvalue"))
    if a_type == "streaming" and text and not _valid_stream_url(url):
        return redirect(url_for("admin.admin_page", error="badurl"))
    if a_type != "streaming":
        url = ""

    dashboard_db.get_db().bot_settings.update_one(
        {"_id": "presence"},
        {"$set": {"type": a_type, "text": text, "status": status, "url": url,
                  "updated_at": datetime.now(timezone.utc)},
         "$unset": {"last_result": ""}},
        upsert=True,
    )
    dashboard_db.enqueue_task(PRESENCE_TASK, "", {})
    _audit("Changed bot activity", f"{a_type}: {text or '(none)'} / {status}")
    return redirect(url_for("admin.admin_page", activity="queued"))


@admin_bp.route("/links", methods=["POST"])
@require_admin()
def set_links():
    support_url = (request.form.get("support_url") or "").strip()
    email = (request.form.get("contact_email") or "").strip()
    if not valid_support_url(support_url):
        return redirect(url_for("admin.admin_page", error="badsupport"))
    if not valid_email(email):
        return redirect(url_for("admin.admin_page", error="bademail"))
    dashboard_db.save_site_settings(support_url, email)
    _audit("Changed links & contact", f"support: {support_url or '(none)'} / email: {email or '(none)'}")
    return redirect(url_for("admin.admin_page", links="saved"))


# ---- TEMPORARY: "Pick Winner" tab (one server asked for it). Deliberately NOT written to the audit
# log, so the server's staff can't see it. Sets `forced_winner` on a running giveaway; the bot uses it
# when the giveaway ends (see cogs/giveaways.py). Remove this block, the tab in admin.html and the
# `forced_winner` lines in cogs/giveaways.py when it is no longer needed.
PICK_SKIP_KINDS = ("fastestfinger", "guessflag", "guesslanguage")


def _pickable_filter(extra=None):
    q = {"ended": False, "game": None, "kind": {"$nin": list(PICK_SKIP_KINDS)}}
    q.update(extra or {})
    return q


@admin_bp.route("/pickwinner/set", methods=["POST"])
@require_admin()
def pickwinner_set():
    gid = (request.form.get("guild_id") or "").strip()
    mid = (request.form.get("message_id") or "").strip()
    uid = (request.form.get("user_id") or "").strip()
    if not (gid.isdigit() and mid.isdigit()):
        return redirect(url_for("admin.admin_page", tab="pickwinner", error="badid"))
    if not (uid.isdigit() and 15 <= len(uid) <= 25):
        return redirect(url_for("admin.admin_page", tab="pickwinner", guild=gid, error="baduser"))
    res = dashboard_db.get_db().giveaways.update_one(
        _pickable_filter({"guild_id": gid, "message_id": mid}), {"$set": {"forced_winner": uid}})
    return redirect(url_for("admin.admin_page", tab="pickwinner", guild=gid,
                            pick="set" if res.matched_count else "gone"))


@admin_bp.route("/pickwinner/clear", methods=["POST"])
@require_admin()
def pickwinner_clear():
    gid = (request.form.get("guild_id") or "").strip()
    mid = (request.form.get("message_id") or "").strip()
    if not (gid.isdigit() and mid.isdigit()):
        return redirect(url_for("admin.admin_page", tab="pickwinner", error="badid"))
    dashboard_db.get_db().giveaways.update_one(
        {"guild_id": gid, "message_id": mid, "ended": False}, {"$unset": {"forced_winner": ""}})
    return redirect(url_for("admin.admin_page", tab="pickwinner", guild=gid, pick="cleared"))


@admin_bp.route("/servers/<guild_id>/leave", methods=["POST"])
@require_admin()
def leave_server(guild_id):
    if not guild_id.isdigit():
        return redirect(url_for("admin.admin_page", tab="servers", error="badid"))
    ok, msg = botguilds.leave_guild(guild_id)
    _audit("Made the bot leave a server", f"{guild_id}" + ("" if ok else f" - failed: {msg}"))
    if ok:
        return redirect(url_for("admin.admin_page", tab="servers", left="1"))
    logger.error(f"[admin] leave {guild_id} failed: {msg}")
    return redirect(url_for("admin.admin_page", tab="servers", error="leave"))


@admin_bp.route("/servers/<guild_id>/invite", methods=["POST"])
@require_admin(json_response=True)
def invite_server(guild_id):
    if not guild_id.isdigit():
        return jsonify(ok=False, error="Bad server id."), 400
    ok, result = botguilds.create_invite(guild_id)
    if ok:
        return jsonify(ok=True, invite_url=result)
    return jsonify(ok=False, error=result), 200


# ---------------------------------------------------------------------------
# Maintenance mode + feature switches (applied by cogs/admin_tools.py within ~10 seconds)
# ---------------------------------------------------------------------------

@admin_bp.route("/maintenance", methods=["POST"])
@require_admin()
def set_maintenance():
    enabled = request.form.get("enabled") == "on"
    message = (request.form.get("message") or "").strip()[:MAINTENANCE_MESSAGE_MAX]
    dashboard_db.get_db().bot_settings.update_one(
        {"_id": "maintenance"},
        {"$set": {"enabled": enabled, "message": message, "updated_at": datetime.now(timezone.utc)}},
        upsert=True)
    _audit("Maintenance mode " + ("ON" if enabled else "OFF"), message or "(default message)")
    return redirect(url_for("admin.admin_page", maint="on" if enabled else "off"))


@admin_bp.route("/features", methods=["POST"])
@require_admin()
def set_features():
    off = [k for k in request.form.getlist("off") if k in bot_features.FEATURE_KEYS]
    dashboard_db.get_db().bot_settings.update_one(
        {"_id": "features"},
        {"$set": {"disabled_keys": off, "disabled_cogs": bot_features.cogs_for(off),
                  "updated_at": datetime.now(timezone.utc)}},
        upsert=True)
    _audit("Changed feature switches", ("Off: " + ", ".join(off)) if off else "Everything on")
    return redirect(url_for("admin.admin_page", features="saved"))


# ---------------------------------------------------------------------------
# Blocked servers
# ---------------------------------------------------------------------------

@admin_bp.route("/servers/<guild_id>/block", methods=["POST"])
@require_admin()
def block_server(guild_id):
    if not guild_id.isdigit():
        return redirect(url_for("admin.admin_page", tab="servers", error="badid"))
    name = (request.form.get("name") or "").strip()[:100]
    dashboard_db.get_db().bot_settings.update_one(
        {"_id": "blocked_guilds"},
        {"$addToSet": {"ids": guild_id}, "$set": {f"names.{guild_id}": name}}, upsert=True)
    ok, msg = botguilds.leave_guild(guild_id)       # blocked even if leaving fails: it leaves on next start
    _audit("Blocked a server", f"{name or guild_id} ({guild_id})" + ("" if ok else f" - leave failed: {msg}"))
    return redirect(url_for("admin.admin_page", tab="servers", blocked="1"))


@admin_bp.route("/servers/<guild_id>/unblock", methods=["POST"])
@require_admin()
def unblock_server(guild_id):
    if not guild_id.isdigit():
        return redirect(url_for("admin.admin_page", tab="servers", error="badid"))
    dashboard_db.get_db().bot_settings.update_one(
        {"_id": "blocked_guilds"}, {"$pull": {"ids": guild_id}, "$unset": {f"names.{guild_id}": ""}})
    _audit("Unblocked a server", guild_id)
    return redirect(url_for("admin.admin_page", tab="servers", unblocked="1"))


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------

@admin_bp.route("/broadcast/send", methods=["POST"])
@require_admin()
def broadcast_send():
    back = lambda **kw: redirect(url_for("admin.admin_page", tab="broadcast", **kw))  # noqa: E731
    title = (request.form.get("title") or "").strip()[:BROADCAST_TITLE_MAX]
    text = (request.form.get("text") or "").strip()
    if not text or len(text) > BROADCAST_TEXT_MAX:
        return back(error="bctext")
    if request.form.get("confirm") != "on":
        return back(error="bcconfirm")

    db = dashboard_db.get_db()
    send_all = request.form.get("audience") != "selected"
    guild_ids = []
    if not send_all:
        valid = {str(g["id"]) for g in (botguilds.get_bot_guilds() or [])}
        guild_ids = [g for g in dict.fromkeys(request.form.getlist("guild_ids")) if g in valid]
        if not guild_ids:
            return back(error="bcnone")

    cutoff = datetime.now(timezone.utc).timestamp() - 30 * 60
    for b in db.admin_broadcasts.find({"status": {"$in": ["queued", "sending"]}}):
        if _aware(b.get("created_at")).timestamp() > cutoff:       # one at a time
            return back(error="bcbusy")

    res = db.admin_broadcasts.insert_one({
        "title": title, "text": text, "all": send_all, "guild_ids": guild_ids,
        "status": "queued", "created_at": datetime.now(timezone.utc),
    })
    dashboard_db.enqueue_task(BROADCAST_TASK, "", {"broadcast_id": str(res.inserted_id)})
    _audit("Sent a broadcast", f"{'all servers' if send_all else str(len(guild_ids)) + ' server(s)'}: {text[:100]}")
    return back(sent="queued")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

@admin_bp.route("/errors/clear", methods=["POST"])
@require_admin()
def errors_clear():
    res = dashboard_db.get_db().bot_errors.delete_many({})
    _audit("Cleared the error list", f"{res.deleted_count} error(s)")
    return redirect(url_for("admin.admin_page", tab="errors", cleared="1"))
