"""Auto-mod settings shape, shared by the bot (async) and the dashboard (sync).

Pure python - no database imports - so both sides normalise the stored document the
same way and can never drift apart. The values live in guild_settings["automod"].
"""
import re

AUTOMOD_RULES = ("spam", "mentions", "invites", "links", "caps", "badwords")

# Allowed range for each rule's limit (rules not listed here have no limit).
AUTOMOD_LIMIT_BOUNDS = {"spam": (3, 15), "mentions": (1, 50), "caps": (30, 100)}

# Allowed range for each strike-ladder field.
AUTOMOD_STRIKE_BOUNDS = {
    "window_minutes": (1, 1440),
    "mute_at": (0, 20),
    "mute_minutes": (1, 40320),
    "kick_at": (0, 20),
    "ban_at": (0, 20),
}

MAX_BADWORDS = 200
MAX_ALLOWED_DOMAINS = 100

AUTOMOD_DEFAULTS = {
    "enabled": False,
    "log_channel_id": None,
    "rules": {
        # limit: spam = messages per 5s, mentions = max mentions per message,
        # caps = max % of capital letters. Others ignore it.
        "spam":     {"enabled": True,  "limit": 5},
        "mentions": {"enabled": True,  "limit": 5},
        "invites":  {"enabled": True,  "limit": 0},
        "links":    {"enabled": False, "limit": 0},
        "caps":     {"enabled": False, "limit": 70},
        "badwords": {"enabled": False, "limit": 0},
    },
    "badwords": [],
    "link_allow": [],
    "exempt_role_ids": [],
    "exempt_channel_ids": [],
    "strikes": {
        "window_minutes": 10,
        "mute_at": 3,
        "mute_minutes": 10,
        "kick_at": 0,
        "ban_at": 0,
    },
}


def normalize_automod_config(stored: dict) -> dict:
    """Turn whatever is stored in guild_settings["automod"] into a complete config."""
    stored = stored or {}
    cfg = {
        "enabled": bool(stored.get("enabled", False)),
        "log_channel_id": str(stored["log_channel_id"]) if stored.get("log_channel_id") else None,
        "badwords": [str(w) for w in (stored.get("badwords") or [])],
        "link_allow": [str(d) for d in (stored.get("link_allow") or [])],
        "exempt_role_ids": [str(r) for r in (stored.get("exempt_role_ids") or [])],
        "exempt_channel_ids": [str(c) for c in (stored.get("exempt_channel_ids") or [])],
        "rules": {},
        "strikes": dict(AUTOMOD_DEFAULTS["strikes"]),
    }
    srules = stored.get("rules") or {}
    for name, dflt in AUTOMOD_DEFAULTS["rules"].items():
        r = dict(dflt)
        if isinstance(srules.get(name), dict):
            r["enabled"] = bool(srules[name].get("enabled", r["enabled"]))
            try:
                r["limit"] = int(srules[name].get("limit", r["limit"]))
            except (TypeError, ValueError):
                pass
        cfg["rules"][name] = r
    for k, v in (stored.get("strikes") or {}).items():
        if k in cfg["strikes"]:
            try:
                cfg["strikes"][k] = max(0, int(v))
            except (TypeError, ValueError):
                pass
    return cfg


def clean_domain(raw: str) -> str:
    """'https://www.YouTube.com/watch?v=1' -> 'youtube.com'"""
    d = (raw or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0].split("?")[0].split("#")[0].split("@")[-1].split(":")[0]
    return d[4:] if d.startswith("www.") else d
