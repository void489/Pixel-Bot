"""
Leave of absence (LOA).

Commands (hybrid = slash AND prefix, all @command_check()):
    /loa request <reason> <duration>   STAFF group  - ask for a LOA (works in any channel)
    /loa end <member>                  STAFF group  - end someone's active LOA early
    /loa add <member> <duration> [reason]  ADMIN group - put a member on LOA directly
    /loa remove <member>               ADMIN group  - delete a member's LOA / pending request

Setup (dashboard -> LOA page): the LOA role that gets given, and the channel where
requests are posted. A request is an embed with Accept / Deny buttons. Accept gives
the role until the duration is over; Deny just closes it.

While someone is on LOA their ladder roles are PAUSED: every Changes-ladder role at
rung 1 or higher (in every type) is taken away - rung 0 (Member) stays - and the list is
saved on the LOA. When the LOA ends the LOA role is removed and those roles are given back.

While someone is on LOA, staff activity checks leave them out (see staff_activity.py),
so they never show up as having missed one.

Everything is timestamp-driven from Mongo, so a restart never loses a LOA or skips an
expiry, and the Accept / Deny buttons are persistent.
"""
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import constants
from database import models
from utils import changes_core as ccore
from database.mongo import loa_requests_col, loa_settings_col
from utils.embeds import success_embed, error_embed, info_embed
from utils.loa_core import (
    MIN_SECONDS, MAX_SECONDS, parse_duration, format_duration, aware,
)
from utils.permissions import command_check

ACTIVE = "active"
PENDING = "pending"

STATUS_LOOK = {
    PENDING: ("⏳ LOA Request • Pending", constants.COLOR_WARNING),
    ACTIVE: ("✅ LOA Request • Approved", constants.COLOR_SUCCESS),
    "denied": ("❌ LOA Request • Denied", constants.COLOR_ERROR),
    "ended": ("🏁 LOA • Ended early", constants.COLOR_INFO),
    "expired": ("🏁 LOA • Finished", constants.COLOR_INFO),
    "removed": ("🗑️ LOA • Removed", constants.COLOR_DEFAULT),
}


async def _reply(ctx: commands.Context, embed: discord.Embed, *, delete_after: float = 8):
    if ctx.interaction is not None:
        return await ctx.send(embed=embed, ephemeral=True)
    return await ctx.send(embed=embed, delete_after=delete_after)


def _ts(dt, style="F") -> str:
    return f"<t:{int(aware(dt).timestamp())}:{style}>"


def build_embed(doc: dict) -> discord.Embed:
    title, color = STATUS_LOOK.get(doc["status"], STATUS_LOOK[PENDING])
    embed = discord.Embed(title=title, color=color, timestamp=aware(doc["requested_at"]))
    embed.add_field(name="Member", value=f"<@{doc['user_id']}>", inline=True)
    embed.add_field(name="Duration", value=format_duration(doc["duration_seconds"]), inline=True)
    embed.add_field(name="Requested", value=_ts(doc["requested_at"], "R"), inline=True)
    embed.add_field(name="Reason", value=(doc.get("reason") or "No reason given")[:1000], inline=False)
    if doc.get("start") and doc.get("end"):
        embed.add_field(name="Starts", value=_ts(doc["start"]), inline=True)
        embed.add_field(name="Ends", value=_ts(doc["end"]), inline=True)
    if doc.get("paused_role_ids"):
        label = "Ranks paused" if doc["status"] == ACTIVE else "Ranks restored"
        embed.add_field(name=label,
                        value=" ".join(f"<@&{r}>" for r in doc["paused_role_ids"])[:1000], inline=False)
    if doc.get("reviewer_id"):
        label = {"denied": "Denied by", "ended": "Ended by", "removed": "Removed by"}.get(
            doc["status"], "Approved by")
        embed.add_field(name=label, value=f"<@{doc['reviewer_id']}>", inline=True)
    if doc.get("source") == "admin":
        embed.set_footer(text="Added directly by an admin")
    return embed


async def _settings(guild_id: int) -> dict:
    return await loa_settings_col().find_one({"guild_id": guild_id}) or {}


