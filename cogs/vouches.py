"""
Vouch / Scam-Vouch / Lookup / Report system.

Message triggers (plain text, NOT slash or prefix commands - only inside channels
configured as vouch channels via /vouch setchannel or the dashboard):
    vouch @user [reason]
    scam vouch @user [reason]
Both add a ✅ reaction to the original message when successfully counted.

Slash / prefix commands:
    /vouches [user]                         -> public vouch card for the user (defaults to self)
    /vouches channels                       -> list configured vouch channels in this guild
    /vouches setreportchannel <channel>     -> set the report channel for this guild
    /vouches scan <channel> [limit]         -> import vouch / scam vouch messages from a channel
    /vouches add <user> <type> [amount] <reason>
                                            -> manually add vouches (staff use)
    /vouches remove <user> <vouch_num>      -> delete a specific vouch of that user
    /vouches scammer add <user>             -> mark user as a global scammer + best-effort role
    /vouches scammer remove <user>          -> clear global scammer flag + best-effort role removal
    /vouch setchannel <channel>             -> add a vouch channel for this guild
    /vouch removechannel <channel>          -> remove a vouch channel for this guild
    /lookup <user>                          -> global vouch/scam counts + global scammer status
                                               + list of guilds where the user currently has the
                                               configured scammer role
    /report <user> <reason> <photo1> [photo2] [photo3]
                                            -> slash-only in practice; the prefix form replies
                                               with an embed telling the user to use /report
    /config                                 -> overview embed pointing at the dashboard page

Prefix forms use the guild's configured prefix (e.g. "!vouches @user", "?lookup @user").

Every vouch has a per-target-per-guild vouch_num (1, 2, 3, ...) so /vouches remove can
target a specific vouch of a specific user. The number is not guild-wide; each user's
vouches are numbered independently.

Staff Accept / Reject buttons on report embeds are persistent DynamicItems registered
in cog_load; they re-read the report from Mongo on every click so the dashboard and
the bot stay in sync without a restart.

Staff permission to decide reports:
  - Admins always can.
  - Members with guild_settings.vouches.report_staff_role_id can also decide.
  - If no staff role is configured, only Admins can decide.
"""
import re
import discord
from discord.ext import commands

from database import models
from utils.embeds import success_embed, error_embed, info_embed, base_embed
from utils.permissions import command_check
from config import constants


VOUCH_PREFIX_RE = re.compile(r"^vouch\s+", re.IGNORECASE)
SCAM_VOUCH_PREFIX_RE = re.compile(r"^scam\s+vouch\s+", re.IGNORECASE)

SCAN_DEFAULT_LIMIT = 500
SCAN_MAX_LIMIT = 2000

ADD_MAX_AMOUNT = 50


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def _fmt_reason(reason: str, limit: int = 120) -> str:
    reason = (reason or "").strip() or "*(no reason)*"
    if len(reason) > limit:
        reason = reason[: limit - 3] + "..."
    return reason


def _vouches_card_embed(user: discord.abc.User, normal: int, scam: int,
                        recent: list, guild: discord.Guild) -> discord.Embed:
    embed = base_embed(
        title=f"⭐ Vouches — {user.display_name}",
        color=constants.COLOR_DEFAULT,
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="👤 User", value=f"{user.mention}\n`{user.id}`", inline=False)
    embed.add_field(name="⭐ Vouches", value=str(normal), inline=True)
    embed.add_field(name="🚨 Scam Vouches", value=str(scam), inline=True)
    embed.add_field(name="​", value="​", inline=True)  # spacing on a 3-col row

    if recent:
        lines = []
        for v in recent:
            mark = "🚨" if v.get("type") == "scam" else "⭐"
            num = v.get("vouch_num")
            prefix = f"`#{num}` " if num is not None else ""
            lines.append(f"{prefix}{mark} <@{v['author_id']}> — {_fmt_reason(v.get('reason'))}")
        embed.add_field(name="🕘 Recent", value="\n".join(lines)[:1024], inline=False)
    else:
        embed.add_field(name="🕘 Recent", value="*No vouches yet.*", inline=False)

    embed.set_footer(text=guild.name, icon_url=guild.icon.url if guild.icon else None)
    return embed


