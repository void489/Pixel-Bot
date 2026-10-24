"""
Staff points - activity leaderboard.

What earns points (all values + the staff role(s) are set on the dashboard's
"Staff Points" page):

  * Message outside a ticket   -> small flat value
  * Message inside a ticket    -> small flat value
  * Moderation action          -> flat value (mute / warn / ban / kick /
                                  softban / purge; the undo actions don't count)
  * Blacklist                  -> flat value, paid to the staff member who
                                  submitted a /blacklist add request, once it's
                                  ACCEPTED. Unblacklist requests don't count, and
                                  you can't approve your own request for points.
  * Ticket handled             -> a bonus paid when the ticket closes, SPLIT
                                  between the staff who worked it in proportion
                                  to how many messages each sent. Nobody gets a
                                  share for clicking "close" - only for
                                  participating (min. messages, set on the
                                  dashboard).
  * Ticket renamed             -> paid once per ticket, at close, and only to
                                  whoever did the FIRST rename, and only if they
                                  also reached the minimum message count. Later
                                  renames never count. If the first renamer
                                  didn't actually work the ticket, nobody gets it.

Only members holding a configured staff role are tracked at all, so the
database stays small. Messages are subject to a per-channel cooldown so
spamming doesn't farm points or inflate a ticket share.

Storage: see utils/staff_points_core.py. Per-ticket counters live in
"staff_ticket_stats" until the ticket closes, at which point they're settled
and deleted.

/staffstats posts a public, arrow-paginated board that refreshes hourly.

Other cogs call the module-level hooks lazily (inside the function body) so a
problem here can never break ticket closing or moderation:
    record_ticket_rename(guild, channel, member)
    settle_ticket(guild, channel)
    record_mod_action(guild, case)
    record_blacklist(guild, req, decider_id)
"""
import time
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks
from pymongo.errors import DuplicateKeyError

from database import models
from database.mongo import (
    staff_points_col, staff_ticket_stats_col, staff_points_board_col,
    blacklist_requests_col,
)
from utils import staff_points_core as core
from utils.permissions import command_check

PAGE_SIZE = 6  # staff members per board page

# ---------------------------------------------------------------------------
# Config (cached so on_message doesn't hit Mongo for every message)
# ---------------------------------------------------------------------------
_CFG_TTL = 30  # seconds - dashboard changes apply within this long
_cfg_cache: dict = {}


async def get_config(guild_id: int) -> dict:
    now = time.monotonic()
    hit = _cfg_cache.get(guild_id)
    if hit and hit[0] > now:
        return hit[1]
    settings = await models.get_guild_settings(guild_id)
    cfg = core.normalize_config(settings.get("staff_points"))
    _cfg_cache[guild_id] = (now + _CFG_TTL, cfg)
    return cfg


def is_staff(member: discord.Member, cfg: dict) -> bool:
    staff_ids = set(cfg["staff_role_ids"])
    if not staff_ids:
        return False
    return any(str(r.id) in staff_ids for r in member.roles)


# ---------------------------------------------------------------------------
# Low-level writes
# ---------------------------------------------------------------------------

async def add_points(guild_id, user_id, event_type: str, points: float, count: int = 1):
    """$inc into this hour's bucket for (guild, user, type)."""
    if event_type not in core.EVENT_TYPES or count <= 0:
        return
    flt = {
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "type": event_type,
        "hour": core.hour_floor(datetime.now(timezone.utc)),
    }
    upd = {"$inc": {"count": int(count), "points": float(points)}}
    for _ in range(2):  # a concurrent upsert can trip the unique index once
        try:
            await staff_points_col().update_one(flt, upd, upsert=True)
            return
        except DuplicateKeyError:
            continue
        except Exception as e:
            print(f"[staff_points] add_points failed: {e}")
            return


async def _count_ticket_message(guild_id, channel_id, user_id):
    flt = {"channel_id": str(channel_id)}
    upd = {
        "$inc": {f"messages.{user_id}": 1},
        "$setOnInsert": {"guild_id": str(guild_id), "created_at": datetime.now(timezone.utc)},
    }
    for _ in range(2):
        try:
            await staff_ticket_stats_col().update_one(flt, upd, upsert=True)
            return
        except DuplicateKeyError:
            continue
        except Exception as e:
            print(f"[staff_points] ticket message count failed: {e}")
            return


# ---------------------------------------------------------------------------
# Hooks called by other cogs
# ---------------------------------------------------------------------------

