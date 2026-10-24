"""
Taskbooks - one private thread per staff member showing their personal stats.

Setup (dashboard -> Taskbooks):
  * pick the channel the threads live in (e.g. #taskbooks)
  * pick the staff role(s) and the roles that are allowed to see the threads
  * press "Add taskbook" and choose a staff member

The bot then creates a private thread for that person, adds them, pings the
allowed roles (which adds those members) and posts a live embed:

    Tickets handled · Tickets renamed · Tickets closed · Blacklists · Sponsored amount

How each counter moves:
  * Tickets handled  - +1 when a staff member CLAIMS a ticket with the Claim
                       button, -1 if they unclaim it (see cogs/tickets.py).
  * Tickets renamed  - +1 to whoever does the FIRST rename of a ticket (once per
                       ticket), straight away - no other condition.
  * Tickets closed   - +1 to whoever closes the ticket, whether or not they
                       handled it.
  * Blacklists       - +1 to the staff member who SUBMITTED a `/blacklist add`
                       request when it is ACCEPTED (see cogs/utilities.py). Remove
                       (unblacklist) and denied requests don't count. Each request
                       is flagged `taskbook_counted` so it can never count twice.
                       The Taskbooks dashboard page has an "Import old blacklists"
                       button that counts accepted requests from before this
                       feature existed (run it once, before your first reset).
  * Sponsored        - manual, via `/taskbook add|remove <number> <amount>`
                       (supports k / m / b, e.g. 1.5m).

Admin-only commands:
    /taskbook reset <all | number>
    /taskbook add <number> <amount>
    /taskbook remove <number> <amount>

Deleting a taskbook on the dashboard deletes its thread too. The dashboard
talks to the bot through the `dashboard_tasks` queue (taskbook_create /
taskbook_delete), like the other tabs.

Other cogs call the module-level hooks lazily so a problem here can never
break ticket closing:
    record_ticket_rename(guild, channel, member)
    record_ticket_close(guild, closer)
    adjust_handled(guild, user_id, delta)
    record_blacklist(guild, req)
"""
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from pymongo.errors import DuplicateKeyError

from database import models
from database.mongo import (
    taskbooks_col, tickets_col, dashboard_tasks_col, guild_settings_col,
    blacklist_requests_col,
)
from utils.embeds import success_embed, error_embed, info_embed
from utils.permissions import command_check
from utils.spawner_math import parse_number, format_short, format_exact

CREATE_TASK = "taskbook_create"
DELETE_TASK = "taskbook_delete"
IMPORT_TASK = "taskbook_import_blacklists"
REFRESH_TASK = "taskbook_refresh_all"
RESET_TASK = "taskbook_reset"

# Every counter a reset puts back to 0 (blacklist `taskbook_counted` flags stay).
# Counters that also keep a lifetime total (life_<name>) which /taskbook reset never touches.
# Used by promotion suggestions so a reset doesn't wipe someone's progress.
LIFETIME_FIELDS = ("handled", "closed", "renamed", "blacklisted")


async def backfill_lifetime() -> int:
    """Taskbooks made before lifetime totals existed start with lifetime = their current
    counters. Runs at startup; only touches taskbooks that have no lifetime fields yet."""
    n = 0
    async for tb in taskbooks_col().find({"life_handled": {"$exists": False}}):
        await taskbooks_col().update_one({"_id": tb["_id"]}, {"$set": {
            "life_handled": int(tb.get("handled") or 0),
            "life_closed": int(tb.get("closed") or 0),
            "life_renamed": int(tb.get("renamed") or 0),
            "life_blacklisted": int(tb.get("blacklisted") or 0),
            "life_rating_sum": int(tb.get("rating_sum") or 0),
            "life_rating_count": int(tb.get("rating_count") or 0),
        }})
        n += 1
    return n


ZERO_STATS = {"handled": 0, "renamed": 0, "closed": 0, "blacklisted": 0, "sponsored": 0,
              "rating_sum": 0, "rating_count": 0}

EMBED_COLOR = 0x5865F2

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CFG_TTL = 30
_cfg_cache: dict = {}


