"""Pure helpers for auto-closing inactive tickets (no Discord or DB imports).

Settings live on the ticket PANEL document (dashboard -> Ticket Types & Panels -> panel form):
    auto_close_warn_hours   0 = off. Hours with no message before the warning is posted.
    auto_close_grace_hours  Hours after the warning before the ticket is closed (default 24).
    auto_close_claimed      "longer" (default: claimed tickets wait twice as long before the
                            warning), "same", or "never" (claimed tickets are left alone).

Activity = the time of the channel's last message. The bot's own warning is the last message
until somebody replies, so "last message is still the warning" means nobody has answered.
"""
from datetime import datetime, timezone

MAX_WARN_HOURS = 24 * 30
MAX_GRACE_HOURS = 24 * 7
DEFAULT_GRACE_HOURS = 24
CLAIMED_MODES = ("longer", "same", "never")


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize(raw: dict) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    mode = raw.get("auto_close_claimed")
    return {
        "warn_hours": max(0, min(MAX_WARN_HOURS, _int(raw.get("auto_close_warn_hours")))),
        "grace_hours": max(1, min(MAX_GRACE_HOURS,
                                  _int(raw.get("auto_close_grace_hours"), DEFAULT_GRACE_HOURS)
                                  or DEFAULT_GRACE_HOURS)),
        "claimed": mode if mode in CLAIMED_MODES else "longer",
    }


def aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def warn_after_hours(cfg: dict, claimed: bool):
    """Hours of silence before the warning, or None if this ticket is exempt."""
    if cfg["warn_hours"] <= 0:
        return None
    if claimed:
        if cfg["claimed"] == "never":
            return None
        if cfg["claimed"] == "longer":
            return cfg["warn_hours"] * 2
    return cfg["warn_hours"]


def decide(now: datetime, last_activity: datetime, warned_at, warn_msg_id, last_msg_id,
           cfg: dict, claimed: bool) -> str:
    """What to do with one open ticket:
       "none"  - leave it
       "reset" - somebody replied after the warning: clear the warning, then re-check
       "warn"  - post the warning now
       "close" - warned long enough ago and still nobody replied
    """
    now, last_activity, warned_at = aware(now), aware(last_activity), aware(warned_at)
    if warned_at is not None:
        # Anything newer than the warning (any message by anyone) cancels it.
        if warn_msg_id is None or last_msg_id is None or int(last_msg_id) != int(warn_msg_id):
            return "reset"
        if (now - warned_at).total_seconds() >= cfg["grace_hours"] * 3600:
            return "close"
        return "none"
    after = warn_after_hours(cfg, claimed)
    if after is None:
        return "none"
    if (now - last_activity).total_seconds() >= after * 3600:
        return "warn"
    return "none"


def hours_text(h: int) -> str:
    if h % 24 == 0 and h >= 24:
        d = h // 24
        return f"{d} day{'' if d == 1 else 's'}"
    return f"{h} hour{'' if h == 1 else 's'}"