async def record_ticket_rename(guild: discord.Guild, channel: discord.abc.GuildChannel,
                               member: discord.Member):
    """Remember who did the FIRST rename of this ticket. Anyone counts for
    claiming the slot (even a non-staff user) - later renames are ignored.
    Nothing is paid here; it's settled when the ticket closes."""
    try:
        col = staff_ticket_stats_col()
        cid = str(channel.id)
        for _ in range(2):
            res = await col.update_one(
                {"channel_id": cid, "first_rename_by": {"$exists": False}},
                {"$set": {"first_rename_by": str(member.id)}},
            )
            if res.matched_count:
                return
            try:
                await col.insert_one({
                    "channel_id": cid,
                    "guild_id": str(guild.id),
                    "created_at": datetime.now(timezone.utc),
                    "messages": {},
                    "first_rename_by": str(member.id),
                })
                return
            except DuplicateKeyError:
                continue  # doc exists now - retry the $set (no-op if already renamed)
    except Exception as e:
        print(f"[staff_points] record_ticket_rename failed: {e}")


async def settle_ticket(guild: discord.Guild, channel: discord.abc.GuildChannel):
    """Called once when a ticket is closed: splits the handled bonus between
    the staff who worked it and pays the rename points."""
    try:
        # Atomic claim, so a ticket can never be settled twice.
        doc = await staff_ticket_stats_col().find_one_and_delete({"channel_id": str(channel.id)})
        if not doc:
            return
        cfg = await get_config(guild.id)
        min_msgs = cfg["min_messages"]

        messages = {str(uid): int(n) for uid, n in (doc.get("messages") or {}).items()}
        eligible = {uid: n for uid, n in messages.items() if n >= min_msgs}
        if not eligible:
            return  # nobody actually worked it -> nothing is paid

        total = sum(eligible.values())
        for uid, n in eligible.items():
            share = round(cfg["ticket_handled"] * n / total, 2)
            await add_points(guild.id, uid, "ticket_handled", share)

        renamer = doc.get("first_rename_by")
        if renamer and renamer in eligible:
            await add_points(guild.id, renamer, "ticket_rename", cfg["ticket_rename"])
    except Exception as e:
        print(f"[staff_points] settle_ticket failed: {e}")


async def record_mod_action(guild: discord.Guild, case: dict):
    try:
        if case.get("action") not in core.COUNTED_MOD_ACTIONS:
            return
        mod_id = int(case["moderator_id"])
        if guild.me and mod_id == guild.me.id:
            return  # the bot's own automatic actions
        member = guild.get_member(mod_id)
        if member is None or member.bot:
            return
        cfg = await get_config(guild.id)
        if not is_staff(member, cfg):
            return
        await add_points(guild.id, mod_id, "mod_action", cfg["mod_action"])
    except Exception as e:
        print(f"[staff_points] record_mod_action failed: {e}")


async def record_blacklist(guild: discord.Guild, req: dict, decider_id: int):
    """Called when a blacklist request is ACCEPTED. Pays the staff member who
    submitted it (req['applicant_id']) - only for 'add' requests, only if they
    hold a staff role, and never when they approved their own request."""
    try:
        if req.get("action") != "add":
            return
        applicant_id = int(req["applicant_id"])
        if applicant_id == int(decider_id):
            return
        member = guild.get_member(applicant_id)
        if member is None or member.bot:
            return
        cfg = await get_config(guild.id)
        if not is_staff(member, cfg):
            return
        # Claim the request first so it can never be paid twice (also what
        # stops the dashboard's "import past blacklists" from re-paying it).
        claimed = await blacklist_requests_col().update_one(
            {"_id": req["_id"], "staff_points_paid": {"$ne": True}},
            {"$set": {"staff_points_paid": True}},
        )
        if claimed.modified_count == 0:
            return
        await add_points(guild.id, applicant_id, "blacklist", cfg["blacklist"])
    except Exception as e:
        print(f"[staff_points] record_blacklist failed: {e}")


# ---------------------------------------------------------------------------
# Ranking + board rendering
# ---------------------------------------------------------------------------

async def build_ranking(guild: discord.Guild) -> list:
    """[(user_id, windows), ...] for members who CURRENTLY hold a staff role,
    sorted by all-time points."""
    now = datetime.now(timezone.utc)
    rows = await staff_points_col().aggregate(
        core.leaderboard_pipeline(guild.id, now)
    ).to_list(None)
    folded = core.fold_rows(rows)

    cfg = await get_config(guild.id)
    staff_ids = set(cfg["staff_role_ids"])
    allowed = set()
    for uid in folded:
        member = guild.get_member(int(uid))
        if member and any(str(r.id) in staff_ids for r in member.roles):
            allowed.add(uid)
    return core.rank(folded, allowed)


