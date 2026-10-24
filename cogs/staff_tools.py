"""
Staff tools - extra moderation and staff-management commands.

All hybrid, all @command_check() (so they appear on the dashboard's Command Permissions
page and are admin-only until enabled there). Their help/permission group is set in
config/command_groups.py.

    /slowmode <time|off> [channel]    set a channel's slowmode (30s, 5m, 1h, off)
    /note add <member> <text>         add a private staff note about a member
    /note list <member>               list a member's staff notes
    /note remove <number>             delete a note by its number
    /history <user>                   a member's full moderation history (all case types)
    /clearwarns <member> [reason]     remove ALL of a member's active warns at once
    /staffprofile [member]            a staff member's taskbook stats, mod-case count and strikes
    /steal <emoji(s)> [name]          (admin) copy custom emoji(s) from another server into this one
    /nuke [channel]                   (admin) clone the channel and delete the old one, with confirmation
    /lockdown [reason]                (admin) stop @everyone from talking in every public channel
    /unlockdown                       (admin) undo /lockdown, restoring only what it changed

slowmode / clearwarns / nuke / lockdown / unlockdown create mod cases and post to the mod log.
"""
import asyncio
import re
from datetime import timezone
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from cogs.moderation import _log_case, parse_duration, format_duration
from cogs.utilities import _reply
from config import constants
from database import models
from database.mongo import taskbooks_col
from utils.spawner_math import format_short
from utils.embeds import base_embed, success_embed, error_embed, info_embed
from utils.permissions import command_check

EMOJI_RE = re.compile(r"<(a?):(\w{2,32}):(\d{15,25})>")
EMOJI_NAME_RE = re.compile(r"[A-Za-z0-9_]{2,32}")
MAX_EMOJI_BYTES = 256 * 1024     # Discord's upload limit for an emoji
MAX_STEAL_AT_ONCE = 10

MAX_SLOWMODE = 21600   # Discord's cap: 6 hours

CASE_ICONS = {
    "warn": "⚠️", "mute": "🔇", "unmute": "🔊", "ban": "🔨", "unban": "♻️", "kick": "👢",
    "softban": "🧹", "purge": "🗑️", "lock": "🔒", "unlock": "🔓", "slowmode": "🐌",
    "nuke": "💥", "lockdown": "🚨", "unlockdown": "✅", "clearwarns": "🧽",
    "tempban": "⏳", "automod": "🤖",
}


async def _download_emoji(emoji_id: str, animated: bool) -> bytes:
    """Fetch an emoji image from Discord's public CDN. Raises ValueError with a readable reason."""
    url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{'gif' if animated else 'png'}"
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status == 404:
                raise ValueError("that emoji no longer exists")
            if resp.status != 200:
                raise ValueError(f"couldn't download it (HTTP {resp.status})")
            if (resp.content_length or 0) > MAX_EMOJI_BYTES:
                raise ValueError("the image is over Discord's 256 KB emoji limit")
            data = await resp.content.read(MAX_EMOJI_BYTES + 1)
    if len(data) > MAX_EMOJI_BYTES:
        raise ValueError("the image is over Discord's 256 KB emoji limit")
    return data


class _ConfirmView(discord.ui.View):
    """Yes / Cancel buttons that only the person who ran the command can press."""

    def __init__(self, author_id: int):
        super().__init__(timeout=45)
        self.author_id = author_id
        self.confirmed = False
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the person who ran this can answer.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()


async def _confirm(ctx: commands.Context, text: str) -> bool:
    view = _ConfirmView(ctx.author.id)
    msg = await ctx.send(embed=base_embed(title="⚠️ Are you sure?", description=text,
                                          color=constants.COLOR_WARNING), view=view)
    await view.wait()
    try:
        await msg.delete()
    except discord.HTTPException:
        pass
    return view.confirmed


def _parse_slowmode(text: str):
    """'off'/'0' -> 0, '30' -> 30, '5m' -> 300. None if unreadable."""
    t = (text or "").strip().lower()
    if t in ("off", "none", "disable", "disabled"):
        return 0
    if re.fullmatch(r"\d+", t):
        return int(t)
    return parse_duration(t)


