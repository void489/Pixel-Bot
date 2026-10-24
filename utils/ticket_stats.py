"""Ticket analytics events (bot side).

Instead of working things out from transcripts later, one small document per ticket is
kept in `ticket_stats` (_id = the ticket channel's ID) and filled in as things happen:

    opened      create_ticket (every ticket kind)      opened_at, panel, kind, opener
    first reply first staff message in the ticket       first_reply_at / _seconds / _by_id
    claim       Claim button                            claimed_by_id / _name, claimed_at
    unclaim     Unclaim button / command                claimed_by_id cleared
    closed      close flow                              closed_at, close_seconds, closer, priority
    rated       the member's 1-5 rating                 rating, rating_staff_id

Every function here is non-fatal: a problem is printed and the ticket carries on. Tickets
opened before this existed get a document the first time claim / close touches them
(opened_at comes from the ticket record).

PENDING_REPLY is the in-memory list of open tickets still waiting for a staff reply, so the
message listener in cogs/tickets.py can ignore every other message without touching the DB.
"""
from datetime import datetime, timezone

from database.mongo import ticket_stats_col

# channel_id -> (guild_id, opener_id) for tickets that have no staff reply yet
PENDING_REPLY: dict = {}

# channel_id -> {"guild_id", "opener_id", "type_id"} for EVERY open ticket, so the keyword
# automation trigger can tell "is this a ticket?" with a dict lookup instead of a DB read
OPEN_TICKETS: dict = {}

_indexed = False