def _lookup_embed(user: discord.abc.User, normal: int, scam: int,
                  db_scammer: bool, scammer_record: dict, role_guilds: list,
                  guild: discord.Guild) -> discord.Embed:
    embed = base_embed(title=f"🔎 Lookup — {user.display_name}")
    embed.set_thumbnail(url=user.display_avatar.url)

    embed.add_field(name="👤 User", value=f"{user.mention}\n`{user.id}`", inline=False)
    embed.add_field(name="⭐ Vouches", value=str(normal), inline=True)
    embed.add_field(name="🚨 Scam Vouches", value=str(scam), inline=True)
    embed.add_field(name="​", value="​", inline=True)

    if db_scammer:
        src = (scammer_record or {}).get("current_source") or "unknown"
        embed.add_field(
            name="⚠️ Scammer Status",
            value=f"**Database: Marked as scammer**\nSource: `{src}`",
            inline=False,
        )
    else:
        embed.add_field(
            name="🟢 Scammer Status",
            value="Not marked as a scammer in the global database.",
            inline=False,
        )

    if role_guilds:
        embed.add_field(
            name="🎭 Discord Role Detected",
            value="\n".join(f"• {name}" for name in role_guilds)[:1024],
            inline=False,
        )
    elif db_scammer:
        embed.add_field(
            name="🎭 Discord Role Detected",
            value="No current Discord Scammer role detected.",
            inline=False,
        )

    embed.set_footer(text=guild.name, icon_url=guild.icon.url if guild.icon else None)
    return embed


def _report_embed(report: dict, reported: discord.abc.User,
                  reporter: discord.abc.User) -> discord.Embed:
    status = report.get("status", "Pending")
    color = {
        "Pending":  constants.COLOR_WARNING,
        "Accepted": constants.COLOR_ERROR,
        "Rejected": constants.COLOR_SUCCESS,
    }.get(status, constants.COLOR_DEFAULT)

    embed = base_embed(title=f"📨 Report `{report['_id']}`", color=color)
    embed.add_field(name="Reported User", value=f"{reported.mention}\n`{reported.id}`", inline=False)
    embed.add_field(name="Reporter", value=f"{reporter.mention}\n`{reporter.id}`", inline=False)
    embed.add_field(name="Reason", value=(report.get("reason") or "*none*")[:1024], inline=False)

    photos = report.get("photo_urls") or []
    for idx, url in enumerate(photos, start=1):
        embed.add_field(name=f"Photo {idx}", value=f"[Evidence {idx}]({url})", inline=False)
    if photos:
        embed.set_image(url=photos[0])

    embed.add_field(name="Status", value=f"`{status}`", inline=False)

    staff_id = report.get("staff_id")
    if staff_id and status != "Pending":
        verb = "Accepted" if status == "Accepted" else "Rejected"
        lines = [f"{verb} by: <@{staff_id}>"]
        if status == "Accepted":
            lines.append("⚠️ User marked as scammer in the global database.")
            if report.get("role_warning"):
                lines.append(f"⚠️ {report['role_warning']}")
        embed.add_field(name="Staff Decision", value="\n".join(lines)[:1024], inline=False)

    embed.set_footer(text=f"Guild {report.get('guild_id','?')}")
    return embed


# ---------------------------------------------------------------------------
# Staff permission helper
# ---------------------------------------------------------------------------

def _member_can_decide(member: discord.Member, cfg: dict) -> bool:
    """Admins always can. Otherwise the configured Report Staff Role is required.
    If no staff role is configured, only Admins can decide."""
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True

    staff_role_id = cfg.get("report_staff_role_id")
    if not staff_role_id:
        return False
    return any(str(r.id) == str(staff_role_id) for r in member.roles)


# ---------------------------------------------------------------------------
# Persistent staff decision buttons (DynamicItem)
# ---------------------------------------------------------------------------