async def get_config(guild_id: int) -> dict:
    now = time.monotonic()
    hit = _cfg_cache.get(guild_id)
    if hit and hit[0] > now:
        return hit[1]
    raw = (await models.get_guild_settings(guild_id)).get("taskbooks") or {}
    cfg = {
        "channel_id": str(raw["channel_id"]) if raw.get("channel_id") else None,
        "staff_role_ids": [str(r) for r in (raw.get("staff_role_ids") or [])],
        "allow_role_ids": [str(r) for r in (raw.get("allow_role_ids") or [])],
    }
    _cfg_cache[guild_id] = (now + _CFG_TTL, cfg)
    return cfg


# ---------------------------------------------------------------------------
# Embed
# ---------------------------------------------------------------------------

MIN_RATINGS = 3   # an average is only shown from this many ratings


def rating_text(tb: dict) -> str:
    """Average ticket rating for a taskbook doc, as embed field text."""
    n = int(tb.get("rating_count", 0) or 0)
    total = int(tb.get("rating_sum", 0) or 0)
    if n == 0:
        return "—\n-# No ratings yet"
    if n < MIN_RATINGS:
        return f"—\n-# {n} rating{'' if n == 1 else 's'} so far (average shows from {MIN_RATINGS})"
    return f"**{total / n:.2f}** / 5\n-# {n} ratings"


async def add_rating(guild: discord.Guild, user_id: int, rating: int) -> None:
    """A ticket rating came in for this staff member: update their taskbook (if they have one)."""
    try:
        tb = await taskbooks_col().find_one_and_update(
            {"guild_id": str(guild.id), "user_id": str(user_id)},
            {"$inc": {"rating_sum": int(rating), "rating_count": 1,
                      "life_rating_sum": int(rating), "life_rating_count": 1}},
            return_document=True,
        )
        if tb:
            await refresh_embed(guild, tb)
    except Exception as e:
        print(f"[taskbook] couldn't add rating for {user_id}: {e}")


def build_embed(guild: discord.Guild, tb: dict) -> discord.Embed:
    member = guild.get_member(int(tb["user_id"]))
    name = member.display_name if member else tb.get("user_name") or tb["user_id"]
    embed = discord.Embed(
        title=f"📒 Taskbook #{tb['number']} — {name}",
        description=f"<@{tb['user_id']}>'s personal staff stats.",
        color=EMBED_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="🎫 Tickets handled", value=f"**{int(tb.get('handled', 0)):,}**", inline=True)
    embed.add_field(name="✏️ Tickets renamed", value=f"**{int(tb.get('renamed', 0)):,}**", inline=True)
    embed.add_field(name="🔒 Tickets closed", value=f"**{int(tb.get('closed', 0)):,}**", inline=True)
    embed.add_field(name="🚫 Blacklists", value=f"**{int(tb.get('blacklisted', 0)):,}**", inline=True)
    sponsored = int(tb.get("sponsored", 0))
    value = f"**{format_short(sponsored)}**"
    if sponsored >= 1000:
        value += f"\n-# {format_exact(sponsored)}"
    embed.add_field(name="🎁 Sponsored amount", value=value, inline=True)
    embed.add_field(name="⭐ Ticket rating", value=rating_text(tb), inline=True)
    if member:
        embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"Taskbook #{tb['number']}")
    return embed


async def _get_thread(guild: discord.Guild, thread_id: int):
    thread = guild.get_thread(thread_id)
    if thread is not None:
        return thread
    try:
        ch = await guild.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return ch if isinstance(ch, discord.Thread) else None


async def refresh_embed(guild: discord.Guild, tb: dict) -> None:
    """Re-render the embed in the taskbook's thread. Silent if the thread is gone."""
    try:
        if not tb.get("thread_id") or not tb.get("message_id"):
            return
        thread = await _get_thread(guild, int(tb["thread_id"]))
        if thread is None:
            return
        if thread.archived:
            await thread.edit(archived=False)
        msg = await thread.fetch_message(int(tb["message_id"]))
        await msg.edit(embed=build_embed(guild, tb))
    except discord.NotFound:
        pass
    except discord.HTTPException as e:
        print(f"[taskbook] couldn't refresh #{tb.get('number')}: {e}")


# ---------------------------------------------------------------------------
# Create / delete
# ---------------------------------------------------------------------------

