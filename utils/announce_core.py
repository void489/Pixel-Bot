"""Pure helpers for scheduled announcements (no Discord or DB imports).

A schedule document (collection `scheduled_announcements`):
    guild_id, channel_id, ping_role_id (or None), use_embed, title, message, color, image_url,
    first_local  "YYYY-MM-DDTHH:MM"  - the first send time on the wall clock of `tz`
    tz           IANA name, e.g. "Europe/Lisbon"
    repeat       "once" | "daily" | "weekly"
    next_run     UTC datetime of the next send (the bot's loop posts when it is due)
    status       "scheduled" | "sent" | "failed" | "cancelled"

Repeats step on the LOCAL wall clock, so "every day at 18:00" stays 18:00 across daylight-saving
changes.
"""
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPEATS = ("once", "daily", "weekly")
STEP_DAYS = {"daily": 1, "weekly": 7}
MAX_ACTIVE = 50
MAX_TEXT = 2000
MAX_EMBED_TEXT = 4000
MAX_TITLE = 256
LATE_AFTER_SECONDS = 300        # posted this much after its time = "late" (bot was down)

COMMON_TIMEZONES = [
    "UTC", "Europe/Lisbon", "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Madrid",
    "Europe/Rome", "Europe/Athens", "Europe/Istanbul", "Europe/Moscow", "Africa/Cairo",
    "Africa/Johannesburg", "Asia/Dubai", "Asia/Kolkata", "Asia/Bangkok", "Asia/Singapore",
    "Asia/Shanghai", "Asia/Tokyo", "Australia/Sydney", "Pacific/Auckland",
    "America/Sao_Paulo", "America/Argentina/Buenos_Aires", "America/New_York",
    "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Toronto",
    "America/Mexico_City",
]


def get_tz(name):
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError, KeyError, TypeError, OSError):
        return None


def parse_local(text):
    """'YYYY-MM-DDTHH:MM' (what <input type=datetime-local> sends) -> naive datetime, or None."""
    try:
        return datetime.strptime((text or "").strip()[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return None


def local_to_utc(naive_local: datetime, tzname: str):
    tz = get_tz(tzname)
    if tz is None:
        return None
    return naive_local.replace(tzinfo=tz).astimezone(timezone.utc)


def aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def next_run(first_local: str, tzname: str, repeat: str, after_utc: datetime):
    """The first send time strictly after `after_utc` (UTC, aware), or None if there is none
    (a one-off whose time has passed, or bad data)."""
    first = parse_local(first_local)
    if first is None or get_tz(tzname) is None or repeat not in REPEATS:
        return None
    after_utc = aware(after_utc)
    start = local_to_utc(first, tzname)
    if repeat == "once":
        return start if start > after_utc else None
    step = STEP_DAYS[repeat]
    if start > after_utc:
        return start
    k = max(0, math.floor((after_utc - start).total_seconds() / (86400 * step)) - 1)
    for _ in range(10):              # a couple of iterations at most (DST can shift by an hour)
        cand = local_to_utc(first + timedelta(days=step * k), tzname)
        if cand > after_utc:
            return cand
        k += 1
    return None


def to_local_text(dt_utc, tzname: str) -> str:
    """'2026-10-18 18:00' on the schedule's own clock (for the dashboard list)."""
    tz = get_tz(tzname) or timezone.utc
    return aware(dt_utc).astimezone(tz).strftime("%Y-%m-%d %H:%M")


def clean_color(raw, default=0x5865F2) -> int:
    raw = (raw or "").strip().lstrip("#")
    try:
        if len(raw) == 6:
            return int(raw, 16)
    except ValueError:
        pass
    return default


def clean_image_url(raw) -> str:
    raw = (raw or "").strip()
    return raw[:500] if raw.startswith("https://") else ""


def is_late(due_utc, now_utc) -> bool:
    return (aware(now_utc) - aware(due_utc)).total_seconds() > LATE_AFTER_SECONDS
