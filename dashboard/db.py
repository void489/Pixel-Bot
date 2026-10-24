"""Sync PyMongo helpers used only by the Flask dashboard (bot side uses motor/async)."""
from pymongo import MongoClient
from bson import ObjectId
import re
from datetime import datetime, timezone, timedelta
from pymongo.errors import DuplicateKeyError
from config import settings
from database.automod_config import normalize_automod_config

_client = None


def get_db():
    global _client
    if _client is None:
        _client = MongoClient(settings.MONGO_URI)
    return _client[settings.MONGO_DB_NAME]


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

def _normalize_ticket_type(data: dict) -> dict:
    out = _normalize_ticket_type_base(data)
    # Spare categories: only written when the form sent them, so other saves that rebuild
    # the type never wipe them. Max 5, digits only, never the main category itself.
    if "overflow_category_ids" in data:
        from utils import ticket_settings as ts
        out["overflow_category_ids"] = ts.clean_overflow_ids(
            data.get("overflow_category_ids"), out.get("category_id")
        )
    # Per-panel settings (utils/ticket_settings.py): only written when the form sent them
    # ("advanced" flag), so other saves never wipe them.
    if data.get("advanced"):
        from utils import ticket_settings as ts
        out.update({
            "button_label": ts.clean_text(data.get("button_label"), ts.LABEL_MAX),
            "max_open_tickets": ts.clamp_limit(data.get("max_open_tickets")),
            "required_role_ids": ts.clean_id_list(data.get("required_role_ids")),
            "welcome_message": ts.clean_text(data.get("welcome_message"), ts.WELCOME_MAX),
            "created_message": ts.clean_text(data.get("created_message"), ts.CREATED_MAX),
            "claiming_enabled": bool(data.get("claiming_enabled", True)),
            "claim_only_visible": bool(data.get("claim_only_visible", False)),
            "save_transcripts": bool(data.get("save_transcripts", True)),
            "linked_type_ids": ts.clean_id_list(data.get("linked_type_ids"), ts.MAX_LINKED),
            "buttons": ts.clean_buttons(data.get("buttons")),
            "transcript_access": data.get("transcript_access") if data.get("transcript_access") in ("managers", "staff", "opener") else "managers",
            "thread_mode": bool(data.get("thread_mode", False)) and str(data.get("thread_parent_id") or "").isdigit(),
            "thread_parent_id": str(data.get("thread_parent_id") or "") if str(data.get("thread_parent_id") or "").isdigit() else None,
            "thread_auto_archive": ts.clean_thread_archive(data.get("thread_auto_archive")),
            "thread_invitable": bool(data.get("thread_invitable", False)),
            "web_reply": bool(data.get("web_reply", False)),
            "priority_enabled": bool(data.get("priority_enabled", False)),
            "priority_rename": data.get("priority_rename") if data.get("priority_rename") in ts.RENAME_MODES else "off",
        })
    return out


def _normalize_ticket_type_base(data: dict) -> dict:
    return {
        "name": str(data.get("name") or ""),
        "emoji": str(data.get("emoji") or ""),
        "button_style": data.get("button_style") or "primary",
        # shown under the option name when the panel is displayed as a dropdown (Discord max 100)
        "description": str(data.get("description") or "").strip()[:100],
        "color": int(data.get("color") or 0x5865F2),
        "color_hex": data.get("color_hex") or "#5865f2",
        "category_id": str(data["category_id"]) if data.get("category_id") else None,
        "ping_role_id": str(data["ping_role_id"]) if data.get("ping_role_id") else None,
        "allow_role_ids": [str(r) for r in (data.get("allow_role_ids") or [])],
        "questions": [str(q) for q in (data.get("questions") or []) if q],
        # one True/False per question (same order); missing = only question 1 required
        "questions_required": [bool(r) for r in (data.get("questions_required") or [])],
        # channel-name pattern, e.g. "{q1}-{q2}-{q3}"; empty = default "{type}-{username}"
        "auto_rename": str(data.get("auto_rename") or "").strip()[:100],
        "allow_blacklisted": bool(data.get("allow_blacklisted", False)),
    }


def _normalize_ticket_panel(data: dict) -> dict:
    out = _normalize_ticket_panel_base(data)
    # Auto-close settings: only written when the form sent them, so other saves that
    # rebuild the panel (like "Post panel") never wipe them.
    if "auto_close_warn_hours" in data:
        from utils import ticket_autoclose as ac
        cfg = ac.normalize({
            "auto_close_warn_hours": data.get("auto_close_warn_hours"),
            "auto_close_grace_hours": data.get("auto_close_grace_hours"),
            "auto_close_claimed": data.get("auto_close_claimed"),
        })
        out["auto_close_warn_hours"] = cfg["warn_hours"]
        out["auto_close_grace_hours"] = cfg["grace_hours"]
        out["auto_close_claimed"] = cfg["claimed"]
    return out


