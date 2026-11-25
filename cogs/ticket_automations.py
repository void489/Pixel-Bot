"""Runs ticket automations (rules are made on the dashboard: Automations page).

    trigger   opened / keyword in a message / no staff reply for N minutes
    steps     set priority, rename, move to another panel, send a message, add a member

Rules of the road:
  * each automation runs ONCE per ticket (an atomic marker on the ticket record)
  * bots never trigger one, and no step can trigger another automation
  * a step that fails is skipped and logged; the others still run, and the ticket is never
    affected. Every run is logged (kept 30 days) for the dashboard.
  * only tickets that are open, in a text channel or a private thread
The logic lives in module-level functions so it can be tested without a running bot.
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from cogs import tickets as T
from database import models
from database.mongo import (
    ticket_automations_col, ticket_automation_runs_col, tickets_col, ticket_stats_col,
)
from utils import ticket_automation as ta
from utils import ticket_container as tcont
from utils import ticket_core
from utils import ticket_settings as ts
from utils import ticket_stats

_CACHE_SECONDS = 60
_cache: dict = {}                 # guild_id -> (loaded at, [enabled automations])
_runs_indexed = False
_SWEEP_MAX_PER_RULE = 25          # tickets handled per rule per minute
_SWEEP_MAX_AGE_DAYS = 7           # ignore tickets that have waited longer than this


async def enabled_automations(guild_id) -> list:
    """The server's enabled automations, cached for a minute (or until the dashboard saves)."""
    gid = str(guild_id)
    hit = _cache.get(gid)
    if hit and time.monotonic() - hit[0] < _CACHE_SECONDS and hit[0] >= ta.dirty_since(gid):
        return hit[1]
    docs = await ticket_automations_col().find({"guild_id": gid, "enabled": True}).to_list(ta.MAX_AUTOMATIONS)
    _cache[gid] = (time.monotonic(), docs)
    return docs


async def claim_run(channel_id, automation_id: str) -> bool:
    """True the first time this automation is claimed for this ticket, False afterwards.
    One atomic update, so two triggers at the same moment can't both run it."""
    aid, cid = str(automation_id), str(channel_id)
    doc = await tickets_col().find_one_and_update(
        {"channel_id": cid, "status": "open", "automations_run": {"$ne": aid}},
        {"$addToSet": {"automations_run": aid}})
    if doc is None:
        return False
    try:                          # lets the no-reply sweep skip it next time
        await ticket_stats_col().update_one({"_id": cid}, {"$addToSet": {"automations_run": aid}})
    except Exception:
        pass
    return True


async def _log_run(guild, channel, automation: dict, results: list, context: dict) -> None:
    global _runs_indexed
    try:
        col = ticket_automation_runs_col()
        if not _runs_indexed:
            _runs_indexed = True
            await col.create_index("at", expireAfterSeconds=30 * 86400)
            await col.create_index([("guild_id", 1), ("at", -1)])
        await col.insert_one({
            "guild_id": str(guild.id), "automation_id": str(automation["_id"]),
            "automation_name": automation.get("name"), "channel_id": str(channel.id),
            "channel_name": getattr(channel, "name", None), "trigger": automation["trigger"]["type"],
            "context": context or {}, "at": datetime.now(timezone.utc), "results": results,
            "ok": all(r["ok"] for r in results),
        })
    except Exception as e:
        print(f"[automations] couldn't log a run: {e}")


# ---------------------------------------------------------------------------
# Step handlers: (guild, channel, ticket_doc, step) -> (ok, note)
# ---------------------------------------------------------------------------

def _opener_name(guild, ticket_doc) -> str:
    member = guild.get_member(int(ticket_doc["opener_id"]))
    return (ticket_doc.get("opener_name") or (member.display_name if member else None)
            or str(ticket_doc["opener_id"]))


def _values(guild, channel, ticket_doc, for_name: bool) -> dict:
    """Placeholders. In a channel NAME {user} is the plain name, in a message it's a mention."""
    name = _opener_name(guild, ticket_doc)
    return {
        "user": name if for_name else f"<@{ticket_doc['opener_id']}>", "username": name,
        "server": guild.name, "channel": getattr(channel, "mention", ""),
        "panel": ticket_doc.get("panel_name") or "", "number": ts.format_number(ticket_doc.get("number")),
        "id": str(ticket_doc["opener_id"]),
        "priority": ts.priority_label(ticket_doc.get("priority")),
    }


async def _step_priority(guild, channel, ticket_doc, step):
    notes = await T._set_priority(channel, guild, ticket_doc, step["level"])
    ticket_doc["priority"] = step["level"]
    return True, " ".join(notes) or f"priority {step['level']}"