async def _set_role(guild: discord.Guild, member, role_id, give: bool, reason: str) -> str:
    """Give/remove the LOA role. Returns "" on success, else a short problem text."""
    if not role_id:
        return "No LOA role is set on the dashboard."
    role = guild.get_role(int(role_id))
    if role is None:
        return "The LOA role no longer exists."
    if member is None:
        return ""  # they left the server, nothing to change
    try:
        if give:
            await member.add_roles(role, reason=reason)
        else:
            await member.remove_roles(role, reason=reason)
    except discord.Forbidden:
        return f"I can't manage {role.mention} - move my role above it."
    except discord.HTTPException:
        return "Discord refused the role change."
    return ""


async def _pause_ranks(guild: discord.Guild, member) -> tuple:
    """Take every Changes-ladder role at rung 1+ (any type) away from the member, plus the
    extra roles of every type where they were at rung 1+. Rung 0 (Member) is never touched.
    Everything removed is saved on the LOA in one list (paused_role_ids) and given back by
    _restore_ranks. Returns (removed_role_ids, failed_role_mentions)."""
    if member is None:
        return [], []
    cfg = await models.get_changes_config(guild.id)
    removed, failed = [], []
    for rid in ccore.loa_pause_role_ids([r.id for r in member.roles], cfg["types"]):
        role = guild.get_role(rid)
        if role is None:
            continue
        try:
            await member.remove_roles(role, reason="LOA: ranks paused")
            removed.append(rid)
        except (discord.Forbidden, discord.HTTPException):
            failed.append(role.mention)
    return removed, failed


async def _restore_ranks(guild: discord.Guild, member, role_ids) -> tuple:
    """Give back the roles LOA paused (ranks and extra roles). Deleted roles are skipped.
    Returns (restored_role_ids, failed_role_mentions)."""
    if member is None:
        return [], []
    restored, failed = [], []
    for rid in role_ids or []:
        role = guild.get_role(int(rid))
        if role is None:
            continue
        try:
            await member.add_roles(role, reason="LOA over: ranks restored")
            restored.append(int(rid))
        except (discord.Forbidden, discord.HTTPException):
            failed.append(role.mention)
    return restored, failed


async def _dm(user, embed: discord.Embed):
    try:
        await user.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        pass


async def _refresh_message(bot, doc: dict, *, buttons: bool = False):
    """Re-render a request's embed (and drop the buttons unless still pending)."""
    guild = bot.get_guild(doc["guild_id"])
    channel = guild.get_channel(doc["channel_id"]) if guild and doc.get("channel_id") else None
    if channel is None or not doc.get("message_id"):
        return
    try:
        msg = await channel.fetch_message(doc["message_id"])
        await msg.edit(embed=build_embed(doc), view=LoaView() if buttons else None)
    except discord.HTTPException:
        pass


async def _is_reviewer(member: discord.Member) -> bool:
    """Admins, plus anyone allowed to run /loa end (the staff group)."""
    if member.guild_permissions.administrator:
        return True
    perm = await models.get_command_permission(member.guild.id, "loa end")
    if perm.get("admin_only", True) or not perm.get("enabled", False):
        return False
    return bool({str(r) for r in perm.get("allowed_roles", [])} & {str(r.id) for r in member.roles})


