"""
Moderation system.

Staff commands (all hybrid, all @command_check()):
    /mute <user> <duration> [reason]     -> timeout the member
    /unmute <user> [reason]              -> remove the timeout
    /warn <user> [reason]                -> warn a member and log a case
    /warns <user>                        -> list a member's active warns
    /warnings remove <case_num>          -> remove one warn case by number
    /ban <user> [reason]                 -> ban the member
    /unban <user> [reason]               -> unban the member
    /kick <user> [reason]                -> kick the member
    /softban <user> [reason]             -> ban + immediate unban (purges recent msgs)
    /purge <amount> [user]               -> bulk-delete messages in the channel
    /lock [reason]                       -> lock the current channel (@everyone can't send)
    /unlock [reason]                     -> unlock the current channel
    /modlog                              -> show the configured mod log channel

Admin commands (listed under /admin help -> Moderation, NOT /staffhelp):
    /tempban <user> <duration> [reason]  -> ban, then unban automatically when time is up
    /tempbans                            -> list the active temp bans

Prefix forms use the guild's configured prefix (e.g. "!warn @user spam", "?ban @user").

Every action creates a per-guild case with a sequential case number (#1, #2, ...).
Cases are stored in mod_cases. Warns are soft-deletable via /warnings remove.

Mute durations accept: 30s, 10m, 2h, 7d. Discord caps timeouts at 28 days.
Tempban durations use the same format (up to 365 days). Expiry is checked every 30s from
the temp_bans collection, so it survives restarts. /unban cancels a pending expiry.
Purge requires Manage Messages; other commands rely on @command_check() plus
Discord's own permission enforcement (ban_members, kick_members, moderate_members,
manage_channels).

No hardcoded IDs. Mod log channel is set on the dashboard.
"""
import re
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands, tasks

from database import models
from utils.embeds import success_embed, error_embed, info_embed, base_embed
from utils.permissions import command_check
from config import constants


DURATION_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)
MAX_TIMEOUT_SECONDS = 28 * 24 * 60 * 60  # Discord cap
MAX_PURGE = 500
MAX_TEMPBAN_SECONDS = 365 * 24 * 60 * 60


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------

def parse_duration(text: str):
    """Return seconds or None. Accepts 30s, 10m, 2h, 7d, 1w."""
    if not text:
        return None
    m = DURATION_RE.match(text.strip())
    if not m:
        return None
    value = int(m.group(1))
    unit = m.group(2).lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return value * mult


def format_duration(seconds: int) -> str:
    if not seconds:
        return "0s"
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    return " ".join(parts[:2]) or "0s"


# ---------------------------------------------------------------------------
# Case logging
# ---------------------------------------------------------------------------