class _ReportDecisionButton(discord.ui.DynamicItem[discord.ui.Button],
                            template=r"vouch_report_(?P<action>accept|reject):(?P<id>R[0-9]+)"):
    def __init__(self, action: str, report_id: str):
        self.action = action
        self.report_id = report_id
        label = "Accept Report" if action == "accept" else "Reject Report"
        style = discord.ButtonStyle.danger if action == "accept" else discord.ButtonStyle.secondary
        super().__init__(discord.ui.Button(
            label=label,
            style=style,
            custom_id=f"vouch_report_{action}:{report_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], match["id"])

    async def callback(self, interaction: discord.Interaction):
        report = await models.get_report(self.report_id)
        if not report:
            return await interaction.response.send_message(
                embed=error_embed("This report no longer exists."), ephemeral=True
            )

        guild = interaction.guild
        if guild is None or str(guild.id) != str(report.get("guild_id")):
            return await interaction.response.send_message(
                embed=error_embed("This report belongs to another server."), ephemeral=True
            )

        cfg = await models.get_vouch_config(guild.id)
        if not _member_can_decide(interaction.user, cfg):
            return await interaction.response.send_message(
                embed=error_embed(
                    "You don't have permission to decide reports. "
                    "Only server Admins or members with the configured staff role can."
                ),
                ephemeral=True,
            )

        if report.get("status") != "Pending":
            return await interaction.response.send_message(
                embed=info_embed(f"This report was already **{report['status']}**."),
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        if self.action == "accept":
            await self._handle_accept(interaction, report, guild)
        else:
            await self._handle_reject(interaction, report, guild)

    async def _edit_report_message(self, interaction, guild, report, role_warning=None):
        channel = guild.get_channel(int(report["channel_id"]))
        if channel is None:
            return
        try:
            message = await channel.fetch_message(int(report["message_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        try:
            reported = guild.get_member(int(report["reported_id"])) or \
                await interaction.client.fetch_user(int(report["reported_id"]))
        except (discord.NotFound, discord.HTTPException):
            reported = None
        try:
            reporter = await interaction.client.fetch_user(int(report["reporter_id"]))
        except (discord.NotFound, discord.HTTPException):
            reporter = None

        if reported is None or reporter is None:
            return

        merged = dict(report)
        if role_warning:
            merged["role_warning"] = role_warning
        embed = _report_embed(merged, reported, reporter)

        disabled_view = discord.ui.View(timeout=None)
        disabled_view.add_item(discord.ui.Button(
            label="Accept Report",
            style=discord.ButtonStyle.danger,
            custom_id=f"vouch_report_accept:{report['_id']}",
            disabled=True,
        ))
        disabled_view.add_item(discord.ui.Button(
            label="Reject Report",
            style=discord.ButtonStyle.secondary,
            custom_id=f"vouch_report_reject:{report['_id']}",
            disabled=True,
        ))

        try:
            await message.edit(embed=embed, view=disabled_view)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def _handle_accept(self, interaction, report, guild):
        claimed = await models.claim_report_decision(
            report["_id"], interaction.user.id, "Accepted"
        )
        if claimed is None:
            return await interaction.followup.send(
                embed=info_embed("Someone else already decided this report."),
                ephemeral=True,
            )

        await models.add_scammer(
            int(report["reported_id"]),
            source="report",
            staff_id=interaction.user.id,
            guild_id=guild.id,
            report_id=report["_id"],
            reason=report.get("reason"),
        )

        role_warning = None
        cfg = await models.get_vouch_config(guild.id)
        role_id = cfg.get("scammer_role_id")
        if not role_id:
            role_warning = "No Scammer role configured in this server — skipped role assignment."
        else:
            role = guild.get_role(int(role_id))
            member = guild.get_member(int(report["reported_id"]))
            if role is None:
                role_warning = "Configured Scammer role no longer exists — skipped role assignment."
            elif member is None:
                role_warning = "Could not assign Scammer role — user is not in this server."
            else:
                try:
                    await member.add_roles(
                        role,
                        reason=f"Report {report['_id']} accepted by {interaction.user}",
                    )
                except discord.Forbidden:
                    role_warning = "Could not assign Scammer role — missing permissions or role hierarchy."
                except discord.HTTPException:
                    role_warning = "Could not assign Scammer role — Discord API error."

        report = await models.get_report(report["_id"]) or report
        await self._edit_report_message(interaction, guild, report, role_warning)

        await interaction.followup.send(
            embed=success_embed(
                f"Report `{report['_id']}` accepted. "
                + (f"\n⚠️ {role_warning}" if role_warning else "")
            ),
            ephemeral=True,
        )

    async def _handle_reject(self, interaction, report, guild):
        claimed = await models.claim_report_decision(
            report["_id"], interaction.user.id, "Rejected"
        )
        if claimed is None:
            return await interaction.followup.send(
                embed=info_embed("Someone else already decided this report."),
                ephemeral=True,
            )
        report = await models.get_report(report["_id"]) or report
        await self._edit_report_message(interaction, guild, report)
        await interaction.followup.send(
            embed=success_embed(f"Report `{report['_id']}` rejected."),
            ephemeral=True,
        )


def _build_report_view(report_id: str, disabled: bool = False) -> discord.ui.View:
    """Build a View for the original report message. Buttons are declared with
    the exact custom_id shape the DynamicItem listens for, so on a live click
    discord.py will route the interaction to _ReportDecisionButton.callback
    (whether or not this View is the one attached to the message)."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="Accept Report",
        style=discord.ButtonStyle.danger,
        custom_id=f"vouch_report_accept:{report_id}",
        disabled=disabled,
    ))
    view.add_item(discord.ui.Button(
        label="Reject Report",
        style=discord.ButtonStyle.secondary,
        custom_id=f"vouch_report_reject:{report_id}",
        disabled=disabled,
    ))
    return view


# ---------------------------------------------------------------------------
# Scan helpers
# ---------------------------------------------------------------------------

def _parse_vouch_message(content: str):
    """Return (target_id, vouch_type, reason) if content is a vouch message, else None."""
    if not content:
        return None
    is_scam = bool(SCAM_VOUCH_PREFIX_RE.match(content))
    is_normal = (not is_scam) and bool(VOUCH_PREFIX_RE.match(content))
    if not (is_scam or is_normal):
        return None

    body = SCAM_VOUCH_PREFIX_RE.sub("", content, count=1) if is_scam \
        else VOUCH_PREFIX_RE.sub("", content, count=1)

    mention_match = re.search(r"<@!?(\d+)>", body)
    if not mention_match:
        return None
    target_id = int(mention_match.group(1))

    reason = re.sub(r"<@!?\d+>", "", body, count=1).strip()[:1000]
    return target_id, ("scam" if is_scam else "normal"), reason


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Vouches(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Persistent staff decision buttons.
        self.bot.add_dynamic_items(_ReportDecisionButton)

    # ------------------------------------------------------------------
    # on_message: vouch + scam vouch text triggers
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        parsed = _parse_vouch_message(message.content or "")
        if not parsed:
            return
        target_id, vouch_type, reason = parsed

        channel_ids = await models.get_guild_vouch_channel_ids(message.guild.id)
        if str(message.channel.id) not in channel_ids:
            return

        target = message.guild.get_member(target_id)
        if target is None:
            try:
                target = await self.bot.fetch_user(target_id)
            except (discord.NotFound, discord.HTTPException):
                return

        if target.id == message.author.id:
            return await message.reply(embed=error_embed("You can't vouch yourself."))
        if target.bot:
            return await message.reply(embed=error_embed("You can't vouch a bot."))
        if target.id == self.bot.user.id:
            return await message.reply(embed=error_embed("You can't vouch the bot."))

        if await models.message_already_vouched(message.guild.id, message.id):
            return

        try:
            await models.create_vouch(
                guild_id=message.guild.id,
                target_id=target_id,
                author_id=message.author.id,
                vouch_type=vouch_type,
                reason=reason,
                channel_id=message.channel.id,
                message_id=message.id,
            )
        except Exception as e:
            print(f"[vouches] failed to save vouch: {e}")
            return

        try:
            await message.add_reaction("✅")
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ------------------------------------------------------------------
    # /vouches group
    # ------------------------------------------------------------------
    @commands.hybrid_group(name="vouches", description="Vouch system")
    async def vouches(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=info_embed(
                "Use `/vouches view [user]`, `/vouches top`, `/vouches channels`, `/vouches setreportchannel`, "
                "`/vouches scan`, `/vouches add`, `/vouches remove`, "
                "`/vouches scammer add`, or `/vouches scammer remove`."
            ))

    @vouches.command(name="view", description="Show a user's vouch card (defaults to you)")
    @command_check()
    async def vouches_view(self, ctx: commands.Context, user: discord.User = None):
        target = user or ctx.author
        normal = await models.count_vouches(ctx.guild.id, target.id, "normal")
        scam = await models.count_vouches(ctx.guild.id, target.id, "scam")
        recent = await models.get_recent_vouches(ctx.guild.id, target.id, limit=5)
        await ctx.send(embed=_vouches_card_embed(target, normal, scam, recent, ctx.guild))

    @vouches.command(name="top", description="Show the most-vouched members")
    @command_check()
    async def vouches_top(self, ctx: commands.Context):
        rows = await models.top_vouched(ctx.guild.id, 10)
        if not rows:
            return await ctx.send(embed=info_embed("Nobody has any vouches yet."))
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{uid}> - **{count}** vouch{'' if count == 1 else 'es'}"
                 for i, (uid, count) in enumerate(rows)]
        await ctx.send(embed=base_embed(title="🏆 Most vouched", description="\n".join(lines)))

    @vouches.command(name="channels", description="List configured vouch channels")
    @command_check()
    async def vouches_channels(self, ctx: commands.Context):
        cfg = await models.get_vouch_config(ctx.guild.id)
        ids = cfg["channel_ids"]
        if not ids:
            return await ctx.send(embed=info_embed(
                "No vouch channels configured. Use `/vouch setchannel` or the dashboard."
            ))
        lines = []
        for cid in ids:
            channel = ctx.guild.get_channel(int(cid))
            lines.append(channel.mention if channel else f"`{cid}` (missing)")
        await ctx.send(embed=base_embed(title="📣 Vouch Channels", description="\n".join(lines)))

    @vouches.command(name="setreportchannel", description="Set the report channel")
    @command_check()
    async def vouches_setreportchannel(self, ctx: commands.Context,
                                       channel: discord.TextChannel):
        await models.set_vouch_config(ctx.guild.id, report_channel_id=str(channel.id))
        await ctx.send(embed=success_embed(f"Report channel set to {channel.mention}."))

    # -- scan ---------------------------------------------------------
    @vouches.command(name="scan", description="Import vouch messages from a channel")
    @command_check()
    async def vouches_scan(self, ctx: commands.Context, channel: discord.TextChannel,
                           limit: int = SCAN_DEFAULT_LIMIT):
        await ctx.defer()

        limit = max(1, min(int(limit), SCAN_MAX_LIMIT))

        if not channel.permissions_for(ctx.guild.me).read_message_history:
            return await ctx.send(embed=error_embed(
                f"I don't have permission to read message history in {channel.mention}."
            ))

        imported = 0
        skipped_dupes = 0
        skipped_invalid = 0
        skipped_bots = 0

        try:
            async for msg in channel.history(limit=limit, oldest_first=True):
                if msg.author.bot:
                    continue
                parsed = _parse_vouch_message(msg.content or "")
                if not parsed:
                    continue
                target_id, vouch_type, reason = parsed

                if target_id == msg.author.id:
                    skipped_bots += 1
                    continue

                target = ctx.guild.get_member(target_id)
                if target is None:
                    try:
                        target = await self.bot.fetch_user(target_id)
                    except (discord.NotFound, discord.HTTPException):
                        skipped_invalid += 1
                        continue
                if target.bot:
                    skipped_bots += 1
                    continue

                if await models.message_already_vouched(ctx.guild.id, msg.id):
                    skipped_dupes += 1
                    continue

                try:
                    await models.create_vouch(
                        guild_id=ctx.guild.id,
                        target_id=target_id,
                        author_id=msg.author.id,
                        vouch_type=vouch_type,
                        reason=reason,
                        channel_id=channel.id,
                        message_id=msg.id,
                    )
                    imported += 1
                except Exception as e:
                    print(f"[vouches] scan insert failed for message {msg.id}: {e}")
                    skipped_invalid += 1
        except discord.Forbidden:
            return await ctx.send(embed=error_embed(
                f"I don't have permission to read history in {channel.mention}."
            ))

        embed = base_embed(title="🔍 Scan Complete", description=(
            f"Scanned **{limit}** message(s) in {channel.mention}.\n\n"
            f"✅ Imported: **{imported}**\n"
            f"⏭️ Already saved (skipped): **{skipped_dupes}**\n"
            f"🤖 Bot/self vouches (skipped): **{skipped_bots}**\n"
            f"❓ Unresolvable targets (skipped): **{skipped_invalid}**"
        ))
        await ctx.send(embed=embed)

    # -- add ----------------------------------------------------------
    @vouches.command(name="add", description="Manually add vouch(es) (staff use)")
    @command_check()
    async def vouches_add(self, ctx: commands.Context,
                          user: discord.User,
                          vouch_type: str,
                          amount: int = 1,
                          *,
                          reason: str = None):
        vouch_type = (vouch_type or "").strip().lower()
        if vouch_type not in ("normal", "scam"):
            return await ctx.send(embed=error_embed("Type must be `normal` or `scam`."))
        if user.id == ctx.author.id:
            return await ctx.send(embed=error_embed("You can't vouch yourself."))
        if user.bot:
            return await ctx.send(embed=error_embed("You can't vouch a bot."))

        try:
            amount = int(amount)
        except (TypeError, ValueError):
            amount = 1
        amount = max(1, min(amount, ADD_MAX_AMOUNT))

        reason = (reason or "").strip()[:1000]
        first_num = None
        last_num = None
        created = 0

        for _ in range(amount):
            try:
                doc = await models.create_vouch(
                    guild_id=ctx.guild.id,
                    target_id=user.id,
                    author_id=ctx.author.id,
                    vouch_type=vouch_type,
                    reason=reason,
                    channel_id=ctx.channel.id,
                    message_id=None,
                )
            except Exception as e:
                print(f"[vouches] add failed: {e}")
                break
            created += 1
            if first_num is None:
                first_num = doc["vouch_num"]
            last_num = doc["vouch_num"]

        if created == 0:
            return await ctx.send(embed=error_embed("Failed to add any vouches."))

        mark = "🚨" if vouch_type == "scam" else "⭐"
        if created == 1:
            desc = f"Added vouch `#{first_num}` {mark} for {user.mention}."
        else:
            desc = (
                f"Added **{created}** vouches {mark} for {user.mention} "
                f"(`#{first_num}` → `#{last_num}`)."
            )
        if reason:
            desc += f"\nReason: {_fmt_reason(reason)}"
        await ctx.send(embed=success_embed(desc))

    # -- remove -------------------------------------------------------
    @vouches.command(name="remove", description="Delete a specific vouch of a user")
    @command_check()
    async def vouches_remove(self, ctx: commands.Context,
                             user: discord.User,
                             vouch_num: int):
        doc = await models.delete_vouch_by_num(ctx.guild.id, user.id, vouch_num)
        if not doc:
            return await ctx.send(embed=error_embed(
                f"{user.mention} has no vouch `#{vouch_num}` in this server."
            ))

        mark = "🚨" if doc.get("type") == "scam" else "⭐"
        await ctx.send(embed=success_embed(
            f"Deleted vouch `#{vouch_num}` {mark} for {user.mention}."
            + (f"\nReason was: {_fmt_reason(doc.get('reason'))}" if doc.get("reason") else "")
        ))

    @vouches_remove.autocomplete("vouch_num")
    async def vouches_remove_ac(self, ctx: commands.Context, current: str):
        # Discord.py exposes the other params via ctx.interaction.namespace on autocomplete.
        user_id = None
        if ctx.interaction is not None:
            ns = ctx.interaction.namespace
            selected_user = getattr(ns, "user", None)
            if selected_user is not None:
                user_id = getattr(selected_user, "id", None)

        if user_id is None:
            return []

        vouches = await models.get_target_vouches(ctx.guild.id, user_id, limit=25)
        out = []
        for v in vouches:
            num = v.get("vouch_num")
            if num is None:
                continue
            if current and not str(num).startswith(current):
                continue
            mark = "🚨" if v.get("type") == "scam" else "⭐"
            label = f"#{num} {mark} — {_fmt_reason(v.get('reason'), 50)}"
            out.append(discord.app_commands.Choice(name=label[:100], value=int(num)))
        return out

    # -- scammer add / remove sub-group -------------------------------
    @vouches.group(name="scammer", description="Manually manage global scammer status",
                   invoke_without_command=True)
    async def vouches_scammer(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=info_embed(
                "Use `/vouches scammer add <user>` or `/vouches scammer remove <user>`."
            ))

    @vouches_scammer.command(name="add", description="Mark a user as a global scammer")
    @command_check()
    async def vouches_scammer_add(self, ctx: commands.Context, user: discord.User):
        await models.add_scammer(
            user.id,
            source="manual",
            staff_id=ctx.author.id,
            guild_id=ctx.guild.id,
        )

        warning = None
        cfg = await models.get_vouch_config(ctx.guild.id)
        role_id = cfg.get("scammer_role_id")
        if not role_id:
            warning = "No Scammer role configured in this server — role not assigned."
        else:
            role = ctx.guild.get_role(int(role_id))
            member = ctx.guild.get_member(user.id)
            if role is None:
                warning = "Configured Scammer role no longer exists."
            elif member is None:
                warning = "User is not in this server — role not assigned."
            else:
                try:
                    await member.add_roles(role, reason=f"Manual scammer add by {ctx.author}")
                except discord.Forbidden:
                    warning = "Missing permissions / role hierarchy — role not assigned."
                except discord.HTTPException:
                    warning = "Discord API error while assigning role."

        desc = f"{user.mention} is now marked as a **global scammer** in the database."
        if warning:
            desc += f"\n⚠️ {warning}"
        await ctx.send(embed=success_embed(desc))

    @vouches_scammer.command(name="remove", description="Clear a user's global scammer status")
    @command_check()
    async def vouches_scammer_remove(self, ctx: commands.Context, user: discord.User):
        await models.remove_scammer(
            user.id,
            source="manual",
            staff_id=ctx.author.id,
            guild_id=ctx.guild.id,
        )

        warning = None
        cfg = await models.get_vouch_config(ctx.guild.id)
        role_id = cfg.get("scammer_role_id")
        if not role_id:
            warning = "No Scammer role configured — nothing to remove."
        else:
            role = ctx.guild.get_role(int(role_id))
            member = ctx.guild.get_member(user.id)
            if role is None:
                warning = "Configured Scammer role no longer exists."
            elif member is None:
                warning = "User is not in this server — no role to remove."
            elif role not in member.roles:
                warning = "User did not have the Scammer role."
            else:
                try:
                    await member.remove_roles(role, reason=f"Manual scammer remove by {ctx.author}")
                except discord.Forbidden:
                    warning = "Missing permissions / role hierarchy — role not removed."
                except discord.HTTPException:
                    warning = "Discord API error while removing role."

        desc = f"{user.mention} is no longer marked as a global scammer in the database."
        if warning:
            desc += f"\nℹ️ {warning}"
        await ctx.send(embed=success_embed(desc))

    # ------------------------------------------------------------------
    # /vouch group (channels)
    # ------------------------------------------------------------------
    @commands.hybrid_group(name="vouch", description="Vouch channel configuration")
    async def vouch(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=info_embed(
                "Use `/vouch setchannel <channel>` or `/vouch removechannel <channel>`."
            ))

    @vouch.command(name="setchannel", description="Add a vouch channel")
    @command_check()
    async def vouch_setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        added = await models.add_vouch_channel(ctx.guild.id, channel.id)
        if not added:
            return await ctx.send(embed=info_embed(f"{channel.mention} is already a vouch channel."))
        await ctx.send(embed=success_embed(f"Added {channel.mention} as a vouch channel."))

    @vouch.command(name="removechannel", description="Remove a vouch channel")
    @command_check()
    async def vouch_removechannel(self, ctx: commands.Context, channel: discord.TextChannel):
        removed = await models.remove_vouch_channel(ctx.guild.id, channel.id)
        if not removed:
            return await ctx.send(embed=info_embed(f"{channel.mention} wasn't a vouch channel."))
        await ctx.send(embed=success_embed(f"Removed {channel.mention} from vouch channels."))

    # ------------------------------------------------------------------
    # /lookup
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="lookup", description="Look up a user's reputation")
    @command_check()
    async def lookup(self, ctx: commands.Context, user: discord.User):
        await ctx.defer()

        normal = await models.count_vouches(ctx.guild.id, user.id, "normal")
        scam = await models.count_vouches(ctx.guild.id, user.id, "scam")

        record = await models.get_scammer_record(user.id)
        db_scammer = bool(record and record.get("active"))

        role_guilds = []
        for guild in self.bot.guilds:
            cfg = await models.get_vouch_config(guild.id)
            role_id = cfg.get("scammer_role_id")
            if not role_id:
                continue
            member = guild.get_member(user.id)
            if member is None:
                continue
            if any(str(r.id) == str(role_id) for r in member.roles):
                role_guilds.append(guild.name)

        await ctx.send(embed=_lookup_embed(
            user, normal, scam, db_scammer, record, role_guilds, ctx.guild
        ))

    # ------------------------------------------------------------------
    # /report
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="report", description="Report a user with evidence")
    @command_check()
    async def report(self, ctx: commands.Context, user: discord.User, *,
                     reason: str,
                     photo1: discord.Attachment,
                     photo2: discord.Attachment = None,
                     photo3: discord.Attachment = None):
        if ctx.interaction is None:
            return await ctx.send(embed=error_embed(
                "Please use the slash version: `/report` — it needs image attachments, "
                "which aren't available through the prefix form."
            ))

        if user.id == ctx.author.id:
            return await ctx.send(embed=error_embed("You can't report yourself."))
        if user.bot:
            return await ctx.send(embed=error_embed("You can't report a bot."))

        cfg = await models.get_vouch_config(ctx.guild.id)
        report_channel_id = cfg.get("report_channel_id")
        if not report_channel_id:
            return await ctx.send(embed=error_embed(
                "No report channel configured. Ask an admin to run "
                "`/vouches setreportchannel` or set it on the dashboard."
            ))
        report_channel = ctx.guild.get_channel(int(report_channel_id))
        if report_channel is None:
            return await ctx.send(embed=error_embed(
                "The configured report channel no longer exists."
            ))

        photos = [photo1.url]
        if photo2 is not None:
            photos.append(photo2.url)
        if photo3 is not None:
            photos.append(photo3.url)

        placeholder = base_embed(
            title="📨 Report — Submitting...",
            description=f"Reported: {user.mention}\nReporter: {ctx.author.mention}",
        )
        try:
            sent = await report_channel.send(embed=placeholder)
        except discord.Forbidden:
            return await ctx.send(embed=error_embed(
                "I can't post in the configured report channel."
            ))

        report_id = await models.create_report(
            guild_id=ctx.guild.id,
            reported_id=user.id,
            reporter_id=ctx.author.id,
            reason=reason,
            photo_urls=photos,
            channel_id=report_channel.id,
            message_id=sent.id,
        )

        report_doc = await models.get_report(report_id)
        embed = _report_embed(report_doc, user, ctx.author)
        try:
            await sent.edit(embed=embed, view=_build_report_view(report_id, disabled=False))
        except (discord.Forbidden, discord.HTTPException):
            pass

        await ctx.send(embed=success_embed(
            f"Report `{report_id}` submitted to {report_channel.mention}."
        ))

    # ------------------------------------------------------------------
    # /config
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="config", description="Vouch system configuration overview")
    @command_check()
    async def config(self, ctx: commands.Context):
        cfg = await models.get_vouch_config(ctx.guild.id)

        lines = []
        if cfg["channel_ids"]:
            chans = []
            for cid in cfg["channel_ids"]:
                channel = ctx.guild.get_channel(int(cid))
                chans.append(channel.mention if channel else f"`{cid}`")
            lines.append("**Vouch channels:** " + ", ".join(chans))
        else:
            lines.append("**Vouch channels:** *none* — set with `/vouch setchannel`")

        if cfg["report_channel_id"]:
            rc = ctx.guild.get_channel(int(cfg["report_channel_id"]))
            lines.append(f"**Report channel:** {rc.mention if rc else '`' + cfg['report_channel_id'] + '`'}")
        else:
            lines.append("**Report channel:** *none* — set with `/vouches setreportchannel`")

        if cfg["scammer_role_id"]:
            role = ctx.guild.get_role(int(cfg["scammer_role_id"]))
            lines.append(f"**Scammer role:** {role.mention if role else '`' + cfg['scammer_role_id'] + '`'}")
        else:
            lines.append("**Scammer role:** *none* — set on the dashboard")

        if cfg.get("report_staff_role_id"):
            role = ctx.guild.get_role(int(cfg["report_staff_role_id"]))
            lines.append(
                f"**Report staff role:** {role.mention if role else '`' + cfg['report_staff_role_id'] + '`'}"
            )
        else:
            lines.append("**Report staff role:** *none* — only Admins can decide reports")

        lines.append("")
        lines.append("Configure everything on the dashboard's **Vouches** page.")

        await ctx.send(embed=base_embed(title="⚙️ Vouch Configuration", description="\n".join(lines)))


async def setup(bot: commands.Bot):
    await bot.add_cog(Vouches(bot))