def _normalize_ticket_panel_base(data: dict) -> dict:
    return {
        "name": str(data.get("name") or ""),
        "title": str(data.get("title") or ""),
        "description": str(data.get("description") or ""),
        "color": int(data.get("color") or 0x5865F2),
        "color_hex": data.get("color_hex") or "#5865f2",
        "ticket_type_ids": [str(t) for t in (data.get("ticket_type_ids") or [])],
        # "buttons" (default, max 5 types) or "dropdown" (max 25 types)
        "display_style": "dropdown" if data.get("display_style") == "dropdown" else "buttons",
        "placeholder": str(data.get("placeholder") or "").strip()[:150],
        "panel_channel_id": str(data["panel_channel_id"]) if data.get("panel_channel_id") else None,
    }


def _normalize_application(data: dict) -> dict:
    return {
        "name": str(data.get("name") or ""),
        "emoji": str(data.get("emoji") or ""),
        "button_label": str(data.get("button_label") or ""),
        "button_style": data.get("button_style") or "primary",
        "is_open": bool(data.get("is_open", True)),
        "submit_channel_id": str(data["submit_channel_id"]) if data.get("submit_channel_id") else None,
        "accepted_channel_id": str(data["accepted_channel_id"]) if data.get("accepted_channel_id") else None,
        "denied_channel_id": str(data["denied_channel_id"]) if data.get("denied_channel_id") else None,
        "decision_role_ids": [str(r) for r in (data.get("decision_role_ids") or [])],
        # Falls back to the old decision_ping_role_ids key so existing configs keep working.
        "ping_role_ids": [str(r) for r in (data.get("ping_role_ids") or data.get("decision_ping_role_ids") or [])],
        "ticket_category_id": str(data["ticket_category_id"]) if data.get("ticket_category_id") else None,
        "ticket_ping_role_ids": [str(r) for r in (data.get("ticket_ping_role_ids") or [])],
        "ticket_allow_role_ids": [str(r) for r in (data.get("ticket_allow_role_ids") or [])],
        "change_type_id": str(data["change_type_id"]) if data.get("change_type_id") else None,
        "accept_message": str(data.get("accept_message") or ""),
        "deny_message": str(data.get("deny_message") or ""),
        "questions": [str(q) for q in (data.get("questions") or []) if q],
    }


# ---------------------------------------------------------------------------
# Guild settings
# ---------------------------------------------------------------------------

def get_guild_settings(guild_id: str) -> dict:
    db = get_db()
    doc = db.guild_settings.find_one({"_id": str(guild_id)})
    if not doc:
        doc = {
            "_id": str(guild_id),
            "prefix": "/",
            "tickets": {
                "category_id": None,
                "log_channel_id": None,
                "transcript_channel_id": None,
                "support_role_ids": [],
                "panel_channel_id": None,
            },
            "applications": {"dm_timeout_minutes": 30},
            "moderation": {"log_channel_id": None, "next_case_num": 1},
            "anti_nuke": {
                "enabled": False,
                "dry_run": False,
                "auto_revert": True,
                "punishment": "alert_only",
                "security_alert_channel_id": None,
                "alert_role_ids": [],
                "whitelist_role_ids": [],
                "whitelist_user_ids": [],
                "whitelist_bot_ids": [],
                "actions": {},
            },
            "blacklist": {
                "giveaway_role_id": None,
                "ticket_role_id": None,
                "request_channel_id": None,
                "staff_role_id": None,
                "application_role_id": None,
                "counters": {
                    "submitted": {"giveaway": 0, "ticket": 0, "application": 0},
                    "accepted":  {"giveaway": 0, "ticket": 0, "application": 0},
                    "denied":    {"giveaway": 0, "ticket": 0, "application": 0},
                },
            },
            "honeypot": {
                "enabled": False,
                "channel_id": None,
                "bypass_role_id": None,
                "panel_message_id": None,
                "kick_count": 0,
                "dm_message": (
                    "You were softbanned from {guild} for posting in a trap channel. "
                    "This is an automated action. If you believe this was a mistake, "
                    "contact the server staff."
                ),
            },
        }
        db.guild_settings.insert_one(doc)
    return doc


def update_guild_settings(guild_id: str, update: dict) -> None:
    get_db().guild_settings.update_one({"_id": str(guild_id)}, {"$set": update}, upsert=True)


# ---------------------------------------------------------------------------
# Command permissions
# ---------------------------------------------------------------------------

def get_all_command_permissions(guild_id: str) -> list:
    return list(get_db().command_permissions.find({"guild_id": str(guild_id)}))


