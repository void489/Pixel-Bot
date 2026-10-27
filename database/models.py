"""
Helper functions for reading/writing the collections used by the ticket system
and the command-permission framework.
"""
import re
from datetime import datetime, timezone, timedelta
from bson import ObjectId
from pymongo import ReturnDocument
from motor.motor_asyncio import AsyncIOMotorGridFSBucket

from database.mongo import get_db
from database.mongo import (
    guild_settings_col,
    command_permissions_col,
    tickets_col,
    ticket_counters_col,
    ticket_types_col,
    ticket_panels_col,
    transcripts_col,
    giveaways_col,
    polls_col,
    vouches_col,
    reports_col,
    scammers_col,
    applications_col,
    application_submissions_col,
    application_panels_col,
    application_sessions_col,
    dashboard_tasks_col,
    mod_cases_col,
    nuke_events_col,
    stickies_col,
    count_games_col,
    afk_col,
    blacklist_requests_col,
    honeypot_kicks_col,
    reaction_roles_col,
    temp_bans_col,
    invite_joins_col,
    automod_strikes_col,
    role_panels_col,
)
from database.automod_config import (  # noqa: F401  (re-exported: other code uses models.AUTOMOD_*)
    AUTOMOD_RULES, AUTOMOD_DEFAULTS, normalize_automod_config,
)

# ---------------------------------------------------------------------------
# GUILD SETTINGS
# ---------------------------------------------------------------------------

ANTI_NUKE_DEFAULT_ACTIONS = {
    "ban":                 {"enabled": True,  "count": 5, "window_seconds": 10},
    "kick":                {"enabled": True,  "count": 5, "window_seconds": 10},
    "channel_delete":      {"enabled": True,  "count": 3, "window_seconds": 10},
    "channel_create":      {"enabled": True,  "count": 3, "window_seconds": 10},
    "role_delete":         {"enabled": True,  "count": 3, "window_seconds": 10},
    "role_create":         {"enabled": False, "count": 5, "window_seconds": 10},
    "webhook_create":      {"enabled": True,  "count": 3, "window_seconds": 10},
    "emoji_delete":        {"enabled": False, "count": 5, "window_seconds": 10},
    "member_prune":        {"enabled": True,  "count": 1, "window_seconds": 10},
    "guild_update":        {"enabled": True,  "count": 2, "window_seconds": 10},
    "automod_rule_delete": {"enabled": True,  "count": 1, "window_seconds": 10},
    "bot_add":             {"enabled": True,  "count": 1, "window_seconds": 10},
    "bot_remove":          {"enabled": True,  "count": 1, "window_seconds": 10},
}


