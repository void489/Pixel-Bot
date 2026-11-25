"""
Leveling - Arcane / MEE6 style XP, levels, rank cards and reward roles.

Everything is configured on the dashboard's "Leveling" page (guild_settings
["leveling"], parsed by utils/leveling_core.normalize_config). Leveling is OFF
until you switch it on there.

  * Message XP   - random xp_min..xp_max per message, once per cooldown per user,
                   x any role / channel / event multipliers.
  * Voice XP     - a fixed amount per minute in voice channels. By default muted /
                   deafened time and sitting alone earn nothing; AFK channel never does.
  * Levels       - only total XP is stored; the level is derived from it
                   (5*L^2 + 50*L + 100 XP per level), so /xp set can't desync.
  * Reward roles - level -> role, "stack" (keep all) or "replace" (only the highest).
                   Re-applied when a member re-joins and kept in sync by /xp commands.
  * Level-up     - announced in the same channel / a set channel / DM / nowhere, as
                   a card image + text line, text only, or an embed (old -> new level).

Commands:
    /rank [member]            rank card image            (everyone)
    /leaderboard              paginated top list         (everyone)
    /xp add|remove|set|setlevel|reset|resetall           (administrators only)

Storage ("levels" collection): {guild_id, user_id, xp, name, messages,
voice_seconds, created_at}; unique per (guild_id, user_id).
"""
import asyncio
import io
import ipaddress
import random
import socket
import time
from urllib.parse import urlparse
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from pymongo.errors import DuplicateKeyError

from database import models
from database.mongo import levels_col
from utils import leveling_core as core
from utils import rankcard
from utils.embeds import success_embed, error_embed, info_embed
from utils.permissions import command_check
from utils.spawner_math import parse_number

PAGE_SIZE = 10
EMBED_COLOR = 0x5865F2
_CFG_TTL = 30

_cfg_cache: dict = {}


async def get_config(guild_id: int) -> dict:
    now = time.monotonic()
    hit = _cfg_cache.get(guild_id)
    if hit and hit[0] > now:
        return hit[1]
    settings = await models.get_guild_settings(guild_id)
    cfg = core.normalize_config(settings.get("leveling"))
    _cfg_cache[guild_id] = (now + _CFG_TTL, cfg)
    return cfg


def _is_admin(ctx: commands.Context) -> bool:
    return bool(ctx.guild and ctx.author.guild_permissions.administrator)


async def _get_doc(guild_id, user_id) -> dict:
    return await levels_col().find_one({"guild_id": str(guild_id), "user_id": str(user_id)}) or {}


async def _rank_of(guild_id, xp: int) -> int:
    return await levels_col().count_documents(
        {"guild_id": str(guild_id), "xp": {"$gt": int(xp)}}) + 1


# ---------------------------------------------------------------------------
# Card background image (dashboard setting) - fetched safely and cached
# ---------------------------------------------------------------------------
_BG_MAX_BYTES = 5 * 1024 * 1024
_bg_cache: dict = {}  # url -> (expires_monotonic, bytes | None)