def set_command_permission(guild_id: str, command_name: str, enabled: bool, allowed_roles: list,
                           group_locked: bool = False) -> None:
    """Save one command. `group_locked` = "not affected by group changes": the group's
    Apply-to-all skips it (it can still be edited here, on its own row)."""
    key = f"{guild_id}:{command_name}"
    get_db().command_permissions.update_one(
        {"_id": key},
        {"$set": {
            "guild_id": str(guild_id),
            "command": command_name,
            "enabled": enabled,
            "allowed_roles": allowed_roles,
            "admin_only": False,
            "group_locked": bool(group_locked),
        }},
        upsert=True,
    )


def set_command_permissions_bulk(guild_id: str, command_names: list, enabled: bool,
                                 allowed_roles: list) -> int:
    """Apply one setting to many commands at once (the Members / Staff / Admin group
    form). The role list REPLACES whatever each command had. Only touches rows that
    already exist, so it never creates a command the bot doesn't have. Commands marked
    group_locked ("not affected by group changes") are skipped. Returns how many rows
    were updated."""
    if not command_names:
        return 0
    res = get_db().command_permissions.update_many(
        {"guild_id": str(guild_id), "command": {"$in": list(command_names)},
         "group_locked": {"$ne": True}},
        {"$set": {
            "enabled": bool(enabled),
            "allowed_roles": [str(r) for r in allowed_roles],
            "admin_only": False,
        }},
    )
    return res.modified_count


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------

_TRANSCRIPT_LIST_FIELDS = {"messages": 0, "html": 0}   # never load message bodies for a list


def get_transcripts(guild_id: str, limit: int = 10) -> list:
    """Newest transcripts, summary fields only (messages are far too big to list)."""
    return list(get_db().transcripts.find(
        {"guild_id": str(guild_id)}, _TRANSCRIPT_LIST_FIELDS
    ).sort("created_at", -1).limit(limit))


def search_transcripts(query: dict, page: int, per_page: int):
    """(rows, total) for the Transcripts page. `query` is a ready Mongo filter."""
    col = get_db().transcripts
    total = col.count_documents(query)
    rows = list(col.find(query, _TRANSCRIPT_LIST_FIELDS).sort("created_at", -1)
                .skip(max(page - 1, 0) * per_page).limit(per_page))
    return rows, total


def get_transcript(transcript_id: str) -> dict:
    try:
        oid = ObjectId(transcript_id)
    except Exception:
        return None
    return get_db().transcripts.find_one({"_id": oid})


# ---------------------------------------------------------------------------
# Ticket automations (rules the bot runs: utils/ticket_automation.py)
# ---------------------------------------------------------------------------

def get_ticket_automations(guild_id) -> list:
    return list(get_db().ticket_automations.find({"guild_id": str(guild_id)}).sort("created_at", 1))


def save_ticket_automation(guild_id, fields: dict, automation_id=None):
    """Create (automation_id None) or update a rule. Returns its id, or None if it can't be
    saved (server is at the limit, or the id isn't one of this server's rules)."""
    from utils import ticket_automation as ta
    col, gid, now = get_db().ticket_automations, str(guild_id), datetime.now(timezone.utc)
    if automation_id:
        try:
            oid = ObjectId(automation_id)
        except Exception:
            return None
        res = col.update_one({"_id": oid, "guild_id": gid}, {"$set": {**fields, "updated_at": now}})
        out = oid if res.matched_count else None
    else:
        if col.count_documents({"guild_id": gid}) >= ta.MAX_AUTOMATIONS:
            return None
        out = col.insert_one({**fields, "guild_id": gid, "created_at": now, "updated_at": now}).inserted_id
    if out:
        ta.mark_dirty(gid)                      # the bot reloads this server's rules now
    return out


def delete_ticket_automation(guild_id, automation_id) -> bool:
    from utils import ticket_automation as ta
    try:
        oid = ObjectId(automation_id)
    except Exception:
        return False
    ok = get_db().ticket_automations.delete_one({"_id": oid, "guild_id": str(guild_id)}).deleted_count == 1
    if ok:
        ta.mark_dirty(guild_id)
    return ok


def set_ticket_automation_enabled(guild_id, automation_id, enabled: bool) -> bool:
    from utils import ticket_automation as ta
    try:
        oid = ObjectId(automation_id)
    except Exception:
        return False
    ok = get_db().ticket_automations.update_one(
        {"_id": oid, "guild_id": str(guild_id)}, {"$set": {"enabled": bool(enabled)}}).matched_count == 1
    if ok:
        ta.mark_dirty(guild_id)
    return ok


def get_automation_runs(guild_id, limit: int = 30) -> list:
    return list(get_db().ticket_automation_runs.find({"guild_id": str(guild_id)}).sort("at", -1).limit(limit))


def transcript_panel_names(guild_id: str) -> list:
    """Panel names that appear on this server's transcripts (for the filter dropdown)."""
    names = get_db().transcripts.distinct("panel_name", {"guild_id": str(guild_id)})
    return sorted((n for n in names if n), key=str.lower)


# ---------------------------------------------------------------------------
# Transcript share links (expiring, revocable, no login needed to open)
# ---------------------------------------------------------------------------