class StaffTools(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ======================================================================
    # /slowmode
    # ======================================================================
    @commands.hybrid_command(name="slowmode", description="Set a channel's slowmode (e.g. 10s, 5m, off)")
    @app_commands.describe(delay="Seconds or a duration like 30s / 5m / 1h, or off",
                           channel="Channel (default: this one)")
    @command_check()
    async def slowmode(self, ctx: commands.Context, delay: str,
                       channel: Optional[discord.TextChannel] = None):
        channel = channel or ctx.channel
        if not isinstance(channel, discord.TextChannel):
            return await _reply(ctx, error_embed("Slowmode only works in text channels."))
        seconds = _parse_slowmode(delay)
        if seconds is None or seconds > MAX_SLOWMODE:
            return await _reply(ctx, error_embed(
                "Use `off`, a number of seconds, or a duration like `30s`, `5m`, `1h` (max 6h)."))
        try:
            await channel.edit(slowmode_delay=seconds, reason=f"Slowmode by {ctx.author}")
        except discord.Forbidden:
            return await _reply(ctx, error_embed("I don't have permission to edit that channel."))
        except discord.HTTPException as e:
            return await _reply(ctx, error_embed(f"Discord error: {e}"))
        case = await models.create_mod_case(
            ctx.guild.id, "slowmode", ctx.author.id, channel_id=channel.id,
            duration_seconds=seconds or None, reason=f"Slowmode {'off' if not seconds else format_duration(seconds)}")
        await _log_case(self.bot, ctx.guild, case, constants.COLOR_INFO, "🐌 Slowmode")
        await ctx.send(embed=success_embed(
            f"Slowmode in {channel.mention} is now **{'off' if not seconds else format_duration(seconds)}**."
            f" Case `#{case['case_num']}`."))

    # ======================================================================
    # /note
    # ======================================================================
    @commands.hybrid_group(name="note", description="Private staff notes about members",
                           invoke_without_command=True)
    async def note(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await _reply(ctx, info_embed(
                "Use `/note add <member> <text>`, `/note list <member>` or `/note remove <number>`."))

    @note.command(name="add", description="Add a private staff note about a member")
    @command_check()
    async def note_add(self, ctx: commands.Context, user: discord.User, *, text: str):
        text = text.strip()
        if not text:
            return await _reply(ctx, error_embed("Write the note after the member."))
        doc = await models.add_mod_note(ctx.guild.id, user.id, ctx.author.id, text[:1000])
        await _reply(ctx, success_embed(f"Note `#{doc['number']}` added for {user.mention}."),
                     ephemeral=True, delete_after=8)

    @note.command(name="list", description="List a member's staff notes")
    @command_check()
    async def note_list(self, ctx: commands.Context, user: discord.User):
        notes = await models.get_mod_notes(ctx.guild.id, user.id, limit=15)
        if not notes:
            return await _reply(ctx, info_embed(f"No staff notes for {user.mention}."))
        total = await models.count_mod_notes(ctx.guild.id, user.id)
        lines = []
        for n in notes:
            when = n["created_at"].replace(tzinfo=timezone.utc) if n["created_at"].tzinfo is None else n["created_at"]
            lines.append(f"`#{n['number']}` {n['text'][:200]}\n-# by <@{n['staff_id']}> · {discord.utils.format_dt(when, 'R')}")
        embed = base_embed(title=f"📝 Staff notes - {user}", description="\n\n".join(lines)[:4000])
        if total > len(notes):
            embed.set_footer(text=f"Showing the latest {len(notes)} of {total}")
        # Notes are staff-only: never show them to the channel on slash.
        await _reply(ctx, embed, ephemeral=True, delete_after=60)

    @note.command(name="remove", description="Delete a staff note by its number")
    @command_check()
    async def note_remove(self, ctx: commands.Context, number: int):
        gone = await models.delete_mod_note(ctx.guild.id, number)
        await _reply(ctx, success_embed(f"Note `#{number}` deleted.") if gone
                     else error_embed(f"No note `#{number}` in this server."), ephemeral=True, delete_after=8)

    # ======================================================================
    # /history
    # ======================================================================
    @commands.hybrid_command(name="history", description="A member's full moderation history")
    @app_commands.describe(user="Who to look up")
    @command_check()
    async def history(self, ctx: commands.Context, user: discord.User):
        cases = await models.get_mod_cases(ctx.guild.id, target_id=user.id, limit=15)
        notes = await models.count_mod_notes(ctx.guild.id, user.id)
        if not cases:
            extra = f" ({notes} staff note(s) - see `/note list`)" if notes else ""
            return await _reply(ctx, info_embed(f"No moderation history for {user.mention}.{extra}"))
        all_cases = await models.mod_cases_col().count_documents(
            {"guild_id": str(ctx.guild.id), "target_id": str(user.id)})
        lines = []
        for c in cases:
            when = c["created_at"].replace(tzinfo=timezone.utc) if c["created_at"].tzinfo is None else c["created_at"]
            reason = (c.get("reason") or "").strip().replace("\n", " ")[:90] or "*no reason*"
            line = (f"{CASE_ICONS.get(c['action'], '•')} `#{c['case_num']}` **{c['action']}** - {reason} "
                    f"- {discord.utils.format_dt(when, 'R')} - <@{c['moderator_id']}>")
            if c.get("removed_at"):
                line = f"~~{line}~~ *(removed)*"
            lines.append(line)
        embed = base_embed(title=f"📜 Moderation history - {user}", description="\n".join(lines)[:4000])
        embed.set_thumbnail(url=user.display_avatar.url)
        foot = f"Showing the latest {len(cases)} of {all_cases} case(s)"
        if notes:
            foot += f" · {notes} staff note(s)"
        embed.set_footer(text=foot)
        await _reply(ctx, embed, ephemeral=True, delete_after=60)

    # ======================================================================
    # /clearwarns
    # ======================================================================
    @commands.hybrid_command(name="clearwarns", description="Remove ALL of a member's active warns")
    @app_commands.describe(member="Whose warns to clear", reason="Why (optional)")
    @command_check()
    async def clearwarns(self, ctx: commands.Context, member: discord.User, *,
                         reason: Optional[str] = None):
        count = await models.clear_user_warns(ctx.guild.id, member.id, ctx.author.id)
        if not count:
            return await _reply(ctx, info_embed(f"{member.mention} has no active warns."))
        case = await models.create_mod_case(
            ctx.guild.id, "clearwarns", ctx.author.id, member.id, reason=reason,
            extra={"cleared": count})
        await _log_case(self.bot, ctx.guild, case, constants.COLOR_SUCCESS, "🧽 Warns cleared")
        await ctx.send(embed=success_embed(
            f"Cleared **{count}** warn(s) for {member.mention}. Case `#{case['case_num']}`."))

    # ======================================================================
    # /staffprofile
    # ======================================================================
    @commands.hybrid_command(name="staffprofile", description="A staff member's taskbook stats and moderation cases")
    @app_commands.describe(member="Which staff member (default: you)")
    @command_check()
    async def staffprofile(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        m = member or ctx.author
        tb = await taskbooks_col().find_one({"guild_id": str(ctx.guild.id), "user_id": str(m.id)})
        embed = base_embed(title=f"🧑‍💼 Staff profile - {m.display_name}")
        embed.set_thumbnail(url=m.display_avatar.url)
        if not tb:
            embed.description = "This member doesn't have a taskbook yet (an admin adds one on the Taskbooks dashboard page)."
        else:
            embed.description = f"📒 Taskbook **#{tb['number']}**"
            for label, key in (("🎫 Tickets handled", "handled"), ("✏️ Tickets renamed", "renamed"),
                               ("🔒 Tickets closed", "closed"), ("🚫 Blacklists", "blacklisted")):
                embed.add_field(name=label, value=f"**{int(tb.get(key, 0)):,}**")
            embed.add_field(name="🎁 Sponsored", value=f"**{format_short(int(tb.get('sponsored', 0)))}**")
            from cogs.taskbook import rating_text
            embed.add_field(name="⭐ Ticket rating", value=rating_text(tb))
        cases = await models.mod_cases_col().count_documents(
            {"guild_id": str(ctx.guild.id), "moderator_id": str(m.id)})
        embed.add_field(name="Mod cases issued", value=f"{cases:,}")
        strikes = await models.get_strike_counts_for_user(ctx.guild.id, m.id)
        embed.add_field(
            name="⚠️ Strikes",
            value=", ".join(f"**{x['type_name']}**: {x['count']}"
                            for x in sorted(strikes, key=lambda x: x["type_name"].lower())) or "None",
            inline=False)
        await ctx.send(embed=embed)

    # ======================================================================
    # /steal
    # ======================================================================
    @commands.hybrid_command(name="steal", description="Copy custom emoji(s) from another server into this one")
    @app_commands.describe(emoji="Paste one or more custom emojis (add a name after a single emoji to rename it)")
    @command_check()
    async def steal(self, ctx: commands.Context, *, emoji: str):
        found, seen = [], set()
        for animated, name, eid in EMOJI_RE.findall(emoji):
            if eid not in seen:
                seen.add(eid)
                found.append((bool(animated), name, eid))
        if not found:
            return await _reply(ctx, error_embed(
                "I didn't find a custom emoji. Paste one from another server "
                "(that needs Nitro) - normal Unicode emojis can't be stolen."))
        if len(found) > MAX_STEAL_AT_ONCE:
            return await _reply(ctx, error_embed(f"Steal at most {MAX_STEAL_AT_ONCE} emojis at a time."))

        # A single emoji followed by a word renames it: /steal <emoji> newname
        rename = None
        leftover = EMOJI_RE.sub("", emoji).strip()
        if len(found) == 1 and leftover:
            if not EMOJI_NAME_RE.fullmatch(leftover):
                return await _reply(ctx, error_embed(
                    "The new name must be 2-32 letters, numbers or underscores."))
            rename = leftover

        me = ctx.guild.me
        perms = me.guild_permissions
        if not (getattr(perms, "manage_expressions", False) or perms.manage_emojis_and_stickers):
            return await _reply(ctx, error_embed("I need the **Manage Expressions** permission to add emojis."))

        await ctx.defer(ephemeral=True)
        existing = {e.name.lower() for e in ctx.guild.emojis}
        used = {False: sum(1 for e in ctx.guild.emojis if not e.animated),
                True: sum(1 for e in ctx.guild.emojis if e.animated)}
        limit = ctx.guild.emoji_limit

        ok, bad = [], []
        for animated, orig_name, eid in found:
            label = rename or orig_name
            if used[animated] >= limit:
                bad.append(f"`{label}` - no free {'animated' if animated else 'static'} emoji slots ({limit} max)")
                continue
            name, n = label, 2
            while name.lower() in existing:                 # keep names unique
                name = f"{label[:32 - len(str(n)) - 1]}_{n}"
                n += 1
            try:
                image = await _download_emoji(eid, animated)
            except ValueError as e:
                bad.append(f"`{label}` - {e}")
                continue
            except (aiohttp.ClientError, asyncio.TimeoutError):
                bad.append(f"`{label}` - download failed, try again")
                continue
            try:
                new = await ctx.guild.create_custom_emoji(
                    name=name, image=image, reason=f"Stolen by {ctx.author} ({ctx.author.id})")
            except discord.Forbidden:
                bad.append(f"`{label}` - I'm missing permission to add emojis")
                break
            except discord.HTTPException as e:
                bad.append(f"`{label}` - Discord refused it ({e.text or e.status})")
                continue
            existing.add(new.name.lower())
            used[animated] += 1
            ok.append(f"{new} `:{new.name}:`")

        embed = base_embed(title="Emoji steal", color=constants.COLOR_SUCCESS if ok and not bad
                           else constants.COLOR_WARNING if ok else constants.COLOR_ERROR)
        if ok:
            embed.add_field(name=f"✅ Added ({len(ok)})", value="\n".join(ok)[:1000], inline=False)
        if bad:
            embed.add_field(name=f"❌ Failed ({len(bad)})", value="\n".join(bad)[:1000], inline=False)
        await ctx.send(embed=embed, ephemeral=True)

    # ======================================================================
    # /nuke  (clone + delete)
    # ======================================================================
    @commands.hybrid_command(name="nuke", description="Clone a channel and delete the old one (wipes its messages)")
    @app_commands.describe(channel="Channel to wipe (default: this one)")
    @command_check()
    async def nuke(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        channel = channel or ctx.channel
        if not isinstance(channel, discord.TextChannel):
            return await _reply(ctx, error_embed("Nuke only works in normal text channels."))
        if await models.get_ticket(channel.id):
            return await _reply(ctx, error_embed("That's an open ticket - close it with `/ticket close` instead."))
        if not await _confirm(ctx, (
                f"This **deletes {channel.mention} and recreates it empty**.\n"
                "All messages are lost, and anything pointing at the old channel by ID "
                "(panels, log/vouch channels, stickies, counting) must be set up again.")):
            return await _reply(ctx, info_embed("Nuke cancelled."))
        try:
            clone = await channel.clone(reason=f"Nuked by {ctx.author}")
            await clone.edit(position=channel.position)
            await channel.delete(reason=f"Nuked by {ctx.author}")
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("I'm missing permission to do that (Manage Channels)."))
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed(f"Discord error: {e}"))
        case = await models.create_mod_case(
            ctx.guild.id, "nuke", ctx.author.id, channel_id=clone.id,
            reason=f"Nuked #{channel.name}")
        await _log_case(self.bot, ctx.guild, case, constants.COLOR_ERROR, "💥 Channel nuked")
        await clone.send(embed=base_embed(
            title="💥 Channel nuked", description=f"Wiped by {ctx.author.mention}. Case `#{case['case_num']}`.",
            color=constants.COLOR_ERROR))
        if ctx.channel.id != channel.id:    # the invoking channel still exists
            await ctx.send(embed=success_embed(f"Nuked #{channel.name} → {clone.mention}."))

    # ======================================================================
    # /lockdown, /unlockdown
    # ======================================================================
    @commands.hybrid_command(name="lockdown", description="Stop @everyone talking in every public text channel")
    @app_commands.describe(reason="Why (shown in the audit log)")
    @command_check()
    async def lockdown(self, ctx: commands.Context, *, reason: Optional[str] = None):
        if await models.get_lockdown(ctx.guild.id):
            return await _reply(ctx, error_embed("The server is already in lockdown. Use `/unlockdown` first."))
        everyone = ctx.guild.default_role
        targets = [c for c in ctx.guild.text_channels
                   if c.permissions_for(everyone).view_channel
                   and c.permissions_for(everyone).send_messages]
        if not targets:
            return await _reply(ctx, info_embed("There's no public channel @everyone can talk in."))
        if not await _confirm(ctx, f"This stops @everyone sending messages in **{len(targets)}** channel(s). "
                                   "`/unlockdown` restores them."):
            return await _reply(ctx, info_embed("Lockdown cancelled."))

        status = await ctx.send(embed=info_embed(f"Locking {len(targets)} channel(s)…"))
        saved, failed = {}, 0
        for ch in targets:
            prev = ch.overwrites_for(everyone).send_messages     # None (inherit) or True
            try:
                await ch.set_permissions(everyone, send_messages=False,
                                         reason=f"Lockdown by {ctx.author}: {reason or 'no reason'}")
                saved[str(ch.id)] = prev
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
            await asyncio.sleep(0.3)    # stay clear of channel-edit rate limits
        await models.set_lockdown(ctx.guild.id, {"channels": saved, "by": str(ctx.author.id)})
        case = await models.create_mod_case(
            ctx.guild.id, "lockdown", ctx.author.id, reason=reason, extra={"channels": len(saved)})
        await _log_case(self.bot, ctx.guild, case, constants.COLOR_ERROR, "🚨 Lockdown")
        text = f"🚨 Locked **{len(saved)}** channel(s). Case `#{case['case_num']}`. Use `/unlockdown` to undo."
        if failed:
            text += f"\n{failed} channel(s) couldn't be changed (missing permissions)."
        await status.edit(embed=base_embed(description=text, color=constants.COLOR_ERROR))

    @commands.hybrid_command(name="unlockdown", description="Undo /lockdown, restoring only what it changed")
    @command_check()
    async def unlockdown(self, ctx: commands.Context):
        data = await models.get_lockdown(ctx.guild.id)
        if not data:
            return await _reply(ctx, info_embed("The server isn't in lockdown."))
        saved = data.get("channels") or {}
        status = await ctx.send(embed=info_embed(f"Unlocking {len(saved)} channel(s)…"))
        everyone = ctx.guild.default_role
        restored = failed = 0
        for cid, prev in saved.items():
            ch = ctx.guild.get_channel(int(cid))
            if ch is None:
                continue    # deleted since the lockdown
            try:
                await ch.set_permissions(everyone, send_messages=prev,
                                         reason=f"Lockdown lifted by {ctx.author}")
                restored += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
            await asyncio.sleep(0.3)
        await models.set_lockdown(ctx.guild.id, None)
        case = await models.create_mod_case(
            ctx.guild.id, "unlockdown", ctx.author.id, extra={"channels": restored})
        await _log_case(self.bot, ctx.guild, case, constants.COLOR_SUCCESS, "✅ Lockdown lifted")
        text = f"✅ Unlocked **{restored}** channel(s). Case `#{case['case_num']}`."
        if failed:
            text += f"\n{failed} channel(s) couldn't be changed."
        await status.edit(embed=base_embed(description=text, color=constants.COLOR_SUCCESS))


async def setup(bot: commands.Bot):
    await bot.add_cog(StaffTools(bot))
