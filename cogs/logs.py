"""
Server logging cog.

Every event is posted as an embed into the guild's configured log channel
(dashboard -> Config & Logs). Which events get logged is also controlled from
that same dashboard tab.

This cog is self-contained - it reads its config from Mongo every time an event
fires, so dashboard changes take effect immediately without a restart.
"""
import asyncio
import io
from datetime import datetime, timezone

import discord
from discord.ext import commands

from database import models
from database.logging_config import get_logging_config


# ---------------------------------------------------------------------------
# Colours + helpers
# ---------------------------------------------------------------------------

COLOR_DELETE = 0xED4245   # red
COLOR_EDIT = 0xFEE75C     # yellow
COLOR_JOIN = 0x57F287     # green
COLOR_LEAVE = 0xEB459E    # pink
COLOR_ROLE = 0x5865F2     # blurple
COLOR_CHANNEL = 0x1ABC9C  # teal
COLOR_MOD = 0xE67E22      # orange


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _short(text: str, limit: int = 1000) -> str:
    if not text:
        return "*(empty)*"
    text = str(text)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _user_line(user: discord.abc.User) -> str:
    return f"{user.mention} (`{user}` | `{user.id}`)"


def _channel_line(channel) -> str:
    if channel is None:
        return "*unknown channel*"
    return f"{channel.mention} (`#{channel.name}` | `{channel.id}`)"


def _role_line(role) -> str:
    if role is None:
        return "*unknown role*"
    return f"{role.mention} (`{role.name}` | `{role.id}`)"