SHARE_MIN_SECONDS = 3600                 # 1 hour
SHARE_MAX_SECONDS = 30 * 86400           # 30 days
_shares_indexed = False


def _shares():
    global _shares_indexed
    col = get_db().transcript_shares
    if not _shares_indexed:
        try:
            col.create_index("token", unique=True)
            col.create_index([("guild_id", 1), ("transcript_id", 1)])
            _shares_indexed = True
        except Exception as e:
            print(f"[dashboard] share index failed: {e}")
    return col


def create_transcript_share(guild_id, transcript_id, by_id, by_name, seconds: int) -> dict:
    """New share link. The token is a long random string, so it can't be guessed."""
    import secrets
    seconds = max(SHARE_MIN_SECONDS, min(int(seconds), SHARE_MAX_SECONDS))
    now = datetime.now(timezone.utc)
    doc = {
        "token": secrets.token_urlsafe(24),
        "guild_id": str(guild_id), "transcript_id": str(transcript_id),
        "created_by_id": str(by_id), "created_by_name": str(by_name),
        "created_at": now, "expires_at": now + timedelta(seconds=seconds),
        "revoked_at": None, "revoked_by_name": None, "views": 0,
    }
    doc["_id"] = _shares().insert_one(doc).inserted_id
    return doc


def get_transcript_shares(guild_id, transcript_id) -> list:
    return list(_shares().find({"guild_id": str(guild_id), "transcript_id": str(transcript_id)})
                .sort("created_at", -1).limit(50))


def share_state(share: dict, now=None) -> str:
    """"active" | "expired" | "revoked" """
    now = now or datetime.now(timezone.utc)
    if share.get("revoked_at"):
        return "revoked"
    exp = share.get("expires_at")
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return "expired" if exp is None or exp <= now else "active"


def revoke_transcript_share(guild_id, share_id, by_name) -> bool:
    try:
        oid = ObjectId(share_id)
    except Exception:
        return False
    res = _shares().update_one(
        {"_id": oid, "guild_id": str(guild_id), "revoked_at": None},
        {"$set": {"revoked_at": datetime.now(timezone.utc), "revoked_by_name": str(by_name)}})
    return res.modified_count == 1


def get_active_share(token: str):
    """The share doc if the link still works (not revoked, not expired), else None."""
    if not token or len(token) > 100:
        return None
    share = _shares().find_one({"token": token})
    return share if share and share_state(share) == "active" else None


def count_share_view(share_id) -> None:
    try:
        _shares().update_one({"_id": share_id}, {"$inc": {"views": 1}})
    except Exception:
        pass


def get_transcript_file(file_id: str):
    """GridFS file (or None). Images are saved by the bot when a ticket closes."""
    import gridfs
    try:
        fs = gridfs.GridFS(get_db(), collection="transcript_files")
        return fs.get(ObjectId(file_id))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Ticket types
# ---------------------------------------------------------------------------

def get_ticket_types(guild_id: str) -> list:
    return list(get_db().ticket_types.find({"guild_id": str(guild_id)}).sort("created_at", 1))


def get_ticket_type(type_id: str) -> dict:
    try:
        return get_db().ticket_types.find_one({"_id": ObjectId(type_id)})
    except Exception:
        return None


def upsert_ticket_type(guild_id: str, data: dict, type_id: str = None) -> str:
    db = get_db()
    data = _normalize_ticket_type(data)
    if type_id and data.get("linked_type_ids"):
        data["linked_type_ids"] = [t for t in data["linked_type_ids"] if t != str(type_id)]
    if type_id:
        db.ticket_types.update_one({"_id": ObjectId(type_id)}, {"$set": data})
        return type_id
    data["guild_id"] = str(guild_id)
    result = db.ticket_types.insert_one(data)
    return str(result.inserted_id)


def delete_ticket_type(type_id: str) -> None:
    get_db().ticket_types.delete_one({"_id": ObjectId(type_id)})
    get_db().ticket_panels.update_many(
        {"ticket_type_ids": type_id},
        {"$pull": {"ticket_type_ids": type_id}},
    )


# ---------------------------------------------------------------------------
# Ticket panels
# ---------------------------------------------------------------------------

def get_ticket_panels(guild_id: str) -> list:
    return list(get_db().ticket_panels.find({"guild_id": str(guild_id)}))


def get_ticket_panel(panel_id: str) -> dict:
    try:
        return get_db().ticket_panels.find_one({"_id": ObjectId(panel_id)})
    except Exception:
        return None


def upsert_ticket_panel(guild_id: str, data: dict, panel_id: str = None) -> str:
    db = get_db()
    data = _normalize_ticket_panel(data)
    data["guild_id"] = str(guild_id)
    if panel_id:
        db.ticket_panels.update_one({"_id": ObjectId(panel_id)}, {"$set": data})
        return panel_id
    result = db.ticket_panels.insert_one(data)
    return str(result.inserted_id)


