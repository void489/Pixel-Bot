"""
Staff changes cog (promotions / demotions).

Commands (hybrid, so slash AND prefix, both @command_check()):
    /promote <user> [reason]  -> gives the member the next role up the staff ladder
                                 (the old role is NOT removed)
    /demote  <user> [reason]  -> removes the highest ladder role the member has

Setup is all on the dashboard:
    - Config & Logs  -> "Staff Changes Channel" (where the cards are posted)
    - Staff Changes  -> the role ladder (role + nickname, index 0 = lowest rank)

The ladder lives in guild_settings.staff_changes (see utils/staff_changes_core.py).
A member's rank is the highest ladder index among the roles they hold.

Application hook: applications.py already gives the accept roles. After that it calls
post_application_promotion() below, which posts a "PROMOTED" card if the roles it gave
moved the applicant up the ladder.

Replies to the person running the command are EPHEMERAL on slash and auto-delete after
5s on prefix. The card itself is public in the staff changes channel.
"""
import asyncio
import io

import discord
from discord import app_commands
from discord.ext import commands

from database import models
from utils import promotioncard
from utils import staff_changes_core as core
from utils.embeds import success_embed, error_embed, info_embed, base_embed
from utils.permissions import command_check
from config import constants


async def _reply(ctx: commands.Context, embed: discord.Embed, *, delete_after: float = 5):
    if ctx.interaction is not None:
        return await ctx.send(embed=embed, ephemeral=True)
    return await ctx.send(embed=embed, delete_after=delete_after)


def _role_label(guild: discord.Guild, entry) -> str:
    """Nick if set, else the role's name, else a placeholder."""
    if entry is None:
        return core.label(None)
    role = guild.get_role(int(entry["role_id"]))
    return core.label(entry, role.name if role else None)


async def post_staff_change(guild: discord.Guild, member: discord.Member, kind: str,
                            old_label: str, new_label: str, reason: str = None,
                            footer: str = None, channel_id=None) -> str:
    """Post the card in the staff changes channel.

    Returns "" on success, otherwise a short human reason it wasn't posted
    (so the caller can tell the person running the command)."""
    if channel_id is None:
        cfg = await models.get_staff_changes_config(guild.id)
        channel_id = cfg.get("channel_id")
    if not channel_id:
        return "No Staff Changes channel is set on the dashboard, so nothing was announced."
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        return "The Staff Changes channel no longer exists, so nothing was announced."

    up = kind in ("promotion", "promoted")
    verb = "promoted" if up else "demoted"
    desc = f"{member.mention} was **{verb}** from **{old_label}** to **{new_label}**."
    if reason:
        desc += f"\n**Reason:** {reason[:1000]}"
    embed = base_embed(
        description=desc,
        color=constants.COLOR_SUCCESS if up else constants.COLOR_ERROR,
    )

    file = None
    try:
        try:
            avatar = await member.display_avatar.replace(size=256, format="png").read()
        except (discord.HTTPException, ValueError):
            avatar = None
        png = await asyncio.to_thread(
            promotioncard.render, avatar, member.name, kind, old_label, new_label,
            reason, footer,
        )
        file = discord.File(io.BytesIO(png), filename="staffchange.png")
        embed.set_image(url="attachment://staffchange.png")
    except Exception as e:  # card is a nicety - fall back to the plain embed
        print(f"[staff_changes] card render failed: {e}")
        file = None

    try:
        kwargs = {"embed": embed, "allowed_mentions": discord.AllowedMentions.none()}
        if file is not None:
            kwargs["file"] = file
        await channel.send(**kwargs)
    except (discord.Forbidden, discord.HTTPException):
        return "I couldn't post in the Staff Changes channel (missing permissions?)."
    return ""


async def post_application_promotion(guild: discord.Guild, member: discord.Member,
                                     before_role_ids, after_role_ids,
                                     reason: str = None, decided_by=None) -> None:
    """Called by applications.py after accept roles were given. Posts a PROMOTED card
    if the applicant's highest ladder index went up. Silent otherwise."""
    cfg = await models.get_staff_changes_config(guild.id)
    ladder = cfg["ladder"]
    if len(ladder) < 2 or not cfg.get("channel_id"):
        return
    old_idx = core.highest_index(before_role_ids, ladder)
    new_idx = core.highest_index(after_role_ids, ladder)
    if new_idx <= old_idx:
        return
    footer = "Application accepted"
    if decided_by is not None:
        footer += f" · by {decided_by.name}"
    await post_staff_change(
        guild, member, "promoted",
        _role_label(guild, core.entry_at(ladder, old_idx)),
        _role_label(guild, core.entry_at(ladder, new_idx)),
        reason=reason, footer=footer, channel_id=cfg["channel_id"],
    )


