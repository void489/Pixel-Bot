"""
Invite tracker - who invited whom.

Member commands (hybrid, @command_check(); shown in /help -> Community):
    /invites [member]           -> how many people someone invited (still here / left / total)
                                   and who invited them
    /topinviters                -> the top 10 inviters by members still in the server

Admin commands (hybrid, @command_check(); shown in /admin help -> Roles & utilities):
    /invitetracker toggle <on|off>   -> start / stop tracking (off by default)
    /invitetracker channel [channel] -> post a line in this channel for every join (empty = none)
    /invitetracker status

How it works: the bot remembers every invite's use count. When someone joins it fetches the
invites again and sees which one went up by one (or which single-use invite just vanished).
That needs the Manage Server permission. If it can't tell (the server's vanity URL, a join
while the bot was offline, several joins at the same instant) the join is recorded as
"unknown" and nobody gets credit.

Each joiner has ONE record per server: if they leave and rejoin through someone else, the
newer inviter gets credit and the older one loses it. Members who left count under "left",
not "still here". Tracking starts when you turn it on - earlier joins aren't known.
"""
import asyncio
from collections import defaultdict

import discord
from discord.ext import commands

from config import constants
from database import models
from utils.embeds import base_embed, error_embed, info_embed, success_embed
from utils.permissions import command_check


async def _reply(ctx: commands.Context, embed: discord.Embed, *, delete_after: float = 8):
    if ctx.interaction is not None:
        return await ctx.send(embed=embed, ephemeral=True)
    return await ctx.send(embed=embed, delete_after=delete_after)