def delete_ticket_panel(panel_id: str) -> None:
    get_db().ticket_panels.delete_one({"_id": ObjectId(panel_id)})


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

def _app_read_migrate(doc: dict) -> dict:
    if not doc:
        return doc
    if not doc.get("submit_channel_id") and doc.get("review_channel_id"):
        doc["submit_channel_id"] = doc["review_channel_id"]
    return doc


def get_applications(guild_id: str) -> list:
    return [
        _app_read_migrate(d)
        for d in get_db().applications.find({"guild_id": str(guild_id)}).sort("created_at", 1)
    ]


def get_application(guild_id: str, application_id: str) -> dict:
    try:
        doc = get_db().applications.find_one(
            {"_id": ObjectId(application_id), "guild_id": str(guild_id)}
        )
        return _app_read_migrate(doc)
    except Exception:
        return None


def upsert_application(guild_id: str, data: dict, application_id: str = None) -> str:
    db = get_db()
    data = _normalize_application(data)
    if application_id:
        db.applications.update_one(
            {"_id": ObjectId(application_id), "guild_id": str(guild_id)},
            {"$set": data},
        )
        return application_id
    data["guild_id"] = str(guild_id)
    result = db.applications.insert_one(data)
    return str(result.inserted_id)


def delete_application(guild_id: str, application_id: str) -> None:
    try:
        get_db().applications.delete_one(
            {"_id": ObjectId(application_id), "guild_id": str(guild_id)}
        )
    except Exception:
        pass


def set_application_open(guild_id: str, application_id: str, is_open: bool) -> None:
    try:
        get_db().applications.update_one(
            {"_id": ObjectId(application_id), "guild_id": str(guild_id)},
            {"$set": {"is_open": bool(is_open)}},
        )
    except Exception:
        pass


def get_application_panel(guild_id: str) -> dict:
    return get_db().application_panels.find_one({"guild_id": str(guild_id)})


def upsert_application_panel(guild_id: str, data: dict) -> None:
    data = dict(data)
    data["guild_id"] = str(guild_id)
    get_db().application_panels.update_one(
        {"guild_id": str(guild_id)}, {"$set": data}, upsert=True
    )


def enqueue_task(task_type: str, guild_id: str, payload: dict = None) -> None:
    get_db().dashboard_tasks.insert_one({
        "type": task_type,
        "guild_id": str(guild_id),
        "payload": payload or {},
        "created_at": datetime.now(timezone.utc),
        "done": False,
        "error": None,
    })


def get_submissions_for_guild(guild_id: str, application_id: str = None,
                              status: str = None) -> list:
    query = {"guild_id": str(guild_id)}
    if application_id:
        query["application_id"] = str(application_id)
    if status:
        query["status"] = status
    return list(get_db().application_submissions.find(query).sort("created_at", -1))


def get_application_submission(submission_id: str) -> dict:
    try:
        return get_db().application_submissions.find_one({"_id": ObjectId(submission_id)})
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Moderation
# ---------------------------------------------------------------------------