# ── Accept / Deny buttons (persistent) ───────────────────────────────────────
class LoaView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _decide(self, interaction: discord.Interaction, accept: bool):
        if not await _is_reviewer(interaction.user):
            return await interaction.response.send_message(
                embed=error_embed("You can't accept or deny LOA requests."), ephemeral=True)

        doc = await loa_requests_col().find_one(
            {"guild_id": interaction.guild.id, "message_id": interaction.message.id})
        if doc is None:
            return await interaction.response.send_message(
                embed=error_embed("I can't find that request any more."), ephemeral=True)
        if doc["status"] != PENDING:
            return await interaction.response.send_message(
                embed=error_embed("That request was already handled."), ephemeral=True)

        await interaction.response.defer()
        now = datetime.now(timezone.utc)
        member = interaction.guild.get_member(int(doc["user_id"]))

        if accept:
            update = {"status": ACTIVE, "start": now,
                      "end": now + timedelta(seconds=doc["duration_seconds"]),
                      "reviewer_id": interaction.user.id}
        else:
            update = {"status": "denied", "reviewer_id": interaction.user.id}

        # Claim first so two reviewers can never both handle it.
        claimed = await loa_requests_col().find_one_and_update(
            {"_id": doc["_id"], "status": PENDING}, {"$set": update}, return_document=True)
        if claimed is None:
            return await interaction.followup.send(
                embed=error_embed("That request was already handled."), ephemeral=True)

        notes = []
        if accept:
            settings = await _settings(interaction.guild.id)
            problem = await _set_role(interaction.guild, member, settings.get("role_id"), True,
                                      f"LOA approved by {interaction.user}")
            if problem:  # put it back to pending so it can be retried
                await loa_requests_col().update_one(
                    {"_id": doc["_id"], "status": ACTIVE},
                    {"$set": {"status": PENDING},
                     "$unset": {"start": "", "end": "", "reviewer_id": ""}})
                return await interaction.followup.send(
                    embed=error_embed(f"{problem} Fix it and press Accept again."), ephemeral=True)

            paused, failed = await _pause_ranks(interaction.guild, member)
            if paused:
                claimed = await loa_requests_col().find_one_and_update(
                    {"_id": doc["_id"]}, {"$set": {"paused_role_ids": paused}},
                    return_document=True)
            if failed:
                notes.append("I couldn't remove these ranks (move my role higher): " + ", ".join(failed))

        await interaction.edit_original_response(embed=build_embed(claimed), view=None)
        if notes:
            await interaction.followup.send(embed=error_embed("\n".join(notes), title="⚠️ Heads up"),
                                            ephemeral=True)

        user = member or interaction.client.get_user(int(doc["user_id"]))
        if accept:
            extra = "\nYour staff ranks are paused and will be given back when it ends." \
                if claimed.get("paused_role_ids") else ""
            await _dm(user, success_embed(
                f"Your LOA in **{interaction.guild.name}** was approved.\n"
                f"It ends {_ts(claimed['end'])} ({_ts(claimed['end'], 'R')}).{extra}",
                title="✅ LOA approved"))
        else:
            await _dm(user, error_embed(
                f"Your LOA request in **{interaction.guild.name}** was denied.",
                title="❌ LOA denied"))

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="loa:accept")
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._decide(interaction, True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="loa:deny")
    async def deny_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._decide(interaction, False)


