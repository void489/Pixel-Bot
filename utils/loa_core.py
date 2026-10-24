"""Leave of absence (LOA) helpers shared by cogs/loa.py and cogs/staff_activity.py.

Records live in "loa_requests"; per-server setup (role + request channel) lives in
"loa_settings" and is edited on the dashboard's LOA page.

Statuses: pending -> active -> expired | ended | removed, or pending -> denied | removed.
"""
import re
from datetime import datetime, timezone

from database.mongo import loa_requests_col

MIN_SECONDS = 3600            # 1 hour
MAX_SECONDS = 365 * 86400     # 1 year

_PART = re.compile(r"(\d+)\s*([wdhm])", re.I)
_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60}


def parse_duration(text: str):
    """'3d', '1w', '2w3d', '1d 12h' -> seconds, or None if it can't be read."""
    text = (text or "").strip().lower()
    if not text:
        return None
    parts = _PART.findall(text)
    if not parts or _PART.sub("", text).strip():
        return None
    return sum(int(n) * _UNITS[u.lower()] for n, u in parts)


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    out = []
    for name, size in (("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n, seconds = divmod(seconds, size)
            out.append(f"{n} {name}{'' if n == 1 else 's'}")
    return ", ".join(out[:3]) or "0 minutes"


def aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def loa_user_ids(guild_id: int, since: datetime = None, until: datetime = None) -> set:
    """User ids that were on an approved LOA at any moment between `since` and `until`
    (both default to now, i.e. 'on LOA right now'). Ended/expired LOAs count if they
    overlapped the window, so an activity check never marks them as missing."""
    now = datetime.now(timezone.utc)
    since = since or now
    until = until or now
    cursor = loa_requests_col().find({
        "guild_id": guild_id,
        "status": {"$in": ["active", "expired", "ended"]},
        "start": {"$lte": until},
        "end": {"$gte": since},
    })
    return {int(d["user_id"]) async for d in cursor}
