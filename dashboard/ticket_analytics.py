"""Numbers for the Ticket Analytics page (no Flask: takes the `ticket_stats` collection).

The bot fills `ticket_stats` at ticket events (utils/ticket_stats.py): one document per
ticket, so nothing here reads transcripts. What each figure is based on:

    volume, busiest panels, first-reply time   tickets OPENED in the range
    average close time, rating, closed         tickets CLOSED in the range
    tickets claimed (per staff)                tickets CLAIMED in the range
"""
from datetime import datetime, timedelta, timezone
from statistics import median

QUICK_DAYS = (7, 30, 90, 365, 0)          # 0 = all time
KINDS = ("support", "application", "claim", "spawner")
LIMIT = 100_000                           # docs per query; the page says so if it's hit
MAX_DAY_BUCKETS = 92                      # longer ranges are grouped by month


def _naive(dt):
    """Mongo hands back naive UTC datetimes; make everything naive UTC to compare."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _date(raw):
    try:
        return datetime.strptime(str(raw or "").strip(), "%Y-%m-%d")
    except ValueError:
        return None


def parse_range(args, now=None):
    """(start, end, filters). `end` is exclusive. Default: the last 30 days (UTC)."""
    now = _naive(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    f = {"days": 30, "date_from": "", "date_to": "",
         "kind": args.get("kind") if args.get("kind") in KINDS else "",
         "panel": str(args.get("panel") or "").strip()[:100]}
    d_from, d_to = _date(args.get("from")), _date(args.get("to"))
    tomorrow = datetime(now.year, now.month, now.day) + timedelta(days=1)
    if d_from or d_to:
        f["days"] = None
        end = (d_to + timedelta(days=1)) if d_to else tomorrow        # "to" day is included
        start = d_from if d_from else end - timedelta(days=30)
        if start >= end:
            start = end - timedelta(days=1)
        f["date_from"], f["date_to"] = start.strftime("%Y-%m-%d"), (end - timedelta(days=1)).strftime("%Y-%m-%d")
        if not d_from:
            f["date_from"] = ""
        if not d_to:
            f["date_to"] = ""
        return start, end, f
    try:
        days = int(args.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    f["days"] = days if days in QUICK_DAYS else 30
    if f["days"] == 0:
        return None, tomorrow, f              # all time: compute() starts at the first ticket
    return tomorrow - timedelta(days=f["days"]), tomorrow, f


def duration_label(seconds) -> str:
    """90 -> 1m, 5400 -> 1h 30m, 200000 -> 2d 7h. None -> a dash."""
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m = seconds // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if h else f"{d}d"


def _avg(values):
    return sum(values) / len(values) if values else None


def _med(values):
    return median(values) if values else None


def _p90(values):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(len(v) * 0.9))]


def _buckets(start, end):
    """[(key, label)] covering the range: days up to ~3 months, months beyond."""
    out = []
    if (end - start).days <= MAX_DAY_BUCKETS:
        d = datetime(start.year, start.month, start.day)
        while d < end:
            out.append((d.strftime("%Y-%m-%d"), d.strftime("%b %d")))
            d += timedelta(days=1)
    else:
        y, m = start.year, start.month
        while datetime(y, m, 1) < end:
            out.append((f"{y:04d}-{m:02d}", datetime(y, m, 1).strftime("%b %Y")))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def compute(stats, guild_id, start, end, kind="", panel="") -> dict:
    base = {"guild_id": str(guild_id)}
    if kind:
        base["kind"] = kind
    if panel:
        base["panel_name"] = panel
    if start is None:                          # all time
        first = stats.find_one(base, sort=[("opened_at", 1)], projection={"opened_at": 1})
        start = _naive(first["opened_at"]).replace(hour=0, minute=0, second=0, microsecond=0) \
            if first else end - timedelta(days=30)

    def find(field, projection):
        q = dict(base, **{field: {"$gte": start, "$lt": end}})
        rows = list(stats.find(q, projection).limit(LIMIT))
        return rows, len(rows) >= LIMIT

    opened, t1 = find("opened_at", {"opened_at": 1, "closed_at": 1, "panel_name": 1,
                                    "first_reply_seconds": 1, "first_reply_by_id": 1,
                                    "first_reply_by_name": 1})
    closed, t2 = find("closed_at", {"closed_at": 1, "close_seconds": 1, "closed_by_id": 1,
                                    "closed_by_name": 1, "claimed_by_id": 1, "claimed_by_name": 1,
                                    "rating": 1, "rating_staff_id": 1, "panel_name": 1})
    claimed_q = dict(base, claimed_at={"$gte": start, "$lt": end}, claimed_by_id={"$ne": None})
    claimed = list(stats.find(claimed_q, {"claimed_by_id": 1, "claimed_by_name": 1}).limit(LIMIT))

    first_replies = [d["first_reply_seconds"] for d in opened if d.get("first_reply_seconds") is not None]
    close_times = [d["close_seconds"] for d in closed if d.get("close_seconds") is not None]
    ratings = [d["rating"] for d in closed if d.get("rating")]

    # ---- volume over time
    keyfmt = "%Y-%m-%d" if (end - start).days <= MAX_DAY_BUCKETS else "%Y-%m"
    vol = {k: {"label": lbl, "opened": 0, "closed": 0} for k, lbl in _buckets(start, end)}
    for d in opened:
        b = vol.get(_naive(d["opened_at"]).strftime(keyfmt))
        if b:
            b["opened"] += 1
    for d in closed:
        b = vol.get(_naive(d["closed_at"]).strftime(keyfmt))
        if b:
            b["closed"] += 1
    volume = list(vol.values())
    peak = max([b["opened"] for b in volume] + [b["closed"] for b in volume] + [1])
    for b in volume:
        b["opened_pct"] = round(b["opened"] * 100 / peak)
        b["closed_pct"] = round(b["closed"] * 100 / peak)

    # ---- busiest panels
    panels = {}
    for d in opened:
        p = panels.setdefault(d.get("panel_name") or "(no panel)", {"opened": 0, "closed": 0, "fr": [], "ct": []})
        p["opened"] += 1
        if d.get("first_reply_seconds") is not None:
            p["fr"].append(d["first_reply_seconds"])
    for d in closed:
        p = panels.setdefault(d.get("panel_name") or "(no panel)", {"opened": 0, "closed": 0, "fr": [], "ct": []})
        p["closed"] += 1
        if d.get("close_seconds") is not None:
            p["ct"].append(d["close_seconds"])
    panel_rows = sorted(
        ({"name": n, "opened": p["opened"], "closed": p["closed"],
          "avg_first_reply": _avg(p["fr"]), "avg_close": _avg(p["ct"])} for n, p in panels.items()),
        key=lambda r: (-r["opened"], -r["closed"], r["name"].lower()))[:10]
    top = max([r["opened"] for r in panel_rows] + [1])
    for r in panel_rows:
        r["pct"] = round(r["opened"] * 100 / top)

    # ---- staff workload
    staff = {}

    def person(sid, name):
        s = staff.setdefault(str(sid), {"id": str(sid), "name": None, "claimed": 0, "closed": 0,
                                        "replies": [], "ratings": []})
        if name:
            s["name"] = name
        return s

    for d in claimed:
        person(d["claimed_by_id"], d.get("claimed_by_name"))["claimed"] += 1
    for d in closed:
        if d.get("closed_by_id"):
            person(d["closed_by_id"], d.get("closed_by_name"))["closed"] += 1
        credited = d.get("rating_staff_id") or d.get("claimed_by_id")
        if d.get("rating") and credited:
            person(credited, d.get("claimed_by_name") if str(credited) == str(d.get("claimed_by_id")) else None)["ratings"].append(d["rating"])
    for d in opened:
        if d.get("first_reply_by_id") and d.get("first_reply_seconds") is not None:
            person(d["first_reply_by_id"], d.get("first_reply_by_name"))["replies"].append(d["first_reply_seconds"])
    staff_rows = sorted(
        ({"id": s["id"], "name": s["name"] or s["id"], "claimed": s["claimed"], "closed": s["closed"],
          "avg_rating": _avg(s["ratings"]), "rating_count": len(s["ratings"]),
          "avg_first_reply": _avg(s["replies"])} for s in staff.values()),
        key=lambda r: (-(r["closed"] + r["claimed"]), r["name"].lower()))[:25]

    return {
        "start": start, "end": end, "last_day": end - timedelta(days=1), "bucket": "day" if keyfmt == "%Y-%m-%d" else "month",
        "opened": len(opened), "closed": len(closed),
        "still_open": sum(1 for d in opened if not d.get("closed_at")),
        "first_reply": {"avg": _avg(first_replies), "median": _med(first_replies),
                        "p90": _p90(first_replies), "count": len(first_replies)},
        "close_time": {"avg": _avg(close_times), "median": _med(close_times), "count": len(close_times)},
        "rating": {"avg": _avg(ratings), "count": len(ratings)},
        "volume": volume, "panels": panel_rows, "staff": staff_rows,
        "truncated": bool(t1 or t2 or len(claimed) >= LIMIT),
    }


def import_history(db, guild_id, chunk=1000) -> int:
    """Create stats for tickets that were closed before analytics existed, from their
    transcripts. Safe to run again (existing tickets are never touched). Imported tickets
    have no first-reply time; their claim is counted at the time they were closed."""
    from pymongo import UpdateOne
    gid = str(guild_id)
    have = {d["_id"] for d in db.ticket_stats.find({"guild_id": gid}, {"_id": 1})}
    ops, added = [], 0
    for t in db.transcripts.find({"guild_id": gid, "channel_id": {"$exists": True}},
                                 {"channel_id": 1, "created_at": 1, "opener_id": 1, "closed_by_id": 1,
                                  "closer_name": 1, "claimed_by_id": 1, "claimed_by_name": 1,
                                  "panel_name": 1, "ticket_type_id": 1, "rating": 1, "priority": 1}):
        cid = str(t["channel_id"])
        if cid in have or not t.get("created_at"):
            continue
        have.add(cid)
        closed = _naive(t["created_at"])
        opened = datetime.fromtimestamp(((int(cid) >> 22) + 1420070400000) / 1000, tz=timezone.utc).replace(tzinfo=None)
        if opened > closed:
            opened = closed
        ops.append(UpdateOne({"_id": cid}, {"$setOnInsert": {
            "guild_id": gid, "ticket_type_id": t.get("ticket_type_id"), "panel_name": t.get("panel_name"),
            "kind": "support", "opener_id": str(t.get("opener_id")), "opened_at": opened,
            "first_reply_at": None, "first_reply_seconds": None, "first_reply_by_id": None,
            "first_reply_by_name": None,
            "claimed_by_id": t.get("claimed_by_id"), "claimed_by_name": t.get("claimed_by_name"),
            "claimed_at": closed if t.get("claimed_by_id") else None,
            "closed_at": closed, "close_seconds": int((closed - opened).total_seconds()),
            "closed_by_id": t.get("closed_by_id"), "closed_by_name": t.get("closer_name"),
            "priority": t.get("priority"), "rating": t.get("rating"),
            "rating_staff_id": t.get("claimed_by_id"), "imported": True,
        }}, upsert=True))
        if len(ops) >= chunk:
            db.ticket_stats.bulk_write(ops, ordered=False)
            added += len(ops)
            ops = []
    if ops:
        db.ticket_stats.bulk_write(ops, ordered=False)
        added += len(ops)
    return added
