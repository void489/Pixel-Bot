"""Builds the Mongo filter for the Transcripts page (pure: no Flask, no DB).

Filters (all optional, all combined with AND):
    q         general text: ticket name, member / staff names, reason, panel, or an ID
    member    the member who opened the ticket (name or ID)
    staff     staff who claimed or closed it (name or ID)
    panel     exact panel name
    text      words inside the ticket's messages (scans messages, so it can be slow)
    in_msgs   old checkbox: also search `q` inside the messages
    rating    rated | unrated | low (1-2) | high (4-5) | 1..5
    priority  low | normal | high | urgent
    from/to   closed between two dates (YYYY-MM-DD, whole days, UTC)
    days      quick range (7 / 30 / 90); ignored when from/to is used
"""
import re
from datetime import datetime, timedelta, timezone

PRIORITIES = ("low", "normal", "high", "urgent")
RATING_WORDS = ("rated", "unrated", "low", "high")
QUICK_DAYS = (0, 7, 30, 90)
MAX_TEXT = 80


def _text(args, key) -> str:
    return str(args.get(key) or "").strip()[:MAX_TEXT]


def _rx(value: str) -> dict:
    return {"$regex": re.escape(value), "$options": "i"}


def _date(raw):
    try:
        return datetime.strptime(str(raw or "").strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def build(guild_id, args, denied_type_ids=(), now=None):
    """(mongo_query, cleaned_filters). `args` is any mapping (request.args).
    `denied_type_ids` are ticket types whose transcripts this person may not open
    (per-panel access rule), so they never show up in the list."""
    now = now or datetime.now(timezone.utc)
    f = {
        "q": _text(args, "q"), "member": _text(args, "member"), "staff": _text(args, "staff"),
        "panel": _text(args, "panel"), "text": _text(args, "text"),
        "in_msgs": args.get("in_msgs") == "1",
        "rating": str(args.get("rating") or ""),
        "priority": str(args.get("priority") or ""),
        "date_from": "", "date_to": "", "days": 0,
    }
    if f["rating"] not in RATING_WORDS + ("1", "2", "3", "4", "5"):
        f["rating"] = ""
    if f["priority"] not in PRIORITIES:
        f["priority"] = ""

    d_from, d_to = _date(args.get("from")), _date(args.get("to"))
    if d_from:
        f["date_from"] = d_from.strftime("%Y-%m-%d")
    if d_to:
        f["date_to"] = d_to.strftime("%Y-%m-%d")
    try:
        days = int(args.get("days", 0))
    except (TypeError, ValueError):
        days = 0
    f["days"] = days if days in QUICK_DAYS else 0

    ands = [{"guild_id": str(guild_id)}]

    if f["q"]:
        ors = [{k: _rx(f["q"])} for k in ("channel_name", "opener_name", "closer_name",
                                          "claimed_by_name", "reason", "panel_name")]
        if f["q"].isdigit():
            ors += [{k: f["q"]} for k in ("opener_id", "closed_by_id", "claimed_by_id", "channel_id")]
        if f["in_msgs"]:
            ors.append({"messages.content": _rx(f["q"])})
        ands.append({"$or": ors})

    if f["member"]:
        ors = [{"opener_name": _rx(f["member"])}]
        if f["member"].isdigit():
            ors.append({"opener_id": f["member"]})
        ands.append({"$or": ors})

    if f["staff"]:
        ors = [{k: _rx(f["staff"])} for k in ("claimed_by_name", "closer_name")]
        if f["staff"].isdigit():
            ors += [{k: f["staff"]} for k in ("claimed_by_id", "closed_by_id")]
        ands.append({"$or": ors})

    if f["panel"]:
        ands.append({"panel_name": f["panel"]})
    if f["text"]:
        ands.append({"messages.content": _rx(f["text"])})

    r = f["rating"]
    if r == "low":
        ands.append({"rating": {"$in": [1, 2]}})
    elif r == "high":
        ands.append({"rating": {"$in": [4, 5]}})
    elif r == "rated":
        ands.append({"rating": {"$ne": None, "$exists": True}})
    elif r == "unrated":
        ands.append({"rating": None})
    elif r:
        ands.append({"rating": int(r)})

    if f["priority"]:
        ands.append({"priority": f["priority"]})

    if d_from or d_to:
        span = {}
        if d_from:
            span["$gte"] = d_from
        if d_to:
            span["$lt"] = d_to + timedelta(days=1)       # the whole "to" day
        ands.append({"created_at": span})
    elif f["days"]:
        ands.append({"created_at": {"$gte": now - timedelta(days=f["days"])}})

    denied = [str(t) for t in denied_type_ids if t]
    if denied:
        ands.append({"ticket_type_id": {"$nin": denied}})

    return ({"$and": ands} if len(ands) > 1 else ands[0]), f