class InviteTracker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache = {}                       # guild_id -> {code: (uses, max_uses, inviter_id)}
        self._locks = defaultdict(asyncio.Lock)  # one join processed at a time per server

    # ------------------------------------------------------------------
    # Invite snapshots
    # ------------------------------------------------------------------
    async def _snapshot(self, guild: discord.Guild):
        """{code: (uses, max_uses, inviter_id)}, or None if the bot isn't allowed to look."""
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            return None
        return {i.code: (i.uses or 0, i.max_uses or 0, i.inviter.id if i.inviter else None)
                for i in invites}

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            try:
                cfg = await models.get_invite_tracker_config(guild.id)
                if cfg["enabled"]:
                    snap = await self._snapshot(guild)
                    if snap is not None:
                        self._cache[guild.id] = snap
            except Exception as e:
                print(f"[invites] startup snapshot failed for {guild.id}: {e}")

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if invite.guild is not None and invite.guild.id in self._cache:
            self._cache[invite.guild.id][invite.code] = (
                invite.uses or 0, invite.max_uses or 0,
                invite.inviter.id if invite.inviter else None)
    # (deleted invites are deliberately kept in the cache: a vanished single-use invite is how
    # a join through it is recognised)

    async def _find_inviter(self, guild: discord.Guild):
        """-> (inviter_id | None, code | None, kind) with kind = invite | vanity | unknown."""
        new = await self._snapshot(guild)
        old = self._cache.get(guild.id)
        if new is None:
            return None, None, "unknown"
        self._cache[guild.id] = new
        if old is None:
            return None, None, "unknown"

        used = sorted(
            ((code, uses - old.get(code, (0, 0, None))[0]) for code, (uses, _m, _i) in new.items()
             if uses > old.get(code, (0, 0, None))[0]),
            key=lambda x: x[1], reverse=True)
        if used:
            code = used[0][0]
            return new[code][2], code, "invite"

        # A single-use invite is deleted the moment it's used.
        gone = [c for c, (uses, mx, _i) in old.items() if c not in new and mx and uses + 1 >= mx]
        if len(gone) == 1:
            return old[gone[0]][2], gone[0], "invite"

        if getattr(guild, "vanity_url_code", None):
            return None, guild.vanity_url_code, "vanity"
        return None, None, "unknown"

    # ------------------------------------------------------------------
    # Joins / leaves
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        guild = member.guild
        try:
            cfg = await models.get_invite_tracker_config(guild.id)
            if not cfg["enabled"]:
                return
            async with self._locks[guild.id]:
                inviter_id, code, kind = await self._find_inviter(guild)
            await models.record_invite_join(guild.id, member.id, inviter_id, code, kind)
            if cfg["channel_id"]:
                await self._log_join(guild, cfg, member, inviter_id, code, kind)
        except Exception as e:
            print(f"[invites] join handling failed in {guild.id}: {type(e).__name__}: {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot:
            return
        try:
            cfg = await models.get_invite_tracker_config(member.guild.id)
            if cfg["enabled"]:
                await models.mark_invite_left(member.guild.id, member.id)
        except Exception as e:
            print(f"[invites] leave handling failed in {member.guild.id}: {e}")

    async def _log_join(self, guild, cfg, member, inviter_id, code, kind):
        channel = guild.get_channel(int(cfg["channel_id"]))
        if channel is None:
            return
        age = discord.utils.format_dt(member.created_at, "R")
        if kind == "invite" and inviter_id:
            stats = await models.get_invite_stats(guild.id, inviter_id)
            text = (f"{member.mention} joined, invited by <@{inviter_id}> "
                    f"(**{stats['active']}** still here). Invite `{code}`.")
        elif kind == "vanity":
            text = f"{member.mention} joined through the server's vanity URL."
        else:
            text = f"{member.mention} joined, but I couldn't tell which invite they used."
        embed = base_embed(title="📥 Member joined", description=f"{text}\nAccount created {age}.",
                           color=constants.COLOR_SUCCESS)
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ------------------------------------------------------------------
    # Member commands
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="invites", description="See how many people you (or someone) invited")
    @command_check()
    async def invites(self, ctx: commands.Context, member: discord.Member = None):
        cfg = await models.get_invite_tracker_config(ctx.guild.id)
        if not cfg["enabled"]:
            return await ctx.send(embed=error_embed("Invite tracking isn't turned on in this server."))
        target = member or ctx.author
        stats = await models.get_invite_stats(ctx.guild.id, target.id)
        rec = await models.get_invited_by(ctx.guild.id, target.id)
        if rec and rec.get("inviter_id"):
            by = f"<@{rec['inviter_id']}>"
        elif rec and rec.get("kind") == "vanity":
            by = "the vanity URL"
        else:
            by = "unknown"
        embed = base_embed(title=f"📨 Invites - {target.display_name}", color=constants.COLOR_INFO)
        embed.add_field(name="Still here", value=str(stats["active"]))
        embed.add_field(name="Left", value=str(stats["left"]))
        embed.add_field(name="Total", value=str(stats["total"]))
        embed.add_field(name="Invited by", value=by, inline=False)
        embed.set_thumbnail(url=target.display_avatar.url)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="topinviters", description="The members who invited the most people")
    @command_check()
    async def topinviters(self, ctx: commands.Context):
        cfg = await models.get_invite_tracker_config(ctx.guild.id)
        if not cfg["enabled"]:
            return await ctx.send(embed=error_embed("Invite tracking isn't turned on in this server."))
        rows = await models.top_inviters(ctx.guild.id, 10)
        if not rows:
            return await ctx.send(embed=info_embed("Nobody has invited anyone yet."))
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{r['_id']}> - **{r['active']}** still here "
            f"· {r['total']} total · {r['total'] - r['active']} left"
            for i, r in enumerate(rows)
        ]
        await ctx.send(embed=base_embed(title="🏆 Top inviters", description="\n".join(lines),
                                        color=constants.COLOR_INFO),
                       allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------------
    # Admin setup
    # ------------------------------------------------------------------
    @commands.hybrid_group(name="invitetracker", description="Invite tracker setup",
                           invoke_without_command=True)
    async def invitetracker(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await _reply(ctx, info_embed(
                "Use `/invitetracker toggle on`, `/invitetracker channel` or `/invitetracker status`."))

    @invitetracker.command(name="toggle", description="Start or stop tracking invites")
    @command_check()
    async def invitetracker_toggle(self, ctx: commands.Context, enabled: bool):
        if enabled:
            snap = await self._snapshot(ctx.guild)
            if snap is None:
                return await _reply(ctx, error_embed(
                    "I can't see this server's invites. Give me the **Manage Server** permission "
                    "and try again."))
            self._cache[ctx.guild.id] = snap
        else:
            self._cache.pop(ctx.guild.id, None)
        await models.set_invite_tracker_fields(ctx.guild.id, {"enabled": bool(enabled)})
        await _reply(ctx, success_embed(
            "Invite tracking is **on**. Joins from now on are recorded." if enabled
            else "Invite tracking is **off**. Existing records are kept."))

    @invitetracker.command(name="channel", description="Post a line in a channel for every join")
    @command_check()
    async def invitetracker_channel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        await models.set_invite_tracker_fields(
            ctx.guild.id, {"channel_id": str(channel.id) if channel else None})
        await _reply(ctx, success_embed(
            f"Joins are logged in {channel.mention}." if channel else "Join logging is off."))

    @invitetracker.command(name="status", description="Show the invite tracker setup")
    @command_check()
    async def invitetracker_status(self, ctx: commands.Context):
        cfg = await models.get_invite_tracker_config(ctx.guild.id)
        can_see = ctx.guild.me.guild_permissions.manage_guild
        records = await models.invite_joins_col().count_documents({"guild_id": str(ctx.guild.id)})
        embed = base_embed(title="📨 Invite tracker", color=constants.COLOR_INFO)
        embed.add_field(name="Tracking", value="✅ on" if cfg["enabled"] else "⬜ off")
        embed.add_field(name="Join log", value=f"<#{cfg['channel_id']}>" if cfg["channel_id"] else "off")
        embed.add_field(name="Manage Server", value="✅ yes" if can_see else "❌ missing")
        embed.add_field(name="Joins recorded", value=str(records))
        await _reply(ctx, embed, delete_after=20)


async def setup(bot: commands.Bot):
    await bot.add_cog(InviteTracker(bot))
