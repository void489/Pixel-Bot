"""
Staff application system.

Panel buttons open a DM session with the applicant, walk them through the
configured questions, and post a submission to the configured submit channel
with Accept / Deny / Accept with reason / Deny with reason / History / Open
ticket / View on dashboard buttons.

On decision:
  - The submission embed is MOVED from the submit channel to the configured
    accepted channel or denied channel.
  - On accept, if the application has a Change type linked, the applicant is promoted in it
    like /promote (rung 1+, extra roles included - see cogs/changes.promote_application).
    No type linked = no roles are given.
  - The applicant is ALWAYS DM'd the outcome, with the reason if any, and the
    per-app accept/deny message if configured.
  - Roles in `ping_role_ids` are pinged in the submit channel when an application is
    SUBMITTED (not on accept/deny).

Everything is configured on the dashboard. There are no / or prefix commands.
Members with the dashboard's "Application blacklist role" can't open an application.

Sessions are stored in Mongo (application_sessions) so a bot restart doesn't lose
in-progress applications. A background sweeper times out stale sessions.

Panel posting is triggered from the dashboard through the `dashboard_tasks`
queue, which a background loop polls every 3 seconds.

Staff decision buttons are persistent DynamicItems registered in cog_load.
"""
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from database import models
from cogs.tickets import TicketControlView
from utils.embeds import success_embed, error_embed, info_embed, base_embed
from config import constants, settings

from utils.embeds import success_embed, error_embed, info_embed, base_embed
from utils.permissions import command_check
MAX_APPS_PER_PANEL = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _relative(ts: datetime) -> str:
    if ts is None:
        return "unknown"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return discord.utils.format_dt(ts, "R")