def _account_age(created_at: datetime) -> str:
    """'created 11 months, 12 days and 8 hours ago' style string."""
    delta = _utcnow() - created_at.replace(tzinfo=timezone.utc) if created_at.tzinfo is None else _utcnow() - created_at
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        total_seconds = 0

    years, rem = divmod(total_seconds, 365 * 86400)
    months, rem = divmod(rem, 30 * 86400)
    days, rem = divmod(rem, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)

    parts = []
    if years:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    if months:
        parts.append(f"{months} month{'s' if months != 1 else ''}")
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if not parts and minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if not parts:
        return "just now"

    if len(parts) == 1:
        joined = parts[0]
    else:
        joined = ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"created {joined} ago"


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Logs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── plumbing ───────────────────────────────────────────────────────

    async def _log_channel(self, guild: discord.Guild):
        if guild is None:
            return None
        try:
            cfg = await get_logging_config(guild.id)
        except Exception:
            return None
        cid = cfg.get("log_channel_id")
        if not cid:
            return None
        channel = guild.get_channel(int(cid))
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _enabled(self, guild_id: int, event: str) -> bool:
        try:
            cfg = await get_logging_config(guild_id)
        except Exception:
            return False
        return bool(cfg.get("events", {}).get(event, False))

    async def _send(self, guild: discord.Guild, embed: discord.Embed, **kwargs):
        channel = await self._log_channel(guild)
        if channel is None:
            return
        try:
            await channel.send(embed=embed, **kwargs)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ── messages ───────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        if not await self._enabled(message.guild.id, "message_delete"):
            return

        # Try to find who deleted it via the audit log.
        # Discord only logs SOME deletions there (bot deletions, Manage Messages
        # deletions by others). If the user deleted their own message, there is
        # usually no audit entry - we report "Unknown / self-deleted".
        deleter = await self._find_deleter(message)

        embed = discord.Embed(
            title="🗑️ Message Deleted",
            color=COLOR_DELETE,
            timestamp=_utcnow(),
        )
        embed.add_field(name="Author", value=_user_line(message.author), inline=False)
        embed.add_field(name="Channel", value=_channel_line(message.channel), inline=False)
        embed.add_field(name="Content", value=_short(message.content), inline=False)
        embed.add_field(
            name="Deleted By",
            value=deleter if deleter else "Unknown (likely self-deleted or not in audit log)",
            inline=False,
        )
        if message.attachments:
            files = "\n".join(f"[{a.filename}]({a.url})" for a in message.attachments)
            embed.add_field(name="Attachments", value=_short(files), inline=False)
        embed.set_footer(text=f"Message ID: {message.id}")

        files = []
        for a in message.attachments[:5]:
            try:
                files.append(await a.to_file())
            except (discord.HTTPException, discord.NotFound):
                pass

        await self._send(message.guild, embed, files=files or None)

    async def _find_deleter(self, message: discord.Message):
        """Best-effort: check recent audit-log entries for a message_delete
        that matches this message's author + channel."""
        try:
            async for entry in message.guild.audit_logs(
                limit=6, action=discord.AuditLogAction.message_delete
            ):
                # Match by target (the author) + channel. Discord's audit entries
                # for message_delete don't include the message ID, so this is
                # approximate - we only accept entries made in the last ~10s.
                if (
                    entry.target and entry.target.id == message.author.id
                    and entry.extra
                    and getattr(entry.extra, "channel", None)
                    and entry.extra.channel.id == message.channel.id
                    and (_utcnow() - entry.created_at).total_seconds() < 10
                ):
                    return f"{entry.user.mention} (`{entry.user}` | `{entry.user.id}`)"
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list):
        if not messages or messages[0].guild is None:
            return
        guild = messages[0].guild
        if not await self._enabled(guild.id, "message_delete"):
            return

        # Build an HTML transcript of the deleted messages for convenience.
        lines = [
            "<html><head><meta charset='utf-8'><title>Bulk Delete</title></head>"
            "<body style='font-family:sans-serif;background:#2b2d31;color:#dbdee1;'>"
            f"<h2>Bulk delete in #{messages[0].channel.name} - {len(messages)} messages</h2><hr>"
        ]
        for m in messages:
            content = discord.utils.escape_markdown(m.content or "")
            lines.append(
                f"<p><b>{discord.utils.escape_markdown(str(m.author))}</b> "
                f"<span style='color:#949ba4;font-size:12px;'>"
                f"{m.created_at.strftime('%Y-%m-%d %H:%M:%S')}</span><br>{content}</p>"
            )
        lines.append("</body></html>")
        transcript = "\n".join(lines)

        embed = discord.Embed(
            title="🗑️ Bulk Message Delete",
            description=(
                f"**{len(messages)}** messages deleted in {_channel_line(messages[0].channel)}"
            ),
            color=COLOR_DELETE,
            timestamp=_utcnow(),
        )
        embed.set_footer(text=f"Channel ID: {messages[0].channel.id}")

        file = discord.File(
            io.BytesIO(transcript.encode()),
            filename=f"bulk-delete-{messages[0].channel.name}.html",
        )
        await self._send(guild, embed, file=file)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.guild is None or before.author.bot:
            return
        # Ignore pure embed updates (link previews) - those have no content change.
        if before.content == after.content and before.attachments == after.attachments:
            return
        if not await self._enabled(before.guild.id, "message_edit"):
            return

        embed = discord.Embed(
            title="✏️ Message Edited",
            color=COLOR_EDIT,
            timestamp=_utcnow(),
            url=after.jump_url,
        )
        embed.add_field(name="Author", value=_user_line(before.author), inline=False)
        embed.add_field(name="Channel", value=_channel_line(before.channel), inline=False)
        embed.add_field(name="Before", value=_short(before.content), inline=False)
        embed.add_field(name="After", value=_short(after.content), inline=False)
        embed.set_footer(text=f"Message ID: {after.id}")

        await self._send(before.guild, embed)

    # ── members ────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not await self._enabled(member.guild.id, "member_join"):
            return

        embed = discord.Embed(
            title="📥 Member Joined",
            color=COLOR_JOIN,
            timestamp=_utcnow(),
        )
        embed.add_field(name="Member", value=_user_line(member), inline=False)
        embed.add_field(
            name="Account",
            value=f"@{member.name} - {_account_age(member.created_at)}",
            inline=False,
        )
        embed.add_field(
            name="Member Count",
            value=f"**{member.guild.member_count}** - they're the "
                  f"**{self._ordinal(member.guild.member_count)}** to join",
            inline=False,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"User ID: {member.id}")

        await self._send(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if not await self._enabled(member.guild.id, "member_leave"):
            return

        roles = [r.mention for r in member.roles if r.name != "@everyone"]
        joined_line = (
            member.joined_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            if member.joined_at else "unknown"
        )

        embed = discord.Embed(
            title="📤 Member Left",
            color=COLOR_LEAVE,
            timestamp=_utcnow(),
        )
        embed.add_field(name="Member", value=_user_line(member), inline=False)
        embed.add_field(name="Joined", value=joined_line, inline=False)
        if roles:
            embed.add_field(name="Roles", value=_short(" ".join(roles)), inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"User ID: {member.id}")

        await self._send(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        guild = after.guild

        # Nickname change
        if before.nick != after.nick and await self._enabled(guild.id, "nickname_change"):
            embed = discord.Embed(
                title="📝 Nickname Changed",
                color=COLOR_ROLE,
                timestamp=_utcnow(),
            )
            embed.add_field(name="Member", value=_user_line(after), inline=False)
            embed.add_field(name="Before", value=_short(before.nick or before.name), inline=True)
            embed.add_field(name="After", value=_short(after.nick or after.name), inline=True)
            embed.set_footer(text=f"User ID: {after.id}")
            await self._send(guild, embed)

        # Role changes
        if before.roles != after.roles and await self._enabled(guild.id, "role_update"):
            added = [r for r in after.roles if r not in before.roles]
            removed = [r for r in before.roles if r not in after.roles]
            if added or removed:
                embed = discord.Embed(
                    title="🎭 Roles Updated",
                    color=COLOR_ROLE,
                    timestamp=_utcnow(),
                )
                embed.add_field(name="Member", value=_user_line(after), inline=False)
                if added:
                    embed.add_field(
                        name="Added",
                        value=_short(" ".join(r.mention for r in added)),
                        inline=False,
                    )
                if removed:
                    embed.add_field(
                        name="Removed",
                        value=_short(" ".join(r.mention for r in removed)),
                        inline=False,
                    )
                embed.set_footer(text=f"User ID: {after.id}")
                await self._send(guild, embed)

        # Timeout start / end
        if before.timed_out_until != after.timed_out_until and await self._enabled(guild.id, "timeout"):
            if after.timed_out_until and (
                before.timed_out_until is None or after.timed_out_until > before.timed_out_until
            ):
                embed = discord.Embed(
                    title="🔇 Member Timed Out",
                    color=COLOR_MOD,
                    timestamp=_utcnow(),
                )
                embed.add_field(name="Member", value=_user_line(after), inline=False)
                embed.add_field(
                    name="Until",
                    value=after.timed_out_until.strftime("%Y-%m-%d %H:%M:%S UTC"),
                    inline=False,
                )
                embed.set_footer(text=f"User ID: {after.id}")
                await self._send(guild, embed)
            elif after.timed_out_until is None and before.timed_out_until is not None:
                embed = discord.Embed(
                    title="🔊 Timeout Removed",
                    color=COLOR_MOD,
                    timestamp=_utcnow(),
                )
                embed.add_field(name="Member", value=_user_line(after), inline=False)
                embed.set_footer(text=f"User ID: {after.id}")
                await self._send(guild, embed)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        mutual = after.mutual_guilds
        if not mutual:
            return
        # Fire once per mutual guild to keep everything loggable.
        for guild in mutual:
            if before.name != after.name and await self._enabled(guild.id, "username_change"):
                embed = discord.Embed(
                    title="👤 Username Changed",
                    color=COLOR_ROLE,
                    timestamp=_utcnow(),
                )
                embed.add_field(name="User", value=_user_line(after), inline=False)
                embed.add_field(name="Before", value=_short(before.name), inline=True)
                embed.add_field(name="After", value=_short(after.name), inline=True)
                embed.set_footer(text=f"User ID: {after.id}")
                await self._send(guild, embed)

            if before.display_avatar != after.display_avatar and await self._enabled(guild.id, "avatar_change"):
                embed = discord.Embed(
                    title="🖼️ Avatar Changed",
                    color=COLOR_ROLE,
                    timestamp=_utcnow(),
                )
                embed.add_field(name="User", value=_user_line(after), inline=False)
                embed.set_thumbnail(url=after.display_avatar.url)
                embed.set_footer(text=f"User ID: {after.id}")
                await self._send(guild, embed)

    # ── channels ───────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        if not await self._enabled(channel.guild.id, "channel_create"):
            return
        category = channel.category.name if getattr(channel, "category", None) else "None"
        embed = discord.Embed(
            title=f"📁 {channel.type.name.replace('_', ' ').title()} Created",
            color=COLOR_CHANNEL,
            timestamp=_utcnow(),
        )
        embed.add_field(name="Name", value=f"`{channel.name}`", inline=True)
        embed.add_field(name="Category", value=category, inline=True)
        embed.add_field(name="Channel ID", value=f"`{channel.id}`", inline=False)
        embed.set_footer(text=f"Type: {channel.type.name}")
        await self._send(channel.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        if not await self._enabled(channel.guild.id, "channel_delete"):
            return
        category = channel.category.name if getattr(channel, "category", None) else "None"
        embed = discord.Embed(
            title=f"🗑️ {channel.type.name.replace('_', ' ').title()} Deleted",
            color=COLOR_DELETE,
            timestamp=_utcnow(),
        )
        embed.add_field(name="Name", value=f"`{channel.name}`", inline=True)
        embed.add_field(name="Category", value=category, inline=True)
        embed.add_field(name="Channel ID", value=f"`{channel.id}`", inline=False)
        embed.set_footer(text=f"Type: {channel.type.name}")
        await self._send(channel.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        if not await self._enabled(after.guild.id, "channel_update"):
            return

        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** `{before.name}` → `{after.name}`")
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            changes.append(
                f"**Topic:** {_short(before.topic or '*(none)*', 400)} → "
                f"{_short(after.topic or '*(none)*', 400)}"
            )
        if getattr(before, "slowmode_delay", None) != getattr(after, "slowmode_delay", None):
            changes.append(f"**Slowmode:** {before.slowmode_delay}s → {after.slowmode_delay}s")
        if getattr(before, "nsfw", None) != getattr(after, "nsfw", None):
            changes.append(f"**NSFW:** {before.nsfw} → {after.nsfw}")
        if getattr(before, "category", None) != getattr(after, "category", None):
            b_cat = before.category.name if before.category else "None"
            a_cat = after.category.name if after.category else "None"
            changes.append(f"**Category:** {b_cat} → {a_cat}")

        if not changes:
            return

        embed = discord.Embed(
            title=f"🔧 {after.type.name.replace('_', ' ').title()} Updated",
            color=COLOR_CHANNEL,
            timestamp=_utcnow(),
            description="\n".join(changes),
        )
        embed.add_field(name="Channel", value=_channel_line(after), inline=False)
        embed.set_footer(text=f"Channel ID: {after.id}")
        await self._send(after.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_update_overwrites(self, before, after):
        # discord.py fires on_guild_channel_update, we already handle it there.
        pass

    # ── roles ──────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        if not await self._enabled(role.guild.id, "role_create"):
            return
        embed = discord.Embed(
            title="➕ Role Created",
            color=COLOR_ROLE,
            timestamp=_utcnow(),
        )
        embed.add_field(name="Role", value=_role_line(role), inline=False)
        embed.add_field(name="Color", value=f"`{role.color}`", inline=True)
        embed.add_field(name="Mentionable", value=str(role.mentionable), inline=True)
        embed.add_field(name="Hoisted", value=str(role.hoist), inline=True)
        embed.set_footer(text=f"Role ID: {role.id}")
        await self._send(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        if not await self._enabled(role.guild.id, "role_delete"):
            return
        embed = discord.Embed(
            title="➖ Role Deleted",
            color=COLOR_DELETE,
            timestamp=_utcnow(),
        )
        embed.add_field(name="Role", value=f"`{role.name}`", inline=False)
        embed.add_field(name="Role ID", value=f"`{role.id}`", inline=False)
        embed.set_footer(text=f"Role ID: {role.id}")
        await self._send(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        if not await self._enabled(after.guild.id, "role_update"):
            return

        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** `{before.name}` → `{after.name}`")
        if before.color != after.color:
            changes.append(f"**Color:** `{before.color}` → `{after.color}`")
        if before.hoist != after.hoist:
            changes.append(f"**Hoisted:** {before.hoist} → {after.hoist}")
        if before.mentionable != after.mentionable:
            changes.append(f"**Mentionable:** {before.mentionable} → {after.mentionable}")
        if before.permissions != after.permissions:
            added = [p for p, v in after.permissions if v and not dict(before.permissions).get(p)]
            removed = [p for p, v in before.permissions if v and not dict(after.permissions).get(p)]
            if added:
                changes.append("**Perms added:** " + ", ".join(f"`{p}`" for p in added[:10]))
            if removed:
                changes.append("**Perms removed:** " + ", ".join(f"`{p}`" for p in removed[:10]))

        if not changes:
            return

        embed = discord.Embed(
            title="🎭 Role Updated",
            color=COLOR_ROLE,
            timestamp=_utcnow(),
            description="\n".join(changes),
        )
        embed.add_field(name="Role", value=_role_line(after), inline=False)
        embed.set_footer(text=f"Role ID: {after.id}")
        await self._send(after.guild, embed)

    # ── moderation ─────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        if not await self._enabled(guild.id, "ban_add"):
            return
        embed = discord.Embed(
            title="🔨 Member Banned",
            color=COLOR_MOD,
            timestamp=_utcnow(),
        )
        embed.add_field(name="User", value=_user_line(user), inline=False)
        embed.set_footer(text=f"User ID: {user.id}")
        await self._send(guild, embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        if not await self._enabled(guild.id, "ban_remove"):
            return
        embed = discord.Embed(
            title="🔓 Member Unbanned",
            color=COLOR_JOIN,
            timestamp=_utcnow(),
        )
        embed.add_field(name="User", value=_user_line(user), inline=False)
        embed.set_footer(text=f"User ID: {user.id}")
        await self._send(guild, embed)

    # ── voice (optional) ───────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if not await self._enabled(member.guild.id, "voice_join") and \
           not await self._enabled(member.guild.id, "voice_leave"):
            return

        if before.channel is None and after.channel is not None:
            if not await self._enabled(member.guild.id, "voice_join"):
                return
            embed = discord.Embed(
                title="🔊 Voice Join",
                description=f"{member.mention} joined {after.channel.mention}",
                color=COLOR_JOIN,
                timestamp=_utcnow(),
            )
            await self._send(member.guild, embed)
        elif before.channel is not None and after.channel is None:
            if not await self._enabled(member.guild.id, "voice_leave"):
                return
            embed = discord.Embed(
                title="🔇 Voice Leave",
                description=f"{member.mention} left {before.channel.mention}",
                color=COLOR_LEAVE,
                timestamp=_utcnow(),
            )
            await self._send(member.guild, embed)

    # ── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _ordinal(n: int) -> str:
        if 10 <= (n % 100) <= 20:
            suffix = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}{suffix}"


async def setup(bot: commands.Bot):
    await bot.add_cog(Logs(bot))