DEFAULT_GUILD_SETTINGS = {
    "prefix": "/",
    "tickets": {
        "category_id": None,
        "log_channel_id": None,
        "transcript_channel_id": None,
        "support_role_ids": [],
        "panel_channel_id": None,
    },
    "vouches": {
        "channel_ids": [],
        "report_channel_id": None,
        "scammer_role_id": None,
        "report_staff_role_id": None,
    },
    "applications": {
        "dm_timeout_minutes": 30,
    },
    "moderation": {
        "log_channel_id": None,
        "next_case_num": 1,
    },
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
        "actions": ANTI_NUKE_DEFAULT_ACTIONS,
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


async def get_guild_settings(guild_id: int) -> dict:
    doc = await guild_settings_col().find_one({"_id": str(guild_id)})
    if not doc:
        doc = {"_id": str(guild_id), **DEFAULT_GUILD_SETTINGS}
        await guild_settings_col().insert_one(doc)
    return doc


async def update_guild_settings(guild_id: int, update: dict) -> None:
    await guild_settings_col().update_one(
        {"_id": str(guild_id)}, {"$set": update}, upsert=True
    )


async def get_prefix(guild_id: int) -> str:
    settings = await get_guild_settings(guild_id)
    return settings.get("prefix", "/")


async def set_prefix(guild_id: int, prefix: str) -> None:
    await update_guild_settings(guild_id, {"prefix": prefix})


async def set_ticket_config(guild_id: int, **kwargs) -> None:
    update = {f"tickets.{k}": v for k, v in kwargs.items() if v is not None}
    if update:
        await update_guild_settings(guild_id, update)


# ---------------------------------------------------------------------------
# COMMAND PERMISSIONS
# ---------------------------------------------------------------------------

# new command name -> old command name whose permission it inherits once
_COMMAND_RENAMES = {"embeds create": "embeds"}


async def register_command(guild_id: int, command_name: str, cog_name: str) -> None:
    key = f"{guild_id}:{command_name}"
    existing = await command_permissions_col().find_one({"_id": key})
    if existing:
        return
    # /embeds became a group (embeds create / embeds edit): carry the old
    # "embeds" permission over so nothing has to be reconfigured.
    if command_name in _COMMAND_RENAMES:
        old = await command_permissions_col().find_one(
            {"_id": f"{guild_id}:{_COMMAND_RENAMES[command_name]}"}
        )
        if old:
            await command_permissions_col().insert_one({
                "_id": key,
                "guild_id": str(guild_id),
                "command": command_name,
                "cog": cog_name,
                "enabled": old.get("enabled", False),
                "allowed_roles": list(old.get("allowed_roles", [])),
                "admin_only": old.get("admin_only", True),
            })
            return
    await command_permissions_col().insert_one({
        "_id": key,
        "guild_id": str(guild_id),
        "command": command_name,
        "cog": cog_name,
        "enabled": False,
        "allowed_roles": [],
        "admin_only": True,
    })


async def get_command_permission(guild_id: int, command_name: str) -> dict:
    key = f"{guild_id}:{command_name}"
    doc = await command_permissions_col().find_one({"_id": key})
    if not doc:
        return {"enabled": False, "allowed_roles": [], "admin_only": True}
    return doc


async def get_all_command_permissions(guild_id: int) -> list:
    cursor = command_permissions_col().find({"guild_id": str(guild_id)})
    return [doc async for doc in cursor]


async def set_command_permission(guild_id: int, command_name: str, *, enabled: bool = None,
                                  allowed_roles: list = None) -> None:
    key = f"{guild_id}:{command_name}"
    update = {}
    if enabled is not None:
        update["enabled"] = enabled
        update["admin_only"] = False
    if allowed_roles is not None:
        update["allowed_roles"] = allowed_roles
    if update:
        await command_permissions_col().update_one({"_id": key}, {"$set": update}, upsert=True)


# ---------------------------------------------------------------------------
# TICKETS
# ---------------------------------------------------------------------------

async def create_ticket(guild_id: int, channel_id: int, opener_id: int,
                        panel_name: str = None, ticket_type_id: str = None,
                        kind: str = "support", extra: dict = None) -> None:
    doc = {
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "opener_id": str(opener_id),
        "panel_name": panel_name,
        "ticket_type_id": ticket_type_id,
        "kind": kind,
        "welcome_message_id": None,
        "status": "open",
        "created_at": datetime.now(timezone.utc),
        "closed_at": None,
    }
    if extra:
        doc.update(extra)
    await tickets_col().insert_one(doc)
    try:                                  # analytics (non-fatal): see utils/ticket_stats.py
        from utils import ticket_stats
        await ticket_stats.record_open(doc)
    except Exception as e:
        print(f"[tickets] analytics open failed: {e}")


async def get_ticket(channel_id: int) -> dict:
    return await tickets_col().find_one({"channel_id": str(channel_id), "status": "open"})


async def get_open_ticket_for_user(guild_id: int, user_id: int) -> dict:
    return await tickets_col().find_one({
        "guild_id": str(guild_id),
        "opener_id": str(user_id),
        "status": "open",
        "kind": {"$nin": ["claim", "spawner"]},
    })


async def next_ticket_number(guild_id: int) -> int:
    """Next ticket number for this server (1, 2, 3...). Atomic, so two tickets opened at
    the same moment never get the same number. Used for the {number} placeholder."""
    doc = await ticket_counters_col().find_one_and_update(
        {"_id": str(guild_id)}, {"$inc": {"seq": 1}},
        upsert=True, return_document=ReturnDocument.AFTER,
    )
    return int(doc["seq"])


async def get_open_tickets_for_user_type(guild_id: int, user_id: int, type_id: str) -> list:
    """The member's open tickets in ONE ticket type (for the per-panel limit)."""
    cursor = tickets_col().find({
        "guild_id": str(guild_id),
        "opener_id": str(user_id),
        "ticket_type_id": str(type_id),
        "status": "open",
    })
    return [doc async for doc in cursor]


async def update_ticket(channel_id: int, **fields) -> None:
    await tickets_col().update_one(
        {"channel_id": str(channel_id), "status": "open"},
        {"$set": fields},
    )


async def close_ticket(channel_id: int) -> None:
    await tickets_col().update_one(
        {"channel_id": str(channel_id), "status": "open"},
        {"$set": {"status": "closed", "closed_at": datetime.now(timezone.utc)}},
    )


async def save_transcript(guild_id: int, channel_id: int, channel_name: str,
                           opener_id: int, closed_by_id: int, html_content: str) -> str:
    doc = {
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "channel_name": channel_name,
        "opener_id": str(opener_id),
        "closed_by_id": str(closed_by_id),
        "created_at": datetime.now(timezone.utc),
        "html": html_content,
    }
    result = await transcripts_col().insert_one(doc)
    return str(result.inserted_id)


def new_transcript_id() -> ObjectId:
    """Reserve a transcript ID up-front so images can be filed under it."""
    return ObjectId()


TRANSCRIPT_FILES_BUCKET = "transcript_files"


async def save_transcript_file(transcript_id: str, guild_id: int, filename: str,
                               content_type: str, data: bytes) -> str:
    """Store one attachment (image) in GridFS; returns the file ID as a string."""
    bucket = AsyncIOMotorGridFSBucket(get_db(), bucket_name=TRANSCRIPT_FILES_BUCKET)
    file_id = await bucket.upload_from_stream(
        filename, data,
        metadata={
            "guild_id": str(guild_id),
            "transcript_id": str(transcript_id),
            "content_type": content_type,
        },
    )
    return str(file_id)


async def save_structured_transcript(transcript_id, guild_id: int, channel_id: int,
                                     channel_name: str, opener_id: int, closed_by_id: int,
                                     reason: str, opener_name: str, closer_name: str,
                                     messages: list, trimmed: int = 0,
                                     claimed_by_id=None, claimed_by_name=None,
                                     panel_name=None, priority=None, ticket_type_id=None) -> str:
    """New-style transcript: structured messages rendered by the dashboard."""
    doc = {
        "_id": ObjectId(str(transcript_id)),
        "version": 2,
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "channel_name": channel_name,
        "opener_id": str(opener_id),
        "opener_name": opener_name,
        "closed_by_id": str(closed_by_id),
        "closer_name": closer_name,
        "reason": reason,
        "created_at": datetime.now(timezone.utc),
        "messages": messages,
        "message_count": len(messages),
        "trimmed": int(trimmed),
        # for the dashboard's Transcripts page / /ticket lookup
        "claimed_by_id": str(claimed_by_id) if claimed_by_id else None,
        "claimed_by_name": claimed_by_name,
        "panel_name": panel_name,
        "priority": priority,    # low / normal / high / urgent, or None if never set
        "ticket_type_id": ticket_type_id,   # decides who may open it (transcript_access)
        "rating": None,          # filled in if the opener rates the ticket
        "rating_comment": None,
    }
    result = await transcripts_col().insert_one(doc)
    return str(result.inserted_id)


async def get_transcripts(guild_id: int) -> list:
    cursor = transcripts_col().find({"guild_id": str(guild_id)}).sort("created_at", -1)
    return [doc async for doc in cursor]


async def get_transcript_by_id(transcript_id):
    return await transcripts_col().find_one({"_id": ObjectId(transcript_id)})


# ---------------------------------------------------------------------------
# TICKET TYPES
# ---------------------------------------------------------------------------

async def create_ticket_type(guild_id: int, **fields) -> str:
    fields["guild_id"] = str(guild_id)
    fields["created_at"] = datetime.now(timezone.utc)
    result = await ticket_types_col().insert_one(fields)
    return str(result.inserted_id)


async def get_ticket_type(type_id: str) -> dict:
    try:
        oid = ObjectId(type_id)
    except Exception:
        return None
    return await ticket_types_col().find_one({"_id": oid})


async def get_ticket_types(guild_id: int) -> list:
    cursor = ticket_types_col().find({"guild_id": str(guild_id)}).sort("created_at", 1)
    return [doc async for doc in cursor]


async def update_ticket_type(type_id: str, update: dict) -> None:
    await ticket_types_col().update_one({"_id": ObjectId(type_id)}, {"$set": update})


async def delete_ticket_type(type_id: str) -> None:
    await ticket_types_col().delete_one({"_id": ObjectId(type_id)})


# ---------------------------------------------------------------------------
# TICKET PANELS
# ---------------------------------------------------------------------------

async def save_ticket_panel(guild_id: int, name: str, message_id: int, channel_id: int,
                            ticket_type_ids: list, title: str = None,
                            description: str = None, color: int = None,
                            color_hex: str = None, display_style: str = None,
                            placeholder: str = None) -> None:
    extra = {}
    if display_style is not None:
        extra["display_style"] = display_style
    if placeholder is not None:
        extra["placeholder"] = placeholder
    await ticket_panels_col().update_one(
        {"guild_id": str(guild_id), "name": name},
        {"$set": {
            **extra,
            "guild_id": str(guild_id),
            "name": name,
            "message_id": str(message_id),
            "channel_id": str(channel_id),
            "ticket_type_ids": ticket_type_ids,
            "title": title,
            "description": description,
            "color": color,
            "color_hex": color_hex,
        }},
        upsert=True,
    )


async def get_ticket_panel(guild_id: int, name: str) -> dict:
    return await ticket_panels_col().find_one({"guild_id": str(guild_id), "name": name})


async def get_ticket_panels(guild_id: int) -> list:
    cursor = ticket_panels_col().find({"guild_id": str(guild_id)})
    return [doc async for doc in cursor]


async def get_ticket_panel_by_message(message_id: int) -> dict:
    return await ticket_panels_col().find_one({"message_id": str(message_id)})


# ---------------------------------------------------------------------------
# CLAIM TICKET CONFIG
# ---------------------------------------------------------------------------

async def get_claim_ticket_config(guild_id: int) -> dict:
    settings = await get_guild_settings(guild_id)
    cfg = settings.get("claim_tickets") or {}
    return {
        "category_id": cfg.get("category_id"),
        "overflow_category_ids": [str(x) for x in (cfg.get("overflow_category_ids") or [])],
        "ping_role_ids": cfg.get("ping_role_ids") or [],
        "allow_role_ids": cfg.get("allow_role_ids") or [],
        "questions": (cfg.get("questions") or ["What is your Minecraft IGN?"])[:5],
        # "roles" = ping opener + ping roles (default), "host" = ping opener + giveaway host
        "ping_mode": cfg.get("ping_mode") if cfg.get("ping_mode") in ("roles", "host") else "roles",
        "rps_timer": _clamp_seconds(cfg.get("rps_timer")),
        "splitsteal_timer": _clamp_seconds(cfg.get("splitsteal_timer")),
    }


def _clamp_seconds(value, default: int = 60) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(10, min(n, 600))


# ---------------------------------------------------------------------------
# SPAWNERS (buy/sell panel + spawner tickets)
#
# guild_settings.spawners = {
#     category_id, allow_role_ids, ping_role_ids, notes,   <- dashboard
#     types:  { <key>: {name, emoji, sell_price, buy_price} },   <- /spawner add, setprice
#     panels: [ {channel_id, message_id}, ... ],                 <- /spawner panel
# }
# Prices are from the CUSTOMER's point of view:
#   sell_price = what the customer RECEIVES when they sell to the server
#   buy_price  = what the customer PAYS when they buy from the server
# Spawner tickets live in the normal `tickets` collection with kind="spawner".
# ---------------------------------------------------------------------------

MAX_SPAWNER_TYPES = 25      # a Discord select menu holds 25 options


def spawner_key(name: str) -> str:
    """Mongo-safe slug used as the field name for a spawner type."""
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")[:32]


def sort_spawner_types(types: dict) -> dict:
    """Types in panel order: by their `order` number (set from the dashboard or
    /spawner move); types without one keep their saved order, after the numbered ones."""
    items = list((types or {}).items())
    items.sort(key=lambda kv: (kv[1].get("order") if isinstance(kv[1].get("order"), int) else 10**9))
    return dict(items)  # sort is stable, so unnumbered types keep their old order


async def reorder_spawner_types(guild_id: int, keys: list) -> None:
    """Save a new panel order: keys[0] is shown first."""
    await update_guild_settings(guild_id, {
        f"spawners.types.{k}.order": i for i, k in enumerate(keys)})


async def get_spawner_config(guild_id: int) -> dict:
    settings = await get_guild_settings(guild_id)
    cfg = settings.get("spawners") or {}
    return {
        "category_id": cfg.get("category_id"),
        "overflow_category_ids": [str(x) for x in (cfg.get("overflow_category_ids") or [])],
        "ping_role_ids": cfg.get("ping_role_ids") or [],
        "allow_role_ids": cfg.get("allow_role_ids") or [],
        "notes": cfg.get("notes") or "",
        "types": sort_spawner_types(cfg.get("types") or {}),
        "panels": cfg.get("panels") or [],
    }


async def add_spawner_type(guild_id: int, key: str, name: str, emoji: str) -> bool:
    """Create a spawner type, or rename / re-emoji an existing one (prices are kept).
    Returns True if it was newly created."""
    cfg = await get_spawner_config(guild_id)
    existed = key in cfg["types"]
    update = {
        f"spawners.types.{key}.name": name,
        f"spawners.types.{key}.emoji": emoji,
    }
    if not existed:  # new types go to the bottom of the panel
        update[f"spawners.types.{key}.order"] = len(cfg["types"])
    await update_guild_settings(guild_id, update)
    return not existed


async def remove_spawner_type(guild_id: int, key: str) -> bool:
    res = await guild_settings_col().update_one(
        {"_id": str(guild_id), f"spawners.types.{key}": {"$exists": True}},
        {"$unset": {f"spawners.types.{key}": ""}},
    )
    return res.modified_count > 0


async def set_spawner_price(guild_id: int, key: str, side: str, price) -> tuple:
    """Set (or with price=None, clear) one side's price. Returns (found, old_price).
    The old value comes from the same atomic update, so two staff changing the
    price at once can't both think they replaced the same old value."""
    field = f"spawners.types.{key}.{side}_price"
    update = {"$unset": {field: ""}} if price is None else {"$set": {field: int(price)}}
    doc = await guild_settings_col().find_one_and_update(
        {"_id": str(guild_id), f"spawners.types.{key}": {"$exists": True}},
        update,
        return_document=ReturnDocument.BEFORE,
        projection={f"spawners.types.{key}": 1},
    )
    if doc is None:
        return False, None
    old = (((doc.get("spawners") or {}).get("types") or {}).get(key) or {}).get(f"{side}_price")
    return True, (int(old) if old else None)


async def add_spawner_panel(guild_id: int, channel_id: int, message_id: int) -> None:
    await get_guild_settings(guild_id)  # make sure the settings doc exists with defaults
    await guild_settings_col().update_one(
        {"_id": str(guild_id)},
        {"$push": {"spawners.panels": {
            "$each": [{"channel_id": str(channel_id), "message_id": str(message_id)}],
            "$slice": -10,
        }}},
    )


async def remove_spawner_panel(guild_id: int, message_id) -> None:
    await guild_settings_col().update_one(
        {"_id": str(guild_id)},
        {"$pull": {"spawners.panels": {"message_id": str(message_id)}}},
    )


async def get_open_spawner_tickets(guild_id: int, key: str = None,
                                   side: str = None, opener_id: int = None) -> list:
    query = {"guild_id": str(guild_id), "kind": "spawner", "status": "open"}
    if key:
        query["spawner_key"] = key
    if side:
        query["side"] = side
    if opener_id is not None:
        query["opener_id"] = str(opener_id)
    return [doc async for doc in tickets_col().find(query)]


async def reprice_spawner_ticket(channel_id: int, old_unit: int, new_unit: int,
                                 new_total: int) -> bool:
    """Compare-and-set: only changes the ticket if it still has `old_unit`, so when two
    price changes race, exactly one of them gets to recalculate and announce.
    The total is computed by the database from the ticket's CURRENT amount, so a
    simultaneous "Change Amount" can't leave a stale total behind (`new_total` is only
    what the caller shows)."""
    res = await tickets_col().update_one(
        {"channel_id": str(channel_id), "status": "open", "unit_price": int(old_unit)},
        [{"$set": {"unit_price": int(new_unit),
                   "total": {"$multiply": ["$amount", int(new_unit)]}}}],
    )
    return res.modified_count > 0


async def set_spawner_ticket_amount(channel_id: int, old_amount: int, unit_price: int,
                                    new_amount: int, new_total: int) -> bool:
    """Compare-and-set for the Change Amount button: only applies if the ticket is still
    open with the amount and unit price the caller calculated from."""
    res = await tickets_col().update_one(
        {"channel_id": str(channel_id), "status": "open", "kind": "spawner",
         "amount": int(old_amount), "unit_price": int(unit_price)},
        {"$set": {"amount": int(new_amount), "total": int(new_total)}},
    )
    return res.modified_count > 0


# ---------------------------------------------------------------------------
# GIVEAWAYS
# ---------------------------------------------------------------------------

async def create_giveaway(guild_id: int, channel_id: int, message_id: int, host_id: int,
                          prize: str, winners_count: int, end_time: datetime,
                          claim_time_seconds: int, display_id: str, game: str = None,
                          kind: str = None, extra: dict = None) -> None:
    doc = {
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "message_id": str(message_id),
        "host_id": str(host_id),
        "prize": prize,
        "winners_count": winners_count,
        "end_time": end_time,
        "claim_time_seconds": claim_time_seconds,
        "display_id": display_id,
        "entries": [],
        "ended": False,
        "winners": [],
        "announcement_message_id": None,
        "claim_end_time": None,
        "claim_expired": False,
        "claimed_users": [],
        "claim_channels": {},
        "game": game,                 # None | "rps" | "splitsteal"
        "game_active": False,
        # None | "membercount" | "fastestfinger" | "doubleornothing" | "guessflag" | "guesslanguage"
        "kind": kind,
        "created_at": datetime.now(timezone.utc),
    }
    if extra:
        doc.update(extra)
    await giveaways_col().insert_one(doc)


async def get_giveaway(message_id) -> dict:
    return await giveaways_col().find_one({"message_id": str(message_id)})


async def get_guild_giveaways(guild_id: int) -> list:
    cursor = giveaways_col().find({"guild_id": str(guild_id)}).sort("created_at", -1)
    return [doc async for doc in cursor]


async def update_giveaway(message_id, **fields) -> None:
    await giveaways_col().update_one({"message_id": str(message_id)}, {"$set": fields})


async def delete_giveaway(message_id) -> None:
    await giveaways_col().delete_one({"message_id": str(message_id)})


async def add_giveaway_entry(message_id, user_id: int) -> bool:
    result = await giveaways_col().update_one(
        {"message_id": str(message_id), "ended": False},
        {"$addToSet": {"entries": str(user_id)}},
    )
    return result.modified_count == 1


async def mark_giveaway_ended(message_id) -> dict:
    return await giveaways_col().find_one_and_update(
        {"message_id": str(message_id), "ended": False},
        {"$set": {"ended": True}},
    )


async def get_due_giveaways(now: datetime) -> list:
    cursor = giveaways_col().find({"ended": False, "end_time": {"$lte": now}})
    return [doc async for doc in cursor]


async def get_expired_claims(now: datetime) -> list:
    cursor = giveaways_col().find({
        "ended": True,
        "claim_expired": False,
        "claim_end_time": {"$ne": None, "$lte": now},
    })
    return [doc async for doc in cursor]


async def reserve_giveaway_claim(message_id, user_id: int) -> bool:
    doc = await giveaways_col().find_one_and_update(
        {"message_id": str(message_id), "claimed_users": {"$ne": str(user_id)}},
        {"$addToSet": {"claimed_users": str(user_id)}},
    )
    return doc is not None


async def release_giveaway_claim(message_id, user_id: int) -> None:
    await giveaways_col().update_one(
        {"message_id": str(message_id)}, {"$pull": {"claimed_users": str(user_id)}}
    )


async def set_giveaway_claim_channel(message_id, user_id: int, channel_id: int) -> None:
    await giveaways_col().update_one(
        {"message_id": str(message_id)},
        {"$set": {f"claim_channels.{user_id}": str(channel_id)}},
    )


# ---------------------------------------------------------------------------
# GIVEAWAY GAMES (Rock Paper Scissors / Split or Steal)
#
# The game lives on the giveaway document: game_players, game_round,
# game_choices {user_id: choice}, game_deadline, game_active, game_message_id,
# game_log. Every state change is a compare-and-set so button spam, the timeout
# loop and a restart can never resolve the same round twice.
# ---------------------------------------------------------------------------

async def game_record_choice(message_id, user_id, rnd: int, choice: str) -> dict:
    """Store a pick. Returns the updated doc, or None if the game is over, the
    round moved on, or this player already picked."""
    return await giveaways_col().find_one_and_update(
        {
            "message_id": str(message_id),
            "game_active": True,
            "game_round": int(rnd),
            f"game_choices.{user_id}": {"$exists": False},
        },
        {"$set": {f"game_choices.{user_id}": choice}},
        return_document=ReturnDocument.AFTER,
    )


async def game_close(message_id, rnd: int) -> dict:
    """Atomically end the round's game. Returns the doc BEFORE the change, or None
    if someone else already closed it."""
    return await giveaways_col().find_one_and_update(
        {"message_id": str(message_id), "game_active": True, "game_round": int(rnd)},
        {"$set": {"game_active": False}},
    )


async def game_next_round(message_id, rnd: int, deadline: datetime, log_line: str) -> dict:
    """Draw: start the next round. Returns the doc BEFORE the change, or None if
    the round was already handled."""
    return await giveaways_col().find_one_and_update(
        {"message_id": str(message_id), "game_active": True, "game_round": int(rnd)},
        {
            "$set": {"game_round": int(rnd) + 1, "game_choices": {}, "game_deadline": deadline},
            "$push": {"game_log": log_line},
        },
    )


async def get_due_games(now: datetime) -> list:
    cursor = giveaways_col().find({"game_active": True, "game_deadline": {"$lte": now}})
    return [doc async for doc in cursor]


# ---------------------------------------------------------------------------
# GIVEAWAY KINDS: member count / fastest finger / double or nothing / guess flag
#
# All of these live on the giveaway document too (kind + a few fields) and every
# state change is a compare-and-set, same as the games above.
# ---------------------------------------------------------------------------

async def get_active_membercount(guild_id: int = None) -> list:
    query = {"kind": "membercount", "ended": False}
    if guild_id is not None:
        query["guild_id"] = str(guild_id)
    return [doc async for doc in giveaways_col().find(query)]


async def get_active_flag_game(guild_id: int = None) -> list:
    query = {"kind": "guessflag", "ended": False}
    if guild_id is not None:
        query["guild_id"] = str(guild_id)
    return [doc async for doc in giveaways_col().find(query)]


async def flag_win(message_id, user_id, now: datetime) -> dict:
    """First correct guess wins. Returns the doc BEFORE the change, or None if the
    flag game was already won / ended."""
    return await giveaways_col().find_one_and_update(
        {"message_id": str(message_id), "kind": "guessflag", "ended": False},
        {"$set": {"ended": True, "winners": [str(user_id)], "end_time": now}},
    )


async def get_active_language_game(guild_id: int = None) -> list:
    query = {"kind": "guesslanguage", "ended": False}
    if guild_id is not None:
        query["guild_id"] = str(guild_id)
    return [doc async for doc in giveaways_col().find(query)]


async def language_win(message_id, user_id, now: datetime) -> dict:
    """First correct answer wins. Returns the doc BEFORE the change, or None if the
    language game was already won / ended."""
    return await giveaways_col().find_one_and_update(
        {"message_id": str(message_id), "kind": "guesslanguage", "ended": False},
        {"$set": {"ended": True, "winners": [str(user_id)], "end_time": now}},
    )


# --- fastest finger ---------------------------------------------------------

async def ff_set_state(message_id, expect, new_state: str, **fields) -> dict:
    """Compare-and-set the fastest-finger state. `expect` is a state name or a list
    of them. Returns the doc BEFORE the change, or None if the state didn't match."""
    expect = expect if isinstance(expect, list) else [expect]
    return await giveaways_col().find_one_and_update(
        {"message_id": str(message_id), "ff_state": {"$in": expect}},
        {"$set": {"ff_state": new_state, **fields}},
    )


async def get_active_ff_live() -> list:
    return [doc async for doc in giveaways_col().find({"ff_state": "live"})]


async def get_due_fastest(now: datetime) -> list:
    cursor = giveaways_col().find({"$or": [
        {"ff_state": "countdown", "ff_arm_time": {"$lte": now}},
        {"ff_state": "live", "ff_deadline": {"$lte": now}},
    ]})
    return [doc async for doc in cursor]


# --- double or nothing ------------------------------------------------------

async def don_advance(message_id, step: int, mult: int, deadline: datetime) -> dict:
    """Successful double. Returns the doc BEFORE the change, or None if the panel
    already moved on (button spam / timeout)."""
    return await giveaways_col().find_one_and_update(
        {"message_id": str(message_id), "don_active": True, "don_step": int(step)},
        {"$set": {"don_step": int(step) + 1, "don_mult": int(mult), "don_deadline": deadline}},
    )


async def don_finish(message_id, step: int, **fields) -> dict:
    """Close the panel (take or bust). Returns the doc BEFORE the change, or None
    if it was already closed."""
    return await giveaways_col().find_one_and_update(
        {"message_id": str(message_id), "don_active": True, "don_step": int(step)},
        {"$set": {"don_active": False, **fields}},
    )


async def get_due_don(now: datetime) -> list:
    cursor = giveaways_col().find({"don_active": True, "don_deadline": {"$lte": now}})
    return [doc async for doc in cursor]


# ---------------------------------------------------------------------------
# POLLS (button polls)
# ---------------------------------------------------------------------------

async def create_poll(guild_id: int, channel_id: int, message_id: int, author_id: int,
                      question: str, options: list, author_name: str = "") -> None:
    await polls_col().insert_one({
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "message_id": str(message_id),
        "author_id": str(author_id),
        "author_name": author_name,
        "question": question,
        "options": list(options),
        "votes": {},               # {user_id: option index}
        "ended": False,
        "created_at": datetime.now(timezone.utc),
    })


async def get_poll(message_id) -> dict:
    return await polls_col().find_one({"message_id": str(message_id)})


async def get_guild_polls(guild_id: int, active_only: bool = False) -> list:
    query = {"guild_id": str(guild_id)}
    if active_only:
        query["ended"] = False
    cursor = polls_col().find(query).sort("created_at", -1)
    return [doc async for doc in cursor]


async def poll_set_vote(message_id, user_id: int, option_index: int) -> dict:
    """Cast or change a vote on a running poll. Returns the updated doc, or None
    if the poll doesn't exist / has ended."""
    return await polls_col().find_one_and_update(
        {"message_id": str(message_id), "ended": False},
        {"$set": {f"votes.{user_id}": int(option_index)}},
        return_document=ReturnDocument.AFTER,
    )


async def end_poll(message_id) -> dict:
    """Mark ended. Returns the doc as it is now, or None if missing / already ended."""
    return await polls_col().find_one_and_update(
        {"message_id": str(message_id), "ended": False},
        {"$set": {"ended": True, "ended_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )


# ---------------------------------------------------------------------------
# VOUCHES
# ---------------------------------------------------------------------------

async def get_vouch_config(guild_id: int) -> dict:
    settings = await get_guild_settings(guild_id)
    cfg = settings.get("vouches") or {}
    return {
        "channel_ids": [str(c) for c in (cfg.get("channel_ids") or [])],
        "report_channel_id": (
            str(cfg["report_channel_id"]) if cfg.get("report_channel_id") else None
        ),
        "scammer_role_id": (
            str(cfg["scammer_role_id"]) if cfg.get("scammer_role_id") else None
        ),
        "report_staff_role_id": (
            str(cfg["report_staff_role_id"]) if cfg.get("report_staff_role_id") else None
        ),
    }


async def set_vouch_config(guild_id: int, **kwargs) -> None:
    update = {f"vouches.{k}": v for k, v in kwargs.items() if v is not None}
    if update:
        await update_guild_settings(guild_id, update)


async def add_vouch_channel(guild_id: int, channel_id: int) -> bool:
    result = await guild_settings_col().update_one(
        {"_id": str(guild_id)},
        {"$addToSet": {"vouches.channel_ids": str(channel_id)}},
        upsert=True,
    )
    return result.modified_count == 1


async def remove_vouch_channel(guild_id: int, channel_id: int) -> bool:
    result = await guild_settings_col().update_one(
        {"_id": str(guild_id)},
        {"$pull": {"vouches.channel_ids": str(channel_id)}},
    )
    return result.modified_count == 1


async def _next_vouch_num_for_target(guild_id: int, target_id: int) -> int:
    count = await vouches_col().count_documents({
        "guild_id": str(guild_id),
        "target_id": str(target_id),
    })
    return count + 1


async def create_vouch(guild_id: int, target_id: int, author_id: int,
                       vouch_type: str, reason: str, channel_id: int = None,
                       message_id: int = None) -> dict:
    vouch_num = await _next_vouch_num_for_target(guild_id, target_id)
    doc = {
        "guild_id": str(guild_id),
        "vouch_num": vouch_num,
        "target_id": str(target_id),
        "author_id": str(author_id),
        "type": vouch_type,
        "reason": reason or "",
        "channel_id": str(channel_id) if channel_id else None,
        "message_id": str(message_id) if message_id else None,
        "created_at": datetime.now(timezone.utc),
    }
    result = await vouches_col().insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def message_already_vouched(guild_id: int, message_id: int) -> bool:
    if message_id is None:
        return False
    doc = await vouches_col().find_one({
        "guild_id": str(guild_id),
        "message_id": str(message_id),
    })
    return doc is not None


async def count_vouches(guild_id: int, target_id: int, vouch_type: str) -> int:
    return await vouches_col().count_documents({
        "guild_id": str(guild_id),
        "target_id": str(target_id),
        "type": vouch_type,
    })


async def get_recent_vouches(guild_id: int, target_id: int, limit: int = 5) -> list:
    cursor = (
        vouches_col()
        .find({"guild_id": str(guild_id), "target_id": str(target_id)})
        .sort("created_at", -1)
        .limit(limit)
    )
    return [doc async for doc in cursor]


async def get_target_vouches(guild_id: int, target_id: int, limit: int = 25) -> list:
    cursor = (
        vouches_col()
        .find({"guild_id": str(guild_id), "target_id": str(target_id)})
        .sort("vouch_num", 1)
        .limit(limit)
    )
    return [doc async for doc in cursor]


async def get_vouch_by_num(guild_id: int, target_id: int, vouch_num: int) -> dict:
    return await vouches_col().find_one({
        "guild_id": str(guild_id),
        "target_id": str(target_id),
        "vouch_num": int(vouch_num),
    })


async def delete_vouch_by_num(guild_id: int, target_id: int, vouch_num: int) -> dict:
    return await vouches_col().find_one_and_delete(
        {
            "guild_id": str(guild_id),
            "target_id": str(target_id),
            "vouch_num": int(vouch_num),
        },
        sort=[("created_at", -1)],
    )


async def get_guild_vouch_channel_ids(guild_id: int) -> set:
    cfg = await get_vouch_config(guild_id)
    return set(cfg["channel_ids"])


# ---------------------------------------------------------------------------
# REPORTS
# ---------------------------------------------------------------------------

async def create_report(guild_id: int, reported_id: int, reporter_id: int,
                        reason: str, photo_urls: list, channel_id: int,
                        message_id: int) -> str:
    report_id = f"R{message_id}"
    doc = {
        "_id": report_id,
        "guild_id": str(guild_id),
        "reported_id": str(reported_id),
        "reporter_id": str(reporter_id),
        "reason": reason,
        "photo_urls": list(photo_urls),
        "channel_id": str(channel_id),
        "message_id": str(message_id),
        "status": "Pending",
        "staff_id": None,
        "decided_at": None,
        "created_at": datetime.now(timezone.utc),
    }
    await reports_col().insert_one(doc)
    return report_id


async def get_report(report_id: str) -> dict:
    return await reports_col().find_one({"_id": report_id})


async def claim_report_decision(report_id: str, staff_id: int, status: str) -> dict:
    return await reports_col().find_one_and_update(
        {"_id": report_id, "status": "Pending"},
        {"$set": {
            "status": status,
            "staff_id": str(staff_id),
            "decided_at": datetime.now(timezone.utc),
        }},
    )


# ---------------------------------------------------------------------------
# GLOBAL SCAMMER DATABASE
# ---------------------------------------------------------------------------

async def add_scammer(user_id: int, *, source: str, staff_id: int, guild_id: int,
                      report_id: str = None, reason: str = None) -> None:
    now = datetime.now(timezone.utc)
    entry = {
        "action": "add",
        "source": source,
        "staff_id": str(staff_id),
        "guild_id": str(guild_id),
        "report_id": report_id,
        "reason": reason,
        "timestamp": now,
    }
    await scammers_col().update_one(
        {"_id": str(user_id)},
        {
            "$set": {
                "user_id": str(user_id),
                "active": True,
                "current_source": source,
                "current_staff_id": str(staff_id),
                "current_guild_id": str(guild_id),
                "current_report_id": report_id,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
            "$push": {"history": entry},
        },
        upsert=True,
    )


async def remove_scammer(user_id: int, *, source: str, staff_id: int, guild_id: int,
                         reason: str = None) -> None:
    now = datetime.now(timezone.utc)
    entry = {
        "action": "remove",
        "source": source,
        "staff_id": str(staff_id),
        "guild_id": str(guild_id),
        "report_id": None,
        "reason": reason,
        "timestamp": now,
    }
    await scammers_col().update_one(
        {"_id": str(user_id)},
        {
            "$set": {
                "user_id": str(user_id),
                "active": False,
                "current_source": source,
                "current_staff_id": str(staff_id),
                "current_guild_id": str(guild_id),
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
            "$push": {"history": entry},
        },
        upsert=True,
    )


async def is_global_scammer(user_id: int) -> bool:
    doc = await scammers_col().find_one({"_id": str(user_id)})
    return bool(doc and doc.get("active"))


async def get_scammer_record(user_id: int) -> dict:
    return await scammers_col().find_one({"_id": str(user_id)})


# ---------------------------------------------------------------------------
# APPLICATIONS
# ---------------------------------------------------------------------------

async def get_application_config(guild_id: int) -> dict:
    settings = await get_guild_settings(guild_id)
    cfg = settings.get("applications") or {}
    return {
        "dm_timeout_minutes": int(cfg.get("dm_timeout_minutes") or 30),
    }


async def set_application_config(guild_id: int, **kwargs) -> None:
    update = {f"applications.{k}": v for k, v in kwargs.items() if v is not None}
    if update:
        await update_guild_settings(guild_id, update)


def _app_from_doc(doc: dict) -> dict:
    if not doc:
        return doc
    if not doc.get("submit_channel_id") and doc.get("review_channel_id"):
        doc["submit_channel_id"] = doc["review_channel_id"]
    return doc


async def create_application(guild_id: int, data: dict) -> str:
    data = dict(data)
    data["guild_id"] = str(guild_id)
    data["created_at"] = datetime.now(timezone.utc)
    data.setdefault("is_open", True)
    data.setdefault("questions", [])
    data.setdefault("decision_role_ids", [])
    data.setdefault("ping_role_ids", [])
    result = await applications_col().insert_one(data)
    return str(result.inserted_id)


async def update_application(guild_id: int, application_id: str, update: dict) -> None:
    try:
        oid = ObjectId(application_id)
    except Exception:
        return
    update = dict(update)
    update["updated_at"] = datetime.now(timezone.utc)
    await applications_col().update_one(
        {"_id": oid, "guild_id": str(guild_id)},
        {"$set": update},
    )


async def delete_application(guild_id: int, application_id: str) -> None:
    try:
        oid = ObjectId(application_id)
    except Exception:
        return
    await applications_col().delete_one({"_id": oid, "guild_id": str(guild_id)})


async def get_application(guild_id: int, application_id: str) -> dict:
    try:
        oid = ObjectId(application_id)
    except Exception:
        return None
    doc = await applications_col().find_one({"_id": oid, "guild_id": str(guild_id)})
    return _app_from_doc(doc)


async def get_application_by_id_any_guild(application_id: str) -> dict:
    try:
        oid = ObjectId(application_id)
    except Exception:
        return None
    doc = await applications_col().find_one({"_id": oid})
    return _app_from_doc(doc)


async def get_applications(guild_id: int) -> list:
    cursor = applications_col().find({"guild_id": str(guild_id)}).sort("created_at", 1)
    return [_app_from_doc(doc) async for doc in cursor]


async def set_application_open(guild_id: int, application_id: str, is_open: bool) -> None:
    await update_application(guild_id, application_id, {"is_open": bool(is_open)})


async def save_application_panel(guild_id: int, data: dict) -> None:
    data = dict(data)
    data["guild_id"] = str(guild_id)
    data["updated_at"] = datetime.now(timezone.utc)
    await application_panels_col().update_one(
        {"guild_id": str(guild_id)},
        {"$set": data},
        upsert=True,
    )


async def get_application_panel(guild_id: int) -> dict:
    return await application_panels_col().find_one({"guild_id": str(guild_id)})


async def create_application_submission(guild_id: int, data: dict) -> str:
    data = dict(data)
    data["guild_id"] = str(guild_id)
    data["created_at"] = datetime.now(timezone.utc)
    data.setdefault("status", "Pending")
    data.setdefault("staff_id", None)
    data.setdefault("decision_reason", None)
    data.setdefault("decided_at", None)
    result = await application_submissions_col().insert_one(data)
    return str(result.inserted_id)


async def get_application_submission(submission_id: str) -> dict:
    try:
        oid = ObjectId(submission_id)
    except Exception:
        return None
    return await application_submissions_col().find_one({"_id": oid})


async def get_submissions_for_guild(guild_id: int, application_id: str = None,
                                    status: str = None) -> list:
    query = {"guild_id": str(guild_id)}
    if application_id:
        query["application_id"] = str(application_id)
    if status:
        query["status"] = status
    cursor = application_submissions_col().find(query).sort("created_at", -1)
    return [doc async for doc in cursor]


async def get_submissions_for_user(guild_id: int, applicant_id: int,
                                   limit: int = 10) -> list:
    cursor = (
        application_submissions_col()
        .find({"guild_id": str(guild_id), "applicant_id": str(applicant_id)})
        .sort("created_at", -1)
        .limit(limit)
    )
    return [doc async for doc in cursor]


async def claim_submission_decision(submission_id: str, staff_id: int, status: str,
                                    reason: str = None) -> dict:
    try:
        oid = ObjectId(submission_id)
    except Exception:
        return None
    return await application_submissions_col().find_one_and_update(
        {"_id": oid, "status": "Pending"},
        {"$set": {
            "status": status,
            "staff_id": str(staff_id),
            "decision_reason": reason,
            "decided_at": datetime.now(timezone.utc),
        }},
    )


async def set_submission_message_id(submission_id: str, channel_id: int,
                                    message_id: int) -> None:
    try:
        oid = ObjectId(submission_id)
    except Exception:
        return
    await application_submissions_col().update_one(
        {"_id": oid},
        {"$set": {
            "submit_channel_id": str(channel_id),
            "submit_message_id": str(message_id),
        }},
    )


async def clear_submission_submit_message(submission_id: str) -> None:
    try:
        oid = ObjectId(submission_id)
    except Exception:
        return
    await application_submissions_col().update_one(
        {"_id": oid},
        {"$set": {"submit_message_id": None}},
    )


async def set_submission_final_message(submission_id: str, channel_id: int,
                                        message_id: int) -> None:
    try:
        oid = ObjectId(submission_id)
    except Exception:
        return
    await application_submissions_col().update_one(
        {"_id": oid},
        {"$set": {
            "final_channel_id": str(channel_id),
            "final_message_id": str(message_id),
        }},
    )


def _session_key(guild_id: int, user_id: int, application_id: str) -> str:
    return f"{guild_id}:{user_id}:{application_id}"


async def start_application_session(guild_id: int, user_id: int, application_id: str,
                                    questions: list, channel_id: int) -> dict:
    key = _session_key(guild_id, user_id, application_id)
    now = datetime.now(timezone.utc)
    doc = {
        "_id": key,
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "application_id": str(application_id),
        "channel_id": str(channel_id),
        "questions": list(questions),
        "answers": [],
        "current_index": 0,
        "prompt_shown_at": now,
        "created_at": now,
        "updated_at": now,
        "completed": False,
        # False until the applicant presses "Start" in the confirmation DM.
        "started": False,
    }
    await application_sessions_col().replace_one({"_id": key}, doc, upsert=True)
    return doc


async def begin_application_session(guild_id: int, user_id: int,
                                    application_id: str) -> dict:
    """Atomically flip a pending session to started. Returns the session doc
    only for the single caller that wins the race; everyone else gets None.
    This is what guarantees the first question is only ever sent once."""
    key = _session_key(guild_id, user_id, application_id)
    now = datetime.now(timezone.utc)
    return await application_sessions_col().find_one_and_update(
        {"_id": key, "completed": False, "started": False},
        {"$set": {"started": True, "prompt_shown_at": now, "updated_at": now}},
        return_document=ReturnDocument.AFTER,
    )


async def get_application_session(guild_id: int, user_id: int,
                                  application_id: str = None) -> dict:
    if application_id:
        return await application_sessions_col().find_one(
            {"_id": _session_key(guild_id, user_id, application_id)}
        )
    return await application_sessions_col().find_one({
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "completed": False,
    })


async def get_session_by_channel(channel_id: int) -> dict:
    return await application_sessions_col().find_one({
        "channel_id": str(channel_id),
        "completed": False,
    })


async def record_session_answer(guild_id: int, user_id: int, application_id: str,
                                answer: str) -> dict:
    key = _session_key(guild_id, user_id, application_id)
    now = datetime.now(timezone.utc)
    return await application_sessions_col().find_one_and_update(
        {"_id": key, "completed": False},
        {"$push": {"answers": {"text": answer, "answered_at": now}},
         "$inc": {"current_index": 1},
         "$set": {"updated_at": now}},
    )


async def annotate_last_answer_duration(guild_id: int, user_id: int,
                                        application_id: str,
                                        duration_seconds: int) -> None:
    key = _session_key(guild_id, user_id, application_id)
    doc = await application_sessions_col().find_one({"_id": key})
    if not doc:
        return
    idx = int(doc.get("current_index") or 0) - 1
    if idx < 0:
        return
    await application_sessions_col().update_one(
        {"_id": key},
        {"$set": {f"answers.{idx}.duration_seconds": int(duration_seconds)}},
    )


async def set_session_prompt_time(guild_id: int, user_id: int, application_id: str) -> None:
    key = _session_key(guild_id, user_id, application_id)
    await application_sessions_col().update_one(
        {"_id": key},
        {"$set": {"prompt_shown_at": datetime.now(timezone.utc),
                  "updated_at": datetime.now(timezone.utc)}},
    )


async def complete_application_session(guild_id: int, user_id: int,
                                       application_id: str) -> None:
    key = _session_key(guild_id, user_id, application_id)
    await application_sessions_col().update_one(
        {"_id": key},
        {"$set": {"completed": True, "updated_at": datetime.now(timezone.utc)}},
    )


async def delete_application_session(guild_id: int, user_id: int,
                                     application_id: str) -> None:
    key = _session_key(guild_id, user_id, application_id)
    await application_sessions_col().delete_one({"_id": key})


# ---------------------------------------------------------------------------
# DASHBOARD TASKS
# ---------------------------------------------------------------------------

async def enqueue_dashboard_task(task_type: str, guild_id: int = None,
                                 payload: dict = None) -> str:
    doc = {
        "type": task_type,
        "guild_id": str(guild_id) if guild_id is not None else None,
        "payload": payload or {},
        "created_at": datetime.now(timezone.utc),
        "done": False,
        "error": None,
    }
    result = await dashboard_tasks_col().insert_one(doc)
    return str(result.inserted_id)


async def take_pending_dashboard_tasks(limit: int = 10, types: list = None) -> list:
    """Claim up to `limit` pending tasks. Pass `types` so a poller only takes the
    tasks it knows how to run - every poller marks what it takes as done, so one
    that grabs a task it ignores would silently drop it."""
    out = []
    query = {"done": False}
    if types:
        query["type"] = {"$in": list(types)}
    for _ in range(limit):
        doc = await dashboard_tasks_col().find_one_and_update(
            query,
            {"$set": {"done": True, "started_at": datetime.now(timezone.utc)}},
            sort=[("created_at", 1)],
        )
        if not doc:
            break
        out.append(doc)
    return out


# ---------------------------------------------------------------------------
# MODERATION CASES
# ---------------------------------------------------------------------------

async def _next_mod_case_num(guild_id: int) -> int:
    doc = await guild_settings_col().find_one_and_update(
        {"_id": str(guild_id)},
        {"$inc": {"moderation.next_case_num": 1}},
        upsert=True,
        return_document=True,
    )
    return int((doc.get("moderation") or {}).get("next_case_num") or 1) - 1 or 1


async def create_mod_case(guild_id: int, action: str, moderator_id: int,
                          target_id: int = None, reason: str = None,
                          duration_seconds: int = None,
                          channel_id: int = None, extra: dict = None) -> dict:
    case_num = await _next_mod_case_num(guild_id)
    doc = {
        "guild_id": str(guild_id),
        "case_num": case_num,
        "action": action,
        "moderator_id": str(moderator_id),
        "target_id": str(target_id) if target_id is not None else None,
        "reason": (reason or "").strip()[:1000],
        "duration_seconds": int(duration_seconds) if duration_seconds else None,
        "channel_id": str(channel_id) if channel_id else None,
        "created_at": datetime.now(timezone.utc),
        "removed_at": None,
        "removed_by_id": None,
    }
    if extra:
        doc.update(extra)
    result = await mod_cases_col().insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def get_mod_case(guild_id: int, case_num: int) -> dict:
    return await mod_cases_col().find_one({
        "guild_id": str(guild_id),
        "case_num": int(case_num),
    })


async def get_mod_cases(guild_id: int, action: str = None, target_id: int = None,
                        moderator_id: int = None, limit: int = 100) -> list:
    query = {"guild_id": str(guild_id)}
    if action:
        query["action"] = action
    if target_id is not None:
        query["target_id"] = str(target_id)
    if moderator_id is not None:
        query["moderator_id"] = str(moderator_id)
    cursor = (
        mod_cases_col()
        .find(query)
        .sort("created_at", -1)
        .limit(limit)
    )
    return [doc async for doc in cursor]


async def get_user_warn_cases(guild_id: int, target_id: int, limit: int = 50) -> list:
    cursor = (
        mod_cases_col()
        .find({
            "guild_id": str(guild_id),
            "target_id": str(target_id),
            "action": "warn",
            "removed_at": None,
        })
        .sort("created_at", 1)
        .limit(limit)
    )
    return [doc async for doc in cursor]


async def count_user_warns(guild_id: int, target_id: int) -> int:
    return await mod_cases_col().count_documents({
        "guild_id": str(guild_id),
        "target_id": str(target_id),
        "action": "warn",
        "removed_at": None,
    })


async def soft_delete_mod_case(guild_id: int, case_num: int, staff_id: int) -> dict:
    return await mod_cases_col().find_one_and_update(
        {
            "guild_id": str(guild_id),
            "case_num": int(case_num),
            "removed_at": None,
        },
        {"$set": {
            "removed_at": datetime.now(timezone.utc),
            "removed_by_id": str(staff_id),
        }},
    )


async def get_moderation_config(guild_id: int) -> dict:
    settings = await get_guild_settings(guild_id)
    cfg = settings.get("moderation") or {}
    return {
        "log_channel_id": (
            str(cfg["log_channel_id"]) if cfg.get("log_channel_id") else None
        ),
    }


async def set_moderation_config(guild_id: int, **kwargs) -> None:
    update = {f"moderation.{k}": v for k, v in kwargs.items() if v is not None}
    if update:
        await update_guild_settings(guild_id, update)


# ---------------------------------------------------------------------------
# ANTI-NUKE
# ---------------------------------------------------------------------------

async def get_anti_nuke_config(guild_id: int) -> dict:
    settings = await get_guild_settings(guild_id)
    cfg = settings.get("anti_nuke") or {}
    actions = dict(ANTI_NUKE_DEFAULT_ACTIONS)
    stored = cfg.get("actions") or {}
    for k, v in stored.items():
        if k in actions and isinstance(v, dict):
            actions[k] = {
                "enabled": bool(v.get("enabled", actions[k]["enabled"])),
                "count": int(v.get("count", actions[k]["count"])),
                "window_seconds": int(
                    v.get("window_seconds", actions[k]["window_seconds"])
                ),
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
        "actions": actions,
    }


async def set_anti_nuke_config(guild_id: int, update: dict) -> None:
    prefixed = {f"anti_nuke.{k}": v for k, v in update.items()}
    if prefixed:
        await update_guild_settings(guild_id, prefixed)


async def log_nuke_event(guild_id: int, actor_id: int, actor_name: str, is_bot: bool,
                         action: str, count: int, window_seconds: int, threshold: int,
                         punishment: str, punishment_applied: bool,
                         punishment_error: str = None) -> str:
    doc = {
        "guild_id": str(guild_id),
        "actor_id": str(actor_id),
        "actor_name": actor_name,
        "is_bot": bool(is_bot),
        "action": action,
        "count": int(count),
        "window_seconds": int(window_seconds),
        "threshold": int(threshold),
        "punishment": punishment,
        "punishment_applied": bool(punishment_applied),
        "punishment_error": punishment_error,
        "created_at": datetime.now(timezone.utc),
    }
    result = await nuke_events_col().insert_one(doc)
    try:
        overflow_ids = []
        cursor = (
            nuke_events_col()
            .find({"guild_id": str(guild_id)})
            .sort("created_at", -1)
            .skip(500)
        )
        async for d in cursor:
            overflow_ids.append(d["_id"])
        if overflow_ids:
            await nuke_events_col().delete_many({"_id": {"$in": overflow_ids}})
    except Exception as e:
        print(f"[anti_nuke] prune failed: {e}")
    return str(result.inserted_id)


async def get_nuke_events(guild_id: int, limit: int = 100) -> list:
    cursor = (
        nuke_events_col()
        .find({"guild_id": str(guild_id)})
        .sort("created_at", -1)
        .limit(limit)
    )
    return [doc async for doc in cursor]


async def prune_nuke_events(guild_id: int, older_than_days: int = 30) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(older_than_days))
    result = await nuke_events_col().delete_many({
        "guild_id": str(guild_id),
        "created_at": {"$lt": cutoff},
    })
    return int(result.deleted_count or 0)


# ---------------------------------------------------------------------------
# STICKIES
# ---------------------------------------------------------------------------

async def get_sticky(guild_id: int, channel_id: int) -> dict:
    return await stickies_col().find_one({
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
    })


async def get_guild_stickies(guild_id: int) -> list:
    cursor = stickies_col().find({"guild_id": str(guild_id)}).sort("created_at", 1)
    return [doc async for doc in cursor]


async def upsert_sticky(guild_id: int, channel_id: int, message_id: int,
                        content: str, author_id: int) -> dict:
    doc = {
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "message_id": str(message_id),
        "content": content[:2000],
        "author_id": str(author_id),
        "created_at": datetime.now(timezone.utc),
        "last_repost_at": datetime.now(timezone.utc),
    }
    await stickies_col().update_one(
        {"guild_id": str(guild_id), "channel_id": str(channel_id)},
        {"$set": doc},
        upsert=True,
    )
    return doc


async def update_sticky_message(guild_id: int, channel_id: int, message_id: int) -> None:
    await stickies_col().update_one(
        {"guild_id": str(guild_id), "channel_id": str(channel_id)},
        {"$set": {
            "message_id": str(message_id),
            "last_repost_at": datetime.now(timezone.utc),
        }},
    )


async def delete_sticky(guild_id: int, channel_id: int) -> dict:
    return await stickies_col().find_one_and_delete({
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
    })


# ---------------------------------------------------------------------------
# COUNT GAMES
# ---------------------------------------------------------------------------

async def get_count_game(guild_id: int, channel_id: int) -> dict:
    return await count_games_col().find_one({
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
    })


async def get_guild_count_games(guild_id: int) -> list:
    cursor = count_games_col().find({"guild_id": str(guild_id)}).sort("started_at", 1)
    return [doc async for doc in cursor]


async def start_count_game(guild_id: int, channel_id: int, start_number: int,
                           started_by: int) -> dict:
    doc = {
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "last_number": int(start_number),
        "last_user_id": None,
        "started_by": str(started_by),
        "started_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    await count_games_col().update_one(
        {"guild_id": str(guild_id), "channel_id": str(channel_id)},
        {"$set": doc},
        upsert=True,
    )
    return doc


async def advance_count_game(guild_id: int, channel_id: int, new_number: int,
                             user_id: int) -> None:
    await count_games_col().update_one(
        {"guild_id": str(guild_id), "channel_id": str(channel_id)},
        {"$set": {
            "last_number": int(new_number),
            "last_user_id": str(user_id),
            "updated_at": datetime.now(timezone.utc),
        }},
    )


async def reset_count_game(guild_id: int, channel_id: int) -> None:
    await count_games_col().update_one(
        {"guild_id": str(guild_id), "channel_id": str(channel_id)},
        {"$set": {
            "last_number": 0,
            "last_user_id": None,
            "updated_at": datetime.now(timezone.utc),
        }},
    )


async def delete_count_game(guild_id: int, channel_id: int) -> dict:
    return await count_games_col().find_one_and_delete({
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
    })


# ---------------------------------------------------------------------------
# AFK
# ---------------------------------------------------------------------------

async def set_afk(guild_id: int, user_id: int, reason: str = None) -> dict:
    doc = {
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "reason": reason or None,
        "since": datetime.now(timezone.utc),
    }
    await afk_col().update_one(
        {"guild_id": str(guild_id), "user_id": str(user_id)},
        {"$set": doc}, upsert=True,
    )
    return doc


async def clear_afk(guild_id: int, user_id: int) -> dict:
    return await afk_col().find_one_and_delete(
        {"guild_id": str(guild_id), "user_id": str(user_id)}
    )


async def get_all_afk() -> list:
    return [doc async for doc in afk_col().find({})]


# ---------------------------------------------------------------------------
# REACTION ROLES
# ---------------------------------------------------------------------------

async def upsert_reaction_role(guild_id: int, channel_id: int, message_id: int,
                               emoji_key: str, emoji_display: str, role_id: int,
                               created_by: int) -> None:
    now = datetime.now(timezone.utc)
    await reaction_roles_col().update_one(
        {"guild_id": str(guild_id), "message_id": str(message_id), "emoji_key": emoji_key},
        {
            "$set": {
                "channel_id": str(channel_id),
                "emoji_display": emoji_display,
                "role_id": str(role_id),
                "created_by": str(created_by),
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def get_reaction_role(guild_id: int, message_id: int, emoji_key: str):
    return await reaction_roles_col().find_one({
        "guild_id": str(guild_id),
        "message_id": str(message_id),
        "emoji_key": emoji_key,
    })


async def delete_reaction_role(guild_id: int, message_id: int, emoji_key: str):
    return await reaction_roles_col().find_one_and_delete({
        "guild_id": str(guild_id),
        "message_id": str(message_id),
        "emoji_key": emoji_key,
    })


async def get_guild_reaction_roles(guild_id: int) -> list:
    cursor = reaction_roles_col().find({"guild_id": str(guild_id)}).sort("created_at", 1)
    return [doc async for doc in cursor]


async def delete_reaction_roles_for_message(message_id: int) -> None:
    await reaction_roles_col().delete_many({"message_id": str(message_id)})


# ---------------------------------------------------------------------------
# BLACKLIST  (role-based; requests go through staff approval)
# ---------------------------------------------------------------------------

BLACKLIST_TYPES = ("giveaway", "ticket", "application")


async def get_blacklist_config(guild_id: int) -> dict:
    settings = await get_guild_settings(guild_id)
    cfg = settings.get("blacklist") or {}
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
                for t in BLACKLIST_TYPES
            }
            for bucket in ("submitted", "accepted", "denied")
        },
    }


async def set_blacklist_config(guild_id: int, giveaway_role_id: str = None,
                               ticket_role_id: str = None,
                               request_channel_id: str = None,
                               staff_role_id: str = None,
                               application_role_id: str = None) -> None:
    update = {}
    if giveaway_role_id is not None:
        update["blacklist.giveaway_role_id"] = str(giveaway_role_id) or None
    if ticket_role_id is not None:
        update["blacklist.ticket_role_id"] = str(ticket_role_id) or None
    if request_channel_id is not None:
        update["blacklist.request_channel_id"] = str(request_channel_id) or None
    if staff_role_id is not None:
        update["blacklist.staff_role_id"] = str(staff_role_id) or None
    if application_role_id is not None:
        update["blacklist.application_role_id"] = str(application_role_id) or None
    if update:
        await update_guild_settings(guild_id, update)


async def create_blacklist_request(guild_id: int, applicant_id: int, target_id: int,
                                   type_: str, action: str, reason: str,
                                   proof_urls: list, channel_id: int) -> str:
    doc = {
        "guild_id": str(guild_id),
        "applicant_id": str(applicant_id),
        "target_id": str(target_id),
        "type": type_,            # "giveaway" | "ticket" | "application"
        "action": action,         # "add" | "remove"
        "reason": (reason or "").strip()[:1000],
        "proof_urls": [str(u) for u in (proof_urls or [])][:3],
        "status": "Pending",
        "staff_id": None,
        "decided_at": None,
        "channel_id": str(channel_id),
        "message_id": None,
        "created_at": datetime.now(timezone.utc),
    }
    result = await blacklist_requests_col().insert_one(doc)
    # bump the counter (per-type submitted)
    await guild_settings_col().update_one(
        {"_id": str(guild_id)},
        {"$inc": {f"blacklist.counters.submitted.{type_}": 1}},
        upsert=True,
    )
    return str(result.inserted_id)


async def get_blacklist_request(request_id: str) -> dict:
    try:
        oid = ObjectId(request_id)
    except Exception:
        return None
    return await blacklist_requests_col().find_one({"_id": oid})


async def get_blacklist_requests_for_guild(guild_id: int, status: str = None,
                                           type_: str = None, action: str = None) -> list:
    query = {"guild_id": str(guild_id)}
    if status:
        query["status"] = status
    if type_:
        query["type"] = type_
    if action:
        query["action"] = action
    cursor = blacklist_requests_col().find(query).sort("created_at", -1)
    return [doc async for doc in cursor]


async def has_pending_blacklist_request(guild_id: int, target_id: int, type_: str,
                                        action: str) -> bool:
    doc = await blacklist_requests_col().find_one({
        "guild_id": str(guild_id),
        "target_id": str(target_id),
        "type": type_,
        "action": action,
        "status": "Pending",
    })
    return doc is not None


async def set_blacklist_request_message(request_id: str, channel_id: int,
                                        message_id: int) -> None:
    try:
        oid = ObjectId(request_id)
    except Exception:
        return
    await blacklist_requests_col().update_one(
        {"_id": oid},
        {"$set": {
            "channel_id": str(channel_id),
            "message_id": str(message_id),
        }},
    )


async def claim_blacklist_request_decision(request_id: str, staff_id: int,
                                           status: str) -> dict:
    """Atomic: only flips Pending -> Accepted/Denied once. Returns the doc as it
    was BEFORE the update, or None if already decided."""
    try:
        oid = ObjectId(request_id)
    except Exception:
        return None
    return await blacklist_requests_col().find_one_and_update(
        {"_id": oid, "status": "Pending"},
        {"$set": {
            "status": status,
            "staff_id": str(staff_id),
            "decided_at": datetime.now(timezone.utc),
        }},
    )


async def bump_blacklist_counter(guild_id: int, bucket: str, type_: str) -> None:
    """bucket: "submitted" | "accepted" | "denied". type_: "giveaway" | "ticket" | "application"."""
    if bucket not in ("submitted", "accepted", "denied"):
        return
    if type_ not in BLACKLIST_TYPES:
        return
    await guild_settings_col().update_one(
        {"_id": str(guild_id)},
        {"$inc": {f"blacklist.counters.{bucket}.{type_}": 1}},
        upsert=True,
    )


# ---------------------------------------------------------------------------
# HONEYPOT  (trap channel that softbans anyone who posts in it)
# ---------------------------------------------------------------------------

DEFAULT_HONEYPOT_DM = (
    "You were softbanned from {guild} for posting in a trap channel. "
    "This is an automated action. If you believe this was a mistake, "
    "contact the server staff."
)


async def get_honeypot_config(guild_id: int) -> dict:
    settings = await get_guild_settings(guild_id)
    cfg = settings.get("honeypot") or {}
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


async def set_honeypot_config(guild_id: int, **kwargs) -> None:
    """Only updates the keys you pass. Values of None are ignored (so you can
    clear a field by passing an empty string)."""
    update = {}
    for k, v in kwargs.items():
        if v is None:
            continue
        update[f"honeypot.{k}"] = v
    if update:
        await update_guild_settings(guild_id, update)


async def set_honeypot_panel_message(guild_id: int, message_id) -> None:
    await update_guild_settings(
        guild_id,
        {"honeypot.panel_message_id": str(message_id) if message_id else None},
    )


async def bump_honeypot_kick_count(guild_id: int) -> int:
    """Atomically increment and return the new count."""
    doc = await guild_settings_col().find_one_and_update(
        {"_id": str(guild_id)},
        {"$inc": {"honeypot.kick_count": 1}},
        upsert=True,
        return_document=True,
    )
    return int((doc.get("honeypot") or {}).get("kick_count") or 0)


async def log_honeypot_kick(guild_id: int, user_id: int, user_name: str,
                            channel_id: int, message_id: int,
                            content_preview: str, content_types: list) -> str:
    doc = {
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "user_name": user_name,
        "channel_id": str(channel_id),
        "message_id": str(message_id),
        "content_preview": (content_preview or "")[:200],
        "content_types": list(content_types or []),
        "created_at": datetime.now(timezone.utc),
    }
    result = await honeypot_kicks_col().insert_one(doc)
    return str(result.inserted_id)


async def get_honeypot_kicks(guild_id: int, limit: int = 200) -> list:
    cursor = (
        honeypot_kicks_col()
        .find({"guild_id": str(guild_id)})
        .sort("created_at", -1)
        .limit(limit)
    )
    return [doc async for doc in cursor]


async def count_honeypot_kicks(guild_id: int) -> int:
    return await honeypot_kicks_col().count_documents({"guild_id": str(guild_id)})


# ---------------------------------------------------------------------------
# WELCOME + AUTO ROLE  (dashboard-only; stored in guild_settings)
# ---------------------------------------------------------------------------

async def get_welcome_config(guild_id: int) -> dict:
    settings = await get_guild_settings(guild_id)
    welcome = settings.get("welcome") or {}
    autorole = settings.get("autorole") or {}
    return {
        "channel_id": welcome.get("channel_id"),
        "message": welcome.get("message") or "",
        "autorole_ids": autorole.get("role_ids") or [],
    }


# ---------------------------------------------------------------------------
# TEMP ROLES
# ---------------------------------------------------------------------------

async def add_temp_role(guild_id: int, user_id: int, role_id: int,
                        expires_at: datetime) -> None:
    """Insert or replace a temp-role entry. If one already exists for this
    (guild, user, role) triple it is overwritten (duration extended)."""
    from database.mongo import temp_roles_col
    now = datetime.now(timezone.utc)
    await temp_roles_col().update_one(
        {
            "guild_id": str(guild_id),
            "user_id": str(user_id),
            "role_id": str(role_id),
        },
        {
            "$set": {
                "expires_at": expires_at,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def get_temp_role(guild_id: int, user_id: int, role_id: int) -> dict:
    from database.mongo import temp_roles_col
    return await temp_roles_col().find_one({
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "role_id": str(role_id),
    })


async def remove_temp_role(guild_id: int, user_id: int, role_id: int) -> dict:
    from database.mongo import temp_roles_col
    return await temp_roles_col().find_one_and_delete({
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "role_id": str(role_id),
    })


async def get_due_temp_roles(now: datetime) -> list:
    """Return all temp-role entries whose expiry has passed."""
    from database.mongo import temp_roles_col
    cursor = temp_roles_col().find({"expires_at": {"$lte": now}})
    return [doc async for doc in cursor]


async def get_guild_temp_roles(guild_id: int) -> list:
    """Return all active temp-role entries for a guild, sorted soonest first."""
    from database.mongo import temp_roles_col
    cursor = temp_roles_col().find(
        {"guild_id": str(guild_id)}
    ).sort("expires_at", 1)
    return [doc async for doc in cursor]


async def get_user_temp_roles(guild_id: int, user_id: int) -> list:
    from database.mongo import temp_roles_col
    cursor = temp_roles_col().find({
        "guild_id": str(guild_id),
        "user_id": str(user_id),
    }).sort("expires_at", 1)
    return [doc async for doc in cursor]


async def delete_temp_role_by_id(doc_id) -> None:
    from database.mongo import temp_roles_col
    from bson import ObjectId
    await temp_roles_col().delete_one({"_id": ObjectId(str(doc_id))})


# ---------------------------------------------------------------------------
# CHANGES  (promotion / demotion ladders, one per type; set on the dashboard, stored in
# guild_settings under "changes": {types: [{id, name, channel_id, ladder: [{index, role_id, nick}]}]})
# ---------------------------------------------------------------------------

async def get_changes_config(guild_id: int) -> dict:
    from utils.changes_core import normalize_types
    settings = await get_guild_settings(guild_id)
    ch = settings.get("changes") or {}
    return {"types": normalize_types(ch.get("types"))}


# ---------------------------------------------------------------------------
# LIVE HELP PANELS
# Help menus posted by admins (/help, /staffhelp, /admin help) are remembered here so the bot can
# edit them when commands are added or removed. One document per message:
#   {_id: message_id, guild_id, channel_id, kind: member|staff|admin, sig, created_at}
# `sig` is a fingerprint of what the message currently shows, so it's only edited when it changes.
# ---------------------------------------------------------------------------

HELP_PANELS_PER_KIND = 10     # newest tracked panels kept per server and menu type


async def save_help_panel(guild_id: int, channel_id: int, message_id: int, kind: str,
                          sig: str = None) -> None:
    """Start (or keep) tracking a help message. `sig=None` leaves an existing fingerprint alone
    (used when adopting an old message, so it gets refreshed on the next sync)."""
    from database.mongo import help_panels_col
    col = help_panels_col()
    update = {
        "$set": {"guild_id": str(guild_id), "channel_id": str(channel_id), "kind": kind},
        "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
    }
    if sig is None:
        update["$setOnInsert"]["sig"] = ""
    else:
        update["$set"]["sig"] = sig
    await col.update_one({"_id": str(message_id)}, update, upsert=True)
    # Stop tracking the oldest ones beyond the cap (their Discord messages are left alone).
    stale = [d["_id"] async for d in col.find(
        {"guild_id": str(guild_id), "kind": kind}).sort("created_at", -1).skip(HELP_PANELS_PER_KIND)]
    if stale:
        await col.delete_many({"_id": {"$in": stale}})


async def get_help_panels() -> list:
    from database.mongo import help_panels_col
    return [d async for d in help_panels_col().find({})]


async def set_help_panel_sig(message_id, sig: str) -> None:
    from database.mongo import help_panels_col
    await help_panels_col().update_one({"_id": str(message_id)}, {"$set": {"sig": sig}})


async def delete_help_panel(message_id) -> None:
    from database.mongo import help_panels_col
    await help_panels_col().delete_one({"_id": str(message_id)})


# ---------------------------------------------------------------------------
# STAFF STRIKES  (per Change type)
# Settings live in guild_settings under "strikes.types.<type_id>" (see utils/strikes_core.py).
# Each strike is one document in `staff_strikes`:
#   {guild_id, number (per-server id), type_id, type_name (snapshot), user_id, reason,
#    issuer_id, issuer_name, created_at}
# ---------------------------------------------------------------------------

async def get_strike_type_config(guild_id: int, type_id: str) -> dict:
    from utils.strikes_core import normalize_type_config
    settings = await get_guild_settings(guild_id)
    raw = ((settings.get("strikes") or {}).get("types") or {}).get(str(type_id))
    return normalize_type_config(raw)


async def add_strike(guild_id: int, type_id: str, type_name: str, user_id: int, reason: str,
                     issuer_id: int, issuer_name: str) -> dict:
    from database.mongo import staff_strikes_col
    number = await _next_counter(guild_id, "strikes.next_num")
    doc = {
        "guild_id": str(guild_id), "number": number,
        "type_id": str(type_id), "type_name": str(type_name),
        "user_id": str(user_id), "reason": reason or "",
        "issuer_id": str(issuer_id), "issuer_name": str(issuer_name),
        "created_at": datetime.now(timezone.utc),
    }
    await staff_strikes_col().insert_one(doc)
    return doc


async def count_strikes(guild_id: int, type_id: str, user_id: int) -> int:
    from database.mongo import staff_strikes_col
    return await staff_strikes_col().count_documents(
        {"guild_id": str(guild_id), "type_id": str(type_id), "user_id": str(user_id)})


async def get_user_strikes(guild_id: int, user_id: int, type_id: str = None) -> list:
    from database.mongo import staff_strikes_col
    q = {"guild_id": str(guild_id), "user_id": str(user_id)}
    if type_id:
        q["type_id"] = str(type_id)
    return [d async for d in staff_strikes_col().find(q).sort("created_at", 1)]


async def get_strike_counts_for_user(guild_id: int, user_id: int) -> list:
    """[{type_id, type_name, count}] for every type the member has strikes in."""
    from database.mongo import staff_strikes_col
    pipeline = [
        {"$match": {"guild_id": str(guild_id), "user_id": str(user_id)}},
        {"$group": {"_id": "$type_id", "type_name": {"$last": "$type_name"}, "count": {"$sum": 1}}},
    ]
    return [{"type_id": d["_id"], "type_name": d.get("type_name") or "?", "count": d["count"]}
            async for d in staff_strikes_col().aggregate(pipeline)]


async def delete_strike(guild_id: int, number: int):
    """Delete one strike by its number. Returns the deleted document, or None."""
    from database.mongo import staff_strikes_col
    return await staff_strikes_col().find_one_and_delete(
        {"guild_id": str(guild_id), "number": int(number)})


async def clear_user_strikes(guild_id: int, type_id: str, user_id: int) -> int:
    from database.mongo import staff_strikes_col
    res = await staff_strikes_col().delete_many(
        {"guild_id": str(guild_id), "type_id": str(type_id), "user_id": str(user_id)})
    return res.deleted_count


async def add_strike_audit(guild_id: int, action: str, type_id: str, type_name: str,
                           user_id, by: str, detail: str = "") -> None:
    """Small trail of strike deletions / resets (the strikes themselves are hard-deleted)."""
    from database.mongo import staff_strike_audit_col
    await staff_strike_audit_col().insert_one({
        "guild_id": str(guild_id), "action": action, "type_id": str(type_id),
        "type_name": str(type_name), "user_id": str(user_id) if user_id else "",
        "by": str(by), "detail": str(detail)[:500], "at": datetime.now(timezone.utc),
    })


# ---------------------------------------------------------------------------
# EXTRAS: reminders, suggestions, staff notes, birthdays, lockdown, vouch top
# ---------------------------------------------------------------------------

async def _next_counter(guild_id: int, path: str) -> int:
    """Per-guild sequential counter stored in guild_settings (e.g. 'notes.next_num')."""
    doc = await guild_settings_col().find_one_and_update(
        {"_id": str(guild_id)},
        {"$inc": {path: 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    node = doc
    for part in path.split("."):
        node = (node or {}).get(part)
    return int(node or 1)


# -- reminders ---------------------------------------------------------------
async def add_reminder(guild_id: int, user_id: int, channel_id: int, text: str,
                       due_at: datetime) -> dict:
    from database.mongo import reminders_col
    doc = {
        "guild_id": str(guild_id), "user_id": str(user_id), "channel_id": str(channel_id),
        "text": text, "due_at": due_at, "created_at": datetime.now(timezone.utc),
    }
    doc["_id"] = (await reminders_col().insert_one(doc)).inserted_id
    return doc


async def count_user_reminders(user_id: int) -> int:
    from database.mongo import reminders_col
    return await reminders_col().count_documents({"user_id": str(user_id)})


async def get_due_reminders(now: datetime) -> list:
    from database.mongo import reminders_col
    return [d async for d in reminders_col().find({"due_at": {"$lte": now}}).limit(100)]


async def delete_reminder(doc_id) -> None:
    from database.mongo import reminders_col
    await reminders_col().delete_one({"_id": ObjectId(str(doc_id))})


# -- suggestions -------------------------------------------------------------
async def get_suggestion_channel_id(guild_id: int):
    settings = await get_guild_settings(guild_id)
    return (settings.get("suggestions") or {}).get("channel_id")


async def create_suggestion(guild_id: int, author_id: int, channel_id: int,
                            message_id: int, text: str, number: int) -> None:
    from database.mongo import suggestions_col
    await suggestions_col().insert_one({
        "guild_id": str(guild_id), "number": number, "author_id": str(author_id),
        "channel_id": str(channel_id), "message_id": str(message_id), "text": text,
        "status": "Pending", "staff_id": None, "reason": None,
        "created_at": datetime.now(timezone.utc),
    })


async def next_suggestion_num(guild_id: int) -> int:
    return await _next_counter(guild_id, "suggestions.next_num")


async def get_suggestion(guild_id: int, number: int) -> dict:
    from database.mongo import suggestions_col
    return await suggestions_col().find_one({"guild_id": str(guild_id), "number": int(number)})


async def decide_suggestion(guild_id: int, number: int, status: str, staff_id: int,
                            reason: str = None) -> dict:
    from database.mongo import suggestions_col
    return await suggestions_col().find_one_and_update(
        {"guild_id": str(guild_id), "number": int(number)},
        {"$set": {"status": status, "staff_id": str(staff_id), "reason": reason,
                  "decided_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )


# -- staff notes --------------------------------------------------------------
async def add_mod_note(guild_id: int, target_id: int, staff_id: int, text: str) -> dict:
    from database.mongo import mod_notes_col
    doc = {
        "guild_id": str(guild_id), "number": await _next_counter(guild_id, "notes.next_num"),
        "target_id": str(target_id), "staff_id": str(staff_id), "text": text,
        "created_at": datetime.now(timezone.utc),
    }
    doc["_id"] = (await mod_notes_col().insert_one(doc)).inserted_id
    return doc


async def get_mod_notes(guild_id: int, target_id: int, limit: int = 25) -> list:
    from database.mongo import mod_notes_col
    cur = mod_notes_col().find(
        {"guild_id": str(guild_id), "target_id": str(target_id)}
    ).sort("created_at", -1).limit(limit)
    return [d async for d in cur]


async def count_mod_notes(guild_id: int, target_id: int) -> int:
    from database.mongo import mod_notes_col
    return await mod_notes_col().count_documents(
        {"guild_id": str(guild_id), "target_id": str(target_id)})


async def delete_mod_note(guild_id: int, number: int) -> dict:
    from database.mongo import mod_notes_col
    return await mod_notes_col().find_one_and_delete(
        {"guild_id": str(guild_id), "number": int(number)})


# -- clear warns --------------------------------------------------------------
async def clear_user_warns(guild_id: int, target_id: int, staff_id: int) -> int:
    res = await mod_cases_col().update_many(
        {"guild_id": str(guild_id), "target_id": str(target_id),
         "action": "warn", "removed_at": None},
        {"$set": {"removed_at": datetime.now(timezone.utc), "removed_by_id": str(staff_id)}},
    )
    return res.modified_count


# -- birthdays ----------------------------------------------------------------
async def set_birthday(guild_id: int, user_id: int, day: int, month: int) -> None:
    from database.mongo import birthdays_col
    await birthdays_col().update_one(
        {"guild_id": str(guild_id), "user_id": str(user_id)},
        {"$set": {"day": day, "month": month, "last_announced_year": None}},
        upsert=True,
    )


async def get_birthday(guild_id: int, user_id: int) -> dict:
    from database.mongo import birthdays_col
    return await birthdays_col().find_one({"guild_id": str(guild_id), "user_id": str(user_id)})


async def delete_birthday(guild_id: int, user_id: int) -> dict:
    from database.mongo import birthdays_col
    return await birthdays_col().find_one_and_delete(
        {"guild_id": str(guild_id), "user_id": str(user_id)})


async def get_birthdays_on(month: int, days: list) -> list:
    from database.mongo import birthdays_col
    return [d async for d in birthdays_col().find({"month": month, "day": {"$in": days}})]


async def mark_birthday_announced(doc_id, year: int) -> None:
    from database.mongo import birthdays_col
    await birthdays_col().update_one({"_id": doc_id}, {"$set": {"last_announced_year": year}})


async def get_birthday_channel_id(guild_id: int):
    settings = await get_guild_settings(guild_id)
    return (settings.get("birthdays") or {}).get("channel_id")


# -- vouch leaderboard --------------------------------------------------------
async def top_vouched(guild_id: int, limit: int = 10) -> list:
    """[(user_id, normal_vouch_count), ...] highest first."""
    pipeline = [
        {"$match": {"guild_id": str(guild_id), "type": "normal"}},
        {"$group": {"_id": "$target_id", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": int(limit)},
    ]
    return [(d["_id"], d["count"]) async for d in vouches_col().aggregate(pipeline)]


# -- lockdown -----------------------------------------------------------------
async def get_lockdown(guild_id: int) -> dict:
    settings = await get_guild_settings(guild_id)
    return settings.get("lockdown") or {}


async def set_lockdown(guild_id: int, data) -> None:
    if data is None:
        await guild_settings_col().update_one(
            {"_id": str(guild_id)}, {"$unset": {"lockdown": ""}})
    else:
        await update_guild_settings(guild_id, {"lockdown": data})


# ---------------------------------------------------------------------------
# REACTION ROLE MODES  (normal = add/remove with the reaction, unique = one role
# per message, verify = reacting gives the role and un-reacting keeps it)
# ---------------------------------------------------------------------------

REACTION_ROLE_MODES = ("normal", "unique", "verify")


async def get_reaction_role_mode(guild_id: int, message_id: int) -> str:
    doc = await reaction_roles_col().find_one({
        "guild_id": str(guild_id), "message_id": str(message_id),
        "mode": {"$in": ["unique", "verify"]},
    })
    return (doc or {}).get("mode") or "normal"


async def set_reaction_role_mode(guild_id: int, message_id: int, mode: str) -> int:
    """Set the mode on every reaction role of one message. Returns how many rows it covers."""
    res = await reaction_roles_col().update_many(
        {"guild_id": str(guild_id), "message_id": str(message_id)},
        {"$set": {"mode": mode}},
    )
    return res.matched_count


async def get_message_reaction_roles(guild_id: int, message_id: int) -> list:
    cursor = reaction_roles_col().find(
        {"guild_id": str(guild_id), "message_id": str(message_id)}
    ).sort("created_at", 1)
    return [doc async for doc in cursor]


async def clear_message_reaction_roles(guild_id: int, message_id: int) -> list:
    rows = await get_message_reaction_roles(guild_id, message_id)
    if rows:
        await reaction_roles_col().delete_many(
            {"guild_id": str(guild_id), "message_id": str(message_id)})
    return rows


# ---------------------------------------------------------------------------
# TEMP BANS
# ---------------------------------------------------------------------------

async def add_temp_ban(guild_id: int, user_id: int, moderator_id: int,
                       expires_at: datetime, reason: str = None,
                       case_num: int = None) -> None:
    now = datetime.now(timezone.utc)
    await temp_bans_col().update_one(
        {"guild_id": str(guild_id), "user_id": str(user_id)},
        {"$set": {
            "moderator_id": str(moderator_id),
            "expires_at": expires_at,
            "reason": (reason or "")[:500],
            "case_num": case_num,
            "attempts": 0,
            "updated_at": now,
        }, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )


async def remove_temp_ban(guild_id: int, user_id: int) -> dict:
    return await temp_bans_col().find_one_and_delete(
        {"guild_id": str(guild_id), "user_id": str(user_id)})


async def get_due_temp_bans(now: datetime) -> list:
    cursor = temp_bans_col().find({"expires_at": {"$lte": now}})
    return [doc async for doc in cursor]


async def get_guild_temp_bans(guild_id: int) -> list:
    cursor = temp_bans_col().find({"guild_id": str(guild_id)}).sort("expires_at", 1)
    return [doc async for doc in cursor]


async def bump_temp_ban_attempts(doc_id) -> int:
    doc = await temp_bans_col().find_one_and_update(
        {"_id": doc_id}, {"$inc": {"attempts": 1}}, return_document=ReturnDocument.AFTER)
    return int((doc or {}).get("attempts", 0))


async def delete_temp_ban_by_id(doc_id) -> None:
    await temp_bans_col().delete_one({"_id": doc_id})


# ---------------------------------------------------------------------------
# AUTOMOD  (guild_settings["automod"])
# ---------------------------------------------------------------------------

async def get_automod_config(guild_id: int) -> dict:
    settings = await get_guild_settings(guild_id)
    return normalize_automod_config(settings.get("automod"))


async def set_automod_fields(guild_id: int, update: dict) -> None:
    """update uses paths relative to 'automod', e.g. {'enabled': True, 'rules.spam.limit': 6}."""
    await update_guild_settings(guild_id, {f"automod.{k}": v for k, v in update.items()})


async def automod_list_add(guild_id: int, field: str, value: str) -> bool:
    await get_guild_settings(guild_id)
    res = await guild_settings_col().update_one(
        {"_id": str(guild_id)}, {"$addToSet": {f"automod.{field}": value}})
    return res.modified_count > 0


async def automod_list_remove(guild_id: int, field: str, value: str) -> bool:
    res = await guild_settings_col().update_one(
        {"_id": str(guild_id)}, {"$pull": {f"automod.{field}": value}})
    return res.modified_count > 0


# ---------------------------------------------------------------------------
# AUTOMOD STRIKES  (collection automod_strikes - one doc per member, survives restarts)
# ---------------------------------------------------------------------------
# {_id: "<guild>:<user>", guild_id, user_id, strikes: [unix timestamps], updated_at}
# A TTL index on updated_at (created by cogs/automod.py) deletes idle docs after 25h, the
# longest strike window the dashboard allows.

AUTOMOD_STRIKE_TTL_SECONDS = 25 * 3600


def _strike_key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"


async def add_automod_strike(guild_id: int, user_id: int, window_seconds: int) -> int:
    """Record one strike, forget the ones older than the window, return how many are left."""
    import time as _time
    now = _time.time()
    key = _strike_key(guild_id, user_id)
    await automod_strikes_col().update_one(
        {"_id": key}, {"$pull": {"strikes": {"$lt": now - window_seconds}}})
    doc = await automod_strikes_col().find_one_and_update(
        {"_id": key},
        {"$push": {"strikes": now},
         "$set": {"guild_id": str(guild_id), "user_id": str(user_id),
                  "updated_at": datetime.now(timezone.utc)}},
        upsert=True, return_document=ReturnDocument.AFTER,
    )
    return len((doc or {}).get("strikes") or [])


async def clear_automod_strikes(guild_id: int, user_id: int = None) -> int:
    """Forget one member's strikes (or everyone's in the server). Returns docs removed."""
    if user_id is not None:
        res = await automod_strikes_col().delete_one({"_id": _strike_key(guild_id, user_id)})
    else:
        res = await automod_strikes_col().delete_many({"guild_id": str(guild_id)})
    return res.deleted_count


# ---------------------------------------------------------------------------
# ROLE PANELS  (collection role_panels - button / dropdown role pickers)
# ---------------------------------------------------------------------------

async def get_role_panel(panel_id: str):
    try:
        oid = ObjectId(str(panel_id))
    except Exception:
        return None
    return await role_panels_col().find_one({"_id": oid})


async def update_role_panel(panel_id, fields: dict) -> None:
    await role_panels_col().update_one({"_id": ObjectId(str(panel_id))}, {"$set": fields})


# ---------------------------------------------------------------------------
# Commands that no longer exist: their old rows are removed from the dashboard's
# Command Permissions page.
# ---------------------------------------------------------------------------
REMOVED_COMMANDS = (
    "automod status", "automod toggle", "automod rule", "automod addword",
    "automod removeword", "automod words", "automod allowdomain",
    "automod removedomain", "automod exemptrole", "automod exemptchannel",
    "automod logchannel", "automod punish",
)


async def purge_removed_commands() -> int:
    res = await command_permissions_col().delete_many({"command": {"$in": list(REMOVED_COMMANDS)}})
    return res.deleted_count


# ---------------------------------------------------------------------------
# STAT CHANNELS  (guild_settings["stat_channels"] = [{channel_id, stat, format}])
# ---------------------------------------------------------------------------

async def get_stat_channels(guild_id: int) -> list:
    settings = await get_guild_settings(guild_id)
    return [dict(x) for x in (settings.get("stat_channels") or []) if isinstance(x, dict)]


async def set_stat_channels(guild_id: int, rows: list) -> None:
    await update_guild_settings(guild_id, {"stat_channels": rows})


# ---------------------------------------------------------------------------
# INVITE TRACKER
# ---------------------------------------------------------------------------

async def get_invite_tracker_config(guild_id: int) -> dict:
    settings = await get_guild_settings(guild_id)
    cfg = settings.get("invite_tracker") or {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "channel_id": str(cfg["channel_id"]) if cfg.get("channel_id") else None,
    }


async def set_invite_tracker_fields(guild_id: int, update: dict) -> None:
    await update_guild_settings(guild_id, {f"invite_tracker.{k}": v for k, v in update.items()})


async def record_invite_join(guild_id: int, user_id: int, inviter_id, code: str,
                             kind: str) -> None:
    """One record per (guild, joiner): a rejoin overwrites the earlier record."""
    await invite_joins_col().update_one(
        {"guild_id": str(guild_id), "user_id": str(user_id)},
        {"$set": {
            "inviter_id": str(inviter_id) if inviter_id else None,
            "code": code, "kind": kind, "active": True,
            "joined_at": datetime.now(timezone.utc), "left_at": None,
        }},
        upsert=True,
    )


async def mark_invite_left(guild_id: int, user_id: int) -> None:
    await invite_joins_col().update_one(
        {"guild_id": str(guild_id), "user_id": str(user_id)},
        {"$set": {"active": False, "left_at": datetime.now(timezone.utc)}},
    )


async def get_invite_stats(guild_id: int, inviter_id: int) -> dict:
    base = {"guild_id": str(guild_id), "inviter_id": str(inviter_id)}
    total = await invite_joins_col().count_documents(base)
    active = await invite_joins_col().count_documents({**base, "active": True})
    return {"total": total, "active": active, "left": total - active}


async def get_invited_by(guild_id: int, user_id: int) -> dict:
    return await invite_joins_col().find_one(
        {"guild_id": str(guild_id), "user_id": str(user_id)})


async def top_inviters(guild_id: int, limit: int = 10) -> list:
    pipeline = [
        {"$match": {"guild_id": str(guild_id), "inviter_id": {"$ne": None}}},
        {"$group": {"_id": "$inviter_id", "total": {"$sum": 1},
                    "active": {"$sum": {"$cond": ["$active", 1, 0]}}}},
        {"$sort": {"active": -1, "total": -1}},
        {"$limit": int(limit)},
    ]
    return [doc async for doc in invite_joins_col().aggregate(pipeline)]