class Loa(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        try:
            await loa_requests_col().create_index([("guild_id", 1), ("user_id", 1), ("status", 1)])
            await loa_requests_col().create_index([("status", 1), ("end", 1)])
            await loa_settings_col().create_index("guild_id", unique=True)
        except Exception as e:
            print(f"[loa] index creation failed: {e}")
        self.bot.add_view(LoaView())
        self.expiry_loop.start()

    async def cog_unload(self):
        self.expiry_loop.cancel()

    # ── Finishing a LOA (expiry / end / remove) ──────────────────────────
    async def _finish(self, doc: dict, status: str, by: int = None, reason: str = ""):
        """Move an active/pending LOA to a final status, take the LOA role back, give back
        the paused ranks, update the embed.
        Returns (updated_doc, notes) - (None, []) if it was already finished. `notes` are
        problems worth telling the person running the command about."""
        now = datetime.now(timezone.utc)
        update = {"status": status, "finished_at": now}
        if by:
            update["reviewer_id"] = by
        if doc["status"] == ACTIVE and status in ("ended", "removed"):
            update["end"] = now  # so activity checks see the real end
        updated = await loa_requests_col().find_one_and_update(
            {"_id": doc["_id"], "status": {"$in": [PENDING, ACTIVE]}}, {"$set": update},
            return_document=True)
        if updated is None:
            return None, []

        notes = []
        guild = self.bot.get_guild(updated["guild_id"])
        if guild and doc["status"] == ACTIVE:
            member = guild.get_member(int(updated["user_id"]))
            settings = await _settings(guild.id)
            problem = await _set_role(guild, member, settings.get("role_id"), False, reason or "LOA over")
            if problem:
                notes.append(problem)
            _, failed = await _restore_ranks(guild, member, updated.get("paused_role_ids"))
            if failed:
                notes.append("I couldn't give back: " + ", ".join(failed))
            if notes:
                print(f"[loa] finish problems in {guild.id}: {notes}")
        await _refresh_message(self.bot, updated)
        return updated, notes

    @tasks.loop(minutes=1)
    async def expiry_loop(self):
        try:
            now = datetime.now(timezone.utc)
            due = await loa_requests_col().find(
                {"status": ACTIVE, "end": {"$lte": now}}).to_list(None)
            for doc in due:
                done, _ = await self._finish(doc, "expired", reason="LOA finished")
                if done:
                    guild = self.bot.get_guild(done["guild_id"])
                    user = (guild.get_member(int(done["user_id"])) if guild else None) \
                        or self.bot.get_user(int(done["user_id"]))
                    await _dm(user, info_embed(
                        f"Your LOA in **{guild.name if guild else 'the server'}** is over. Welcome back!"
                        + (" Your ranks have been given back." if done.get("paused_role_ids") else ""),
                        title="🏁 LOA finished"))
        except Exception as e:
            print(f"[loa] loop error: {e}")

    @expiry_loop.before_loop
    async def _before_loop(self):
        await self.bot.wait_until_ready()

    async def _open_doc(self, guild_id: int, user_id: int):
        return await loa_requests_col().find_one(
            {"guild_id": guild_id, "user_id": user_id, "status": {"$in": [PENDING, ACTIVE]}})

    # ── /loa ─────────────────────────────────────────────────────────────
    @commands.hybrid_group(name="loa", description="Leave of absence", invoke_without_command=True)
    async def loa(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await _reply(ctx, info_embed(
                "`/loa request` · `/loa end` · `/loa add` · `/loa remove`"))

    @loa.command(name="request", description="Request a leave of absence")
    @app_commands.describe(reason="Why you need the leave",
                           duration="How long, e.g. 3d, 1w, 2w3d (min 1h, max 365d)")
    @command_check()
    async def loa_request(self, ctx: commands.Context, reason: str, duration: str):
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        seconds = parse_duration(duration)
        if seconds is None or not (MIN_SECONDS <= seconds <= MAX_SECONDS):
            return await _reply(ctx, error_embed(
                "I couldn't read that duration. Use things like `3d`, `1w`, `2w3d` or `12h` "
                "(between 1 hour and 365 days)."))

        settings = await _settings(ctx.guild.id)
        channel = ctx.guild.get_channel(int(settings["channel_id"])) if settings.get("channel_id") else None
        if not settings.get("role_id") or not isinstance(channel, discord.TextChannel):
            return await _reply(ctx, error_embed(
                "LOA isn't set up yet. An admin needs to choose the LOA role and request "
                "channel on the dashboard's **LOA** page."))

        existing = await self._open_doc(ctx.guild.id, ctx.author.id)
        if existing:
            what = "already on LOA" if existing["status"] == ACTIVE else "already have a pending request"
            return await _reply(ctx, error_embed(f"You {what}."))

        doc = {
            "guild_id": ctx.guild.id, "user_id": ctx.author.id,
            "reason": reason[:1000], "duration_seconds": seconds,
            "status": PENDING, "requested_at": datetime.now(timezone.utc),
            "channel_id": channel.id, "source": "request",
        }
        try:
            msg = await channel.send(embed=build_embed(doc), view=LoaView(),
                                     allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            return await _reply(ctx, error_embed(
                f"I couldn't post in {channel.mention}. Check my permissions there."))
        doc["message_id"] = msg.id
        await loa_requests_col().insert_one(doc)
        await _reply(ctx, success_embed(
            f"Your LOA request was sent to {channel.mention}. You'll get a DM when it's decided."))

    @loa.command(name="end", description="End a member's active LOA early")
    @app_commands.describe(member="The member whose LOA to end")
    @command_check()
    async def loa_end(self, ctx: commands.Context, member: discord.Member):
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        doc = await loa_requests_col().find_one(
            {"guild_id": ctx.guild.id, "user_id": member.id, "status": ACTIVE})
        if not doc:
            return await _reply(ctx, error_embed(f"{member.mention} isn't on LOA."))
        done, notes = await self._finish(doc, "ended", by=ctx.author.id,
                                         reason=f"LOA ended by {ctx.author}")
        if not done:
            return await _reply(ctx, error_embed("That LOA was already finished."))
        back = bool(done.get("paused_role_ids"))
        await _dm(member, info_embed(
            f"Your LOA in **{ctx.guild.name}** was ended early by staff."
            + (" Your ranks have been given back." if back else ""), title="🏁 LOA ended"))
        msg = f"Ended {member.mention}'s LOA and took the LOA role back."
        if back:
            msg += " Their paused ranks were given back."
        if notes:
            msg += "\n⚠️ " + " ".join(notes)
        await _reply(ctx, success_embed(msg), delete_after=15)

    @loa.command(name="add", description="Put a member on LOA directly (no request)")
    @app_commands.describe(member="The member to put on LOA",
                           duration="How long, e.g. 3d, 1w, 2w3d",
                           reason="Why (optional)")
    @command_check()
    async def loa_add(self, ctx: commands.Context, member: discord.Member, duration: str,
                      reason: str = None):
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        seconds = parse_duration(duration)
        if seconds is None or not (MIN_SECONDS <= seconds <= MAX_SECONDS):
            return await _reply(ctx, error_embed(
                "I couldn't read that duration. Use things like `3d`, `1w`, `2w3d` or `12h`."))
        if member.bot:
            return await _reply(ctx, error_embed("Bots can't go on LOA."))

        settings = await _settings(ctx.guild.id)
        if not settings.get("role_id"):
            return await _reply(ctx, error_embed(
                "No LOA role is set. Choose one on the dashboard's **LOA** page first."))
        if await self._open_doc(ctx.guild.id, member.id):
            return await _reply(ctx, error_embed(
                f"{member.mention} already has an active or pending LOA. Use `/loa remove` first."))

        problem = await _set_role(ctx.guild, member, settings["role_id"], True,
                                  f"LOA added by {ctx.author}")
        if problem:
            return await _reply(ctx, error_embed(problem))

        paused, failed = await _pause_ranks(ctx.guild, member)

        now = datetime.now(timezone.utc)
        doc = {
            "guild_id": ctx.guild.id, "user_id": member.id,
            "reason": (reason or "Added by an admin")[:1000], "duration_seconds": seconds,
            "status": ACTIVE, "requested_at": now, "start": now,
            "end": now + timedelta(seconds=seconds), "reviewer_id": ctx.author.id,
            "channel_id": None, "source": "admin", "paused_role_ids": paused,
        }
        channel = ctx.guild.get_channel(int(settings["channel_id"])) if settings.get("channel_id") else None
        if isinstance(channel, discord.TextChannel):
            try:
                msg = await channel.send(embed=build_embed(doc),
                                         allowed_mentions=discord.AllowedMentions.none())
                doc["channel_id"], doc["message_id"] = channel.id, msg.id
            except discord.HTTPException:
                pass
        await loa_requests_col().insert_one(doc)
        await _dm(member, info_embed(
            f"You were put on LOA in **{ctx.guild.name}** until {_ts(doc['end'])}."
            + (" Your staff ranks are paused and will be given back when it ends." if paused else ""),
            title="🏖️ LOA started"))
        msg = f"{member.mention} is on LOA for **{format_duration(seconds)}** (until {_ts(doc['end'])})."
        if paused:
            msg += "\nPaused ranks: " + " ".join(f"<@&{r}>" for r in paused)
        if failed:
            msg += "\n⚠️ I couldn't remove: " + ", ".join(failed) + " (move my role higher)."
        await _reply(ctx, success_embed(msg), delete_after=15)

    @loa.command(name="remove", description="Delete a member's LOA or pending request")
    @app_commands.describe(member="The member whose LOA to remove")
    @command_check()
    async def loa_remove(self, ctx: commands.Context, member: discord.Member):
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        doc = await self._open_doc(ctx.guild.id, member.id)
        if not doc:
            return await _reply(ctx, error_embed(f"{member.mention} has no active or pending LOA."))
        done, notes = await self._finish(doc, "removed", by=ctx.author.id,
                                         reason=f"LOA removed by {ctx.author}")
        if not done:
            return await _reply(ctx, error_embed("That LOA was already finished."))
        back = bool(done.get("paused_role_ids"))
        await _dm(member, info_embed(
            f"Your LOA in **{ctx.guild.name}** was removed by an admin."
            + (" Your ranks have been given back." if back else ""), title="🗑️ LOA removed"))
        msg = f"Removed {member.mention}'s LOA."
        if doc["status"] == ACTIVE:
            msg += " The LOA role was taken back"
            msg += " and their paused ranks were given back." if back else "."
        if notes:
            msg += "\n⚠️ " + " ".join(notes)
        await _reply(ctx, success_embed(msg), delete_after=15)


async def setup(bot: commands.Bot):
    await bot.add_cog(Loa(bot))
