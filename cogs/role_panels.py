"""
Role panels - the newer button / dropdown style of self-assign roles.

(The older "react with an emoji" roles are still in cogs/utilities.py under /reactionrole.)

Everything is set up on the dashboard's **Role Panels** tab - there are no commands. A panel is
a message with an embed plus either:
    buttons   one button per role. Click = get the role, click again = remove it.
    dropdown  one select menu. Members pick the roles they want and the menu applies exactly
              that choice (roles from this panel that they didn't pick are removed, so picking
              nothing clears them all).

Modes
    multiple  members can hold any number of the panel's roles
    unique    members can hold only ONE of the panel's roles (picking another swaps it)

How it works
    * The dashboard saves the panel in the `role_panels` collection and, when you press
      "Save & post", queues a `post_role_panel` task. This cog picks it up within a few
      seconds, then posts the message (or edits it in place if it was already posted) and
      writes the result (message id, or the error) back onto the panel for the dashboard.
    * Deleting a panel on the dashboard queues `delete_role_panel`, which removes the message.
    * The components use fixed custom_ids (rp:<panel>:<role> / rps:<panel>) and are
      re-registered at startup, so old panels keep working after a restart or redeploy.
    * The roles a click affects are read from the database at click time, so changing a
      panel's roles on the dashboard works straight away (re-post to refresh the labels).

The bot needs Manage Roles, and every role it hands out must sit BELOW the bot's top role.
Managed (bot/integration) roles can't be given. Limits: 25 roles per panel.
"""
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from database import models
from database.mongo import dashboard_tasks_col, role_panels_col
from utils.embeds import error_embed

POST_TASK = "post_role_panel"
DELETE_TASK = "delete_role_panel"

MAX_ENTRIES = 25

BUTTON_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}