async def _log_case(bot, guild: discord.Guild, case: dict, color: int,
                    title: str):
    cfg = await models.get_moderation_config(guild.id)
    log_id = cfg.get("log_channel_id")
    if not log_id:
        return
    channel = guild.get_channel(int(log_id))
    if channel is None:
        return

    embed = base_embed(title=f"{title} · Case #{case['case_num']}", color=color)
    if case.get("target_id"):
        embed.add_field(name="Target", value=f"<@{case['target_id']}>\n`{case['target_id']}`", inline=False)
    embed.add_field(name="Moderator", value=f"<@{case['moderator_id']}>", inline=False)
    if case.get("duration_seconds"):
        embed.add_field(name="Duration", value=format_duration(case["duration_seconds"]), inline=False)
    embed.add_field(name="Reason", value=case.get("reason") or "*none*", inline=False)
    embed.set_footer(text=f"Case ID: {case['_id']}")

    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def _dm_target(target: discord.abc.User, guild: discord.Guild, title: str,
                     description: str, color: int):
    try:
        await target.send(embed=base_embed(title=title, description=description, color=color))
    except (discord.Forbidden, discord.HTTPException):
        pass


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self._expire_temp_bans.start()

    async def cog_unload(self):
        self._expire_temp_bans.cancel()

    # ------------------------------------------------------------------
    # Mute / unmute
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="mute", description="Timeout a member")
    @command_check()
    async def mute(self, ctx: commands.Context, member: discord.Member,
                   duration: str, *, reason: str = None):
        seconds = parse_duration(duration)
        if seconds is None:
            return await ctx.send(embed=error_embed(
                "Invalid duration. Use `30s`, `10m`, `2h`, `7d`, or `1w`."
            ))
        if seconds > MAX_TIMEOUT_SECONDS:
            return await ctx.send(embed=error_embed("Maximum timeout is 28 days."))
        if member.top_role >= ctx.guild.me.top_role:
            return await ctx.send(embed=error_embed(
                "I can't timeout that member — their top role is at or above mine."
            ))
        if member.id == ctx.author.id:
            return await ctx.send(embed=error_embed("You can't timeout yourself."))

        until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        try:
            await member.timeout(until, reason=reason or f"By {ctx.author}")
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("Missing permission to timeout that member."))
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed(f"Discord error: {e}"))

        case = await models.create_mod_case(
            ctx.guild.id, "mute", ctx.author.id, member.id,
            reason=reason, duration_seconds=seconds,
        )
        await _log_case(self.bot, ctx.guild, case, constants.COLOR_WARNING, "🔇 Muted")
        await _dm_target(member, ctx.guild,
                         f"🔇 You were muted in {ctx.guild.name}",
                         f"Duration: {format_duration(seconds)}\nReason: {reason or 'none'}",
                         constants.COLOR_WARNING)
        await ctx.send(embed=success_embed(
            f"Muted {member.mention} for **{format_duration(seconds)}**. Case `#{case['case_num']}`."
        ))

    @commands.hybrid_command(name="unmute", description="Remove a member's timeout")
    @command_check()
    async def unmute(self, ctx: commands.Context, member: discord.Member, *,
                     reason: str = None):
        try:
            await member.timeout(None, reason=reason or f"By {ctx.author}")
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("Missing permission to unmute that member."))
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed(f"Discord error: {e}"))

        case = await models.create_mod_case(
            ctx.guild.id, "unmute", ctx.author.id, member.id, reason=reason,
        )
        await _log_case(self.bot, ctx.guild, case, constants.COLOR_SUCCESS, "🔊 Unmuted")
        await ctx.send(embed=success_embed(
            f"Unmuted {member.mention}. Case `#{case['case_num']}`."
        ))

    # ------------------------------------------------------------------
    # Warn / warns / warnings remove
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="warn", description="Warn a member")
    @command_check()
    async def warn(self, ctx: commands.Context, member: discord.Member, *,
                   reason: str = None):
        case = await models.create_mod_case(
            ctx.guild.id, "warn", ctx.author.id, member.id, reason=reason,
        )
        await _log_case(self.bot, ctx.guild, case, constants.COLOR_WARNING, "⚠️ Warned")
        await _dm_target(member, ctx.guild,
                         f"⚠️ You were warned in {ctx.guild.name}",
                         f"Reason: {reason or 'none'}\nCase: `#{case['case_num']}`",
                         constants.COLOR_WARNING)
        total = await models.count_user_warns(ctx.guild.id, member.id)
        await ctx.send(embed=success_embed(
            f"Warned {member.mention}. Case `#{case['case_num']}`. "
            f"They now have **{total}** active warn(s)."
        ))

    @commands.hybrid_command(name="warns", description="List a member's warns")
    @command_check()
    async def warns(self, ctx: commands.Context, member: discord.Member):
        cases = await models.get_user_warn_cases(ctx.guild.id, member.id, limit=25)
        if not cases:
            return await ctx.send(embed=info_embed(f"{member.mention} has no active warns."))
        lines = []
        for c in cases:
            reason = (c.get("reason") or "").strip() or "*no reason*"
            when = c.get("created_at")
            if when is not None and when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            rel = discord.utils.format_dt(when, "R") if when else "unknown"
            lines.append(f"`#{c['case_num']}` — {reason[:80]} — {rel} — by <@{c['moderator_id']}>")
        await ctx.send(embed=base_embed(
            title=f"⚠️ Warns — {member}",
            description="\n".join(lines)[:4000],
        ))

    @commands.hybrid_group(name="warnings", description="Manage warnings", invoke_without_command=True)
    async def warnings(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=info_embed("Use `/warnings remove <case_num>`."))

    @warnings.command(name="remove", description="Remove a warn case by number")
    @command_check()
    async def warnings_remove(self, ctx: commands.Context, case_num: int):
        removed = await models.soft_delete_mod_case(ctx.guild.id, case_num, ctx.author.id)
        if not removed:
            return await ctx.send(embed=error_embed(
                f"No active case `#{case_num}` in this server."
            ))
        if removed.get("action") != "warn":
            # Put it back — we only remove warns with this command.
            await models.mod_cases_col().update_one(
                {"_id": removed["_id"]},
                {"$set": {"removed_at": None, "removed_by_id": None}},
            )
            return await ctx.send(embed=error_embed(
                f"Case `#{case_num}` is a **{removed.get('action')}** case, not a warn. "
                "Use the appropriate command to reverse it."
            ))
        await ctx.send(embed=success_embed(
            f"Removed warn case `#{case_num}` (target <@{removed['target_id']}>)."
        ))

    # ------------------------------------------------------------------
    # Ban / unban / kick / softban
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="ban", description="Ban a member")
    @command_check()
    async def ban(self, ctx: commands.Context, member: discord.Member, *,
                  reason: str = None, delete_days: int = 0):
        delete_days = max(0, min(int(delete_days), 7))
        if member.top_role >= ctx.guild.me.top_role:
            return await ctx.send(embed=error_embed(
                "I can't ban that member — their top role is at or above mine."
            ))
        if member.id == ctx.author.id:
            return await ctx.send(embed=error_embed("You can't ban yourself."))

        try:
            await member.ban(reason=reason or f"By {ctx.author}",
                             delete_message_days=delete_days)
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("Missing permission to ban that member."))
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed(f"Discord error: {e}"))

        case = await models.create_mod_case(
            ctx.guild.id, "ban", ctx.author.id, member.id, reason=reason,
            extra={"delete_days": delete_days},
        )
        await _log_case(self.bot, ctx.guild, case, constants.COLOR_ERROR, "🔨 Banned")
        await _dm_target(member, ctx.guild,
                         f"🔨 You were banned from {ctx.guild.name}",
                         f"Reason: {reason or 'none'}",
                         constants.COLOR_ERROR)
        await ctx.send(embed=success_embed(
            f"Banned {member} (`{member.id}`). Case `#{case['case_num']}`."
        ))

    @commands.hybrid_command(name="unban", description="Unban a user by ID")
    @command_check()
    async def unban(self, ctx: commands.Context, user_id: str, *, reason: str = None):
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return await ctx.send(embed=error_embed("Provide the user's numeric ID."))

        try:
            user = await self.bot.fetch_user(uid)
        except (discord.NotFound, discord.HTTPException):
            return await ctx.send(embed=error_embed("Couldn't find that user."))

        try:
            await ctx.guild.unban(user, reason=reason or f"By {ctx.author}")
        except discord.NotFound:
            return await ctx.send(embed=error_embed("That user isn't banned."))
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("Missing permission to unban."))
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed(f"Discord error: {e}"))

        await models.remove_temp_ban(ctx.guild.id, uid)  # cancel a pending tempban expiry
        case = await models.create_mod_case(
            ctx.guild.id, "unban", ctx.author.id, uid, reason=reason,
        )
        await _log_case(self.bot, ctx.guild, case, constants.COLOR_SUCCESS, "🔓 Unbanned")
        await ctx.send(embed=success_embed(
            f"Unbanned {user} (`{user.id}`). Case `#{case['case_num']}`."
        ))

    @commands.hybrid_command(name="kick", description="Kick a member")
    @command_check()
    async def kick(self, ctx: commands.Context, member: discord.Member, *,
                   reason: str = None):
        if member.top_role >= ctx.guild.me.top_role:
            return await ctx.send(embed=error_embed(
                "I can't kick that member — their top role is at or above mine."
            ))
        if member.id == ctx.author.id:
            return await ctx.send(embed=error_embed("You can't kick yourself."))

        try:
            await member.kick(reason=reason or f"By {ctx.author}")
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("Missing permission to kick that member."))
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed(f"Discord error: {e}"))

        case = await models.create_mod_case(
            ctx.guild.id, "kick", ctx.author.id, member.id, reason=reason,
        )
        await _log_case(self.bot, ctx.guild, case, constants.COLOR_WARNING, "👢 Kicked")
        await _dm_target(member, ctx.guild,
                         f"👢 You were kicked from {ctx.guild.name}",
                         f"Reason: {reason or 'none'}",
                         constants.COLOR_WARNING)
        await ctx.send(embed=success_embed(
            f"Kicked {member} (`{member.id}`). Case `#{case['case_num']}`."
        ))

    @commands.hybrid_command(name="softban", description="Ban then unban a member (purges messages)")
    @command_check()
    async def softban(self, ctx: commands.Context, member: discord.Member, *,
                      reason: str = None, delete_days: int = 1):
        delete_days = max(0, min(int(delete_days), 7))
        if member.top_role >= ctx.guild.me.top_role:
            return await ctx.send(embed=error_embed(
                "I can't softban that member — their top role is at or above mine."
            ))
        if member.id == ctx.author.id:
            return await ctx.send(embed=error_embed("You can't softban yourself."))

        try:
            await member.ban(reason=f"Softban by {ctx.author}: {reason or 'no reason'}",
                             delete_message_days=delete_days)
            await ctx.guild.unban(member, reason=f"Softban by {ctx.author}")
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("Missing permission to softban that member."))
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed(f"Discord error: {e}"))

        case = await models.create_mod_case(
            ctx.guild.id, "softban", ctx.author.id, member.id, reason=reason,
            extra={"delete_days": delete_days},
        )
        await _log_case(self.bot, ctx.guild, case, constants.COLOR_ERROR, "🧹 Softbanned")
        await _dm_target(member, ctx.guild,
                         f"🧹 You were softbanned from {ctx.guild.name}",
                         f"Reason: {reason or 'none'}",
                         constants.COLOR_ERROR)
        await ctx.send(embed=success_embed(
            f"Softbanned {member} (`{member.id}`). Case `#{case['case_num']}`."
        ))

    # ------------------------------------------------------------------
    # Tempban / tempbans
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="tempban",
                             description="Ban a member for a set time, then unban automatically")
    @command_check()
    async def tempban(self, ctx: commands.Context, member: discord.Member,
                      duration: str, *, reason: str = None):
        seconds = parse_duration(duration)
        if seconds is None:
            return await ctx.send(embed=error_embed(
                "Invalid duration. Use `30m`, `2h`, `7d`, or `1w`."
            ))
        if seconds > MAX_TEMPBAN_SECONDS:
            return await ctx.send(embed=error_embed(
                "Maximum tempban is 365 days. Use `/ban` for a permanent ban."
            ))
        if member.top_role >= ctx.guild.me.top_role:
            return await ctx.send(embed=error_embed(
                "I can't ban that member — their top role is at or above mine."
            ))
        if member.id == ctx.author.id:
            return await ctx.send(embed=error_embed("You can't ban yourself."))

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        # DM first: once they're banned the bot can no longer reach them.
        await _dm_target(member, ctx.guild,
                         f"⏳ You were temporarily banned from {ctx.guild.name}",
                         f"Duration: {format_duration(seconds)}\n"
                         f"You can rejoin {discord.utils.format_dt(expires_at, 'R')}.\n"
                         f"Reason: {reason or 'none'}",
                         constants.COLOR_ERROR)
        try:
            await member.ban(
                reason=f"Tempban ({format_duration(seconds)}) by {ctx.author}: {reason or 'no reason'}",
                delete_message_days=0)
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("Missing permission to ban that member."))
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed(f"Discord error: {e}"))

        case = await models.create_mod_case(
            ctx.guild.id, "tempban", ctx.author.id, member.id,
            reason=reason, duration_seconds=seconds,
        )
        await models.add_temp_ban(ctx.guild.id, member.id, ctx.author.id, expires_at,
                                  reason=reason, case_num=case["case_num"])
        await _log_case(self.bot, ctx.guild, case, constants.COLOR_ERROR, "⏳ Temp banned")
        await ctx.send(embed=success_embed(
            f"Temp banned {member} (`{member.id}`) for **{format_duration(seconds)}**. "
            f"They're unbanned {discord.utils.format_dt(expires_at, 'R')}. "
            f"Case `#{case['case_num']}`."
        ))

    @commands.hybrid_command(name="tempbans", description="List the active temp bans")
    @command_check()
    async def tempbans(self, ctx: commands.Context):
        rows = await models.get_guild_temp_bans(ctx.guild.id)
        if not rows:
            return await ctx.send(embed=info_embed("There are no active temp bans."))
        lines = []
        for r in rows[:25]:
            exp = r.get("expires_at")
            if exp is not None and exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            when = discord.utils.format_dt(exp, "R") if exp else "unknown"
            lines.append(f"<@{r['user_id']}> (`{r['user_id']}`) — unbans {when} — "
                         f"by <@{r['moderator_id']}>")
        extra = f"\n…and {len(rows) - 25} more" if len(rows) > 25 else ""
        await ctx.send(embed=base_embed(title="⏳ Active temp bans",
                                        description="\n".join(lines) + extra))

    @tasks.loop(seconds=30)
    async def _expire_temp_bans(self):
        try:
            for doc in await models.get_due_temp_bans(datetime.now(timezone.utc)):
                await self._finish_temp_ban(doc)
        except Exception as e:
            print(f"[tempban] loop error: {e}")

    @_expire_temp_bans.before_loop
    async def _before_expire_temp_bans(self):
        await self.bot.wait_until_ready()

    async def _finish_temp_ban(self, doc: dict):
        guild = self.bot.get_guild(int(doc["guild_id"]))
        if guild is None:  # the bot left that server
            return await models.delete_temp_ban_by_id(doc["_id"])
        uid = int(doc["user_id"])
        unbanned = False
        try:
            await guild.unban(discord.Object(id=uid), reason="Temp ban expired")
            unbanned = True
        except discord.NotFound:
            pass  # already unbanned by hand
        except (discord.Forbidden, discord.HTTPException) as e:
            attempts = await models.bump_temp_ban_attempts(doc["_id"])
            print(f"[tempban] couldn't unban {uid} in {guild.id} (try {attempts}): {e}")
            if attempts < 10:
                return  # try again on the next loop
        await models.delete_temp_ban_by_id(doc["_id"])
        if unbanned:
            case = await models.create_mod_case(
                guild.id, "unban", self.bot.user.id, uid, reason="Temp ban expired",
            )
            await _log_case(self.bot, guild, case, constants.COLOR_SUCCESS,
                            "♻️ Temp ban expired")

    # ------------------------------------------------------------------
    # Purge
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="purge", description="Bulk-delete recent messages")
    @command_check()
    async def purge(self, ctx: commands.Context, amount: int,
                    member: discord.Member = None):
        if not ctx.channel.permissions_for(ctx.guild.me).manage_messages:
            return await ctx.send(embed=error_embed(
                "I need Manage Messages in this channel to purge."
            ))
        amount = max(1, min(int(amount), MAX_PURGE))

        # Defer because the delete may take >3s for large amounts.
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)

        def check(msg: discord.Message):
            if member is not None and msg.author.id != member.id:
                return False
            return True

        try:
            deleted = await ctx.channel.purge(limit=amount, check=check, reason=f"Purge by {ctx.author}")
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("Missing permission to purge."))
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed(f"Discord error: {e}"))

        case = await models.create_mod_case(
            ctx.guild.id, "purge", ctx.author.id,
            target_id=member.id if member else None,
            reason=f"Deleted {len(deleted)} message(s)",
            channel_id=ctx.channel.id,
            extra={"deleted_count": len(deleted)},
        )
        await _log_case(self.bot, ctx.guild, case, constants.COLOR_INFO,
                        f"🧽 Purged {len(deleted)} message(s)")
        await ctx.send(embed=success_embed(
            f"Purged **{len(deleted)}** message(s) in {ctx.channel.mention}. "
            f"Case `#{case['case_num']}`."
        ), ephemeral=(ctx.interaction is not None))

    # ------------------------------------------------------------------
    # Lock / unlock
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="lock", description="Lock the current channel")
    @command_check()
    async def lock(self, ctx: commands.Context, *, reason: str = None):
        if not isinstance(ctx.channel, discord.TextChannel):
            return await ctx.send(embed=error_embed("This command only works in text channels."))
        try:
            await ctx.channel.set_permissions(
                ctx.guild.default_role,
                send_messages=False,
                reason=reason or f"Locked by {ctx.author}",
            )
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("Missing permission to lock this channel."))
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed(f"Discord error: {e}"))

        case = await models.create_mod_case(
            ctx.guild.id, "lock", ctx.author.id, reason=reason, channel_id=ctx.channel.id,
        )
        await _log_case(self.bot, ctx.guild, case, constants.COLOR_WARNING, "🔒 Locked")
        await ctx.send(embed=success_embed(
            f"Locked {ctx.channel.mention}. Case `#{case['case_num']}`."
        ))

    @commands.hybrid_command(name="unlock", description="Unlock the current channel")
    @command_check()
    async def unlock(self, ctx: commands.Context, *, reason: str = None):
        if not isinstance(ctx.channel, discord.TextChannel):
            return await ctx.send(embed=error_embed("This command only works in text channels."))
        try:
            # Passing None clears the overwrite entirely.
            await ctx.channel.set_permissions(
                ctx.guild.default_role,
                overwrite=None,
                reason=reason or f"Unlocked by {ctx.author}",
            )
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("Missing permission to unlock this channel."))
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed(f"Discord error: {e}"))

        case = await models.create_mod_case(
            ctx.guild.id, "unlock", ctx.author.id, reason=reason, channel_id=ctx.channel.id,
        )
        await _log_case(self.bot, ctx.guild, case, constants.COLOR_SUCCESS, "🔓 Unlocked")
        await ctx.send(embed=success_embed(
            f"Unlocked {ctx.channel.mention}. Case `#{case['case_num']}`."
        ))

    # ------------------------------------------------------------------
    # Modlog info
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="modlog", description="Show the configured mod log channel")
    @command_check()
    async def modlog(self, ctx: commands.Context):
        cfg = await models.get_moderation_config(ctx.guild.id)
        if not cfg["log_channel_id"]:
            return await ctx.send(embed=info_embed(
                "No mod log channel configured. Set it on the dashboard's "
                "**Moderation** page."
            ))
        channel = ctx.guild.get_channel(int(cfg["log_channel_id"]))
        await ctx.send(embed=info_embed(
            f"Mod log channel: {channel.mention if channel else '`' + cfg['log_channel_id'] + '` (missing)'}"
        ))


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))