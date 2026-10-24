"""Staff points - pure helpers shared by the bot cog and the Flask dashboard.

No discord / database imports on purpose: the bot reads/writes with Motor
(async) and the dashboard with PyMongo (sync), but both need identical
config parsing and identical leaderboard maths.

Storage model
-------------
"staff_points_hourly" holds one document per (guild, user, event type, hour):
    {guild_id, user_id, type, hour, count, points}
Events are $inc'd into the current hour's bucket, so the collection stays small
no matter how many messages staff send, and any time window is a date-filtered
sum. Points are stored on the bucket at the moment they're earned, so changing
the weights later never rewrites history.
"""
from datetime import timedelta

# Display order everywhere (board + dashboard).
EVENT_TYPES = ("mod_action", "blacklist", "ticket_handled", "ticket_rename", "msg_ticket", "msg_outside")

EVENT_LABELS = {
    "mod_action":     "Moderation actions",
    "blacklist":      "Blacklists",
    "ticket_handled": "Tickets handled",
    "ticket_rename":  "Ticket renames",
    "msg_ticket":     "Messages in tickets",
    "msg_outside":    "Messages outside tickets",
}

WINDOWS = ("all", "24h", "7d", "30d")
WINDOW_LABELS = {"all": "All time", "24h": "24h", "7d": "7 days", "30d": "30 days"}

# Moderation cases that earn points. The "undo" actions (unmute / unban /
# unlock) are deliberately left out.
COUNTED_MOD_ACTIONS = ("mute", "warn", "ban", "kick", "softban", "tempban", "purge")

DEFAULT_CONFIG = {
    "staff_role_ids":   [],
    "msg_outside":      0.05,
    "msg_ticket":       0.25,
    "ticket_rename":    3.0,
    "ticket_handled":   15.0,
    "mod_action":       30.0,
    "blacklist":        30.0,
    "min_messages":     3,
    "cooldown_seconds": 10,
}


def _num(raw, default, lo=0.0, hi=100000.0):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return max(lo, min(hi, value))


def normalize_config(raw: dict) -> dict:
    """Turns whatever is stored in guild_settings['staff_points'] (or a raw
    dashboard form) into a complete, validated config."""
    raw = raw or {}
    cfg = {
        "staff_role_ids": [str(r) for r in (raw.get("staff_role_ids") or []) if str(r).isdigit()],
    }
    for key in ("msg_outside", "msg_ticket", "ticket_rename", "ticket_handled", "mod_action", "blacklist"):
        cfg[key] = _num(raw.get(key), DEFAULT_CONFIG[key])
    cfg["min_messages"] = int(_num(raw.get("min_messages"), DEFAULT_CONFIG["min_messages"], 1, 1000))
    cfg["cooldown_seconds"] = int(_num(raw.get("cooldown_seconds"), DEFAULT_CONFIG["cooldown_seconds"], 0, 3600))
    return cfg


def hour_floor(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def window_cutoffs(now) -> dict:
    """Bucket-aligned cutoffs (buckets are hourly, so windows are accurate to
    the hour)."""
    return {
        "24h": hour_floor(now - timedelta(hours=24)),
        "7d":  hour_floor(now - timedelta(days=7)),
        "30d": hour_floor(now - timedelta(days=30)),
    }


def leaderboard_pipeline(guild_id, now) -> list:
    """One aggregation that returns per-(user, type) sums for every window."""
    def windowed(cutoff, field):
        return {"$sum": {"$cond": [{"$gte": ["$hour", cutoff]}, field, 0]}}

    group = {
        "_id": {"u": "$user_id", "t": "$type"},
        "count_all":  {"$sum": "$count"},
        "points_all": {"$sum": "$points"},
    }
    for key, cutoff in window_cutoffs(now).items():
        group[f"count_{key}"] = windowed(cutoff, "$count")
        group[f"points_{key}"] = windowed(cutoff, "$points")
    return [{"$match": {"guild_id": str(guild_id)}}, {"$group": group}]


def empty_bucket() -> dict:
    bucket = {t: 0 for t in EVENT_TYPES}
    bucket["points"] = 0.0
    return bucket


def fold_rows(rows) -> dict:
    """{user_id: {window: {event_type: count, ..., 'points': total}}}"""
    out = {}
    for row in rows:
        uid = row["_id"]["u"]
        etype = row["_id"]["t"]
        if etype not in EVENT_TYPES:
            continue
        entry = out.setdefault(uid, {w: empty_bucket() for w in WINDOWS})
        for w in WINDOWS:
            entry[w][etype] += int(row.get(f"count_{w}") or 0)
            entry[w]["points"] += float(row.get(f"points_{w}") or 0)
    return out


def rank(folded: dict, allowed_uids=None, sort_window: str = "all") -> list:
    """[(user_id, windows), ...] sorted by points in `sort_window`, highest
    first. If `allowed_uids` is given, everyone else is dropped."""
    items = [
        (uid, windows) for uid, windows in folded.items()
        if allowed_uids is None or uid in allowed_uids
    ]
    items.sort(key=lambda kv: kv[1][sort_window]["points"], reverse=True)
    return items


def fmt_points(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def leaderboard_rows(folded: dict, members: dict, staff_role_ids, window: str = "all") -> list:
    """Rows for the dashboard table.

    `members` maps user_id -> {"name": str, "roles": [role ids], "bot": bool} for the
    people Discord says are in the server. Only members who CURRENTLY hold a staff
    role are listed (same rule as the in-Discord board)."""
    staff = {str(r) for r in staff_role_ids}
    allowed = {
        uid for uid, m in members.items()
        if uid in folded and not m.get("bot") and staff & {str(r) for r in (m.get("roles") or [])}
    }
    rows = []
    for position, (uid, windows) in enumerate(rank(folded, allowed, window), 1):
        bucket = windows[window]
        rows.append({
            "rank": position,
            "name": members[uid].get("name") or uid,
            "points": fmt_points(bucket["points"]),
            "counts": {t: bucket[t] for t in EVENT_TYPES},
        })
    return rows