async def _next_number(guild_id: int) -> int:
    doc = await guild_settings_col().find_one_and_update(
        {"_id": str(guild_id)},
        {"$inc": {"taskbooks.next_number": 1}},
        upsert=True,
        return_document=True,
    )
    return int((doc.get("taskbooks") or {}).get("next_number") or 1)


async def create_taskbook(guild: discord.Guild, user_id: int) -> str:
    """Creates the thread + doc. Returns "" on success, otherwise a readable error."""
    cfg = await get_config(guild.id)
    if not cfg["channel_id"]:
        return "No taskbook channel is selected on the dashboard."
    channel = guild.get_channel(int(cfg["channel_id"]))
    if not isinstance(channel, discord.TextChannel):
        return "The selected taskbook channel doesn't exist any more."

    if await taskbooks_col().find_one({"guild_id": str(guild.id), "user_id": str(user_id)}):
        return "That person already has a taskbook."

    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.HTTPException):
            return "That person isn't in the server."

    number = await _next_number(guild.id)
    tb = {
        "guild_id": str(guild.id),
        "user_id": str(user_id),
        "user_name": member.display_name,
        "number": number,
        "channel_id": str(channel.id),
        "thread_id": None,
        "message_id": None,
        "handled": 0,
        "renamed": 0,
        "closed": 0,
        "blacklisted": 0,
        "sponsored": 0,
        "created_at": datetime.now(timezone.utc),
    }
    try:
        inserted = await taskbooks_col().insert_one(tb)
    except DuplicateKeyError:
        return "That person already has a taskbook."
    tb["_id"] = inserted.inserted_id

    try:
        thread = await channel.create_thread(
            name=f"taskbook-{number}-{member.display_name}"[:100],
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=10080,
            reason=f"Taskbook #{number} for {member}",
        )
        await thread.add_user(member)

        pings = [member.mention] + [f"<@&{r}>" for r in cfg["allow_role_ids"]
                                    if guild.get_role(int(r))]
        msg = await thread.send(
            content=" ".join(pings),
            embed=build_embed(guild, tb),
            allowed_mentions=discord.AllowedMentions(users=True, roles=True),
        )
        try:
            await msg.pin()
        except discord.HTTPException:
            pass
    except (discord.Forbidden, discord.HTTPException) as e:
        await taskbooks_col().delete_one({"_id": tb["_id"]})
        try:
            if "thread" in locals():
                await thread.delete()
        except discord.HTTPException:
            pass
        return (f"I couldn't create the thread: {e}. I need View Channel, Create Private "
                f"Threads, Send Messages in Threads and Manage Threads in {channel.mention}.")

    await taskbooks_col().update_one(
        {"_id": tb["_id"]},
        {"$set": {"thread_id": str(thread.id), "message_id": str(msg.id)}},
    )
    return ""


async def delete_thread(guild: discord.Guild, thread_id: int) -> None:
    thread = await _get_thread(guild, thread_id)
    if thread is None:
        return
    try:
        await thread.delete(reason="Taskbook deleted on the dashboard")
    except discord.HTTPException as e:
        print(f"[taskbook] couldn't delete thread {thread_id}: {e}")


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

async def _bump(guild: discord.Guild, user_id, field: str, delta: int = 1) -> None:
    """Add `delta` to one counter of the user's taskbook (if they have one) and
    refresh the embed. Counters never go below zero."""
    flt = {"guild_id": str(guild.id), "user_id": str(user_id)}
    if delta < 0:
        flt[field] = {"$gte": -delta}
    inc = {field: delta}
    if field in LIFETIME_FIELDS:
        inc[f"life_{field}"] = delta     # lifetime totals survive /taskbook reset
    tb = await taskbooks_col().find_one_and_update(
        flt, {"$inc": inc}, return_document=True)
    if tb:
        await refresh_embed(guild, tb)


async def adjust_handled(guild: discord.Guild, user_id, delta: int) -> None:
    """Called when a ticket is claimed (+1) or unclaimed (-1)."""
    try:
        await _bump(guild, user_id, "handled", delta)
    except Exception as e:
        print(f"[taskbook] adjust_handled failed: {e}")