class StaffChanges(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _precheck(self, ctx: commands.Context, member: discord.Member):
        """Shared validation. Returns the ladder, or None after replying with the error."""
        cfg = await models.get_staff_changes_config(ctx.guild.id)
        ladder = cfg["ladder"]
        if len(ladder) < 2:
            await _reply(ctx, error_embed(
                "The staff ladder isn't set up yet. Add at least 2 roles on the "
                "dashboard's **Staff Changes** page."
            ))
            return None
        if member.bot:
            await _reply(ctx, error_embed("You can't do that to a bot."))
            return None
        if member.id == ctx.author.id:
            await _reply(ctx, error_embed("You can't promote or demote yourself."))
            return None
        return cfg

    # ------------------------------------------------------------------
    # /promote
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="promote", description="Promote a member one level up the staff ladder")
    @app_commands.describe(member="Who to promote", reason="Why (optional)")
    @command_check()
    async def promote(self, ctx: commands.Context, member: discord.Member, *,
                      reason: str = None):
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)

        cfg = await self._precheck(ctx, member)
        if cfg is None:
            return
        ladder = cfg["ladder"]

        cur = core.highest_index([r.id for r in member.roles], ladder)
        if cur >= len(ladder) - 1:
            return await _reply(ctx, info_embed(
                f"{member.mention} is at the highest role right now "
                f"(**{_role_label(ctx.guild, core.entry_at(ladder, cur))}**)."
            ))

        target = core.entry_at(ladder, cur + 1)
        role = ctx.guild.get_role(int(target["role_id"]))
        if role is None:
            return await _reply(ctx, error_embed(
                "The next role on the ladder no longer exists. Fix it on the "
                "dashboard's **Staff Changes** page."
            ))
        if role.managed or role.is_default():
            return await _reply(ctx, error_embed("That role can't be assigned manually."))
        if role >= ctx.guild.me.top_role:
            return await _reply(ctx, error_embed(
                f"{role.mention} is at or above my top role, so I can't give it."
            ))

        try:
            await member.add_roles(
                role, reason=f"Promotion by {ctx.author}" + (f": {reason}" if reason else "")
            )
        except discord.Forbidden:
            return await _reply(ctx, error_embed("Missing permission to add that role."))
        except discord.HTTPException as e:
            return await _reply(ctx, error_embed(f"Discord error: {e}"))

        new_label = _role_label(ctx.guild, target)
        note = await post_staff_change(
            ctx.guild, member, "promotion",
            _role_label(ctx.guild, core.entry_at(ladder, cur)), new_label,
            reason=reason, footer=f"By {ctx.author.name}", channel_id=cfg["channel_id"],
        )
        msg = f"Promoted {member.mention} to **{new_label}**."
        if note:
            msg += f"\n⚠️ {note}"
        await _reply(ctx, success_embed(msg))

    # ------------------------------------------------------------------
    # /demote
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="demote", description="Demote a member (removes their highest staff ladder role)")
    @app_commands.describe(member="Who to demote", reason="Why (optional)")
    @command_check()
    async def demote(self, ctx: commands.Context, member: discord.Member, *,
                     reason: str = None):
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)

        cfg = await self._precheck(ctx, member)
        if cfg is None:
            return
        ladder = cfg["ladder"]

        held_ids = [r.id for r in member.roles]
        cur = core.highest_index(held_ids, ladder)
        if cur < 0:
            return await _reply(ctx, info_embed(
                f"{member.mention} doesn't have any staff ladder role to remove."
            ))
        if cur == 0:
            return await _reply(ctx, info_embed(
                f"{member.mention} is already at the lowest role "
                f"(**{_role_label(ctx.guild, core.entry_at(ladder, 0))}**) and can't be demoted any lower."
            ))

        entry = core.entry_at(ladder, cur)
        role = ctx.guild.get_role(int(entry["role_id"]))
        if role is None:
            return await _reply(ctx, error_embed(
                "That role no longer exists. Fix it on the dashboard's **Staff Changes** page."
            ))
        if role >= ctx.guild.me.top_role:
            return await _reply(ctx, error_embed(
                f"{role.mention} is at or above my top role, so I can't remove it."
            ))

        try:
            await member.remove_roles(
                role, reason=f"Demotion by {ctx.author}" + (f": {reason}" if reason else "")
            )
        except discord.Forbidden:
            return await _reply(ctx, error_embed("Missing permission to remove that role."))
        except discord.HTTPException as e:
            return await _reply(ctx, error_embed(f"Discord error: {e}"))

        # Where they land = the highest ladder role they still hold.
        remaining = [rid for rid in held_ids if rid != role.id]
        new_idx = core.highest_index(remaining, ladder)
        new_label = _role_label(ctx.guild, core.entry_at(ladder, new_idx))
        note = await post_staff_change(
            ctx.guild, member, "demotion",
            _role_label(ctx.guild, entry), new_label,
            reason=reason, footer=f"By {ctx.author.name}", channel_id=cfg["channel_id"],
        )
        msg = f"Demoted {member.mention} from **{_role_label(ctx.guild, entry)}** to **{new_label}**."
        if note:
            msg += f"\n⚠️ {note}"
        await _reply(ctx, success_embed(msg))


async def setup(bot: commands.Bot):
    await bot.add_cog(StaffChanges(bot))