def _join_duration(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    if member is None or member.joined_at is None:
        return "unknown"
    delta = datetime.now(timezone.utc) - member.joined_at
    return f"{delta.days} day(s) ago"


def _member_can_decide(member: discord.Member, app: dict) -> bool:
    """Admins always can. Otherwise the member must have at least one role in
    app['decision_role_ids']. If that list is empty, only Admins can decide."""
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    role_ids = {str(r) for r in (app.get("decision_role_ids") or [])}
    if not role_ids:
        return False
    return bool(role_ids & {str(r.id) for r in member.roles})


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def _panel_embed(panel: dict, apps: list) -> discord.Embed:
    title = panel.get("title") or "Applications"
    lines = []
    if panel.get("description"):
        lines.append(panel["description"])
    lines.append("")
    for app in apps:
        status = "🟢 Open" if app.get("is_open") else "🔴 Closed"
        emoji = app.get("emoji") or "📝"
        lines.append(f"{emoji} **{app.get('name', 'Application')}** • {status}")

    embed = base_embed(title=title, description="\n".join(lines).strip())
    if panel.get("color"):
        embed.color = int(panel["color"])
    if panel.get("footer"):
        embed.set_footer(text=panel["footer"])
    return embed


def _review_embed(submission: dict, app: dict, guild: discord.Guild) -> discord.Embed:
    status = submission.get("status", "Pending")
    color = {
        "Pending":  constants.COLOR_WARNING,
        "Accepted": constants.COLOR_SUCCESS,
        "Denied":   constants.COLOR_ERROR,
    }.get(status, constants.COLOR_DEFAULT)

    applicant_id = submission.get("applicant_id")
    applicant_name = submission.get("applicant_name") or "unknown"

    embed = base_embed(
        title=f"📝 {app.get('name', 'Application')} — Submission",
        color=color,
    )
    embed.add_field(
        name="Submission stats",
        value=(
            f"**UserId:** `{applicant_id}`\n"
            f"**Username:** `{applicant_name}`\n"
            f"**User:** <@{applicant_id}>\n"
            f"**Duration:** {int(submission.get('total_duration_seconds') or 0)}s\n"
            f"**Joined guild:** {_join_duration(guild, int(applicant_id))}\n"
            f"**Submitted:** {_relative(submission.get('created_at'))}"
        ),
        inline=False,
    )

    for i, qa in enumerate(submission.get("answers", []), start=1):
        q = qa.get("question", "(unknown)")
        a = qa.get("answer", "")
        secs = qa.get("duration_seconds")
        header = f"Q{i}. {q}"
        if secs is not None:
            header += f"  ·  ⏱ {secs}s"
        embed.add_field(name=header[:250], value=(a or "*(no answer)*")[:1024], inline=False)

    embed.add_field(name="Status", value=f"`{status}`", inline=False)

    staff_id = submission.get("staff_id")
    if staff_id and status != "Pending":
        verb = "Accepted" if status == "Accepted" else "Denied"
        line = f"{verb} by <@{staff_id}>"
        reason = submission.get("decision_reason")
        if reason:
            line += f"\nReason: {reason}"
        embed.add_field(name="Staff Decision", value=line[:1024], inline=False)

    embed.set_footer(text=f"Submission ID: {submission.get('_id')}")
    return embed


# ---------------------------------------------------------------------------
# Panel buttons
# ---------------------------------------------------------------------------

async def _resolve_app_category(guild: discord.Guild, app: dict):
    """The dashboard stores either a category ID or '__new__:<name>' (create on first use)."""
    from cogs.tickets import category_lock, find_category_by_name

    raw = app.get("ticket_category_id")
    if not raw:
        return None

    if str(raw).startswith("__new__:"):
        async with category_lock(guild.id):
            fresh = (await models.get_application(guild.id, str(app["_id"]))).get("ticket_category_id")
            if fresh and not str(fresh).startswith("__new__:"):
                raw = fresh
            else:
                name = str(raw)[len("__new__:"):].strip() or "Applications"
                category = find_category_by_name(guild, name)
                if category is None:
                    try:
                        category = await guild.create_category(name=name, reason="Application ticket setup")
                    except discord.HTTPException:
                        return None
                await models.update_application(
                    guild.id, str(app["_id"]), {"ticket_category_id": str(category.id)}
                )
                return category

    try:
        category = guild.get_channel(int(raw))
    except (TypeError, ValueError):
        return None
    return category if isinstance(category, discord.CategoryChannel) else None


class _PanelOpenButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r"application_open:(?P<id>[0-9a-f]{24})"):
    def __init__(self, application_id: str, label: str, emoji: str = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.primary):
        self.application_id = application_id
        super().__init__(discord.ui.Button(
            label=label[:80],
            emoji=emoji or None,
            style=style,
            custom_id=f"application_open:{application_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        app = await models.get_application_by_id_any_guild(match["id"])
        label = (app or {}).get("name") or "Application"
        emoji = (app or {}).get("emoji")
        style_name = (app or {}).get("button_style") or "primary"
        style = {
            "primary":   discord.ButtonStyle.primary,
            "secondary": discord.ButtonStyle.secondary,
            "success":   discord.ButtonStyle.success,
            "danger":    discord.ButtonStyle.danger,
        }.get(style_name, discord.ButtonStyle.primary)
        return cls(match["id"], label, emoji, style)

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                embed=error_embed("Applications can only be opened from a server."),
                ephemeral=True,
            )

        app = await models.get_application(guild.id, self.application_id)
        if not app:
            return await interaction.response.send_message(
                embed=error_embed("This application no longer exists."),
                ephemeral=True,
            )

        # Application blacklist (dashboard: Config & Logs -> Application blacklist role)
        bl_role_id = (await models.get_blacklist_config(guild.id))["application_role_id"]
        if bl_role_id:
            member = guild.get_member(interaction.user.id)
            if member and any(str(r.id) == str(bl_role_id) for r in member.roles):
                return await interaction.response.send_message(
                    embed=error_embed("You're blacklisted from applying."), ephemeral=True
                )

        if not app.get("is_open", True):
            return await interaction.response.send_message(
                embed=info_embed("This application is currently **closed**."),
                ephemeral=True,
            )

        questions = [str(q) for q in (app.get("questions") or []) if str(q).strip()]
        if not questions:
            return await interaction.response.send_message(
                embed=error_embed("This application has no questions configured."),
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        # Ignore rapid double-clicks on the same panel button.
        inflight_key = (guild.id, interaction.user.id, self.application_id)
        if inflight_key in _INFLIGHT_OPENS:
            return await interaction.followup.send(
                embed=info_embed("I've already sent you a DM for this application — check your messages."),
                ephemeral=True,
            )
        _INFLIGHT_OPENS.add(inflight_key)

        try:
            cfg = await models.get_application_config(guild.id)

            try:
                dm = await interaction.user.create_dm()
            except (discord.Forbidden, discord.HTTPException):
                return await interaction.followup.send(
                    embed=error_embed(_DM_BLOCKED_TEXT), ephemeral=True
                )

            # Replace any unfinished session for this application with a fresh,
            # NOT-yet-started one. Nothing is asked until the user presses Start.
            existing = await models.get_application_session(
                guild.id, interaction.user.id, self.application_id
            )
            if existing and not existing.get("completed"):
                await models.delete_application_session(
                    guild.id, interaction.user.id, self.application_id
                )

            await models.start_application_session(
                guild_id=guild.id,
                user_id=interaction.user.id,
                application_id=self.application_id,
                questions=questions,
                channel_id=dm.id,
            )

            try:
                await dm.send(
                    embed=info_embed(
                        f"You're about to start the **{app.get('name', 'Application')}** "
                        f"application ({len(questions)} questions).\n\n"
                        "Press **Start** to begin or **Cancel** to stop.\n"
                        "You can type `cancel` at any time to cancel your application.\n"
                        f"You have {cfg['dm_timeout_minutes']} minutes per question before it times out."
                    ),
                    view=_StartConfirmView(guild.id, self.application_id),
                )
            except (discord.Forbidden, discord.HTTPException):
                await models.delete_application_session(
                    guild.id, interaction.user.id, self.application_id
                )
                return await interaction.followup.send(
                    embed=error_embed(_DM_BLOCKED_TEXT), ephemeral=True
                )

            await interaction.followup.send(
                embed=success_embed(
                    f"I've sent you a DM for your **{app.get('name', 'Application')}**. "
                    "Press **Start** there to begin."
                ),
                ephemeral=True,
            )
        finally:
            _INFLIGHT_OPENS.discard(inflight_key)


_DM_BLOCKED_TEXT = (
    "I can't DM you. Please enable **Direct Messages from server members** "
    "in your privacy settings, then try again."
)

# (guild_id, user_id, application_id) tuples currently being opened.
_INFLIGHT_OPENS: set = set()


class _StartConfirmButton(discord.ui.DynamicItem[discord.ui.Button],
                          template=r"application_confirm:(?P<action>start|cancel):(?P<gid>[0-9]+):(?P<id>[0-9a-f]{24})"):
    """Start / Cancel buttons in the confirmation DM. Persistent across restarts."""

    def __init__(self, action: str, guild_id, application_id: str):
        self.action = action
        self.guild_id = int(guild_id)
        self.application_id = application_id
        super().__init__(discord.ui.Button(
            label="Start" if action == "start" else "Cancel",
            emoji="✅" if action == "start" else "❌",
            style=discord.ButtonStyle.success if action == "start" else discord.ButtonStyle.danger,
            custom_id=f"application_confirm:{action}:{guild_id}:{application_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], match["gid"], match["id"])

    async def _disable_message(self, interaction: discord.Interaction):
        """Strip the buttons so they can't be pressed twice."""
        try:
            await interaction.message.edit(view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id

        if self.action == "cancel":
            await interaction.response.defer()
            await self._disable_message(interaction)
            session = await models.get_application_session(self.guild_id, uid, self.application_id)
            if session and not session.get("completed"):
                await models.delete_application_session(self.guild_id, uid, self.application_id)
            return await interaction.channel.send(embed=info_embed(
                "Application cancelled. Click the panel button again whenever you want to apply."
            ))

        # --- Start ---
        await interaction.response.defer()
        await self._disable_message(interaction)

        # Atomic: only one caller can ever flip pending -> started, so the
        # first question can never be sent twice.
        session = await models.begin_application_session(self.guild_id, uid, self.application_id)
        if session is None:
            return await interaction.channel.send(embed=info_embed(
                "This application was already started, cancelled, or has expired. "
                "Click the panel button again to restart."
            ))

        await _send_question(interaction.channel, session, 0)


class _StartConfirmView(discord.ui.View):
    def __init__(self, guild_id, application_id: str):
        super().__init__(timeout=None)
        self.add_item(_StartConfirmButton("start", guild_id, application_id))
        self.add_item(_StartConfirmButton("cancel", guild_id, application_id))


async def _send_question(dm: discord.DMChannel, session: dict, index: int):
    questions = session.get("questions") or []
    if index >= len(questions):
        return
    embed = base_embed(
        title=f"Question {index + 1} of {len(questions)}",
        description=questions[index],
    )
    embed.set_footer(text="Type cancel anytime to cancel your application.")
    await dm.send(embed=embed)


# ---------------------------------------------------------------------------
# Review buttons
# ---------------------------------------------------------------------------

class _ReviewDecisionButton(discord.ui.DynamicItem[discord.ui.Button],
                            template=r"application_(?P<action>accept|deny|accept_reason|deny_reason|history|ticket):(?P<id>[0-9a-f]{24})"):
    def __init__(self, action: str, submission_id: str):
        self.action = action
        self.submission_id = submission_id
        labels = {
            "accept":        ("Accept",                discord.ButtonStyle.success, "✅"),
            "deny":          ("Deny",                  discord.ButtonStyle.danger,  "❌"),
            "accept_reason": ("Accept with reason",    discord.ButtonStyle.success, "✅"),
            "deny_reason":   ("Deny with reason",      discord.ButtonStyle.danger,  "❌"),
            "history":       ("History",               discord.ButtonStyle.primary, "🕘"),
            "ticket":        ("Open ticket with user", discord.ButtonStyle.secondary, "📩"),
        }
        label, style, emoji = labels.get(action, ("Action", discord.ButtonStyle.secondary, None))
        super().__init__(discord.ui.Button(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=f"application_{action}:{submission_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], match["id"])

    async def callback(self, interaction: discord.Interaction):
        sub = await models.get_application_submission(self.submission_id)
        if not sub:
            return await interaction.response.send_message(
                embed=error_embed("This submission no longer exists."), ephemeral=True
            )

        guild = interaction.guild
        if guild is None or str(guild.id) != str(sub.get("guild_id")):
            return await interaction.response.send_message(
                embed=error_embed("This submission belongs to another server."),
                ephemeral=True,
            )

        app = await models.get_application(guild.id, sub.get("application_id"))
        if not app:
            return await interaction.response.send_message(
                embed=error_embed("The application this came from no longer exists."),
                ephemeral=True,
            )

        if self.action in ("accept", "deny", "accept_reason", "deny_reason"):
            if not _member_can_decide(interaction.user, app):
                return await interaction.response.send_message(
                    embed=error_embed(
                        "You don't have permission to decide this application. "
                        "Only Admins or members with one of the configured decision roles can."
                    ),
                    ephemeral=True,
                )

        if self.action == "history":
            return await self._handle_history(interaction, sub, guild)

        if self.action == "ticket":
            return await self._handle_ticket(interaction, sub, app, guild)

        if self.action in ("accept_reason", "deny_reason"):
            return await interaction.response.send_modal(
                _ReasonModal(self.submission_id, self.action)
            )

        await interaction.response.defer(ephemeral=True)
        await self._apply_decision(
            interaction, sub, app, guild,
            status=("Accepted" if self.action == "accept" else "Denied"),
            reason=None,
        )

    async def _handle_history(self, interaction, sub, guild):
        subs = await models.get_submissions_for_user(
            guild.id, int(sub["applicant_id"]), limit=10
        )
        lines = []
        for s in subs:
            emoji = {"Accepted": "✅", "Denied": "❌", "Pending": "⏳"}.get(s.get("status"), "?")
            name = s.get("application_name") or "Application"
            lines.append(f"{emoji} **{name}** — {s.get('status')} — {_relative(s.get('created_at'))}")
        if not lines:
            lines = ["*No previous submissions.*"]
        await interaction.response.send_message(
            embed=base_embed(
                title=f"History — <@{sub['applicant_id']}>",
                description="\n".join(lines)[:4000],
            ),
            ephemeral=True,
        )

    async def _handle_ticket(self, interaction, sub, app, guild):
        await interaction.response.defer(ephemeral=True)

        applicant = guild.get_member(int(sub["applicant_id"]))
        if applicant is None:
            try:
                applicant = await interaction.client.fetch_user(int(sub["applicant_id"]))
            except (discord.NotFound, discord.HTTPException):
                applicant = None

        category = await _resolve_app_category(guild, app)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True
            ),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
        }
        if isinstance(applicant, discord.Member):
            overwrites[applicant] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )
        for rid in (app.get("ticket_allow_role_ids") or []):
            role = guild.get_role(int(rid))
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )

        from cogs.tickets import category_lock
        from utils import ticket_core
        try:
            # (_resolve_app_category above takes the lock itself, so it can't run in here)
            async with category_lock(guild.id):
                category, overflow = await ticket_core.kind_category_plan(guild, category, "application")
                channel = await ticket_core.create_ticket_channel(
                    guild, category, overflow,
                    name=f"app-{sub.get('applicant_name', 'user')}"[:90],
                    overwrites=overwrites,
                    reason=f"Application ticket for {sub['applicant_id']}",
                )
        except ticket_core.CategoriesFull as e:
            await ticket_core.alert_staff_full(guild, e.names, "application ticket")
            return await interaction.followup.send(
                embed=error_embed(ticket_core.FULL_MESSAGE), ephemeral=True
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            return await interaction.followup.send(
                embed=error_embed(f"Could not create ticket: {e}"), ephemeral=True
            )

        await models.create_ticket(
            guild_id=guild.id,
            channel_id=channel.id,
            opener_id=int(sub["applicant_id"]),
            panel_name=app.get("name"),
            ticket_type_id=None,
            kind="application",
            extra={"application_id": str(app["_id"]), "submission_id": str(sub["_id"])},
        )

        ping_parts = []
        if isinstance(applicant, discord.Member):
            ping_parts.append(applicant.mention)
        for rid in (app.get("ticket_ping_role_ids") or []):
            role = guild.get_role(int(rid))
            if role:
                ping_parts.append(role.mention)

        await channel.send(
            content=" ".join(ping_parts) if ping_parts else None,
            embed=base_embed(
                title=f"📩 Application ticket — {app.get('name', 'Application')}",
                description=(
                    f"Submission `{sub['_id']}` from <@{sub['applicant_id']}>.\n"
                    "Use the buttons below to claim or close it."
                ),
            ),
            view=TicketControlView(),
        )

        await interaction.followup.send(
            embed=success_embed(f"Ticket created: {channel.mention}"), ephemeral=True
        )

    async def _apply_decision(self, interaction, sub, app, guild, status: str, reason: str):
        claimed = await models.claim_submission_decision(
            str(sub["_id"]), interaction.user.id, status, reason
        )
        if claimed is None:
            return await interaction.followup.send(
                embed=info_embed("Someone else already decided this submission."),
                ephemeral=True,
            )

        # On accept: promote the applicant in the linked Change type (exactly like /promote,
        # but always onto rung 1+). Role failures go in the warning spot (reviewer + DM);
        # `review_notes` (LOA, type gone...) are for the reviewer only. A failure here never
        # breaks the accept flow.
        role_warnings = []
        review_notes = []
        if status == "Accepted":
            applicant = guild.get_member(int(sub["applicant_id"]))
            try:
                from cogs.changes import promote_application
                role_warnings, review_notes = await promote_application(
                    guild, applicant, app.get("change_type_id"),
                    decided_by=interaction.user, reason=reason,
                )
            except Exception as e:
                print(f"[applications] promotion failed: {e}")
                review_notes = ["Something went wrong promoting the applicant. Use `/promote` instead."]

        refreshed = await models.get_application_submission(str(sub["_id"])) or sub

        dest_channel_id = app.get("accepted_channel_id") if status == "Accepted" \
            else app.get("denied_channel_id")
        dest_channel = guild.get_channel(int(dest_channel_id)) if dest_channel_id else None

        # Delete the old message from the submit channel.
        old_channel_id = refreshed.get("submit_channel_id")
        old_message_id = refreshed.get("submit_message_id")
        if old_channel_id and old_message_id:
            old_channel = guild.get_channel(int(old_channel_id))
            if old_channel:
                try:
                    old_msg = await old_channel.fetch_message(int(old_message_id))
                    await old_msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            await models.clear_submission_submit_message(str(sub["_id"]))

        # Post to the destination channel (no pings on decision).
        if dest_channel is not None:
            try:
                posted = await dest_channel.send(
                    embed=_review_embed(refreshed, app, guild),
                    view=_disabled_review_view(refreshed),
                )
                await models.set_submission_final_message(str(sub["_id"]), dest_channel.id, posted.id)
            except (discord.Forbidden, discord.HTTPException):
                pass

        # ALWAYS DM the applicant, with reason if any, prefixed by the configured message if set.
        configured_message = app.get("accept_message") if status == "Accepted" \
            else app.get("deny_message")
        default_message = (
            f"Your application for **{app.get('name', 'Application')}** has been **accepted**."
            if status == "Accepted"
            else f"Your application for **{app.get('name', 'Application')}** has been **denied**."
        )
        body = (configured_message or "").strip() or default_message
        if reason:
            body += f"\n\n**Reason:** {reason}"
        if role_warnings:
            body += "\n\n⚠️ Some roles couldn't be assigned: " + "; ".join(role_warnings)

        try:
            user = await interaction.client.fetch_user(int(sub["applicant_id"]))
            await user.send(embed=base_embed(
                title=f"{app.get('name', 'Application')} — {status}",
                description=body[:4000],
                color=(constants.COLOR_SUCCESS if status == "Accepted"
                       else constants.COLOR_ERROR),
            ))
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

        await interaction.followup.send(
            embed=success_embed(
                f"Submission `{sub['_id']}` marked **{status}**."
                + (f"\n⚠️ {'; '.join(role_warnings + review_notes)}"
                   if (role_warnings or review_notes) else "")
            ),
            ephemeral=True,
        )


class _ReasonModal(discord.ui.Modal):
    def __init__(self, submission_id: str, action: str):
        title = "Accept with reason" if action.startswith("accept") else "Deny with reason"
        super().__init__(title=title[:45])
        self.submission_id = submission_id
        self.action = action
        self.add_item(discord.ui.TextInput(
            label="Reason",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=500,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        sub = await models.get_application_submission(self.submission_id)
        if not sub:
            return await interaction.response.send_message(
                embed=error_embed("This submission no longer exists."), ephemeral=True
            )
        guild = interaction.guild
        app = await models.get_application(guild.id, sub.get("application_id"))
        if not app:
            return await interaction.response.send_message(
                embed=error_embed("The application no longer exists."), ephemeral=True
            )
        if not _member_can_decide(interaction.user, app):
            return await interaction.response.send_message(
                embed=error_embed("You don't have permission to decide this application."),
                ephemeral=True,
            )

        status = "Accepted" if self.action.startswith("accept") else "Denied"
        reason = self.children[0].value.strip()

        await interaction.response.defer(ephemeral=True)

        btn = _ReviewDecisionButton(self.action, self.submission_id)
        await btn._apply_decision(interaction, sub, app, guild, status=status, reason=reason)


def _disabled_review_view(submission: dict) -> discord.ui.View:
    sid = str(submission["_id"])
    view = discord.ui.View(timeout=None)
    for action, label, style in (
        ("accept", "Accept", discord.ButtonStyle.success),
        ("deny", "Deny", discord.ButtonStyle.danger),
        ("accept_reason", "Accept with reason", discord.ButtonStyle.success),
        ("deny_reason", "Deny with reason", discord.ButtonStyle.danger),
    ):
        view.add_item(discord.ui.Button(
            label=label, style=style,
            custom_id=f"application_{action}:{sid}", disabled=True,
        ))
    view.add_item(discord.ui.Button(
        label="History", style=discord.ButtonStyle.primary,
        custom_id=f"application_history:{sid}", disabled=True,
    ))
    view.add_item(discord.ui.Button(
        label="Open ticket with user", style=discord.ButtonStyle.secondary,
        custom_id=f"application_ticket:{sid}", disabled=True,
    ))
    return view


def build_review_view(submission: dict, dashboard_url: str = None) -> discord.ui.View:
    sid = str(submission["_id"])
    view = discord.ui.View(timeout=None)
    for action, label, style in (
        ("accept", "Accept", discord.ButtonStyle.success),
        ("deny", "Deny", discord.ButtonStyle.danger),
        ("accept_reason", "Accept with reason", discord.ButtonStyle.success),
        ("deny_reason", "Deny with reason", discord.ButtonStyle.danger),
    ):
        view.add_item(discord.ui.Button(
            label=label, style=style,
            custom_id=f"application_{action}:{sid}",
        ))
    view.add_item(discord.ui.Button(
        label="History", style=discord.ButtonStyle.primary,
        custom_id=f"application_history:{sid}",
    ))
    view.add_item(discord.ui.Button(
        label="Open ticket with user", style=discord.ButtonStyle.secondary,
        custom_id=f"application_ticket:{sid}",
    ))
    if dashboard_url:
        url = f"{dashboard_url.rstrip('/')}/guild/{submission['guild_id']}/applications/submission/{sid}"
        view.add_item(discord.ui.Button(
            label="View submission on dashboard",
            style=discord.ButtonStyle.link,
            url=url,
            emoji="↗️",
        ))
    return view


def build_panel_view(apps: list) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for app in apps[:MAX_APPS_PER_PANEL]:
        style_name = app.get("button_style") or "primary"
        style = {
            "primary":   discord.ButtonStyle.primary,
            "secondary": discord.ButtonStyle.secondary,
            "success":   discord.ButtonStyle.success,
            "danger":    discord.ButtonStyle.danger,
        }.get(style_name, discord.ButtonStyle.primary)
        label = app.get("button_label") or app.get("name") or "Apply"
        view.add_item(discord.ui.Button(
            label=label[:80],
            emoji=app.get("emoji") or None,
            style=style,
            custom_id=f"application_open:{app['_id']}",
        ))
    return view


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Applications(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_dynamic_items(_PanelOpenButton)
        self.bot.add_dynamic_items(_ReviewDecisionButton)
        self.bot.add_dynamic_items(_StartConfirmButton)
        self._sweep_sessions.start()
        self._poll_dashboard_tasks.start()

    def cog_unload(self):
        self._sweep_sessions.cancel()
        self._poll_dashboard_tasks.cancel()

    # ------------------------------------------------------------------
    # on_message: DM replies advance the session
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.guild is not None:
            return  # DMs only

        session = await models.get_session_by_channel(message.channel.id)
        if not session or session.get("completed"):
            return

        gid = int(session["guild_id"])
        uid = int(session["user_id"])
        aid = str(session["application_id"])

        # Typing "cancel" cancels the application at any point (before or after Start).
        if message.content.strip().lower() == "cancel":
            await models.delete_application_session(gid, uid, aid)
            try:
                await message.channel.send(embed=info_embed(
                    "Application cancelled. Click the panel button again whenever you want to apply."
                ))
            except (discord.Forbidden, discord.HTTPException):
                pass
            return

        # Not started yet: waiting for the Start button, so don't treat text as an answer.
        # (Sessions created before this feature have no "started" key and count as started.)
        if not session.get("started", True):
            try:
                await message.channel.send(embed=info_embed(
                    "Press **Start** on the message above to begin, or type `cancel` to cancel."
                ))
            except (discord.Forbidden, discord.HTTPException):
                pass
            return

        # Snapshot the pre-update state.
        before = await models.record_session_answer(gid, uid, aid, message.content[:4000])
        if before is None:
            return

        questions = list(before.get("questions") or [])
        # `before` is the snapshot taken BEFORE this answer was pushed, so its
        # answer count is the index of the question that was just answered.
        # The next question to ask is therefore one past that.
        next_index = len(before.get("answers") or []) + 1

        prompt_shown_at = before.get("prompt_shown_at")
        answered_at = datetime.now(timezone.utc)
        duration_secs = None
        if prompt_shown_at is not None:
            if prompt_shown_at.tzinfo is None:
                prompt_shown_at = prompt_shown_at.replace(tzinfo=timezone.utc)
            duration_secs = max(int((answered_at - prompt_shown_at).total_seconds()), 0)

        if duration_secs is not None:
            await models.annotate_last_answer_duration(gid, uid, aid, duration_secs)

        if next_index < len(questions):
            await models.set_session_prompt_time(gid, uid, aid)
            refreshed = await models.get_application_session(gid, uid, aid)
            await _send_question(message.channel, refreshed, next_index)
            return

        await self._finalize_submission(gid, uid, aid, message.channel)

    async def _finalize_submission(self, gid: int, uid: int, aid: str,
                                    dm: discord.DMChannel):
        guild = self.bot.get_guild(gid)
        if guild is None:
            return

        app = await models.get_application(guild.id, aid)
        if not app:
            await models.complete_application_session(gid, uid, aid)
            try:
                await dm.send(embed=error_embed(
                    "The application you were filling out no longer exists."
                ))
            except (discord.Forbidden, discord.HTTPException):
                pass
            return

        final = await models.get_application_session(gid, uid, aid)
        if not final:
            return

        questions = list(final.get("questions") or [])
        answers = list(final.get("answers") or [])

        answers_out = []
        total = 0
        for i, q in enumerate(questions):
            ans = answers[i] if i < len(answers) else {}
            dur = ans.get("duration_seconds")
            if dur is None:
                dur = 0
            total += dur
            answers_out.append({
                "question": q,
                "answer": (ans.get("text") or "").strip(),
                "duration_seconds": dur,
            })

        try:
            user = await self.bot.fetch_user(uid)
            name = str(user)
        except (discord.NotFound, discord.HTTPException):
            name = "unknown"

        submission = {
            "application_id": str(app["_id"]),
            "application_name": app.get("name"),
            "applicant_id": str(uid),
            "applicant_name": name,
            "answers": answers_out,
            "total_duration_seconds": total,
        }
        submission_id = await models.create_application_submission(guild.id, submission)
        # Use the stored document so guild_id / created_at / status are present.
        stored = await models.get_application_submission(submission_id)
        if stored:
            submission = stored
        else:
            submission["_id"] = submission_id
            submission["guild_id"] = str(guild.id)

        submit_channel = None
        if app.get("submit_channel_id"):
            submit_channel = guild.get_channel(int(app["submit_channel_id"]))

        if submit_channel is None:
            try:
                await dm.send(embed=info_embed(
                    "Your application was submitted, but the submit channel isn't "
                    "configured. Please contact staff."
                ))
            except (discord.Forbidden, discord.HTTPException):
                pass
        else:
            try:
                ping_parts = []
                for rid in (app.get("ping_role_ids") or app.get("decision_ping_role_ids") or []):
                    role = guild.get_role(int(rid))
                    if role:
                        ping_parts.append(role.mention)

                posted = await submit_channel.send(
                    content=" ".join(ping_parts) if ping_parts else None,
                    allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
                    embed=_review_embed(submission, app, guild),
                    view=build_review_view(submission, getattr(settings, "DASHBOARD_BASE_URL", None)),
                )
                await models.set_submission_message_id(submission_id, submit_channel.id, posted.id)
            except (discord.Forbidden, discord.HTTPException):
                pass

        await models.complete_application_session(gid, uid, aid)

        try:
            await dm.send(embed=success_embed(
                f"Your **{app.get('name', 'Application')}** has been submitted. "
                "Staff will review it shortly."
            ))
        except (discord.Forbidden, discord.HTTPException):
            pass
    # ------------------------------------------------------------------
    # /applications group
    # ------------------------------------------------------------------
    @commands.hybrid_group(
        name="applications",
        description="Application panel management",
        invoke_without_command=True,
    )
    async def applications(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=info_embed(
                "Use `/applications panel` to post or refresh the applications panel."
            ))

    @applications.command(
        name="panel",
        description="Post or refresh the applications panel",
    )
    @command_check()
    async def applications_panel(self, ctx: commands.Context):
        panel = await models.get_application_panel(ctx.guild.id)
        if not panel or not panel.get("channel_id"):
            return await ctx.send(embed=error_embed(
                "No panel channel configured. Set it on the dashboard's "
                "**Applications** page and save the panel first."
            ))

        channel = ctx.guild.get_channel(int(panel["channel_id"]))
        if channel is None:
            return await ctx.send(embed=error_embed(
                "The configured panel channel no longer exists."
            ))

        apps = await models.get_applications(ctx.guild.id)
        if not apps:
            return await ctx.send(embed=error_embed(
                "No applications configured. Create one on the dashboard first."
            ))

        embed = _panel_embed(panel, apps[:MAX_APPS_PER_PANEL])
        view = build_panel_view(apps)

        existing_id = panel.get("message_id")
        if existing_id:
            try:
                msg = await channel.fetch_message(int(existing_id))
                await msg.edit(embed=embed, view=view)
                return await ctx.send(embed=success_embed(
                    f"Panel refreshed in {channel.mention}."
                ), ephemeral=(ctx.interaction is not None))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        try:
            posted = await channel.send(embed=embed, view=view)
            await models.save_application_panel(
                ctx.guild.id, {"message_id": str(posted.id)}
            )
        except discord.Forbidden:
            return await ctx.send(embed=error_embed(
                f"I can't post in {channel.mention} — check my permissions there."
            ))
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed(f"Discord error: {e}"))

        await ctx.send(embed=success_embed(
            f"Panel posted in {channel.mention}."
        ), ephemeral=(ctx.interaction is not None))

    # ------------------------------------------------------------------
    # Sweeper — expires stale sessions
    # ------------------------------------------------------------------
    @tasks.loop(minutes=1)
    async def _sweep_sessions(self):
        try:
            now = datetime.now(timezone.utc)
            cursor = models.application_sessions_col().find({"completed": False})
            async for session in cursor:
                gid = int(session["guild_id"])
                uid = int(session["user_id"])
                aid = str(session["application_id"])
                cfg = await models.get_application_config(gid)
                updated = session.get("updated_at")
                if updated is not None and updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if updated is None:
                    continue
                age_min = (now - updated).total_seconds() / 60
                if age_min < cfg["dm_timeout_minutes"]:
                    continue

                await models.complete_application_session(gid, uid, aid)
                try:
                    user = await self.bot.fetch_user(uid)
                    await user.send(embed=info_embed(
                        "Your application session timed out. "
                        "Click the panel button again to restart."
                    ))
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    pass
        except Exception as e:
            print(f"[applications] sweeper error: {e}")

    @_sweep_sessions.before_loop
    async def _before_sweep(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Dashboard task poller
    # ------------------------------------------------------------------
    @tasks.loop(seconds=3)
    async def _poll_dashboard_tasks(self):
        try:
            tasks_batch = await models.take_pending_dashboard_tasks(
                limit=10, types=["post_application_panel"]
            )
            for t in tasks_batch:
                try:
                    await self._run_task(t)
                except Exception as e:
                    print(f"[applications] dashboard task {t.get('_id')} failed: {e}")
        except Exception as e:
            print(f"[applications] poller error: {e}")

    @_poll_dashboard_tasks.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    async def _run_task(self, t: dict):
        ttype = t.get("type")
        if ttype == "post_application_panel":
            await self._post_panel(int(t["guild_id"]))

    async def _post_panel(self, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        panel = await models.get_application_panel(guild_id)
        if not panel:
            return
        channel_id = panel.get("channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            return

        apps = await models.get_applications(guild_id)
        if not apps:
            return

        embed = _panel_embed(panel, apps[:MAX_APPS_PER_PANEL])
        view = build_panel_view(apps)

        existing_id = panel.get("message_id")
        if existing_id:
            try:
                msg = await channel.fetch_message(int(existing_id))
                await msg.edit(embed=embed, view=view)
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        try:
            posted = await channel.send(embed=embed, view=view)
            await models.save_application_panel(guild_id, {"message_id": str(posted.id)})
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[applications] could not post panel: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Applications(bot))