def _utc(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def _col():
    global _indexed
    col = ticket_stats_col()
    if not _indexed:
        _indexed = True
        try:
            await col.create_index([("guild_id", 1), ("opened_at", -1)])
            await col.create_index([("guild_id", 1), ("closed_at", -1)])
            await col.create_index([("guild_id", 1), ("claimed_at", -1)])
        except Exception as e:
            print(f"[ticket_stats] index creation failed: {e}")
    return col


def _baseline(ticket_doc: dict) -> dict:
    """A fresh stats document for a ticket (also used for tickets that predate analytics)."""
    return {
        "guild_id": str(ticket_doc.get("guild_id")),
        "ticket_type_id": ticket_doc.get("ticket_type_id"),
        "panel_name": ticket_doc.get("panel_name"),
        "kind": ticket_doc.get("kind") or "support",
        "opener_id": str(ticket_doc.get("opener_id")),
        "opener_name": ticket_doc.get("opener_name"),
        "opened_at": _utc(ticket_doc.get("created_at")) or datetime.now(timezone.utc),
        "first_reply_at": None, "first_reply_seconds": None, "first_reply_by_id": None,
        "first_reply_by_name": None,
        "claimed_by_id": None, "claimed_by_name": None, "claimed_at": None,
        "closed_at": None, "close_seconds": None, "closed_by_id": None, "closed_by_name": None,
        "priority": ticket_doc.get("priority"),
        "rating": None, "rating_staff_id": None,
    }


async def _upsert(ticket_doc: dict, fields: dict) -> None:
    """Set `fields`, creating the document first if this ticket has none yet."""
    base = {k: v for k, v in _baseline(ticket_doc).items() if k not in fields}
    await (await _col()).update_one(
        {"_id": str(ticket_doc["channel_id"])},
        {"$set": fields, "$setOnInsert": base}, upsert=True)


async def record_open(ticket_doc: dict) -> None:
    try:
        doc = _baseline(ticket_doc)
        cid = str(ticket_doc["channel_id"])
        await (await _col()).update_one({"_id": cid}, {"$setOnInsert": doc}, upsert=True)
        PENDING_REPLY[int(cid)] = (int(doc["guild_id"]), int(doc["opener_id"]))
        OPEN_TICKETS[int(cid)] = {"guild_id": doc["guild_id"], "opener_id": doc["opener_id"],
                                  "type_id": doc["ticket_type_id"]}
    except Exception as e:
        print(f"[ticket_stats] open failed: {e}")


async def load_open() -> None:
    """After a restart: every ticket that is still open."""
    try:
        from database.mongo import tickets_col
        async for d in tickets_col().find({"status": "open"},
                                          {"channel_id": 1, "guild_id": 1, "opener_id": 1, "ticket_type_id": 1}):
            OPEN_TICKETS[int(d["channel_id"])] = {"guild_id": str(d["guild_id"]), "opener_id": str(d["opener_id"]),
                                                  "type_id": d.get("ticket_type_id")}
    except Exception as e:
        print(f"[ticket_stats] couldn't load open tickets: {e}")


async def load_pending() -> None:
    """After a restart: tickets still waiting for their first staff reply."""
    try:
        cursor = (await _col()).find(
            {"closed_at": None, "first_reply_at": None},
            {"guild_id": 1, "opener_id": 1})
        async for d in cursor:
            PENDING_REPLY[int(d["_id"])] = (int(d["guild_id"]), int(d["opener_id"]))
    except Exception as e:
        print(f"[ticket_stats] couldn't load waiting tickets: {e}")


async def record_first_reply(channel_id: int, staff, at) -> None:
    """`staff` is the member who replied; `at` the message time."""
    PENDING_REPLY.pop(int(channel_id), None)
    try:
        col = await _col()
        doc = await col.find_one({"_id": str(channel_id)}, {"opened_at": 1, "first_reply_at": 1})
        if not doc or doc.get("first_reply_at"):
            return
        at = _utc(at) or datetime.now(timezone.utc)
        seconds = max(0, int((at - _utc(doc["opened_at"])).total_seconds()))
        await col.update_one(
            {"_id": str(channel_id), "first_reply_at": None},
            {"$set": {"first_reply_at": at, "first_reply_seconds": seconds,
                      "first_reply_by_id": str(staff.id),
                      "first_reply_by_name": getattr(staff, "display_name", str(staff.id))}})
    except Exception as e:
        print(f"[ticket_stats] first reply failed: {e}")


async def record_claim(ticket_doc: dict, staff) -> None:
    try:
        await _upsert(ticket_doc, {
            "claimed_by_id": str(staff.id),
            "claimed_by_name": getattr(staff, "display_name", str(staff.id)),
            "claimed_at": datetime.now(timezone.utc)})
    except Exception as e:
        print(f"[ticket_stats] claim failed: {e}")


async def record_unclaim(ticket_doc: dict) -> None:
    try:
        await _upsert(ticket_doc, {"claimed_by_id": None, "claimed_by_name": None, "claimed_at": None})
    except Exception as e:
        print(f"[ticket_stats] unclaim failed: {e}")


async def record_close(ticket_doc: dict, closer, claimer_name=None) -> None:
    """Call right after the ticket is closed. Fills in who claimed it from the ticket
    record, so tickets claimed before analytics existed are still credited."""
    PENDING_REPLY.pop(int(ticket_doc["channel_id"]), None)
    OPEN_TICKETS.pop(int(ticket_doc["channel_id"]), None)
    try:
        col = await _col()
        existing = await col.find_one({"_id": str(ticket_doc["channel_id"])}, {"opened_at": 1})
        opened = _utc(existing["opened_at"]) if existing else (
            _utc(ticket_doc.get("created_at")) or datetime.now(timezone.utc))
        now = datetime.now(timezone.utc)
        fields = {
            "closed_at": now, "close_seconds": max(0, int((now - opened).total_seconds())),
            "closed_by_id": str(closer.id), "closed_by_name": getattr(closer, "display_name", str(closer.id)),
            "priority": ticket_doc.get("priority"),
        }
        if ticket_doc.get("claimed_by"):
            fields["claimed_by_id"] = str(ticket_doc["claimed_by"])
            if claimer_name:
                fields["claimed_by_name"] = claimer_name
        await _upsert(ticket_doc, fields)
    except Exception as e:
        print(f"[ticket_stats] close failed: {e}")


async def record_rating(channel_id, rating: int, staff_id) -> None:
    """The member's rating. `channel_id` comes from the rating record (absent on ratings
    created before analytics existed, which are skipped)."""
    if not channel_id:
        return
    try:
        await (await _col()).update_one(
            {"_id": str(channel_id)},
            {"$set": {"rating": int(rating), "rating_staff_id": str(staff_id) if staff_id else None}})
    except Exception as e:
        print(f"[ticket_stats] rating failed: {e}")