async def _step_rename(guild, channel, ticket_doc, step):
    name = T._name_slug(ts.render(step["template"], _values(guild, channel, ticket_doc, True)), 100)
    if not name:
        return False, "the name came out empty"
    if await T._rename_ok(channel, name, "Ticket automation"):
        return True, f"renamed to {name}"
    return False, "Discord didn't allow the rename (rate limit)"


async def _step_send_message(guild, channel, ticket_doc, step):
    text = ts.render(step["text"], _values(guild, channel, ticket_doc, False))[:2000]
    role = guild.get_role(int(step["role_id"])) if step.get("role_id") else None
    if step.get("role_id") and role is None:
        return False, "the role to ping no longer exists"
    await tcont.ensure_unarchived(channel)
    await channel.send(
        content=(f"{role.mention} " if role else "") + text,
        allowed_mentions=discord.AllowedMentions(
            everyone=False, roles=[role] if role else False,
            users=[discord.Object(id=int(ticket_doc["opener_id"]))]))
    return True, "message sent"


async def _step_add_member(guild, channel, ticket_doc, step):
    uid = int(step["user_id"])
    member = guild.get_member(uid)
    if member is None:
        try:
            member = await guild.fetch_member(uid)
        except discord.HTTPException:
            return False, "that user isn't in this server"
    await tcont.add_member(channel, member, "Ticket automation")
    return True, f"added {member}"


async def _step_move(guild, channel, ticket_doc, step):
    """Headless version of /ticket move: same permission sync and category choice, but no
    questions are asked, so the new welcome message shows "(no answer)". The old welcome
    message is left in place as a record of the original answers."""
    if tcont.is_thread(channel):
        return False, "thread tickets can't be moved"
    if not ticket_doc.get("ticket_type_id"):
        return False, "only panel tickets can be moved"
    new_type = await models.get_ticket_type(step["type_id"])
    if not new_type or str(new_type.get("guild_id")) != str(guild.id):
        return False, "the target panel no longer exists"
    if str(new_type["_id"]) == str(ticket_doc["ticket_type_id"]):
        return True, "already in that panel"
    opener = guild.get_member(int(ticket_doc["opener_id"]))
    if opener is None:
        return False, "the member who opened the ticket left"

    await T._sync_channel_perms(channel, guild, opener, new_type)
    old_category, moved, full = channel.category, False, None
    async with T.category_lock(guild.id):
        category = await T._resolve_category(guild, new_type)
        if category is None:
            category, overflow = await ticket_core.kind_category_plan(guild, None)
            owner = ("server", guild.id)
        else:
            overflow = ticket_core.clean_overflow_ids(new_type.get("overflow_category_ids"), category.id)
            owner = ("type", str(new_type["_id"]))
        category, all_full, tried = await ticket_core.first_with_room(guild, category, overflow, old_category, owner)
        if all_full:
            full = tried
        else:
            try:
                await channel.edit(category=category, reason=f"Ticket automation: moved to '{new_type['name']}'")
                moved = True
            except discord.HTTPException:
                pass
    if full is not None:
        await ticket_core.alert_staff_full(guild, full, "moved ticket")
        return False, "every category is full"
    if moved and old_category is not None and (category is None or category.id != old_category.id):
        await T._cleanup_empty_category(guild, old_category, {channel.id})

    await models.update_ticket(channel.id, ticket_type_id=str(new_type["_id"]), panel_name=new_type["name"])
    ticket_doc["ticket_type_id"], ticket_doc["panel_name"] = str(new_type["_id"]), new_type["name"]
    prio = ts.clean_priority(ticket_doc.get("priority")) or (
        "normal" if ts.settings_for(new_type)["priority_enabled"] else None)
    if prio and not ticket_doc.get("priority"):
        await models.update_ticket(channel.id, priority=prio)
        ticket_doc["priority"] = prio
    embed = T._build_welcome_embed(
        new_type, opener, [], guild=guild, channel=channel, number=ticket_doc.get("number"),
        linked_names=await T._linked_type_names(new_type), priority=prio)
    msg = await T._send_welcome(channel, T._ping_content(new_type, guild, opener), embed, new_type)
    await models.update_ticket(channel.id, welcome_message_id=str(msg.id))
    new_name = T._auto_rename_name(new_type, opener, [], ticket_doc.get("number"))
    if new_name:
        prefix = ts.priority_prefix(prio, ts.settings_for(new_type)["priority_rename"])
        if await T._rename_ok(channel, (prefix + new_name)[:100], f"Ticket automation: moved to '{new_type['name']}'"):
            await models.update_ticket(channel.id, priority_prefix=prefix)
    return True, f"moved to {new_type['name']}"


_STEPS = {
    "set_priority": _step_priority, "rename": _step_rename, "move": _step_move,
    "send_message": _step_send_message, "add_member": _step_add_member,
}


