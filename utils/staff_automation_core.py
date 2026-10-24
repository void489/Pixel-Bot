"""Pure helpers for the staff automation features (no Discord or DB imports):
promotion suggestions, probation reviews and the weekly staff report.

Settings live in guild_settings["staff_automation"]:
    mgmt_channel_id, ping_role_id
    suggestions: {enabled, types: {type_id: {use_lifetime, rungs: {"<target rung>": req}}}}
        req = {handled, closed, min_rating (0-5), min_ratings}
    probation:   {enabled, types: {type_id: days}}
    report:      {enabled, channel_id, weekday (0=Mon), hour, tz}

Stats come from TASKBOOKS. A taskbook's normal counters can be reset (/taskbook reset), so
the bot also keeps LIFETIME totals (life_*) that a reset never touches.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_COUNT = 1_000_000
MAX_PROBATION_DAYS = 365
MAX_EXTEND_DAYS = 90
DEFAULT_EXTEND_DAYS = 7
STAT_FIELDS = ("handled", "closed", "renamed", "blacklisted")
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _int(v, default=0, lo=0, hi=MAX_COUNT):
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _float(v, default=0.0, lo=0.0, hi=5.0):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def clean_req(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "handled": _int(raw.get("handled")),
        "closed": _int(raw.get("closed")),
        "min_rating": round(_float(raw.get("min_rating")), 2),
        "min_ratings": _int(raw.get("min_ratings"), 0, 0, 1000),
    }


def req_is_empty(req: dict) -> bool:
    return not (req["handled"] or req["closed"] or req["min_rating"])


def normalize(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    sug = raw.get("suggestions") if isinstance(raw.get("suggestions"), dict) else {}
    prob = raw.get("probation") if isinstance(raw.get("probation"), dict) else {}
    rep = raw.get("report") if isinstance(raw.get("report"), dict) else {}
    stypes = {}
    for tid, t in (sug.get("types") or {}).items():
        t = t if isinstance(t, dict) else {}
        rungs = {}
        for k, r in (t.get("rungs") or {}).items():
            if str(k).isdigit() and int(k) >= 1:
                req = clean_req(r)
                if not req_is_empty(req):
                    rungs[str(int(k))] = req
        stypes[str(tid)] = {"use_lifetime": bool(t.get("use_lifetime", True)), "rungs": rungs}
    ptypes = {str(tid): _int(d, 0, 0, MAX_PROBATION_DAYS)
              for tid, d in (prob.get("types") or {}).items()}
    tz = str(rep.get("tz") or "UTC")
    return {
        "mgmt_channel_id": str(raw["mgmt_channel_id"]) if raw.get("mgmt_channel_id") else "",
        "ping_role_id": str(raw["ping_role_id"]) if raw.get("ping_role_id") else "",
        "suggestions": {"enabled": bool(sug.get("enabled")), "types": stypes},
        "probation": {"enabled": bool(prob.get("enabled")), "types": ptypes},
        "report": {
            "enabled": bool(rep.get("enabled")),
            "channel_id": str(rep["channel_id"]) if rep.get("channel_id") else "",
            "weekday": _int(rep.get("weekday"), 0, 0, 6),
            "hour": _int(rep.get("hour"), 18, 0, 23),
            "tz": tz if get_tz(tz) else "UTC",
        },
    }


def get_tz(name):
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError, KeyError, TypeError, OSError):
        return None


# ------------------------------------------------------------------ suggestions
def stats_from_taskbook(tb: dict, lifetime: bool) -> dict:
    """handled / closed / rating totals. Lifetime falls back to the normal counters for a
    taskbook that has never been backfilled."""
    def pick(field):
        if lifetime and tb.get(f"life_{field}") is not None:
            return int(tb.get(f"life_{field}") or 0)
        return int(tb.get(field) or 0)
    return {"handled": pick("handled"), "closed": pick("closed"),
            "rating_sum": pick("rating_sum"), "rating_count": pick("rating_count")}


def meets(req: dict, stats: dict):
    """(ok, [what is missing]). A minimum rating only counts once there are `min_ratings`
    ratings, so one lucky 5-star can't promote anyone."""
    missing = []
    if stats["handled"] < req["handled"]:
        missing.append(f"handled {stats['handled']}/{req['handled']}")
    if stats["closed"] < req["closed"]:
        missing.append(f"closed {stats['closed']}/{req['closed']}")
    if req["min_rating"]:
        n = stats["rating_count"]
        avg = stats["rating_sum"] / n if n else 0.0
        if n < max(1, req["min_ratings"]):
            missing.append(f"ratings {n}/{max(1, req['min_ratings'])}")
        elif avg + 1e-9 < req["min_rating"]:
            missing.append(f"rating {avg:.2f}/{req['min_rating']:.2f}")
    return (not missing, missing)


def describe_stats(stats: dict) -> str:
    n = stats["rating_count"]
    rating = f"{stats['rating_sum'] / n:.2f}/5 ({n})" if n else "no ratings"
    return f"{stats['handled']} handled · {stats['closed']} closed · {rating}"


# ------------------------------------------------------------------ probation
def aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def probation_end(start, days: int, extended_until=None):
    """When probation ends: `days` after the start, or later if a review was extended
    (extensions run from the day they were clicked, not from the start)."""
    if start is None or days <= 0:
        return None
    end = aware(start) + timedelta(days=days)
    ext = aware(extended_until)
    return ext if ext is not None and ext > end else end


# ------------------------------------------------------------------ weekly report
def week_diff(prev: dict, cur: dict) -> dict:
    """Per-field difference between two snapshots of one taskbook. If a counter went DOWN
    (a reset happened in between) the new value is what happened since the reset."""
    out = {}
    for f in STAT_FIELDS + ("rating_sum", "rating_count"):
        a, b = int((prev or {}).get(f) or 0), int((cur or {}).get(f) or 0)
        out[f] = b - a if b >= a else b
    return out


def last_due(now_utc: datetime, weekday: int, hour: int, tzname: str):
    """The most recent scheduled report time at or before now (UTC), on the schedule's own
    wall clock, so '18:00 Monday' survives daylight-saving changes."""
    tz = get_tz(tzname) or timezone.utc
    local_now = aware(now_utc).astimezone(tz)
    days_back = (local_now.weekday() - weekday) % 7
    cand = (local_now - timedelta(days=days_back)).replace(
        hour=hour, minute=0, second=0, microsecond=0, tzinfo=None)
    if cand.replace(tzinfo=tz).astimezone(timezone.utc) > aware(now_utc):
        cand -= timedelta(days=7)
    return cand.replace(tzinfo=tz).astimezone(timezone.utc)