def get_mod_cases(guild_id: str, action: str = None, target_id: str = None,
                  page: int = 0, per_page: int = 50) -> tuple:
    query = {"guild_id": str(guild_id)}
    if action:
        query["action"] = action
    if target_id:
        query["target_id"] = str(target_id)

    total = get_db().mod_cases.count_documents(query)
    skip = max(page, 0) * per_page
    cursor = (
        get_db().mod_cases.find(query)
        .sort("created_at", -1)
        .skip(skip)
        .limit(per_page)
    )
    cases = list(cursor)
    total_pages = max((total + per_page - 1) // per_page, 1)
    return cases, total, total_pages


def get_moderation_config(guild_id: str) -> dict:
    doc = get_guild_settings(guild_id)
    cfg = doc.get("moderation") or {}
    return {
        "log_channel_id": (
            str(cfg["log_channel_id"]) if cfg.get("log_channel_id") else None
        ),
    }


# ---------------------------------------------------------------------------
# Auto-mod (settings live in guild_settings["automod"]; strikes in automod_strikes)
# ---------------------------------------------------------------------------

def get_automod_config(guild_id: str) -> dict:
    return normalize_automod_config(get_guild_settings(guild_id).get("automod"))


def save_automod_config(guild_id: str, cfg: dict) -> None:
    """Replace the whole automod settings block (the page always submits everything)."""
    update_guild_settings(guild_id, {"automod": cfg})


def get_automod_strikes(guild_id: str, window_minutes: int, limit: int = 100) -> list:
    """Members with at least one strike still inside the window: [{user_id, count, last_at}]."""
    import time as _time
    cutoff = _time.time() - max(60, int(window_minutes) * 60)
    rows = []
    for doc in get_db().automod_strikes.find({"guild_id": str(guild_id)}):
        live = [t for t in (doc.get("strikes") or []) if t >= cutoff]
        if live:
            rows.append({"user_id": str(doc.get("user_id")), "count": len(live),
                         "last_at": datetime.fromtimestamp(max(live), tz=timezone.utc)})
    rows.sort(key=lambda r: (r["count"], r["last_at"]), reverse=True)
    return rows[:limit]


def clear_automod_strikes(guild_id: str, user_id: str = None) -> int:
    col = get_db().automod_strikes
    if user_id:
        return col.delete_one({"_id": f"{guild_id}:{user_id}"}).deleted_count
    return col.delete_many({"guild_id": str(guild_id)}).deleted_count


# ---------------------------------------------------------------------------
# Role panels (button / dropdown role pickers)
# ---------------------------------------------------------------------------

def get_role_panels(guild_id: str) -> list:
    return list(get_db().role_panels.find({"guild_id": str(guild_id)}).sort("created_at", 1))


def get_role_panel(guild_id: str, panel_id: str):
    try:
        oid = ObjectId(str(panel_id))
    except Exception:
        return None
    return get_db().role_panels.find_one({"_id": oid, "guild_id": str(guild_id)})


def save_role_panel(guild_id: str, panel_id, data: dict) -> str:
    """Create (panel_id empty) or update a panel. Returns its id as a string."""
    now = datetime.now(timezone.utc)
    col = get_db().role_panels
    if panel_id:
        col.update_one({"_id": ObjectId(str(panel_id)), "guild_id": str(guild_id)},
                       {"$set": {**data, "updated_at": now}})
        return str(panel_id)
    res = col.insert_one({**data, "guild_id": str(guild_id), "message_id": None,
                          "posted_channel_id": None, "posted_at": None, "post_error": "",
                          "post_warning": "", "created_at": now, "updated_at": now})
    return str(res.inserted_id)


def delete_role_panel(guild_id: str, panel_id: str) -> None:
    get_db().role_panels.delete_one({"_id": ObjectId(str(panel_id)), "guild_id": str(guild_id)})


# ---------------------------------------------------------------------------
# Anti-nuke
# ---------------------------------------------------------------------------

ANTI_NUKE_ACTION_KEYS = [
    "ban",
    "kick",
    "channel_delete",
    "channel_create",
    "role_delete",
    "role_create",
    "webhook_create",
    "emoji_delete",
    "member_prune",
    "guild_update",
    "automod_rule_delete",
    "bot_add",
    "bot_remove",
]

ANTI_NUKE_ACTION_LABELS = {
    "ban": "Member banned",
    "kick": "Member kicked",
    "channel_delete": "Channel deleted",
    "channel_create": "Channel created",
    "role_delete": "Role deleted",
    "role_create": "Role created",
    "webhook_create": "Webhook created",
    "emoji_delete": "Emoji/sticker deleted",
    "member_prune": "Member prune",
    "guild_update": "Guild settings changed",
    "automod_rule_delete": "Automod rule deleted",
    "bot_add": "Bot added to guild",
    "bot_remove": "Bot / integration removed",
}


def get_anti_nuke_config(guild_id: str) -> dict:
    doc = get_guild_settings(guild_id)
    cfg = doc.get("anti_nuke") or {}
    actions = cfg.get("actions") or {}
    out_actions = {}
    for k in ANTI_NUKE_ACTION_KEYS:
        a = actions.get(k) or {}
        out_actions[k] = {
            "enabled": bool(a.get("enabled", False)),
            "count": int(a.get("count") or 5),
            "window_seconds": int(a.get("window_seconds") or 10),
        }
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "dry_run": bool(cfg.get("dry_run", False)),
        "auto_revert": bool(cfg.get("auto_revert", True)),
        "punishment": cfg.get("punishment") or "alert_only",
        "security_alert_channel_id": (
            str(cfg["security_alert_channel_id"])
            if cfg.get("security_alert_channel_id") else None
        ),
        "alert_role_ids": [str(r) for r in (cfg.get("alert_role_ids") or [])],
        "whitelist_role_ids": [str(r) for r in (cfg.get("whitelist_role_ids") or [])],
        "whitelist_user_ids": [str(r) for r in (cfg.get("whitelist_user_ids") or [])],
        "whitelist_bot_ids": [str(r) for r in (cfg.get("whitelist_bot_ids") or [])],
        "actions": out_actions,
    }


def set_anti_nuke_config(guild_id: str, update: dict) -> None:
    prefixed = {f"anti_nuke.{k}": v for k, v in update.items()}
    if prefixed:
        update_guild_settings(guild_id, prefixed)


def get_nuke_events(guild_id: str, limit: int = 100) -> list:
    return list(
        get_db().nuke_events.find({"guild_id": str(guild_id)})
        .sort("created_at", -1)
        .limit(limit)
    )