async def run_automation(guild, channel, ticket_doc: dict, automation: dict, context: dict = None) -> bool:
    """Run one automation on one ticket, once. False if it had already run for this ticket."""
    if not await claim_run(channel.id, str(automation["_id"])):
        return False
    results = []
    for step in (automation.get("steps") or [])[:ta.MAX_STEPS]:
        handler = _STEPS.get(step.get("type"))
        try:
            ok, note = await handler(guild, channel, ticket_doc, step) if handler else (False, "unknown step")
        except discord.Forbidden:
            ok, note = False, "I don't have permission to do that"
        except Exception as e:
            ok, note = False, f"error: {e}"[:200]
        results.append({"type": step.get("type"), "ok": bool(ok), "note": note})
    await _log_run(guild, channel, automation, results, context)
    return True


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------

async def on_ticket_opened(guild, channel, ticket_doc: dict, ticket_type: dict) -> None:
    """Called after a panel ticket is fully set up (welcome message sent). Never raises."""
    try:
        for a in await enabled_automations(guild.id):
            if a["trigger"]["type"] == "opened" and ta.applies_to_panel(a["trigger"], ticket_type["_id"]):
                await run_automation(guild, channel, ticket_doc, a, {"trigger": "opened"})
    except Exception as e:
        print(f"[automations] opened trigger failed: {e}")


async def on_ticket_message(message: discord.Message) -> None:
    """Keyword trigger. Cheap checks first, so ordinary messages cost a dict lookup."""
    info = ticket_stats.OPEN_TICKETS.get(message.channel.id)
    if info is None or message.guild is None or message.author.bot:
        return
    if message.type not in T._NORMAL_MESSAGE_TYPES or not message.content:
        return
    rules = [a for a in await enabled_automations(message.guild.id) if a["trigger"]["type"] == "keyword"]
    if not rules:
        return
    ticket_doc = None
    for a in rules:
        trig = a["trigger"]
        if not ta.applies_to_panel(trig, info["type_id"]):
            continue
        if trig.get("from", "opener") == "opener" and str(message.author.id) != info["opener_id"]:
            continue
        hit = ta.matches_keyword(message.content, trig.get("keywords"))
        if not hit:
            continue
        ticket_doc = ticket_doc or await models.get_ticket(message.channel.id)
        if not ticket_doc:
            ticket_stats.OPEN_TICKETS.pop(message.channel.id, None)
            return
        await run_automation(message.guild, message.channel, ticket_doc, a,
                             {"trigger": "keyword", "keyword": hit, "by": str(message.author.id)})


async def sweep_no_reply(guild) -> None:
    """"No staff reply after N minutes": tickets whose analytics record still has no first
    reply. Each ticket is handled once per rule (see claim_run / automations_run)."""
    rules = [a for a in await enabled_automations(guild.id) if a["trigger"]["type"] == "no_reply"]
    now = datetime.now(timezone.utc)
    for a in rules:
        trig = a["trigger"]
        query = {
            "guild_id": str(guild.id), "first_reply_at": None, "closed_at": None,
            "automations_run": {"$ne": str(a["_id"])},
            "opened_at": {"$lte": now - timedelta(minutes=trig["minutes"]),
                          "$gte": now - timedelta(days=_SWEEP_MAX_AGE_DAYS)},
        }
        if trig.get("panel_ids"):
            query["ticket_type_id"] = {"$in": [str(p) for p in trig["panel_ids"]]}
        docs = await ticket_stats_col().find(query, {"_id": 1}).limit(_SWEEP_MAX_PER_RULE).to_list(_SWEEP_MAX_PER_RULE)
        for d in docs:
            channel = await tcont.get_container(guild, d["_id"])
            ticket_doc = await models.get_ticket(d["_id"])
            if channel is None or not ticket_doc:
                # the ticket is gone: mark it done so it isn't picked up again every minute
                await ticket_stats_col().update_one(
                    {"_id": d["_id"]}, {"$addToSet": {"automations_run": str(a["_id"])}})
                continue
            await run_automation(guild, channel, ticket_doc, a, {"trigger": "no_reply", "minutes": trig["minutes"]})


class TicketAutomations(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self._open_load = asyncio.create_task(ticket_stats.load_open())      # in the background
        self._no_reply_sweep.start()

    async def cog_unload(self):
        self._no_reply_sweep.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            await on_ticket_message(message)
        except Exception as e:
            print(f"[automations] keyword trigger failed: {e}")

    @tasks.loop(seconds=60)
    async def _no_reply_sweep(self):
        for guild in list(self.bot.guilds):
            try:
                await sweep_no_reply(guild)
            except Exception as e:
                print(f"[automations] sweep failed for {guild.id}: {e}")

    @_no_reply_sweep.before_loop
    async def _before_sweep(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketAutomations(bot))