def _format_member_block(rank: int, guild: discord.Guild, user_id: str, windows: dict) -> str:
    member = guild.get_member(int(user_id))
    name = member.display_name if member else f"Unknown ({user_id})"
    mention = member.mention if member else name

    points_line = " • ".join(core.fmt_points(windows[w]["points"]) for w in core.WINDOWS)
    lines = [f"**#{rank} {name}**", f"{mention} • **Points:** {points_line}"]
    for key in core.EVENT_TYPES:
        row = " • ".join(str(windows[w][key]) for w in core.WINDOWS)
        lines.append(f"**{core.EVENT_LABELS[key]}:** {row}")
    return "\n".join(lines)


def build_board_embed(guild: discord.Guild, ranking: list, cfg: dict, page: int,
                      bot_user: Optional[discord.ClientUser]):
    """Returns (embed, resolved_page, total_pages)."""
    total_pages = max(1, -(-len(ranking) // PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    chunk = ranking[start:start + PAGE_SIZE]

    legend = (
        "**Format:** All Time • 24h • 7d • 30d\n"
        f"**Scoring:** non-ticket msg `{core.fmt_points(cfg['msg_outside'])}` • "
        f"ticket msg `{core.fmt_points(cfg['msg_ticket'])}` • "
        f"rename `{core.fmt_points(cfg['ticket_rename'])}` • "
        f"ticket handled `{core.fmt_points(cfg['ticket_handled'])}` (split) • "
        f"mod action `{core.fmt_points(cfg['mod_action'])}` • "
        f"blacklist `{core.fmt_points(cfg['blacklist'])}`"
    )

    embed = discord.Embed(title=f"🏆 Staff Leaderboard (Page {page + 1}/{total_pages})", color=0x5865F2)
    if not cfg["staff_role_ids"]:
        embed.description = legend + "\n\nNo staff role is set yet. Choose one on the dashboard's **Staff Points** page."
    elif not chunk:
        embed.description = legend + "\n\nNo staff activity has been recorded yet."
    else:
        blocks = [legend] + [
            _format_member_block(start + i + 1, guild, uid, windows)
            for i, (uid, windows) in enumerate(chunk)
        ]
        embed.description = "\n\n".join(blocks)

    footer = "Updates hourly"
    if bot_user:
        footer = f"Made by {bot_user.name} • {footer}"
    embed.set_footer(text=footer)
    return embed, page, total_pages


class StaffBoardView(discord.ui.View):
    """Persistent pagination for the public board message. One instance is
    registered at startup so buttons keep working after a restart; every
    send/edit uses a fresh instance so the page label is per-message."""

    def __init__(self, page: int = 0, total_pages: int = 1):
        super().__init__(timeout=None)
        self.page_info_btn.label = f"Page {page + 1}/{total_pages}"

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary,
                       custom_id="staffpoints_board_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._paginate(interaction, -1)

    @discord.ui.button(label="Page 1/1", style=discord.ButtonStyle.secondary,
                       custom_id="staffpoints_board_pageinfo", disabled=True)
    async def page_info_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass  # display only

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary,
                       custom_id="staffpoints_board_next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._paginate(interaction, 1)

    async def _paginate(self, interaction: discord.Interaction, delta: int):
        if interaction.message is None:
            return await interaction.response.defer()

        board = await staff_points_board_col().find_one({"message_id": str(interaction.message.id)})
        guild = interaction.client.get_guild(int(board["guild_id"])) if board else None
        if not board or guild is None:
            return await interaction.response.defer()

        ranking = await build_ranking(guild)
        cfg = await get_config(guild.id)
        embed, page, total_pages = build_board_embed(
            guild, ranking, cfg, int(board.get("page", 0)) + delta, interaction.client.user
        )
        await staff_points_board_col().update_one({"_id": board["_id"]}, {"$set": {"page": page}})
        await interaction.response.edit_message(
            embed=embed, view=StaffBoardView(page=page, total_pages=total_pages)
        )


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

# (user_id, channel_id) -> monotonic time of the last counted message
_last_counted: dict = {}
# channel_id -> (is_ticket, expires_at)
_ticket_cache: dict = {}


async def _is_ticket(channel_id: int) -> bool:
    now = time.monotonic()
    hit = _ticket_cache.get(channel_id)
    if hit and hit[1] > now:
        return hit[0]
    is_ticket = (await models.get_ticket(channel_id)) is not None
    if len(_ticket_cache) > 5000:
        _ticket_cache.clear()
    # Open tickets stay tickets until deleted; non-tickets are re-checked soon
    # in case the channel is a brand-new ticket.
    _ticket_cache[channel_id] = (is_ticket, now + (3600 if is_ticket else 60))
    return is_ticket


class StaffPoints(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        try:
            await staff_points_col().create_index(
                [("guild_id", 1), ("user_id", 1), ("type", 1), ("hour", 1)], unique=True)
            await staff_points_col().create_index([("guild_id", 1), ("hour", 1)])
            await staff_ticket_stats_col().create_index("channel_id", unique=True)
        except Exception as e:
            print(f"[staff_points] index creation failed: {e}")
        self.bot.add_view(StaffBoardView())
        self.refresh_loop.start()

    def cog_unload(self):
        self.refresh_loop.cancel()

    # ── Message tracking ──────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        # Ticket threads count like ticket channels; other threads were never counted.
        in_thread = isinstance(message.channel, discord.Thread)
        if not (in_thread or isinstance(message.channel, discord.TextChannel)):
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return

        cfg = await get_config(message.guild.id)
        if not is_staff(member, cfg):
            return

        now = time.monotonic()
        key = (member.id, message.channel.id)
        last = _last_counted.get(key)
        if cfg["cooldown_seconds"] and last is not None and now - last < cfg["cooldown_seconds"]:
            return
        if len(_last_counted) > 10000:
            _last_counted.clear()
        _last_counted[key] = now

        if await _is_ticket(message.channel.id):
            await add_points(message.guild.id, member.id, "msg_ticket", cfg["msg_ticket"])
            await _count_ticket_message(message.guild.id, message.channel.id, member.id)
        elif not in_thread:
            await add_points(message.guild.id, member.id, "msg_outside", cfg["msg_outside"])

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        """A ticket deleted without going through the close flow is never
        settled - drop its leftover counters."""
        try:
            await staff_ticket_stats_col().delete_one({"channel_id": str(channel.id)})
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_thread_delete(self, thread):
        """Same for a thread ticket."""
        try:
            await staff_ticket_stats_col().delete_one({"channel_id": str(thread.id)})
        except Exception:
            pass

    # ── Hourly refresh of every tracked board message ─────────────────────
    @tasks.loop(hours=1)
    async def refresh_loop(self):
        async for board in staff_points_board_col().find({}):
            guild = self.bot.get_guild(int(board["guild_id"]))
            if not guild:
                continue
            channel = guild.get_channel(int(board["channel_id"]))
            if not channel:
                continue
            try:
                msg = await channel.fetch_message(int(board["message_id"]))
            except discord.NotFound:
                await staff_points_board_col().delete_one({"_id": board["_id"]})
                continue
            except discord.HTTPException as e:
                print(f"[staff_points] couldn't fetch board in guild {board['guild_id']}: {e}")
                continue

            ranking = await build_ranking(guild)
            cfg = await get_config(guild.id)
            embed, page, total_pages = build_board_embed(
                guild, ranking, cfg, int(board.get("page", 0)), self.bot.user
            )
            try:
                await msg.edit(embed=embed, view=StaffBoardView(page=page, total_pages=total_pages))
            except discord.HTTPException as e:
                print(f"[staff_points] couldn't refresh board in guild {board['guild_id']}: {e}")

    @refresh_loop.before_loop
    async def _before_refresh(self):
        await self.bot.wait_until_ready()

    # ── /staffstats ───────────────────────────────────────────────────────
    @commands.hybrid_command(
        name="staffstats",
        description="Post or refresh the public staff points leaderboard in this channel",
    )
    @command_check()
    async def staffstats(self, ctx: commands.Context):
        await ctx.defer()

        # One board per guild: remove the old one first.
        existing = await staff_points_board_col().find_one({"guild_id": str(ctx.guild.id)})
        if existing:
            old_channel = ctx.guild.get_channel(int(existing["channel_id"]))
            if old_channel:
                try:
                    old_msg = await old_channel.fetch_message(int(existing["message_id"]))
                    await old_msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
                except discord.HTTPException as e:
                    print(f"[staff_points] couldn't delete old board: {e}")

        ranking = await build_ranking(ctx.guild)
        cfg = await get_config(ctx.guild.id)
        embed, page, total_pages = build_board_embed(ctx.guild, ranking, cfg, 0, self.bot.user)

        msg = await ctx.send(embed=embed, view=StaffBoardView(page=page, total_pages=total_pages))
        await staff_points_board_col().update_one(
            {"guild_id": str(ctx.guild.id)},
            {"$set": {
                "guild_id": str(ctx.guild.id),
                "channel_id": str(ctx.channel.id),
                "message_id": str(msg.id),
                "page": 0,
            }},
            upsert=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(StaffPoints(bot))