# ---------------------------------------------------------------------------
# Building the message
# ---------------------------------------------------------------------------
def _emoji(raw):
    """'<:name:123>' / '🎮' -> a PartialEmoji, or None if empty/unreadable."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return discord.PartialEmoji.from_str(raw)
    except Exception:
        return None


def _entries(panel: dict) -> list:
    return [e for e in (panel.get("entries") or []) if e.get("role_id")][:MAX_ENTRIES]


def build_embed(panel: dict, guild: discord.Guild = None) -> discord.Embed:
    desc = (panel.get("description") or "").strip()
    if panel.get("show_list", True):
        lines = []
        for e in _entries(panel):
            role = guild.get_role(int(e["role_id"])) if guild else None
            who = role.mention if role else "*(deleted role)*"
            emoji = (e.get("emoji") or "").strip()
            extra = f" - {e['description']}" if e.get("description") else ""
            lines.append(f"{emoji + ' ' if emoji else ''}{who}{extra}")
        if lines:
            desc = (desc + "\n\n" if desc else "") + "\n".join(lines)
    footer = {
        ("buttons", "unique"): "Press a button to pick your role (you can only have one).",
        ("buttons", "multiple"): "Press a button to get a role - press it again to remove it.",
        ("dropdown", "unique"): "Pick your role from the menu (you can only have one).",
        ("dropdown", "multiple"): "Pick the roles you want from the menu - anything you leave out is removed.",
    }.get((panel.get("style"), panel.get("mode")), "")
    embed = discord.Embed(
        title=(panel.get("title") or "")[:256] or None,
        description=desc[:4000] or None,
        color=int(panel.get("color") or 0x5865F2),
    )
    if footer:
        embed.set_footer(text=footer)
    return embed


class _RoleButton(discord.ui.Button):
    def __init__(self, panel_id: str, entry: dict, label: str):
        super().__init__(
            label=(label or "")[:80] or None,
            emoji=_emoji(entry.get("emoji")),
            style=BUTTON_STYLES.get(entry.get("button_style"), discord.ButtonStyle.secondary),
            custom_id=f"rp:{panel_id}:{entry['role_id']}",
        )
        self.panel_id = panel_id
        self.role_id = str(entry["role_id"])

    async def callback(self, interaction: discord.Interaction):
        await handle_button(interaction, self.panel_id, self.role_id)


class _RoleSelect(discord.ui.Select):
    def __init__(self, panel_id: str, panel: dict, labels: dict):
        entries = _entries(panel)
        options = []
        for e in entries:
            options.append(discord.SelectOption(
                label=(labels.get(str(e["role_id"])) or "Role")[:100],
                value=str(e["role_id"]),
                description=(e.get("description") or None) and e["description"][:100],
                emoji=_emoji(e.get("emoji")),
            ))
        unique = panel.get("mode") == "unique"
        super().__init__(
            custom_id=f"rps:{panel_id}",
            placeholder=(panel.get("placeholder") or "Select your roles…")[:150],
            min_values=0,
            max_values=1 if unique else max(1, len(options)),
            options=options,
        )
        self.panel_id = panel_id

    async def callback(self, interaction: discord.Interaction):
        await handle_select(interaction, self.panel_id, [str(v) for v in self.values])


def build_view(panel: dict, guild: discord.Guild = None) -> discord.ui.View:
    """The button / dropdown components. `guild` only supplies role names for labels."""
    pid = str(panel["_id"])
    labels = {}
    for e in _entries(panel):
        role = guild.get_role(int(e["role_id"])) if guild else None
        labels[str(e["role_id"])] = (e.get("label") or "").strip() or (role.name if role else "Role")
    view = discord.ui.View(timeout=None)
    if panel.get("style") == "dropdown":
        view.add_item(_RoleSelect(pid, panel, labels))
    else:
        for e in _entries(panel):
            view.add_item(_RoleButton(pid, e, labels[str(e["role_id"])]))
    return view


# ---------------------------------------------------------------------------
# Handling clicks
# ---------------------------------------------------------------------------
def _role_problem(guild: discord.Guild, role) -> str:
    if role is None:
        return "That role no longer exists."
    if role.managed:
        return f"{role.mention} is managed by an integration, so I can't give it."
    me = guild.me
    if not me.guild_permissions.manage_roles:
        return "I don't have the Manage Roles permission."
    if role >= me.top_role:
        return f"I can't give {role.mention} - it's at or above my highest role."
    return ""


async def _prepare(interaction: discord.Interaction, panel_id: str):
    """Common checks. Returns (panel, member) or None after replying."""
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            embed=error_embed("Role panels only work inside a server."), ephemeral=True)
        return None
    panel = await models.get_role_panel(panel_id)
    if not panel or str(panel.get("guild_id")) != str(interaction.guild.id):
        await interaction.response.send_message(
            embed=error_embed("This role panel doesn't exist any more."), ephemeral=True)
        return None
    await interaction.response.defer(ephemeral=True)
    return panel, interaction.user


async def handle_button(interaction: discord.Interaction, panel_id: str, role_id: str):
    prepared = await _prepare(interaction, panel_id)
    if prepared is None:
        return
    panel, member = prepared
    guild = interaction.guild
    panel_roles = {str(e["role_id"]) for e in _entries(panel)}
    if role_id not in panel_roles:
        return await interaction.followup.send(
            embed=error_embed("That role was removed from this panel."), ephemeral=True)
    role = guild.get_role(int(role_id))
    problem = _role_problem(guild, role)
    if problem:
        return await interaction.followup.send(embed=error_embed(problem), ephemeral=True)

    try:
        if role in member.roles:
            await member.remove_roles(role, reason=f"Role panel: {panel.get('name')}")
            text = f"➖ Removed {role.mention}."
        else:
            swapped = []
            if panel.get("mode") == "unique":
                for rid in panel_roles - {role_id}:
                    other = guild.get_role(int(rid))
                    if other is not None and other in member.roles and not _role_problem(guild, other):
                        swapped.append(other)
                if swapped:
                    await member.remove_roles(*swapped, reason=f"Role panel (one role only): {panel.get('name')}")
            await member.add_roles(role, reason=f"Role panel: {panel.get('name')}")
            text = f"➕ You now have {role.mention}."
            if swapped:
                text += "\nRemoved: " + ", ".join(r.mention for r in swapped)
    except (discord.Forbidden, discord.HTTPException):
        return await interaction.followup.send(
            embed=error_embed("I couldn't change your roles. Tell an admin to check my permissions."),
            ephemeral=True)
    await interaction.followup.send(text, ephemeral=True,
                                    allowed_mentions=discord.AllowedMentions.none())


async def handle_select(interaction: discord.Interaction, panel_id: str, chosen: list):
    prepared = await _prepare(interaction, panel_id)
    if prepared is None:
        return
    panel, member = prepared
    guild = interaction.guild
    panel_roles = {str(e["role_id"]) for e in _entries(panel)}
    chosen = {c for c in chosen if c in panel_roles}
    if panel.get("mode") == "unique":
        chosen = set(list(chosen)[:1])

    to_add, to_remove, problems = [], [], []
    for rid in panel_roles:
        role = guild.get_role(int(rid))
        if role is None:
            continue
        has = role in member.roles
        if rid in chosen and not has:
            problem = _role_problem(guild, role)
            if problem:
                problems.append(problem)
            else:
                to_add.append(role)
        elif rid not in chosen and has and not _role_problem(guild, role):
            to_remove.append(role)

    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason=f"Role panel: {panel.get('name')}")
        if to_add:
            await member.add_roles(*to_add, reason=f"Role panel: {panel.get('name')}")
    except (discord.Forbidden, discord.HTTPException):
        return await interaction.followup.send(
            embed=error_embed("I couldn't change your roles. Tell an admin to check my permissions."),
            ephemeral=True)

    lines = []
    if to_add:
        lines.append("➕ Added: " + ", ".join(r.mention for r in to_add))
    if to_remove:
        lines.append("➖ Removed: " + ", ".join(r.mention for r in to_remove))
    if not lines and not problems:
        lines.append("Nothing changed.")
    lines += [f"⚠️ {p}" for p in problems]
    await interaction.followup.send("\n".join(lines), ephemeral=True,
                                    allowed_mentions=discord.AllowedMentions.none())

    # Discord keeps showing the member's picks in the menu until the message is re-sent;
    # put the menu back to its clean state so it doesn't look "stuck".
    try:
        await interaction.message.edit(view=build_view(panel, guild))
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        pass


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------
async def post_panel(guild: discord.Guild, panel: dict) -> tuple:
    """Post or update the panel's message. Returns (error, warning) - error '' on success."""
    entries = _entries(panel)
    if not entries:
        return "Add at least one role first.", ""
    if not panel.get("channel_id"):
        return "Pick a channel to post the panel in.", ""
    channel = guild.get_channel(int(panel["channel_id"]))
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return "That channel doesn't exist (or isn't a text channel).", ""
    perms = channel.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        return f"I need View Channel, Send Messages and Embed Links in #{channel.name}.", ""

    # Roles the bot can't hand out: still post, but tell the admin.
    bad = []
    for e in entries:
        role = guild.get_role(int(e["role_id"]))
        problem = _role_problem(guild, role)
        if problem:
            bad.append(f"{role.name if role else e['role_id']}: {problem.split(' - ')[-1].rstrip('.')}")
    warning = ("These roles can't be given right now - " + "; ".join(bad)) if bad else ""

    embed = build_embed(panel, guild)
    view = build_view(panel, guild)
    old_id = panel.get("message_id")
    old_channel = panel.get("posted_channel_id")
    message = None
    try:
        if old_id and str(old_channel) == str(channel.id):
            try:
                message = await channel.get_partial_message(int(old_id)).edit(embed=embed, view=view)
            except discord.NotFound:
                message = None
        if message is None:
            message = await channel.send(embed=embed, view=view)
            if old_id and str(old_channel) != str(channel.id):      # moved channels: remove the old one
                old_ch = guild.get_channel(int(old_channel)) if old_channel else None
                if old_ch is not None:
                    try:
                        await old_ch.get_partial_message(int(old_id)).delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
    except discord.Forbidden:
        return f"Discord refused (missing permission in #{channel.name}).", ""
    except discord.HTTPException as e:
        return f"Discord error: {e}", ""

    await models.update_role_panel(panel["_id"], {
        "message_id": str(message.id),
        "posted_channel_id": str(channel.id),
        "posted_at": datetime.now(timezone.utc),
        "post_error": "",
        "post_warning": warning,
    })
    return "", warning