async def record_ticket_rename(guild: discord.Guild, channel, member) -> None:
    """+1 renamed for whoever does the FIRST rename of a ticket. The flag lives on
    the ticket itself, so later renames never count and it can't double-count."""
    try:
        res = await tickets_col().update_one(
            {"channel_id": str(channel.id), "status": "open", "rename_counted": {"$ne": True}},
            {"$set": {"rename_counted": True}},
        )
        if res.modified_count:
            await _bump(guild, member.id, "renamed", 1)
    except Exception as e:
        print(f"[taskbook] record_ticket_rename failed: {e}")


async def record_ticket_close(guild: discord.Guild, closer) -> None:
    """+1 closed for whoever closed the ticket."""
    try:
        if closer is not None:
            await _bump(guild, closer.id, "closed", 1)
    except Exception as e:
        print(f"[taskbook] record_ticket_close failed: {e}")


async def record_blacklist(guild: discord.Guild, req: dict) -> None:
    """+1 blacklists for the staff member who SUBMITTED an accepted `/blacklist add`
    request. If they have no taskbook the request is left unflagged, so the
    dashboard import can still count it once they get one."""
    try:
        if req.get("action") != "add" or not req.get("applicant_id"):
            return
        applicant_id = str(req["applicant_id"])
        if not await taskbooks_col().find_one(
                {"guild_id": str(guild.id), "user_id": applicant_id}, {"_id": 1}):
            return
        claimed = await blacklist_requests_col().update_one(
            {"_id": req["_id"], "taskbook_counted": {"$ne": True}},
            {"$set": {"taskbook_counted": True}},
        )
        if claimed.modified_count:
            await _bump(guild, applicant_id, "blacklisted", 1)
    except Exception as e:
        print(f"[taskbook] record_blacklist failed: {e}")


async def reset_taskbooks(guild: discord.Guild, number=None) -> int:
    """Zero one taskbook (by number) or all of them, and redraw the embeds.
    Returns how many were reset."""
    flt = {"guild_id": str(guild.id)}
    if number is not None:
        flt["number"] = int(number)
    docs = await taskbooks_col().find(flt).to_list(None)
    if not docs:
        return 0
    await taskbooks_col().update_many(flt, {"$set": dict(ZERO_STATS)})
    for d in docs:
        d.update(ZERO_STATS)
        await refresh_embed(guild, d)
    return len(docs)


async def refresh_all(guild: discord.Guild) -> int:
    """Redraw every taskbook embed from the stored numbers (changes nothing)."""
    docs = await taskbooks_col().find({"guild_id": str(guild.id)}).to_list(None)
    for d in docs:
        await refresh_embed(guild, d)
    return len(docs)


async def import_blacklists(guild: discord.Guild) -> str:
    """Count old ACCEPTED `add` requests that were never counted. Returns a
    readable summary. Safe to run again: counted requests are flagged."""
    gid = str(guild.id)
    owners = {tb["user_id"] async for tb in taskbooks_col().find(
        {"guild_id": gid}, {"user_id": 1})}
    per_user: dict = {}
    skipped = 0
    cursor = blacklist_requests_col().find({
        "guild_id": gid, "status": "Accepted", "action": "add",
        "taskbook_counted": {"$ne": True},
    })
    async for req in cursor:
        uid = str(req.get("applicant_id") or "")
        if uid not in owners:
            skipped += 1
            continue
        claimed = await blacklist_requests_col().update_one(
            {"_id": req["_id"], "taskbook_counted": {"$ne": True}},
            {"$set": {"taskbook_counted": True}},
        )
        if claimed.modified_count:
            per_user[uid] = per_user.get(uid, 0) + 1

    for uid, n in per_user.items():
        tb = await taskbooks_col().find_one_and_update(
            {"guild_id": gid, "user_id": uid},
            {"$inc": {"blacklisted": n}}, return_document=True)
        if tb:
            await refresh_embed(guild, tb)

    total = sum(per_user.values())
    msg = (f"Counted {total} old blacklist{'' if total == 1 else 's'} "
           f"across {len(per_user)} taskbook{'' if len(per_user) == 1 else 's'}.")
    if skipped:
        msg += (f" {skipped} skipped because the person who submitted "
                f"{'it has' if skipped == 1 else 'them have'} no taskbook.")
    return msg