async def _host_is_public(host: str) -> bool:
    """False for anything that resolves to a private / local address."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)
    except (OSError, ValueError):
        return False


async def get_background(url: str):
    """Bytes of the configured background image, or None (never raises)."""
    if not url:
        return None
    now = time.monotonic()
    hit = _bg_cache.get(url)
    if hit and hit[0] > now:
        return hit[1]
    data = None
    try:
        host = urlparse(url).hostname
        if host and await _host_is_public(host):
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                # no redirects: a public URL must not be able to bounce us to an internal one
                async with http.get(url, allow_redirects=False) as r:
                    length = int(r.headers.get("Content-Length") or 0)
                    if r.status == 200 and length <= _BG_MAX_BYTES:
                        buf = bytearray()
                        async for chunk in r.content.iter_chunked(65536):
                            buf += chunk
                            if len(buf) > _BG_MAX_BYTES:
                                buf = None
                                break
                        data = bytes(buf) if buf else None
    except Exception:
        data = None
    if len(_bg_cache) > 50:
        _bg_cache.clear()
    _bg_cache[url] = (now + (600 if data else 60), data)
    return data


async def _avatar_bytes(member: discord.abc.User):
    try:
        return await member.display_avatar.replace(size=256, format="png").read()
    except (discord.HTTPException, discord.NotFound):
        return None


# ---------------------------------------------------------------------------
# Leaderboard view
# ---------------------------------------------------------------------------

async def build_leaderboard_embed(guild: discord.Guild, page: int):
    gid = str(guild.id)
    total = await levels_col().count_documents({"guild_id": gid, "xp": {"$gt": 0}})
    pages = max(1, -(-total // PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    rows = await levels_col().find({"guild_id": gid, "xp": {"$gt": 0}}) \
        .sort("xp", -1).skip(page * PAGE_SIZE).limit(PAGE_SIZE).to_list(PAGE_SIZE)

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for i, r in enumerate(rows, start=page * PAGE_SIZE + 1):
        level = core.level_from_xp(r["xp"])
        lines.append(f"{medals.get(i, f'`#{i}`')} <@{r['user_id']}> — **Level {level}** · {int(r['xp']):,} XP")

    embed = discord.Embed(
        title=f"🏆 {guild.name} Leaderboard",
        description="\n".join(lines) or "Nobody has earned any XP yet. Start chatting!",
        color=EMBED_COLOR,
    )
    embed.set_footer(text=f"Page {page + 1}/{pages} · {total} ranked")
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    return embed, page, pages


class LeaderboardView(discord.ui.View):
    def __init__(self, guild: discord.Guild, owner_id: int, page: int, pages: int):
        super().__init__(timeout=180)
        self.guild, self.owner_id, self.page, self.pages = guild, owner_id, page, pages
        self.message: Optional[discord.Message] = None
        self._sync()

    def _sync(self):
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.pages - 1
        self.page_btn.label = f"{self.page + 1}/{self.pages}"

    async def _go(self, interaction: discord.Interaction, delta: int):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message(
                "Run `/leaderboard` yourself to flip through pages.", ephemeral=True)
        embed, self.page, self.pages = await build_leaderboard_embed(self.guild, self.page + delta)
        self._sync()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, -1)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def page_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, +1)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ConfirmResetView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, reset everyone", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.defer()


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Leveling(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cooldowns: dict = {}   # (guild_id, user_id) -> monotonic time of last message xp

    async def cog_load(self):
        try:
            await levels_col().create_index([("guild_id", 1), ("user_id", 1)], unique=True)
            await levels_col().create_index([("guild_id", 1), ("xp", -1)])
        except Exception as e:
            print(f"[leveling] index creation failed: {e}")
        self.voice_loop.start()

    async def cog_unload(self):
        self.voice_loop.cancel()

    # ── Core: give xp, detect level-ups ───────────────────────────────────
    async def award(self, guild: discord.Guild, member: discord.Member, amount: int, cfg: dict,
                    *, source: str, channel=None) -> None:
        if amount <= 0:
            return
        inc = {"xp": int(amount)}
        if source == "message":
            inc["messages"] = 1
        else:
            inc["voice_seconds"] = 60
        flt = {"guild_id": str(guild.id), "user_id": str(member.id)}
        upd = {
            "$inc": inc,
            "$set": {"name": member.display_name[:80]},
            "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
        }
        doc = None
        for _ in range(2):  # a concurrent upsert can trip the unique index once
            try:
                doc = await levels_col().find_one_and_update(
                    flt, upd, upsert=True, return_document=True)
                break
            except DuplicateKeyError:
                continue
        if not doc:
            return

        new_xp = min(int(doc["xp"]), core.MAX_XP)
        old_level = core.level_from_xp(new_xp - amount)
        new_level = core.level_from_xp(new_xp)
        if new_level > old_level:
            await self._on_level_up(guild, member, new_level, cfg, channel,
                                    old_level=old_level, xp=new_xp)

    async def _on_level_up(self, guild, member, level: int, cfg: dict, channel=None,
                           old_level: int = None, xp: int = None):
        await self.sync_roles(member, level, cfg)
        mode = cfg["announce_mode"]
        if mode == "off":
            return
        if old_level is None:
            old_level = max(0, level - 1)
        if xp is None:
            xp = int((await _get_doc(guild.id, member.id)).get("xp", 0))

        _, into, need = core.progress(xp)
        maxed = level >= core.MAX_LEVEL
        text = core.render_announce(
            cfg["announce_message"], user=member.mention, name=member.display_name,
            old_level=old_level, level=level, xp_to_next=max(0, need - into), server=guild.name)

        # Rewards reached by this level-up (several if levels were skipped).
        unlocked = []
        for r in core.rewards_unlocked(cfg, old_level, level):
            role = guild.get_role(int(r["role_id"]))
            if role is not None:
                unlocked.append(role.name)
        extra = None
        if unlocked:
            extra = "Unlocked: " + ", ".join(unlocked)
        else:
            nxt = core.next_reward(cfg, level)
            role = guild.get_role(int(nxt["role_id"])) if nxt else None
            if role is not None:
                extra = f"Next reward: {role.name} at level {nxt['level']}"

        mentions = discord.AllowedMentions(users=[member], roles=False, everyone=False)
        kwargs = {}
        style = cfg["announce_style"]
        rank_pos = await _rank_of(guild.id, xp)

        if style == "card":
            try:
                png = await asyncio.to_thread(
                    rankcard.render_levelup, await _avatar_bytes(member), member.display_name,
                    old_level, level, rank_pos, into, need, accent=cfg["card_accent"],
                    bg_bytes=await get_background(cfg["card_background_url"]),
                    extra=extra, max_level=maxed)
                kwargs["file"] = discord.File(
                    io.BytesIO(png), filename="levelup.png",
                    description=f"{member.display_name} leveled up from level {old_level} to level {level}")
                kwargs["content"] = text
            except Exception as e:
                print(f"[leveling] level-up card failed in {guild.id}: {e}")
                style = "text"
        if style == "embed":
            embed = discord.Embed(title="⬆️ Level up!", description=text,
                                  color=int(cfg["card_accent"].lstrip("#"), 16))
            embed.add_field(name="Level", value=f"{old_level} → {level}")
            embed.add_field(name="Rank", value=f"#{rank_pos}")
            embed.add_field(name="To next level",
                            value="MAX" if maxed else f"{max(0, need - into):,} XP")
            if extra:
                embed.add_field(name="🎁 Rewards", value=extra, inline=False)
            embed.set_thumbnail(url=member.display_avatar.url)
            kwargs["embed"] = embed
        if style == "text":
            kwargs["content"] = text + (f"\n🎁 {extra}" if extra else "")

        try:
            if mode == "dm":
                await member.send(**kwargs)
                return
            target = None
            if mode == "current":
                target = channel
            if target is None and cfg["announce_channel_id"]:
                target = guild.get_channel(int(cfg["announce_channel_id"]))
            if target is not None:
                await target.send(allowed_mentions=mentions, **kwargs)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def sync_roles(self, member: discord.Member, level: int, cfg: dict) -> None:
        """Make the member's reward roles match their level (adds missing, removes
        ones above their level). Roles the bot can't manage are skipped."""
        should, all_ids = core.reward_roles_for(cfg, level)
        if not all_ids:
            return
        guild = member.guild
        have = {str(r.id) for r in member.roles}
        top = guild.me.top_role if guild.me else None

        def manageable(role):
            return role is not None and not role.managed and top is not None and role < top

        add = [r for rid in should if rid not in have
               and manageable(r := guild.get_role(int(rid)))]
        remove = [r for rid in all_ids if rid not in should and rid in have
                  and manageable(r := guild.get_role(int(rid)))]
        try:
            if add:
                await member.add_roles(*add, reason=f"Level {level} reward")
            if remove:
                await member.remove_roles(*remove, reason=f"Level {level} reward sync")
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[leveling] couldn't sync roles for {member.id} in {guild.id}: {e}")

    # ── Message XP ────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or message.webhook_id:
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return

        cfg = await get_config(message.guild.id)
        if not cfg["enabled"]:
            return

        channel = message.channel
        channel_ids = {str(channel.id)}
        parent_id = getattr(channel, "parent_id", None)
        if parent_id:
            channel_ids.add(str(parent_id))
        category_id = getattr(channel, "category_id", None)
        if category_id:
            channel_ids.add(str(category_id))
        if channel_ids & set(cfg["ignored_channel_ids"]):
            return
        role_ids = {str(r.id) for r in member.roles}
        if role_ids & set(cfg["ignored_role_ids"]):
            return
        if len(message.content.strip()) < cfg["min_length"]:
            return

        now = time.monotonic()
        key = (message.guild.id, member.id)
        last = self._cooldowns.get(key)
        if cfg["cooldown_seconds"] and last is not None and now - last < cfg["cooldown_seconds"]:
            return
        if len(self._cooldowns) > 20000:
            self._cooldowns.clear()
        self._cooldowns[key] = now

        base = random.randint(cfg["xp_min"], cfg["xp_max"])
        amount = int(round(base * core.multiplier_for(cfg, role_ids, channel_ids)))
        await self.award(message.guild, member, amount, cfg, source="message", channel=channel)

    # ── Voice XP ──────────────────────────────────────────────────────────
    @tasks.loop(seconds=60)
    async def voice_loop(self):
        try:
            for guild in self.bot.guilds:
                cfg = await get_config(guild.id)
                if not cfg["enabled"] or not cfg["voice_enabled"] or cfg["voice_xp_per_minute"] <= 0:
                    continue
                ignored_channels = set(cfg["ignored_channel_ids"])
                ignored_roles = set(cfg["ignored_role_ids"])
                afk_id = guild.afk_channel.id if guild.afk_channel else None

                for vc in list(guild.voice_channels) + list(guild.stage_channels):
                    if vc.id == afk_id:
                        continue
                    ch_ids = {str(vc.id)}
                    if vc.category_id:
                        ch_ids.add(str(vc.category_id))
                    if ch_ids & ignored_channels:
                        continue

                    humans = [m for m in vc.members if not m.bot]
                    if cfg["voice_require_company"] and len(humans) < 2:
                        continue

                    for m in humans:
                        vs = m.voice
                        if cfg["voice_ignore_muted"] and vs and (
                                vs.self_mute or vs.mute or vs.self_deaf or vs.deaf):
                            continue
                        role_ids = {str(r.id) for r in m.roles}
                        if role_ids & ignored_roles:
                            continue
                        amount = int(round(cfg["voice_xp_per_minute"]
                                           * core.multiplier_for(cfg, role_ids, ch_ids)))
                        await self.award(guild, m, amount, cfg, source="voice", channel=vc)
        except Exception as e:
            print(f"[leveling] voice loop error: {e}")

    @voice_loop.before_loop
    async def _before_voice(self):
        await self.bot.wait_until_ready()

    # ── Re-apply reward roles when someone re-joins ───────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        try:
            cfg = await get_config(member.guild.id)
            if not cfg["enabled"] or not cfg["rewards"]:
                return
            doc = await _get_doc(member.guild.id, member.id)
            if doc.get("xp"):
                await self.sync_roles(member, core.level_from_xp(doc["xp"]), cfg)
        except Exception as e:
            print(f"[leveling] member join sync failed: {e}")

    # ── /rank ─────────────────────────────────────────────────────────────
    @commands.hybrid_command(name="rank", description="Show your (or someone's) rank card")
    @app_commands.describe(member="Whose rank card to show (default: you)")
    @command_check()
    async def rank(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        if ctx.guild is None:
            return await ctx.send("This command only works in a server.")
        await ctx.defer()
        cfg = await get_config(ctx.guild.id)
        if not cfg["enabled"]:
            return await ctx.send(embed=error_embed("Leveling isn't enabled on this server yet."))

        target = member or ctx.author
        if target.bot:
            return await ctx.send(embed=error_embed("Bots don't earn XP."))
        doc = await _get_doc(ctx.guild.id, target.id)
        xp = int(doc.get("xp", 0))
        level, into, need = core.progress(xp)
        rank_pos = await _rank_of(ctx.guild.id, xp)
        maxed = level >= core.MAX_LEVEL

        # Next reward role, if any are set up above this level.
        reward_text = None
        nxt = core.next_reward(cfg, level)
        role = ctx.guild.get_role(int(nxt["role_id"])) if nxt else None
        if role is not None:
            away = nxt["level"] - level
            reward_text = (f"Next reward: {role.name} at level {nxt['level']} "
                           f"({away} level{'s' if away != 1 else ''} away)")

        # XP boost that applies to this member here (roles x this channel x event).
        chan = ctx.channel
        chan_ids = {str(chan.id)}
        for attr in ("parent_id", "category_id"):
            if getattr(chan, attr, None):
                chan_ids.add(str(getattr(chan, attr)))
        mult = core.multiplier_for(cfg, [r.id for r in target.roles], chan_ids)
        boost = f"{mult:g}x XP" if abs(mult - 1.0) > 0.001 else None

        png = await asyncio.to_thread(
            rankcard.render, await _avatar_bytes(target), target.display_name, level, rank_pos,
            into, need, xp, accent=cfg["card_accent"],
            bg_bytes=await get_background(cfg["card_background_url"]),
            next_reward=reward_text, boost=boost, max_level=maxed)

        left = max(0, need - into)
        summary = (f"**{target.display_name}** · Level {level} · Rank #{rank_pos}\n"
                   + ("MAX LEVEL" if maxed else f"XP: {into:,} / {need:,} · {left:,} until level {level + 1}")
                   + f" · {xp:,} total")
        # Image only: the summary goes in the file's alt text, not the message body.
        await ctx.send(
            file=discord.File(io.BytesIO(png), filename="rank.png", description=summary.replace("**", "")),
            allowed_mentions=discord.AllowedMentions.none())

    # ── /leaderboard ──────────────────────────────────────────────────────
    @commands.hybrid_command(name="leaderboard", description="Show the server XP leaderboard")
    @command_check()
    async def leaderboard(self, ctx: commands.Context):
        if ctx.guild is None:
            return await ctx.send("This command only works in a server.")
        await ctx.defer()
        cfg = await get_config(ctx.guild.id)
        if not cfg["enabled"]:
            return await ctx.send(embed=error_embed("Leveling isn't enabled on this server yet."))
        embed, page, pages = await build_leaderboard_embed(ctx.guild, 0)
        view = LeaderboardView(ctx.guild, ctx.author.id, page, pages) if pages > 1 else None
        msg = await ctx.send(embed=embed, view=view)
        if view:
            view.message = msg

    # ── /xp (admin) ───────────────────────────────────────────────────────
    @commands.hybrid_group(name="xp", description="Manage member XP (admin only)")
    async def xp(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=info_embed(
                "Use `xp add / remove / set / setlevel / reset / resetall`."), ephemeral=True)

    async def _admin_guard(self, ctx) -> bool:
        if not _is_admin(ctx):
            await ctx.send(embed=error_embed("Only administrators can use this."), ephemeral=True)
            return False
        return True

    async def _apply_xp_change(self, ctx, member: discord.Member, new_xp: int, verb: str):
        new_xp = max(0, min(int(new_xp), core.MAX_XP))
        await levels_col().update_one(
            {"guild_id": str(ctx.guild.id), "user_id": str(member.id)},
            {"$set": {"xp": new_xp, "name": member.display_name[:80]},
             "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        level = core.level_from_xp(new_xp)
        cfg = await get_config(ctx.guild.id)
        await self.sync_roles(member, level, cfg)
        await ctx.send(embed=success_embed(
            f"{verb} {member.mention} — now **Level {level}** with **{new_xp:,} XP**."),
            ephemeral=True)

    @xp.command(name="add", description="Add XP to a member")
    @app_commands.describe(member="Who", amount="XP to add (e.g. 500, 2k)")
    @command_check()
    async def xp_add(self, ctx: commands.Context, member: discord.Member, amount: str):
        if not await self._admin_guard(ctx):
            return
        await ctx.defer(ephemeral=True)
        try:
            value = parse_number(amount)
        except ValueError as e:
            return await ctx.send(embed=error_embed(str(e)), ephemeral=True)
        current = int((await _get_doc(ctx.guild.id, member.id)).get("xp", 0))
        await self._apply_xp_change(ctx, member, current + value, "Added XP to")

    @xp.command(name="remove", description="Remove XP from a member")
    @app_commands.describe(member="Who", amount="XP to remove (e.g. 500, 2k)")
    @command_check()
    async def xp_remove(self, ctx: commands.Context, member: discord.Member, amount: str):
        if not await self._admin_guard(ctx):
            return
        await ctx.defer(ephemeral=True)
        try:
            value = parse_number(amount)
        except ValueError as e:
            return await ctx.send(embed=error_embed(str(e)), ephemeral=True)
        current = int((await _get_doc(ctx.guild.id, member.id)).get("xp", 0))
        await self._apply_xp_change(ctx, member, current - value, "Removed XP from")

    @xp.command(name="set", description="Set a member's total XP")
    @app_commands.describe(member="Who", amount="Total XP (e.g. 5000, 12k)")
    @command_check()
    async def xp_set(self, ctx: commands.Context, member: discord.Member, amount: str):
        if not await self._admin_guard(ctx):
            return
        await ctx.defer(ephemeral=True)
        try:
            value = 0 if amount.strip() == "0" else parse_number(amount)
        except ValueError as e:
            return await ctx.send(embed=error_embed(str(e)), ephemeral=True)
        await self._apply_xp_change(ctx, member, value, "Set XP for")

    @xp.command(name="setlevel", description="Set a member to the start of a level")
    @app_commands.describe(member="Who", level="Level to set (0-1000)")
    @command_check()
    async def xp_setlevel(self, ctx: commands.Context, member: discord.Member,
                          level: app_commands.Range[int, 0, core.MAX_LEVEL]):
        if not await self._admin_guard(ctx):
            return
        await ctx.defer(ephemeral=True)
        await self._apply_xp_change(ctx, member, core.total_xp_for_level(level), "Set level for")

    @xp.command(name="reset", description="Reset one member's XP to zero")
    @app_commands.describe(member="Who")
    @command_check()
    async def xp_reset(self, ctx: commands.Context, member: discord.Member):
        if not await self._admin_guard(ctx):
            return
        await ctx.defer(ephemeral=True)
        await levels_col().delete_one(
            {"guild_id": str(ctx.guild.id), "user_id": str(member.id)})
        cfg = await get_config(ctx.guild.id)
        await self.sync_roles(member, 0, cfg)
        await ctx.send(embed=success_embed(f"Reset {member.mention} to level 0."), ephemeral=True)

    @xp.command(name="resetall", description="Reset EVERYONE's XP (asks for confirmation)")
    @command_check()
    async def xp_resetall(self, ctx: commands.Context):
        if not await self._admin_guard(ctx):
            return
        count = await levels_col().count_documents({"guild_id": str(ctx.guild.id)})
        view = ConfirmResetView(ctx.author.id)
        msg = await ctx.send(embed=error_embed(
            f"This will wipe the XP of **{count}** member{'' if count == 1 else 's'} and "
            "can't be undone. Reward roles are not removed automatically.",
            title="⚠️ Reset everyone?"), view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            return await msg.edit(embed=info_embed("Cancelled. Nothing was changed."), view=None)
        await levels_col().delete_many({"guild_id": str(ctx.guild.id)})
        await msg.edit(embed=success_embed(f"Reset **{count}** member{'' if count == 1 else 's'}."),
                       view=None)


async def setup(bot: commands.Bot):
    await bot.add_cog(Leveling(bot))