def prune_nuke_events(guild_id: str, older_than_days: int = 30) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(older_than_days))
    result = get_db().nuke_events.delete_many({
        "guild_id": str(guild_id),
        "created_at": {"$lt": cutoff},
    })
    return int(result.deleted_count or 0)


# ---------------------------------------------------------------------------
# Blacklist
# ---------------------------------------------------------------------------

def get_blacklist_config(guild_id: str) -> dict:
    doc = get_guild_settings(guild_id)
    cfg = doc.get("blacklist") or {}
    counters = cfg.get("counters") or {}
    return {
        "giveaway_role_id": (
            str(cfg["giveaway_role_id"]) if cfg.get("giveaway_role_id") else None
        ),
        "ticket_role_id": (
            str(cfg["ticket_role_id"]) if cfg.get("ticket_role_id") else None
        ),
        "request_channel_id": (
            str(cfg["request_channel_id"]) if cfg.get("request_channel_id") else None
        ),
        "staff_role_id": (
            str(cfg["staff_role_id"]) if cfg.get("staff_role_id") else None
        ),
        "application_role_id": (
            str(cfg["application_role_id"]) if cfg.get("application_role_id") else None
        ),
        "counters": {
            bucket: {
                t: int((counters.get(bucket) or {}).get(t) or 0)
                for t in ("giveaway", "ticket", "application")
            }
            for bucket in ("submitted", "accepted", "denied")
        },
    }


def get_blacklist_requests(guild_id: str, status: str = None, type_: str = None,
                           action: str = None) -> list:
    query = {"guild_id": str(guild_id)}
    if status:
        query["status"] = status
    if type_:
        query["type"] = type_
    if action:
        query["action"] = action
    return list(get_db().blacklist_requests.find(query).sort("created_at", -1))


def get_blacklist_request(request_id: str) -> dict:
    try:
        return get_db().blacklist_requests.find_one({"_id": ObjectId(request_id)})
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Honeypot
# ---------------------------------------------------------------------------

DEFAULT_HONEYPOT_DM = (
    "You were softbanned from {guild} for posting in a trap channel. "
    "This is an automated action. If you believe this was a mistake, "
    "contact the server staff."
)


def get_honeypot_config(guild_id: str) -> dict:
    doc = get_guild_settings(guild_id)
    cfg = doc.get("honeypot") or {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "channel_id": (
            str(cfg["channel_id"]) if cfg.get("channel_id") else None
        ),
        "bypass_role_id": (
            str(cfg["bypass_role_id"]) if cfg.get("bypass_role_id") else None
        ),
        "panel_message_id": (
            str(cfg["panel_message_id"]) if cfg.get("panel_message_id") else None
        ),
        "kick_count": int(cfg.get("kick_count") or 0),
        "dm_message": str(cfg.get("dm_message") or DEFAULT_HONEYPOT_DM),
    }


def get_honeypot_kicks(guild_id: str, limit: int = 200) -> list:
    return list(
        get_db().honeypot_kicks
        .find({"guild_id": str(guild_id)})
        .sort("created_at", -1)
        .limit(limit)
    )


# ---------------------------------------------------------------------------
# Staff strikes (per Change type) - the bot writes strikes, the dashboard manages them
# ---------------------------------------------------------------------------

def get_strike_type_config(guild_id: str, type_id: str) -> dict:
    from utils.strikes_core import normalize_type_config
    doc = get_guild_settings(guild_id)
    raw = ((doc.get("strikes") or {}).get("types") or {}).get(str(type_id))
    return normalize_type_config(raw)


def save_strike_type_config(guild_id: str, type_id: str, cfg: dict) -> None:
    update_guild_settings(guild_id, {f"strikes.types.{type_id}": cfg})


def get_strike_rows(guild_id: str, type_id: str, limit: int = 200) -> list:
    """Members with strikes in a type: [{user_id, count, last_at, strikes: [...]}], most first."""
    rows = {}
    cursor = get_db().staff_strikes.find(
        {"guild_id": str(guild_id), "type_id": str(type_id)}).sort("created_at", 1)
    for d in cursor:
        r = rows.setdefault(d["user_id"], {"user_id": d["user_id"], "count": 0,
                                          "last_at": d["created_at"], "strikes": []})
        r["count"] += 1
        r["last_at"] = d["created_at"]
        r["strikes"].append(d)
    out = sorted(rows.values(), key=lambda r: (r["count"], r["last_at"]), reverse=True)
    return out[:limit]


def delete_strike(guild_id: str, number: int, type_id: str = None):
    """Delete one strike by number (optionally only if it's in `type_id`).
    Returns the deleted document or None."""
    q = {"guild_id": str(guild_id), "number": int(number)}
    if type_id:
        q["type_id"] = str(type_id)
    return get_db().staff_strikes.find_one_and_delete(q)