# ---------------------------------------------------------------------------
# Amount parsing
# ---------------------------------------------------------------------------

def _admin_only(ctx: commands.Context) -> bool:
    return bool(ctx.guild and ctx.author.guild_permissions.administrator)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Taskbook(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        try:
            await taskbooks_col().create_index([("guild_id", 1), ("number", 1)], unique=True)
            await taskbooks_col().create_index([("guild_id", 1), ("user_id", 1)], unique=True)
        except Exception as e:
            print(f"[taskbook] index creation failed: {e}")
        try:
            done = await backfill_lifetime()
            if done:
                print(f"[taskbook] lifetime totals started for {done} existing taskbook(s)")
        except Exception as e:
            print(f"[taskbook] lifetime backfill failed: {e}")
        self._poll_dashboard_tasks.start()

    async def cog_unload(self):
        self._poll_dashboard_tasks.cancel()

    # ── Dashboard task poller ─────────────────────────────────────────────
    @tasks.loop(seconds=3)
    async def _poll_dashboard_tasks(self):
        try:
            batch = await models.take_pending_dashboard_tasks(
                limit=10, types=[CREATE_TASK, DELETE_TASK, IMPORT_TASK,
                                     REFRESH_TASK, RESET_TASK])
            for t in batch:
                error = ""
                result = ""
                try:
                    guild = self.bot.get_guild(int(t["guild_id"]))
                    payload = t.get("payload") or {}
                    if guild is None:
                        error = "The bot isn't in that server."
                    elif t["type"] == CREATE_TASK:
                        error = await create_taskbook(guild, int(payload["user_id"]))
                    elif t["type"] == IMPORT_TASK:
                        result = await import_blacklists(guild)
                    elif t["type"] == REFRESH_TASK:
                        n = await refresh_all(guild)
                        result = f"Refreshed {n} taskbook{'' if n == 1 else 's'}."
                    elif t["type"] == RESET_TASK:
                        target = str(payload.get("target", "")).lower()
                        n = await reset_taskbooks(
                            guild, None if target == "all" else int(target))
                        result = (f"Reset {n} taskbook{'' if n == 1 else 's'}."
                                  if n else "No matching taskbook to reset.")
                    else:
                        await delete_thread(guild, int(payload["thread_id"]))
                except Exception as e:
                    error = f"Unexpected error: {e}"
                    print(f"[taskbook] dashboard task {t.get('_id')} failed: {e}")
                if error or result:
                    await dashboard_tasks_col().update_one(
                        {"_id": t["_id"]}, {"$set": {"error": error, "result": result}})
        except Exception as e:
            print(f"[taskbook] dashboard poller error: {e}")

    @_poll_dashboard_tasks.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    # ── Listeners ─────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent):
        """Thread deleted by hand -> forget the taskbook so the dashboard stays in sync."""
        try:
            await taskbooks_col().delete_one(
                {"guild_id": str(payload.guild_id), "thread_id": str(payload.thread_id)})
        except Exception:
            pass

    # ── Commands ──────────────────────────────────────────────────────────
    async def _number_autocomplete(self, interaction: discord.Interaction, current: str):
        if interaction.guild is None:
            return []
        docs = await taskbooks_col().find({"guild_id": str(interaction.guild.id)}) \
            .sort("number", 1).to_list(50)
        wanted = (current or "").lower().lstrip("#")
        out = []
        for d in docs:
            label = f"#{d['number']} — {d.get('user_name') or d['user_id']}"
            if wanted in label.lower():
                out.append((d["number"], label[:100]))
        return out[:25]

    @commands.hybrid_group(name="taskbook", description="Manage staff taskbooks (admin only)")
    async def taskbook(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=info_embed(
                "Use `taskbook reset <all|number>`, `taskbook add <number> <amount>` "
                "or `taskbook remove <number> <amount>`."
            ), ephemeral=True)

    @taskbook.command(name="reset", description="Reset one taskbook's stats, or all of them")
    @app_commands.describe(target="'all' or a taskbook number (shown on the dashboard)")
    @command_check()
    async def taskbook_reset(self, ctx: commands.Context, target: str):
        if not _admin_only(ctx):
            return await ctx.send(embed=error_embed("Only administrators can use this."), ephemeral=True)
        await ctx.defer(ephemeral=True)

        zero = dict(ZERO_STATS)
        t = target.strip().lstrip("#").lower()
        if t == "all":
            docs = await taskbooks_col().find({"guild_id": str(ctx.guild.id)}).to_list(None)
            await taskbooks_col().update_many({"guild_id": str(ctx.guild.id)}, {"$set": zero})
            for d in docs:
                d.update(zero)
                await refresh_embed(ctx.guild, d)
            return await ctx.send(embed=success_embed(
                f"Reset **{len(docs)}** taskbook{'' if len(docs) == 1 else 's'}."), ephemeral=True)

        if not t.isdigit():
            return await ctx.send(embed=error_embed(
                "Use `all` or a taskbook number, e.g. `3`."), ephemeral=True)
        tb = await taskbooks_col().find_one_and_update(
            {"guild_id": str(ctx.guild.id), "number": int(t)},
            {"$set": zero}, return_document=True)
        if not tb:
            return await ctx.send(embed=error_embed(f"There's no taskbook #{t}."), ephemeral=True)
        await refresh_embed(ctx.guild, tb)
        await ctx.send(embed=success_embed(f"Reset taskbook **#{tb['number']}**."), ephemeral=True)

    @taskbook_reset.autocomplete("target")
    async def _reset_autocomplete(self, interaction: discord.Interaction, current: str):
        choices = [app_commands.Choice(name="all — every taskbook", value="all")]
        for value, label in await self._number_autocomplete(interaction, current):
            choices.append(app_commands.Choice(name=label, value=str(value)))
        wanted = (current or "").lower()
        return [c for c in choices if wanted in c.name.lower() or c.value == "all"][:25]

    async def _change_sponsored(self, ctx: commands.Context, number: int, amount: str, sign: int):
        if not _admin_only(ctx):
            return await ctx.send(embed=error_embed("Only administrators can use this."), ephemeral=True)
        await ctx.defer(ephemeral=True)
        try:
            value = parse_number(amount)
        except ValueError as e:
            return await ctx.send(embed=error_embed(str(e)), ephemeral=True)

        tb = await taskbooks_col().find_one({"guild_id": str(ctx.guild.id), "number": int(number)})
        if not tb:
            return await ctx.send(embed=error_embed(f"There's no taskbook #{number}."), ephemeral=True)

        new_total = max(0, int(tb.get("sponsored", 0)) + sign * value)
        tb = await taskbooks_col().find_one_and_update(
            {"_id": tb["_id"]}, {"$set": {"sponsored": new_total}}, return_document=True)
        await refresh_embed(ctx.guild, tb)

        verb = "Added" if sign > 0 else "Removed"
        await ctx.send(embed=success_embed(
            f"{verb} **{format_short(value)}** {'to' if sign > 0 else 'from'} taskbook "
            f"**#{tb['number']}**. Sponsored total: **{format_short(new_total)}**."), ephemeral=True)

    @taskbook.command(name="add", description="Add to a taskbook's sponsored amount")
    @app_commands.describe(number="Taskbook number", amount="e.g. 500k, 1.5m, 2b")
    @command_check()
    async def taskbook_add(self, ctx: commands.Context, number: int, amount: str):
        await self._change_sponsored(ctx, number, amount, +1)

    @taskbook.command(name="remove", description="Remove from a taskbook's sponsored amount")
    @app_commands.describe(number="Taskbook number", amount="e.g. 500k, 1.5m, 2b")
    @command_check()
    async def taskbook_remove(self, ctx: commands.Context, number: int, amount: str):
        await self._change_sponsored(ctx, number, amount, -1)

    @taskbook_add.autocomplete("number")
    async def _add_number_autocomplete(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=l, value=v)
                for v, l in await self._number_autocomplete(interaction, current)]

    @taskbook_remove.autocomplete("number")
    async def _remove_number_autocomplete(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=l, value=v)
                for v, l in await self._number_autocomplete(interaction, current)]


async def setup(bot: commands.Bot):
    await bot.add_cog(Taskbook(bot))