class RolePanels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Re-register every posted panel so its buttons / menu work after a restart.
        try:
            async for panel in role_panels_col().find({"message_id": {"$nin": [None, ""]}}):
                try:
                    self.bot.add_view(build_view(panel), message_id=int(panel["message_id"]))
                except Exception as e:
                    print(f"[role_panels] couldn't re-register panel {panel.get('_id')}: {e}")
        except Exception as e:
            print(f"[role_panels] startup registration failed: {e}")
        self._poll_dashboard_tasks.start()

    async def cog_unload(self):
        self._poll_dashboard_tasks.cancel()

    # -- dashboard tasks -------------------------------------------------------
    @tasks.loop(seconds=3)
    async def _poll_dashboard_tasks(self):
        try:
            batch = await models.take_pending_dashboard_tasks(
                limit=10, types=[POST_TASK, DELETE_TASK])
        except Exception as e:
            print(f"[role_panels] poller error: {e}")
            return
        for t in batch:
            payload = t.get("payload") or {}
            error = ""
            try:
                guild = self.bot.get_guild(int(t["guild_id"]))
                if guild is None:
                    error = "The bot isn't in that server."
                elif t["type"] == POST_TASK:
                    panel = await models.get_role_panel(payload.get("panel_id"))
                    if not panel:
                        error = "That panel no longer exists."
                    else:
                        error, _ = await post_panel(guild, panel)
                        if error:
                            await models.update_role_panel(panel["_id"], {"post_error": error})
                else:
                    ch = guild.get_channel(int(payload["channel_id"]))
                    if ch is not None:
                        try:
                            await ch.get_partial_message(int(payload["message_id"])).delete()
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            pass
            except Exception as e:
                error = f"Unexpected error: {e}"
                print(f"[role_panels] task {t.get('_id')} failed: {e}")
            if error:
                await dashboard_tasks_col().update_one({"_id": t["_id"]}, {"$set": {"error": error}})

    @_poll_dashboard_tasks.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    # -- housekeeping ----------------------------------------------------------
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """Panel message deleted by hand -> the dashboard shows it as 'not posted'."""
        if payload.guild_id is None:
            return
        try:
            await role_panels_col().update_many(
                {"message_id": str(payload.message_id)},
                {"$set": {"message_id": None, "posted_channel_id": None}})
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(RolePanels(bot))