def clear_strikes(guild_id: str, type_id: str, user_id: str = None) -> int:
    q = {"guild_id": str(guild_id), "type_id": str(type_id)}
    if user_id:
        q["user_id"] = str(user_id)
    return get_db().staff_strikes.delete_many(q).deleted_count


def add_strike_audit(guild_id: str, action: str, type_id: str, type_name: str, user_id,
                     by: str, detail: str = "") -> None:
    get_db().staff_strike_audit.insert_one({
        "guild_id": str(guild_id), "action": action, "type_id": str(type_id),
        "type_name": str(type_name), "user_id": str(user_id) if user_id else "",
        "by": str(by), "detail": str(detail)[:500], "at": datetime.now(timezone.utc),
    })


def get_strike_audit(guild_id: str, limit: int = 20) -> list:
    return list(get_db().staff_strike_audit.find(
        {"guild_id": str(guild_id)}).sort("at", -1).limit(limit))



# ---------------------------------------------------------------------------
# Audit log: who did what from the dashboard (shown on Config & Logs)
# ---------------------------------------------------------------------------
_audit_indexed = False


AUDIT_RETENTION_DAYS = 180        # older entries are deleted by Mongo itself (TTL index)


def log_audit(guild_id, actor_id, actor_name, action: str, detail: str = "",
              page: str = "", lines: list = None) -> None:
    """Record a dashboard action. `page` / `lines` carry the readable before/after of a save
    (see dashboard/audit.py). Never raises (a logging problem must not block the action)."""
    global _audit_indexed
    try:
        db = get_db()
        if not _audit_indexed:
            db.dashboard_audit.create_index([("guild_id", 1), ("at", -1)])
            db.dashboard_audit.create_index("at", name="audit_ttl",
                                            expireAfterSeconds=AUDIT_RETENTION_DAYS * 86400)
            _audit_indexed = True
        res = db.dashboard_audit.insert_one({
            "guild_id": str(guild_id), "actor_id": str(actor_id), "actor_name": str(actor_name),
            "action": action, "detail": detail, "page": page, "lines": list(lines or []),
            "at": datetime.now(timezone.utc),
        })
        try:       # the generic hook adds its before/after lines to this entry, not a duplicate
            from flask import g, has_request_context
            if has_request_context() and action != "dashboard_save":
                g.audit_last_id = res.inserted_id
        except Exception:
            pass
    except Exception as e:
        print(f"[dashboard] audit log failed: {e}")


def search_audit(guild_id, actor: str = "", page: str = "", q: str = "",
                 skip: int = 0, limit: int = 50):
    """(rows, total) newest first, filtered by person (name or id), page and text."""
    flt = {"guild_id": str(guild_id)}
    actor = (actor or "").strip()
    if actor:
        flt["$or"] = [{"actor_name": {"$regex": re.escape(actor), "$options": "i"}},
                      {"actor_id": actor}]
    if page:
        flt["page"] = page
    q = (q or "").strip()
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        flt["$and"] = [{"$or": [{"detail": rx}, {"lines": rx}, {"action": rx}]}]
    try:
        col = get_db().dashboard_audit
        return (list(col.find(flt).sort("at", -1).skip(max(0, skip)).limit(limit)),
                col.count_documents(flt))
    except Exception as e:
        print(f"[dashboard] audit search failed: {e}")
        return [], 0


def audit_pages(guild_id) -> list:
    try:
        return sorted(p for p in get_db().dashboard_audit.distinct("page", {"guild_id": str(guild_id)}) if p)
    except Exception:
        return []


def get_audit(guild_id, limit: int = 25) -> list:
    try:
        return list(get_db().dashboard_audit.find({"guild_id": str(guild_id)})
                    .sort("at", -1).limit(limit))
    except Exception as e:
        print(f"[dashboard] audit read failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Promotion history (written by cogs/changes.py, read-only on the dashboard)
# ---------------------------------------------------------------------------
def get_promotion_history(guild_id: str, search: str = "", type_id: str = "", kind: str = "",
                          limit: int = 200) -> list:
    """Newest first. `search` = a member ID (digits) or part of a name."""
    q = {"guild_id": str(guild_id)}
    search = (search or "").strip()
    if search:
        if search.isdigit() and len(search) >= 15:
            q["member_id"] = search
        else:
            q["member_name"] = {"$regex": re.escape(search), "$options": "i"}
    if type_id:
        q["type_id"] = str(type_id)
    if kind in ("promotion", "demotion"):
        q["kind"] = kind
    try:
        return list(get_db().promotion_history.find(q).sort("at", -1).limit(limit))
    except Exception as e:
        print(f"[dashboard] promotion history read failed: {e}")
        return []


def count_expiring_strikes(guild_id: str, type_id: str, days: int) -> int:
    """How many strikes the next sweep would remove if strikes expired after `days`."""
    if not days or days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
    return get_db().staff_strikes.count_documents(
        {"guild_id": str(guild_id), "type_id": str(type_id), "created_at": {"$lt": cutoff}})
