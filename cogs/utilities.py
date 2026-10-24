"""
Utility cog.

Commands (all hybrid, all @command_check()):
    /sticky <message>               -> set a sticky message in this channel (plain text)
    /unsticky                       -> remove the sticky from this channel
    /calc <expression>              -> evaluate a math expression
    /count start [number]           -> start a counting game
    /count stop                     -> stop the counting game
    /afk [reason]                   -> go AFK (member command)
    /membercount                    -> total / members / bots (member command)
    /embeds create                  -> open a form (header/text/footer/color) and post an embed
    /embeds edit <message_id>       -> edit one of the bot's embeds in the same form (pre-filled)
    /reactionrole add <message_id> <emoji> <role> [channel]
                                    -> react-to-get-a-role on an existing message
    /reactionrole remove <message_id> <emoji>
    /reactionrole list
    /reactionrole mode <message_id> <normal|unique|verify>
                                    -> normal: un-reacting removes the role; unique: one role per
                                       message (picking another swaps); verify: un-reacting keeps it
    /reactionrole clear <message_id>
                                    -> remove every reaction role from one message
    /statchannel add <stat> [channel] [text]
                                    -> a channel whose name shows a live count, e.g. "Members: 1,204"
                                       (stat: members|humans|bots|boosts|roles|channels|voice; with no
                                       channel the bot makes a locked voice channel; {count} = the number)
    /statchannel remove <channel> | list | refresh
    /poll create <question> <options>
                                    -> post a button poll (| separated options)
    /poll end <message_id>          -> close a poll and lock its buttons
    /nick <user> [nickname]         -> set or clear a nickname
    /role add user <user> <role>    -> add a role to one member
    /role remove user <user> <role> -> remove a role from one member
    /role add all <role>            -> add a role to every member (confirm + ETA)
    /role remove all <role>         -> remove a role from every member (confirm + ETA)
    /blacklist add <user> <type> <reason> <proof1> [proof2] [proof3]
                                    -> submit a blacklist request for staff review
    /blacklist remove <user> <type> <reason> [proof1] [proof2] [proof3]
                                    -> submit an unblacklist request for staff review
       (type = giveaway | ticket | application)
    /temprole add <user> <role> <duration>
                                    -> give a member a role that auto-expires (30s/10m/2h/7d/1w)
    /temprole remove <user> <role>  -> remove a temp role entry early
    /temprole list [user]           -> list active temp roles (server-wide or per member)

Reply visibility:
  - Bot confirmations / errors / info are EPHEMERAL when invoked as slash, and
    auto-delete after 5s when invoked via prefix (prefix can't be ephemeral).
  - Public replies are reserved for things the channel needs to see: the sticky
    message itself, count start/stop announcements, and the poll embed.
  - A tiny _reply() helper enforces this so I don't have to remember per call.

Notes:
    - Stickies are posted as plain messages (no embed) and auto-repost.
    - Counting game: cannot count twice in a row; ANY message that isn't exactly the next number (text, 3_#22, wrong numbers, double counts) is deleted for everyone, count never resets.
    - /embeds create|edit: slash opens the form directly; prefix posts an "Open embed form" button.
    - Reaction roles: raw reaction events, so they survive restarts and old messages.
    - Stat channels: renamed at most every 10 minutes (Discord allows 2 renames per 10 min per
      channel), and only when the number changed. Deleted channels are dropped automatically.
    - Polls: "Option N" buttons, one vote per person (can be changed), live counts in the
      embed, stored in Mongo so they survive restarts. Max 10 options. /poll end locks one.
    - Role mass-ops show estimated time and confirm via Yes/Cancel buttons.
    - Blacklist requests go through staff approval (Accept / Deny buttons).
"""
import ast
import asyncio
import math
import operator
import re
import time
from datetime import datetime, timezone
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import models
from utils.embeds import success_embed, error_embed, info_embed, base_embed
from utils.permissions import command_check
from config import constants


# ---------------------------------------------------------------------------
# /calc - safe expression evaluator
# ---------------------------------------------------------------------------

_ALLOWED_BINOPS = {
    ast.Add:      operator.add,
    ast.Sub:      operator.sub,
    ast.Mult:     operator.mul,
    ast.Div:      operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod:      operator.mod,
    ast.Pow:      operator.pow,
}

_ALLOWED_UNARY = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_ALLOWED_FUNCS = {
    "sqrt":  math.sqrt,
    "sin":   math.sin,
    "cos":   math.cos,
    "tan":   math.tan,
    "log":   math.log,
    "log10": math.log10,
    "abs":   abs,
    "round": round,
    "floor": math.floor,
    "ceil":  math.ceil,
}

_ALLOWED_CONSTS = {
    "pi":  math.pi,
    "e":   math.e,
    "tau": math.tau,
}


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("only numeric constants allowed")
    if isinstance(node, ast.BinOp):
        fn = _ALLOWED_BINOPS.get(type(node.op))
        if fn is None:
            raise ValueError("operator not allowed")
        return fn(_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp):
        fn = _ALLOWED_UNARY.get(type(node.op))
        if fn is None:
            raise ValueError("unary operator not allowed")
        return fn(_eval_node(node.operand))
    if isinstance(node, ast.Name):
        if node.id in _ALLOWED_CONSTS:
            return _ALLOWED_CONSTS[node.id]
        raise ValueError(f"unknown name '{node.id}'")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("only simple function calls allowed")
        fn = _ALLOWED_FUNCS.get(node.func.id)
        if fn is None:
            raise ValueError(f"unknown function '{node.func.id}'")
        if node.keywords:
            raise ValueError("keyword arguments not allowed")
        return fn(*[_eval_node(a) for a in node.args])
    raise ValueError("unsupported expression")


def safe_eval(expr: str):
    tree = ast.parse(expr, mode="eval")
    return _eval_node(tree)


# ---------------------------------------------------------------------------
# Reply helper: ephemeral for slash, auto-delete for prefix
# ---------------------------------------------------------------------------

def _fmt_duration(since) -> str:
    if since is None:
        return "a moment"
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    secs = max(int((datetime.now(timezone.utc) - since).total_seconds()), 0)
    if secs < 60:
        return f"{secs}s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


async def _reply(ctx: commands.Context, embed: discord.Embed,
                 *, ephemeral: bool = True, delete_after: float = 5):
    """Send an embed. If ephemeral=True and this is a slash invocation, only the
    invoker sees it. If ephemeral=True and it's a prefix invocation, delete the
    message after delete_after seconds. If ephemeral=False, always public."""
    if not ephemeral:
        return await ctx.send(embed=embed)
    if ctx.interaction is not None:
        return await ctx.send(embed=embed, ephemeral=True)
    return await ctx.send(embed=embed, delete_after=delete_after)


# ---------------------------------------------------------------------------
# /role mass-op helpers
# ---------------------------------------------------------------------------

SECONDS_PER_MEMBER = 0.33


def _fmt_eta(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    if seconds < 60:
        return f"{seconds}s"
    mins, secs = divmod(seconds, 60)
    return f"{mins}m {secs}s"


class _MassRoleConfirmView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.value = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=error_embed("Only the person who ran the command can confirm."),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


async def _mass_role_op(ctx: commands.Context, role: discord.Role, add: bool):
    if role.is_default():
        return await _reply(ctx, error_embed("Can't assign @everyone."))
    if role.managed:
        return await _reply(ctx, error_embed(
            "That role is managed by an integration and can't be assigned manually."
        ))

    me = ctx.guild.me
    if role >= me.top_role:
        return await _reply(ctx, error_embed(
            "That role is at or above my top role — I can't manage it."
        ))

    targets = [
        m for m in ctx.guild.members
        if not m.bot
        and m.id != ctx.guild.owner_id
        and m.id != me.id
    ]

    if add:
        targets = [m for m in targets if role not in m.roles]
        verb = "add"
        verb_past = "added"
    else:
        targets = [m for m in targets if role in m.roles]
        verb = "remove"
        verb_past = "removed"

    if not targets:
        return await _reply(ctx, info_embed(
            f"No members need the role {role.mention} {verb_past}."
        ))

    eta_seconds = len(targets) * SECONDS_PER_MEMBER
    view = _MassRoleConfirmView(ctx.author.id)
    prompt = base_embed(
        title=f"Confirm mass role {verb}",
        description=(
            f"About to **{verb}** {role.mention} for **{len(targets)}** member(s).\n"
            f"Estimated time: **~{_fmt_eta(eta_seconds)}**.\n\n"
            "This will run in the background and can't be undone by me. Continue?"
        ),
    )
    # The confirm prompt should stay visible (buttons must be clickable), but
    # only ephemeral if slash so it doesn't clutter. Prefix users get a normal msg.
    if ctx.interaction is not None:
        await ctx.send(embed=prompt, view=view, ephemeral=True)
    else:
        await ctx.send(embed=prompt, view=view)
    await view.wait()

    if view.value is not True:
        return await _reply(ctx, info_embed("Mass role operation cancelled."))

    ok = 0
    failed = 0
    errors = []

    progress_embed = base_embed(
        title=f"Mass role {verb} — running",
        description=f"0/{len(targets)} done...",
    )
    if ctx.interaction is not None:
        progress_msg = await ctx.send(embed=progress_embed, ephemeral=True)
    else:
        progress_msg = await ctx.send(embed=progress_embed)
    last_update = time.time()

    for i, member in enumerate(targets, start=1):
        try:
            if add:
                await member.add_roles(role, reason=f"Mass role add by {ctx.author}")
            else:
                await member.remove_roles(role, reason=f"Mass role remove by {ctx.author}")
            ok += 1
        except (discord.Forbidden, discord.HTTPException) as e:
            failed += 1
            if len(errors) < 3:
                errors.append(f"{member.id}: {e}")

        now = time.time()
        if now - last_update >= 3 or i == len(targets):
            try:
                await progress_msg.edit(embed=base_embed(
                    title=f"Mass role {verb} — running",
                    description=f"{i}/{len(targets)} done — {ok} ok, {failed} failed",
                ))
            except (discord.NotFound, discord.HTTPException):
                pass
            last_update = now

    done = base_embed(
        title=f"Mass role {verb} — complete",
        description=(
            f"✅ {ok} {verb_past}\n"
            f"❌ {failed} failed"
            + ("\n\nFirst errors:\n" + "\n".join(errors) if errors else "")
        ),
    )
    try:
        await progress_msg.edit(embed=done)
    except (discord.NotFound, discord.HTTPException):
        pass


# ---------------------------------------------------------------------------
# Blacklist — helper + persistent review buttons
# ---------------------------------------------------------------------------

def _member_has_role(member: discord.Member, role_id) -> bool:
    if not role_id:
        return False
    return any(str(r.id) == str(role_id) for r in member.roles)


def _blacklist_action_label(action: str) -> str:
    return "Add to blacklist" if action == "add" else "Remove from blacklist"


def _blacklist_type_label(type_: str) -> str:
    return {"giveaway": "Giveaway", "application": "Application"}.get(type_, "Ticket")


def _blacklist_role_id(cfg: dict, type_: str):
    """The dashboard-configured blacklist role for a type."""
    return cfg.get(f"{type_}_role_id")


def build_blacklist_embed(req: dict, guild: discord.Guild) -> discord.Embed:
    status = req.get("status", "Pending")
    color = {
        "Pending":  constants.COLOR_WARNING,
        "Accepted": constants.COLOR_SUCCESS,
        "Denied":   constants.COLOR_ERROR,
    }.get(status, constants.COLOR_DEFAULT)

    action_label = _blacklist_action_label(req["action"])
    type_label = _blacklist_type_label(req["type"])

    embed = base_embed(
        title=f"🚫 {action_label} — {type_label}",
        description=None,
        color=color,
    )
    embed.add_field(name="Applicant", value=f"<@{req['applicant_id']}>\n`{req['applicant_id']}`", inline=False)
    embed.add_field(name="Target", value=f"<@{req['target_id']}>\n`{req['target_id']}`", inline=False)
    embed.add_field(name="Type", value=f"`{type_label}`", inline=True)
    embed.add_field(name="Action", value=f"`{action_label}`", inline=True)
    embed.add_field(name="Reason", value=(req.get("reason") or "*none*")[:1024], inline=False)

    proofs = req.get("proof_urls") or []
    for i, url in enumerate(proofs, 1):
        embed.add_field(name=f"Proof {i}", value=f"[Open]({url})", inline=True)
    if proofs:
        embed.set_image(url=proofs[0])

    embed.add_field(name="Status", value=f"`{status}`", inline=False)

    staff_id = req.get("staff_id")
    if staff_id and status != "Pending":
        verb = "Accepted" if status == "Accepted" else "Denied"
        line = f"{verb} by <@{staff_id}>"
        err = req.get("role_error")
        if err:
            line += f"\n⚠️ {err}"
        embed.add_field(name="Staff Decision", value=line[:1024], inline=False)

    embed.set_footer(text=f"Request ID: {req.get('_id')}")
    return embed


class _BlacklistDecisionView(discord.ui.View):
    def __init__(self, request_id: str, disabled: bool = False):
        super().__init__(timeout=None)
        accept = discord.ui.Button(
            label="Accept",
            style=discord.ButtonStyle.success,
            custom_id=f"blacklist_accept:{request_id}",
            disabled=disabled,
        )
        deny = discord.ui.Button(
            label="Deny",
            style=discord.ButtonStyle.danger,
            custom_id=f"blacklist_deny:{request_id}",
            disabled=disabled,
        )
        self.add_item(accept)
        self.add_item(deny)


class _BlacklistDecisionButton(discord.ui.DynamicItem[discord.ui.Button],
                               template=r"blacklist_(?P<action>accept|deny):(?P<id>[0-9a-f]{24})"):
    def __init__(self, action: str, request_id: str):
        self.action = action
        self.request_id = request_id
        label = "Accept" if action == "accept" else "Deny"
        style = discord.ButtonStyle.success if action == "accept" else discord.ButtonStyle.danger
        super().__init__(discord.ui.Button(
            label=label,
            style=style,
            custom_id=f"blacklist_{action}:{request_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], match["id"])

    async def callback(self, interaction: discord.Interaction):
        req = await models.get_blacklist_request(self.request_id)
        if not req:
            return await interaction.response.send_message(
                embed=error_embed("This request no longer exists."), ephemeral=True
            )

        guild = interaction.guild
        if guild is None or str(guild.id) != str(req.get("guild_id")):
            return await interaction.response.send_message(
                embed=error_embed("This request belongs to another server."), ephemeral=True
            )

        cfg = await models.get_blacklist_config(guild.id)
        is_admin = isinstance(interaction.user, discord.Member) and \
            interaction.user.guild_permissions.administrator
        is_staff = False
        if cfg["staff_role_id"] and isinstance(interaction.user, discord.Member):
            is_staff = any(str(r.id) == str(cfg["staff_role_id"])
                           for r in interaction.user.roles)
        if not (is_admin or is_staff):
            return await interaction.response.send_message(
                embed=error_embed(
                    "Only Admins or members with the configured blacklist staff role "
                    "can decide these requests."
                ),
                ephemeral=True,
            )

        if req.get("status") != "Pending":
            return await interaction.response.send_message(
                embed=info_embed(f"This request was already **{req['status']}**."),
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        status = "Accepted" if self.action == "accept" else "Denied"
        claimed = await models.claim_blacklist_request_decision(
            self.request_id, interaction.user.id, status
        )
        if claimed is None:
            return await interaction.followup.send(
                embed=info_embed("Someone else already decided this request."),
                ephemeral=True,
            )

        role_error = None
        if status == "Accepted":
            role_error = await self._apply_role_change(guild, req)

        try:
            await models.bump_blacklist_counter(
                guild.id, ("accepted" if status == "Accepted" else "denied"), req["type"]
            )
        except Exception as e:
            print(f"[blacklist] counter bump failed: {e}")

        # Taskbook: +1 blacklists for whoever submitted an accepted /blacklist add
        if status == "Accepted" and req.get("action") == "add":
            try:
                from cogs.taskbook import record_blacklist as tb_blacklist
                await tb_blacklist(guild, req)
            except Exception as e:
                print(f"[blacklist] taskbook hook failed: {e}")

        fresh = await models.get_blacklist_request(self.request_id) or req
        if role_error:
            fresh = dict(fresh)
            fresh["role_error"] = role_error

        ch_id = fresh.get("channel_id")
        msg_id = fresh.get("message_id")
        if ch_id and msg_id:
            channel = guild.get_channel(int(ch_id))
            if channel:
                try:
                    msg = await channel.fetch_message(int(msg_id))
                    await msg.edit(
                        embed=build_blacklist_embed(fresh, guild),
                        view=_BlacklistDecisionView(self.request_id, disabled=True),
                    )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        await interaction.followup.send(
            embed=success_embed(
                f"Request `{self.request_id}` marked **{status}**."
                + (f"\n⚠️ {role_error}" if role_error else "")
            ),
            ephemeral=True,
        )

    async def _apply_role_change(self, guild: discord.Guild, req: dict):
        cfg = await models.get_blacklist_config(guild.id)
        role_id = _blacklist_role_id(cfg, req["type"])
        if not role_id:
            return f"No {req['type']} blacklist role configured."

        role = guild.get_role(int(role_id))
        if role is None:
            return "The configured blacklist role no longer exists."

        member = guild.get_member(int(req["target_id"]))
        if member is None:
            return "Target isn't in the server — role not applied."

        action = req["action"]
        try:
            if action == "add":
                if role in member.roles:
                    return None
                await member.add_roles(role, reason=f"Blacklist request {req['_id']} accepted")
            else:
                if role not in member.roles:
                    return None
                await member.remove_roles(role, reason=f"Unblacklist request {req['_id']} accepted")
        except discord.Forbidden:
            return "Missing permission or role hierarchy issue."
        except discord.HTTPException as e:
            return f"Discord error: {e}"
        return None


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# /embeds - form (modal) that builds and posts an embed
# ---------------------------------------------------------------------------

_NAMED_COLORS = {
    "red": 0xED4245, "green": 0x57F287, "blue": 0x3498DB, "blurple": 0x5865F2,
    "yellow": 0xFEE75C, "orange": 0xE67E22, "purple": 0x9B59B6, "pink": 0xEB459E,
    "white": 0xFFFFFF, "black": 0x000000, "gold": 0xF1C40F, "teal": 0x1ABC9C,
}


def _parse_color(raw: str):
    """'#5865F2' / '5865f2' / '#fff' / 'red' -> int. Empty -> default. Bad -> None."""
    raw = (raw or "").strip().lower()
    if not raw:
        return constants.COLOR_INFO
    if raw in _NAMED_COLORS:
        return _NAMED_COLORS[raw]
    h = raw.lstrip("#")
    if re.fullmatch(r"[0-9a-f]{3}", h):
        h = "".join(c * 2 for c in h)
    if re.fullmatch(r"[0-9a-f]{6}", h):
        return int(h, 16)
    return None


class _EmbedModal(discord.ui.Modal, title="Create Embed"):
    header = discord.ui.TextInput(
        label="Header", max_length=256, required=False,
        placeholder="Embed title (optional)",
    )
    text = discord.ui.TextInput(
        label="Text", style=discord.TextStyle.paragraph, max_length=4000,
        placeholder="Main body of the embed",
    )
    footer = discord.ui.TextInput(
        label="Footer", max_length=2048, required=False,
        placeholder="Footer text (optional)",
    )
    color = discord.ui.TextInput(
        label="Color", max_length=20, required=False,
        placeholder="#5865F2, or red / green / blue / pink ...",
    )

    def __init__(self, channel, on_done=None):
        super().__init__()
        self.channel = channel
        self.on_done = on_done

    async def on_submit(self, interaction: discord.Interaction):
        color = _parse_color(self.color.value)
        if color is None:
            return await interaction.response.send_message(
                embed=error_embed(
                    "Invalid color. Use a hex code like `#5865F2` or a name like "
                    "`red`, `green`, `blue`, `pink`. Run the command again to retry."
                ),
                ephemeral=True,
            )

        embed = discord.Embed(
            title=(self.header.value or "").strip() or None,
            description=self.text.value,
            color=color,
        )
        footer = (self.footer.value or "").strip()
        if footer:
            embed.set_footer(text=footer)

        if len(embed) > 6000:
            return await interaction.response.send_message(
                embed=error_embed("That embed is over Discord's 6000 character limit."),
                ephemeral=True,
            )

        try:
            await self.channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message(
                embed=error_embed("I can't post in that channel."), ephemeral=True
            )

        await interaction.response.send_message(
            embed=success_embed(f"Embed posted in {self.channel.mention}."), ephemeral=True
        )
        if self.on_done:
            await self.on_done()


class _EmbedFormView(discord.ui.View):
    """Prefix invocations can't open a modal directly (no interaction), so the
    bot posts this button; clicking it opens the same form."""

    def __init__(self, author_id: int, channel, modal_factory=None):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.channel = channel
        self.message = None
        # modal_factory(on_done) -> Modal. Default: the "create" form.
        self.modal_factory = modal_factory or (
            lambda on_done: _EmbedModal(self.channel, on_done=on_done)
        )

    @discord.ui.button(label="Open embed form", style=discord.ButtonStyle.primary, emoji="📝")
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                embed=error_embed("This form isn't yours."), ephemeral=True
            )
        await interaction.response.send_modal(self.modal_factory(self._finish))

    async def _finish(self):
        self.stop()
        await self._remove_message()

    async def on_timeout(self):
        await self._remove_message()

    async def _remove_message(self):
        if self.message is not None:
            try:
                await self.message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            self.message = None


class _EmbedEditModal(discord.ui.Modal, title="Edit Embed"):
    """Same form as /embeds create, pre-filled from an embed the bot already posted.
    The inputs are built per instance so one admin's prefill never leaks to another."""

    def __init__(self, message: discord.Message, on_done=None):
        super().__init__()
        self.target = message
        self.on_done = on_done
        e = message.embeds[0]
        color = f"#{e.color.value:06X}" if e.color is not None else None
        self.header = discord.ui.TextInput(
            label="Header", max_length=256, required=False,
            placeholder="Embed title (optional)", default=e.title or None,
        )
        self.text = discord.ui.TextInput(
            label="Text", style=discord.TextStyle.paragraph, max_length=4000,
            placeholder="Main body of the embed",
            default=(e.description or "")[:4000] or None,
        )
        self.footer = discord.ui.TextInput(
            label="Footer", max_length=2048, required=False,
            placeholder="Footer text (optional)",
            default=(e.footer.text if e.footer and e.footer.text else None),
        )
        self.color = discord.ui.TextInput(
            label="Color", max_length=20, required=False,
            placeholder="#5865F2, or red / green / blue / pink ...", default=color,
        )
        for item in (self.header, self.text, self.footer, self.color):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        color = _parse_color(self.color.value)
        if color is None:
            return await interaction.response.send_message(
                embed=error_embed(
                    "Invalid color. Use a hex code like `#5865F2` or a name like "
                    "`red`, `green`, `blue`, `pink`. Run the command again to retry."
                ),
                ephemeral=True,
            )

        embed = self.target.embeds[0].copy()
        embed.title = (self.header.value or "").strip() or None
        embed.description = self.text.value
        embed.color = color
        footer = (self.footer.value or "").strip()
        if footer:
            icon = embed.footer.icon_url if embed.footer else None
            embed.set_footer(text=footer, icon_url=icon)
        else:
            embed.remove_footer()

        if len(embed) > 6000:
            return await interaction.response.send_message(
                embed=error_embed("That embed is over Discord's 6000 character limit."),
                ephemeral=True,
            )

        try:
            await self.target.edit(embeds=[embed] + list(self.target.embeds[1:]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message(
                embed=error_embed("I couldn't edit that message (deleted, or I lost access)."),
                ephemeral=True,
            )

        await interaction.response.send_message(
            embed=success_embed(f"[Embed]({self.target.jump_url}) updated."), ephemeral=True
        )
        if self.on_done:
            await self.on_done()


# ---------------------------------------------------------------------------
# Button polls
# ---------------------------------------------------------------------------

def build_poll_embed(poll: dict) -> discord.Embed:
    options = poll["options"]
    counts = [0] * len(options)
    for v in (poll.get("votes") or {}).values():
        try:
            counts[int(v)] += 1
        except (ValueError, IndexError):
            pass
    total = sum(counts)

    blocks = []
    for i, opt in enumerate(options):
        pct = round(100 * counts[i] / total) if total else 0
        filled = round(pct / 10)
        bar = "▰" * filled + "▱" * (10 - filled)
        n = counts[i]
        blocks.append(f"**Option {i + 1}** - {opt}\n{bar}  **{n}** vote{'' if n == 1 else 's'} ({pct}%)")

    ended = bool(poll.get("ended"))
    embed = base_embed(
        title=f"📊 {poll['question'][:230]}" + (" (Ended)" if ended else ""),
        description="\n\n".join(blocks),
        color=constants.COLOR_DEFAULT,
    )
    by = f" • Poll by {poll['author_name']}" if poll.get("author_name") else ""
    embed.set_footer(text=f"{total} vote{'' if total == 1 else 's'}{by}")
    return embed


class PollVoteButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"poll_vote:(?P<mid>[0-9]+):(?P<idx>[0-9]+)"
):
    def __init__(self, message_id, index: int):
        super().__init__(discord.ui.Button(
            label=f"Option {int(index) + 1}",
            style=discord.ButtonStyle.primary,
            custom_id=f"poll_vote:{message_id}:{int(index)}",
        ))
        self.message_id = str(message_id)
        self.index = int(index)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match, /):
        return cls(match["mid"], int(match["idx"]))

    async def callback(self, interaction: discord.Interaction):
        poll = await models.get_poll(self.message_id)
        if not poll or poll.get("ended"):
            return await interaction.response.send_message(
                embed=error_embed("This poll has ended."), ephemeral=True
            )
        if self.index >= len(poll["options"]):
            return await interaction.response.send_message(
                embed=error_embed("That option doesn't exist."), ephemeral=True
            )

        uid = str(interaction.user.id)
        votes = poll.get("votes") or {}
        previous = votes.get(uid)
        if previous == self.index:
            return await interaction.response.send_message(
                embed=info_embed(f"You already voted for **Option {self.index + 1}**."),
                ephemeral=True,
            )

        updated = await models.poll_set_vote(self.message_id, interaction.user.id, self.index)
        if updated is None:
            return await interaction.response.send_message(
                embed=error_embed("This poll has ended."), ephemeral=True
            )

        text = f"Your vote for **Option {self.index + 1}** is in."
        if previous is not None:
            text = (f"Vote changed from **Option {int(previous) + 1}** "
                    f"to **Option {self.index + 1}**.")
        await interaction.response.send_message(embed=success_embed(text), ephemeral=True)
        try:
            await interaction.message.edit(embed=build_poll_embed(updated))
        except discord.HTTPException as e:
            print(f"[poll] couldn't update counts: {e}")


def build_poll_view(message_id, option_count: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for i in range(option_count):
        if disabled:
            view.add_item(discord.ui.Button(
                label=f"Option {i + 1}", style=discord.ButtonStyle.secondary,
                custom_id=f"poll_vote:{message_id}:{i}", disabled=True,
            ))
        else:
            view.add_item(PollVoteButton(message_id, i))
    return view


# ---------------------------------------------------------------------------
# Reaction roles - helpers
# ---------------------------------------------------------------------------

_CUSTOM_EMOJI_RE = re.compile(r"^<a?:[A-Za-z0-9_]{2,32}:(\d{17,22})>$")

# Roles with any of these permissions can't be handed out via reactions.
_DANGEROUS_PERMS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels",
    "manage_webhooks", "manage_messages", "ban_members", "kick_members",
    "moderate_members", "mention_everyone",
)


def _norm_unicode(name: str) -> str:
    # Strip variation selectors so "❤" and "❤️" map to the same key.
    return (name or "").replace("\ufe0f", "").replace("\ufe0e", "")


def _parse_emoji(raw: str):
    """-> (emoji_key, display) or None. Custom emoji key = ID, unicode key = normalised char."""
    raw = (raw or "").strip()
    if not raw or any(ch.isspace() for ch in raw) or len(raw) > 64:
        return None
    m = _CUSTOM_EMOJI_RE.match(raw)
    if m:
        return m.group(1), raw
    if raw.startswith("<"):
        return None
    return _norm_unicode(raw), raw


def _payload_key(emoji: discord.PartialEmoji) -> str:
    return str(emoji.id) if emoji.id else _norm_unicode(emoji.name or "")


def _role_problem(ctx: commands.Context, role: discord.Role):
    """Return a reason string if `role` can't be used for reaction roles, else None."""
    if role.is_default():
        return "You can't use @everyone."
    if role.managed:
        return "That role is managed by an integration and can't be assigned."
    if role >= ctx.guild.me.top_role:
        return "That role is higher than (or equal to) my top role, so I can't assign it."
    perms = role.permissions
    bad = [p.replace("_", " ") for p in _DANGEROUS_PERMS if getattr(perms, p)]
    if bad:
        return f"That role has dangerous permissions ({', '.join(bad[:3])}) and can't be given out by reaction."
    author = ctx.author
    if (not author.guild_permissions.administrator and author.id != ctx.guild.owner_id
            and role >= author.top_role):
        return "You can't set up a role that's higher than (or equal to) your own top role."
    return None


# ---------------------------------------------------------------------------
# Stat channels - helpers
# ---------------------------------------------------------------------------

STAT_DEFAULT_FORMATS = {
    "members": "Members: {count}", "humans": "Humans: {count}", "bots": "Bots: {count}",
    "boosts": "Boosts: {count}", "roles": "Roles: {count}", "channels": "Channels: {count}",
    "voice": "In voice: {count}",
}
MAX_STAT_CHANNELS = 10


def _stat_value(guild: discord.Guild, stat: str) -> int:
    total = guild.member_count or len(guild.members)
    if stat == "members":
        return total
    bots = sum(1 for m in guild.members if m.bot)
    if stat == "bots":
        return bots
    if stat == "humans":
        return max(0, total - bots)
    if stat == "boosts":
        return guild.premium_subscription_count or 0
    if stat == "roles":
        return max(0, len(guild.roles) - 1)          # not counting @everyone
    if stat == "channels":
        return len(guild.channels)
    if stat == "voice":
        return sum(len(vc.members) for vc in guild.voice_channels)
    return 0


def _stat_name(guild: discord.Guild, row: dict) -> str:
    fmt = row.get("format") or STAT_DEFAULT_FORMATS.get(row.get("stat"), "{count}")
    return fmt.replace("{count}", f"{_stat_value(guild, row.get('stat')):,}")[:100]


class Utilities(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.afk = {}              # (guild_id, user_id) -> afk doc
        self._afk_loaded = False

    async def cog_load(self):
        self.bot.add_dynamic_items(_BlacklistDecisionButton, PollVoteButton)
        self._check_temp_roles.start()
        self._update_stat_channels.start()

    async def cog_unload(self):
        self._check_temp_roles.cancel()
        self._update_stat_channels.cancel()

    # ------------------------------------------------------------------
    # Sticky / unsticky  (plain text messages, not embeds)
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="sticky", description="Set a sticky message in this channel")
    @command_check()
    async def sticky(self, ctx: commands.Context, *, message: str):
        message = (message or "").strip()
        if not message:
            return await _reply(ctx, error_embed("Provide a message."))

        # Hold the channel lock while posting + saving, so the new sticky's own gateway echo
        # (bot messages now trigger reposts) can't be mistaken for a message that scrolled it off.
        async with self._sticky_lock(ctx.channel.id):
            existing = await models.get_sticky(ctx.guild.id, ctx.channel.id)
            if existing and existing.get("message_id"):
                try:
                    old = await ctx.channel.fetch_message(int(existing["message_id"]))
                    await old.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

            try:
                posted = await ctx.channel.send(message[:2000])
            except discord.Forbidden:
                return await _reply(ctx, error_embed("I can't post in this channel."))

            await models.upsert_sticky(
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                message_id=posted.id,
                content=message,
                author_id=ctx.author.id,
            )

        await _reply(ctx, success_embed(
            f"Sticky set in {ctx.channel.mention}. It will auto-repost when it scrolls off."
        ))

    @commands.hybrid_command(name="unsticky", description="Remove the sticky from this channel")
    @command_check()
    async def unsticky(self, ctx: commands.Context):
        removed = await models.delete_sticky(ctx.guild.id, ctx.channel.id)
        if not removed:
            return await _reply(ctx, info_embed("There's no sticky in this channel."))

        if removed.get("message_id"):
            try:
                old = await ctx.channel.fetch_message(int(removed["message_id"]))
                await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        await _reply(ctx, success_embed("Sticky removed from this channel."))

    def _sticky_lock(self, channel_id: int) -> asyncio.Lock:
        locks = self.__dict__.setdefault("_sticky_locks", {})
        return locks.setdefault(channel_id, asyncio.Lock())

    async def _repost_sticky(self, message: discord.Message):
        """Keep the sticky as the last message in the channel. Triggered by ANY new message,
        including other bots', webhooks' and this bot's own (except the sticky itself).
        One lock per channel, so the sticky's own gateway echo can't trigger another repost
        before its new message id is saved."""
        async with self._sticky_lock(message.channel.id):
            sticky = await models.get_sticky(message.guild.id, message.channel.id)
            if not sticky:
                return
            if str(message.id) == str(sticky.get("message_id") or ""):
                return
            content = (sticky.get("content") or "").strip()
            if not content:
                return
            if self.bot.user is not None and message.author.id == self.bot.user.id \
                    and (message.content or "").strip() == content[:2000]:
                return  # our own sticky text (e.g. the echo of a repost still being saved)

            if sticky.get("message_id"):
                try:
                    old = await message.channel.fetch_message(int(sticky["message_id"]))
                    await old.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            try:
                posted = await message.channel.send(content[:2000])
                await models.update_sticky_message(
                    message.guild.id, message.channel.id, posted.id
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return

        # Sticky reacts to every message, bots included.
        await self._repost_sticky(message)

        if message.author.bot:
            return

        # ---- Count game ----
        game = await models.get_count_game(message.guild.id, message.channel.id)
        if not game:
            return

        # Strict: the message must be EXACTLY the next number - nothing else, nobody exempt.
        content = (message.content or "").strip()
        expected = int(game.get("last_number") or 0) + 1
        last_user = game.get("last_user_id")
        same_user = last_user is not None and str(last_user) == str(message.author.id)
        is_number = re.fullmatch(r"-?[0-9]+", content) is not None

        if content != str(expected) or same_user:
            # Count never resets: just delete the bad message and leave
            # last_number / last_user_id untouched so the chain continues.
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
            if same_user and content == str(expected):
                reason = "You can't count twice in a row."
            elif is_number:
                reason = f"Expected **{expected}**, got **{content}**."
            else:
                reason = "Only the next number is allowed here."
            try:
                await message.channel.send(
                    f"{message.author.mention} {reason} Message deleted, "
                    f"next number is **{expected}**.",
                    delete_after=5,
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            return

        await models.advance_count_game(
            message.guild.id, message.channel.id, expected, message.author.id
        )
        try:
            await message.add_reaction("✅")
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ------------------------------------------------------------------
    # AFK
    # ------------------------------------------------------------------
    async def _load_afk(self):
        if self._afk_loaded:
            return
        self._afk_loaded = True
        try:
            for doc in await models.get_all_afk():
                self.afk[(str(doc["guild_id"]), str(doc["user_id"]))] = doc
        except Exception as e:
            self._afk_loaded = False
            print(f"[utilities] afk load failed: {e}")

    @commands.Cog.listener("on_message")
    async def afk_on_message(self, message: discord.Message):
        """AFK handling: welcome people back and tell pingers who is AFK.
        Every notice deletes itself so it never floods the chat."""
        if message.author.bot or message.guild is None:
            return
        await self._load_afk()
        gid, uid = str(message.guild.id), str(message.author.id)

        mine = self.afk.get((gid, uid))
        if mine:
            since = mine.get("since")
            if since is not None and since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            # Grace period so a prefix `!afk` doesn't clear itself immediately.
            if since is None or (datetime.now(timezone.utc) - since).total_seconds() > 3:
                self.afk.pop((gid, uid), None)
                await models.clear_afk(message.guild.id, message.author.id)
                try:
                    await message.channel.send(
                        f"👋 Welcome back {message.author.mention}, I removed your AFK. "
                        f"You were away for **{_fmt_duration(since)}**.",
                        delete_after=8,
                        allowed_mentions=discord.AllowedMentions(users=[message.author]),
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass

        if not message.mentions:
            return
        lines = []
        seen = set()
        for user in message.mentions:
            if user.bot or user.id == message.author.id or user.id in seen:
                continue
            seen.add(user.id)
            doc = self.afk.get((gid, str(user.id)))
            if not doc:
                continue
            reason = doc.get("reason")
            lines.append(
                f"💤 **{user.display_name}** is AFK"
                + (f": {reason}" if reason else "")
                + f" - for {_fmt_duration(doc.get('since'))}"
            )
        if lines:
            try:
                await message.reply(
                    "\n".join(lines[:10]), delete_after=10, mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

    @commands.hybrid_command(name="afk", description="Set yourself as AFK")
    @app_commands.describe(reason="Why you're away (optional)")
    @command_check()
    async def afk(self, ctx: commands.Context, *, reason: str = None):
        await self._load_afk()
        reason = (reason or "").strip()[:150] or None
        doc = await models.set_afk(ctx.guild.id, ctx.author.id, reason)
        self.afk[(str(ctx.guild.id), str(ctx.author.id))] = doc
        text = "You're now AFK" + (f": **{discord.utils.escape_markdown(reason)}**" if reason else ".")
        await _reply(ctx, success_embed(
            text + "\nPeople who ping you will be told. It clears when you next talk."
        ))

    # ------------------------------------------------------------------
    # Member count
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="membercount", description="Show the server's member count")
    @command_check()
    async def membercount(self, ctx: commands.Context):
        guild = ctx.guild
        total = guild.member_count or len(guild.members)
        members = guild.members
        if len(members) < total * 0.9:      # member cache incomplete - ask Discord
            try:
                members = [m async for m in guild.fetch_members(limit=None)]
            except Exception:
                pass
        bots = sum(1 for m in members if m.bot)
        humans = max(total - bots, 0)
        embed = base_embed(
            title=f"👥 {ctx.guild.name} - member count",
            description=(
                f"**Total:** {total:,}\n"
                f"**Members:** {humans:,}\n"
                f"**Bots:** {bots:,}"
            ),
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Calc
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="calc", description="Evaluate a math expression")
    @command_check()
    async def calc(self, ctx: commands.Context, *, expression: str):
        expression = (expression or "").strip()
        if not expression:
            return await _reply(ctx, error_embed("Provide an expression."))
        if len(expression) > 200:
            return await _reply(ctx, error_embed("Expression too long."))
        try:
            result = safe_eval(expression)
        except ZeroDivisionError:
            return await _reply(ctx, error_embed("Division by zero."))
        except (SyntaxError, ValueError, TypeError, OverflowError) as e:
            return await _reply(ctx, error_embed(f"Invalid expression: {e}"))

        if isinstance(result, float) and result.is_integer():
            result = int(result)
        await _reply(ctx, base_embed(
            title="🧮 Result",
            description=f"`{expression}` = **{result}**",
        ))

    # ------------------------------------------------------------------
    # Count
    # ------------------------------------------------------------------
    @commands.hybrid_group(name="count", description="Counting game", invoke_without_command=True)
    async def count(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            game = await models.get_count_game(ctx.guild.id, ctx.channel.id)
            if game:
                last = int(game.get("last_number") or 0)
                await _reply(ctx, info_embed(
                    f"Counting is active in this channel. Next number: **{last + 1}**."
                ))
            else:
                await _reply(ctx, info_embed(
                    "Use `/count start [number]` to start a counting game."
                ))

    @count.command(name="start", description="Start the counting game in this channel")
    @command_check()
    async def count_start(self, ctx: commands.Context, number: int = None):
        existing = await models.get_count_game(ctx.guild.id, ctx.channel.id)
        if existing:
            return await _reply(ctx, info_embed(
                "There's already a counting game in this channel. "
                "Use `/count stop` to end it first."
            ))

        start_number = int(number) if number is not None else 0
        await models.start_count_game(
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            start_number=start_number,
            started_by=ctx.author.id,
        )

        next_number = start_number + 1
        # Public — the whole channel needs to see the start announcement.
        await ctx.send(embed=base_embed(
            title="🔢 Counting started",
            description=(
                f"Starting from **{start_number}**.\n"
                f"Next number: **{next_number}**.\n\n"
                "Rules:\n"
                "• Post the next number to continue.\n"
                "• You can't count twice in a row.\n"
                "• Wrong numbers are deleted. The count never resets."
            ),
            color=constants.COLOR_SUCCESS,
        ))

    @count.command(name="stop", description="Stop the counting game in this channel")
    @command_check()
    async def count_stop(self, ctx: commands.Context):
        removed = await models.delete_count_game(ctx.guild.id, ctx.channel.id)
        if not removed:
            return await _reply(ctx, info_embed(
                "There's no counting game running in this channel."
            ))
        last = int(removed.get("last_number") or 0)
        # Public — the channel should see the final count.
        await ctx.send(embed=base_embed(
            title="🔢 Counting stopped",
            description=f"Final count: **{last}**.",
            color=constants.COLOR_INFO,
        ))

    # ------------------------------------------------------------------
    # Poll
    # ------------------------------------------------------------------
    @commands.hybrid_group(name="poll", description="Create polls", invoke_without_command=True)
    async def poll(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await _reply(ctx, info_embed(
                "Use `/poll create <question> <options>` to post a poll and "
                "`/poll end <message_id>` to close one.\n"
                "Options are separated by `|`, e.g. "
                "`/poll create \"Best color?\" \"Red | Blue | Green\"`."
            ))

    @poll.command(name="create", description="Create a poll with voting buttons")
    @command_check()
    async def poll_create(self, ctx: commands.Context, question: str, options: str):
        question = (question or "").strip()
        if not question:
            return await _reply(ctx, error_embed("Provide a question."))
        raw_opts = [o.strip()[:150] for o in (options or "").split("|") if o.strip()]
        if len(raw_opts) < 2:
            return await _reply(ctx, error_embed(
                "Provide at least 2 options, separated by `|`."
            ))
        if len(raw_opts) > 10:
            return await _reply(ctx, error_embed("Maximum 10 options."))

        await ctx.defer(ephemeral=True)
        question = question[:240]
        draft = {
            "question": question, "options": raw_opts, "votes": {}, "ended": False,
            "author_name": str(ctx.author),
        }

        # The poll itself is public.
        try:
            poll_msg = await ctx.channel.send(embed=build_poll_embed(draft))
        except (discord.Forbidden, discord.HTTPException):
            return await _reply(ctx, error_embed("I can't post in this channel."))

        await models.create_poll(
            ctx.guild.id, ctx.channel.id, poll_msg.id, ctx.author.id,
            question, raw_opts, author_name=str(ctx.author),
        )
        try:
            await poll_msg.edit(view=build_poll_view(poll_msg.id, len(raw_opts)))
        except discord.HTTPException:
            return await _reply(ctx, error_embed("The poll posted but I couldn't add its buttons."))

        # Confirmation is ephemeral (slash only).
        if ctx.interaction is not None:
            await ctx.send(embed=success_embed("Poll created."), ephemeral=True)

    @poll.command(name="end", description="End a poll and lock its buttons")
    @command_check()
    async def poll_end(self, ctx: commands.Context, message_id: str):
        try:
            mid = int(message_id)
        except ValueError:
            return await _reply(ctx, error_embed("Invalid message ID."))
        poll = await models.get_poll(mid)
        if not poll or poll.get("guild_id") != str(ctx.guild.id):
            return await _reply(ctx, error_embed("Poll not found."))
        ended = await models.end_poll(mid)
        if ended is None:
            return await _reply(ctx, info_embed("That poll has already ended."))

        channel = self.bot.get_channel(int(ended["channel_id"]))
        if channel is not None:
            try:
                msg = await channel.fetch_message(mid)
                await msg.edit(
                    embed=build_poll_embed(ended),
                    view=build_poll_view(mid, len(ended["options"]), disabled=True),
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        await _reply(ctx, success_embed("Poll ended. The final results are on the poll message."))

    @poll_end.autocomplete("message_id")
    async def poll_end_autocomplete(self, interaction: discord.Interaction, current: str):
        current = (current or "").lower()
        choices = []
        for pl in await models.get_guild_polls(interaction.guild.id, active_only=True):
            label = f"{pl['question'][:70]} ({pl['message_id'][-4:]})"
            if current in pl["question"].lower() or current in pl["message_id"]:
                choices.append(app_commands.Choice(name=label[:100], value=pl["message_id"]))
        return choices[:25]

    # ------------------------------------------------------------------
    # Nick
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="nick", description="Set or clear a member's nickname")
    @command_check()
    async def nick(self, ctx: commands.Context, member: discord.Member, *, nickname: str = None):
        if member.top_role >= ctx.guild.me.top_role and member.id != ctx.guild.me.id:
            return await _reply(ctx, error_embed(
                "That member's top role is at or above mine — I can't rename them."
            ))
        nickname = (nickname or "").strip()[:32] or None
        try:
            await member.edit(nick=nickname, reason=f"Nick change by {ctx.author}")
        except discord.Forbidden:
            return await _reply(ctx, error_embed("Missing permission to change that nickname."))
        except discord.HTTPException as e:
            return await _reply(ctx, error_embed(f"Discord error: {e}"))

        if nickname is None:
            await _reply(ctx, success_embed(f"Cleared {member.mention}'s nickname."))
        else:
            await _reply(ctx, success_embed(f"Set {member.mention}'s nickname to **{nickname}**."))

    # ------------------------------------------------------------------
    # Role
    # ------------------------------------------------------------------
    @commands.hybrid_group(name="role", description="Manage roles", invoke_without_command=True)
    async def role(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await _reply(ctx, info_embed(
                "Use `/role add user`, `/role add all`, `/role remove user`, "
                "or `/role remove all`."
            ))

    @role.group(name="add", description="Add a role", invoke_without_command=True)
    async def role_add(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await _reply(ctx, info_embed(
                "Use `/role add user <user> <role>` or `/role add all <role>`."
            ))

    @role_add.command(name="user", description="Add a role to one member")
    @command_check()
    async def role_add_user(self, ctx: commands.Context, member: discord.Member,
                            role: discord.Role):
        if role.is_default():
            return await _reply(ctx, error_embed("Can't assign @everyone."))
        if role.managed:
            return await _reply(ctx, error_embed("That role is managed by an integration."))
        if role >= ctx.guild.me.top_role:
            return await _reply(ctx, error_embed("That role is at or above my top role."))
        if role in member.roles:
            return await _reply(ctx, info_embed(
                f"{member.mention} already has {role.mention}."
            ))
        try:
            await member.add_roles(role, reason=f"By {ctx.author}")
        except discord.Forbidden:
            return await _reply(ctx, error_embed("Missing permission to add that role."))
        except discord.HTTPException as e:
            return await _reply(ctx, error_embed(f"Discord error: {e}"))
        await _reply(ctx, success_embed(f"Added {role.mention} to {member.mention}."))

    @role_add.command(name="all", description="Add a role to every member")
    @command_check()
    async def role_add_all(self, ctx: commands.Context, role: discord.Role):
        await _mass_role_op(ctx, role, add=True)

    @role.group(name="remove", description="Remove a role", invoke_without_command=True)
    async def role_remove(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await _reply(ctx, info_embed(
                "Use `/role remove user <user> <role>` or `/role remove all <role>`."
            ))

    @role_remove.command(name="user", description="Remove a role from one member")
    @command_check()
    async def role_remove_user(self, ctx: commands.Context, member: discord.Member,
                               role: discord.Role):
        if role.is_default():
            return await _reply(ctx, error_embed("Can't remove @everyone."))
        if role >= ctx.guild.me.top_role:
            return await _reply(ctx, error_embed("That role is at or above my top role."))
        if role not in member.roles:
            return await _reply(ctx, info_embed(
                f"{member.mention} doesn't have {role.mention}."
            ))
        try:
            await member.remove_roles(role, reason=f"By {ctx.author}")
        except discord.Forbidden:
            return await _reply(ctx, error_embed("Missing permission to remove that role."))
        except discord.HTTPException as e:
            return await _reply(ctx, error_embed(f"Discord error: {e}"))
        await _reply(ctx, success_embed(f"Removed {role.mention} from {member.mention}."))

    @role_remove.command(name="all", description="Remove a role from every member")
    @command_check()
    async def role_remove_all(self, ctx: commands.Context, role: discord.Role):
        await _mass_role_op(ctx, role, add=False)

    # ------------------------------------------------------------------
    # Blacklist (request + approval flow)
    # ------------------------------------------------------------------
    @commands.hybrid_group(name="blacklist",
                           description="Blacklist request commands",
                           invoke_without_command=True)
    async def blacklist(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await _reply(ctx, info_embed(
                "Use `/blacklist add <user> <type> <reason> <proof1> [proof2] [proof3]` "
                "or `/blacklist remove <user> <type> <reason> [proof1] [proof2] [proof3]`.\n"
                "`type` is `giveaway`, `ticket` or `application`. Requests go through staff approval."
            ))

    @blacklist.command(name="add",
                       description="Submit a request to blacklist a user")
    @command_check()
    async def blacklist_add(self, ctx: commands.Context,
                            user: discord.User,
                            type: Literal["giveaway", "ticket", "application"],
                            proof1: discord.Attachment,
                            *,
                            reason: str,
                            proof2: discord.Attachment = None,
                            proof3: discord.Attachment = None):
        await self._submit_blacklist_request(
            ctx, user=user, type_=type, action="add",
            reason=reason, proofs=[p for p in (proof1, proof2, proof3) if p],
        )

    @blacklist.command(name="remove",
                       description="Submit a request to unblacklist a user")
    @command_check()
    async def blacklist_remove(self, ctx: commands.Context,
                               user: discord.User,
                               type: Literal["giveaway", "ticket", "application"],
                               *,
                               reason: str,
                               proof1: discord.Attachment = None,
                               proof2: discord.Attachment = None,
                               proof3: discord.Attachment = None):
        await self._submit_blacklist_request(
            ctx, user=user, type_=type, action="remove",
            reason=reason, proofs=[p for p in (proof1, proof2, proof3) if p],
        )

    async def _submit_blacklist_request(self, ctx: commands.Context, *,
                                        user: discord.User, type_: str, action: str,
                                        reason: str, proofs: list):
        if ctx.interaction is None:
            return await _reply(ctx, error_embed(
                "Please use the slash version of this command — it takes image "
                "attachments for proof, which aren't available through the prefix form."
            ))

        type_ = (type_ or "").strip().lower()
        if type_ not in ("giveaway", "ticket", "application"):
            return await _reply(ctx, error_embed(
                "Type must be `giveaway`, `ticket` or `application`."
            ))

        if user.id == ctx.author.id:
            return await _reply(ctx, error_embed("You can't submit a request for yourself."))
        if user.bot:
            return await _reply(ctx, error_embed("You can't submit a request for a bot."))

        if action == "add" and not proofs:
            return await _reply(ctx, error_embed("Proof 1 is required for blacklist requests."))

        cfg = await models.get_blacklist_config(ctx.guild.id)
        role_id = _blacklist_role_id(cfg, type_)
        if not role_id:
            return await _reply(ctx, error_embed(
                f"No **{type_}** blacklist role configured. "
                "Set it on the dashboard's Config & Logs page."
            ))

        if await models.has_pending_blacklist_request(ctx.guild.id, user.id, type_, action):
            return await _reply(ctx, info_embed(
                "A request for this user and type is already pending. "
                "Wait for it to be reviewed."
            ))

        member = ctx.guild.get_member(user.id)
        has_role = _member_has_role(member, role_id) if member else False
        if action == "add" and has_role:
            return await _reply(ctx, info_embed(
                f"{user.mention} already has the **{type_}** blacklist role."
            ))
        if action == "remove" and member is not None and not has_role:
            return await _reply(ctx, info_embed(
                f"{user.mention} doesn't have the **{type_}** blacklist role."
            ))

        if not cfg["request_channel_id"]:
            return await _reply(ctx, error_embed(
                "No blacklist request channel configured. "
                "Set it on the dashboard's Config & Logs page."
            ))
        channel = ctx.guild.get_channel(int(cfg["request_channel_id"]))
        if channel is None:
            return await _reply(ctx, error_embed(
                "The configured blacklist request channel no longer exists."
            ))

        proof_urls = [p.url for p in proofs if p]
        request_id = await models.create_blacklist_request(
            guild_id=ctx.guild.id,
            applicant_id=ctx.author.id,
            target_id=user.id,
            type_=type_,
            action=action,
            reason=reason or "",
            proof_urls=proof_urls,
            channel_id=channel.id,
        )

        doc = await models.get_blacklist_request(request_id)
        embed = build_blacklist_embed(doc, ctx.guild)

        content = None
        if cfg["staff_role_id"]:
            staff_role = ctx.guild.get_role(int(cfg["staff_role_id"]))
            if staff_role:
                content = staff_role.mention

        try:
            msg = await channel.send(
                content=content,
                embed=embed,
                view=_BlacklistDecisionView(request_id),
            )
            await models.set_blacklist_request_message(request_id, channel.id, msg.id)
        except discord.Forbidden:
            return await _reply(ctx, error_embed(
                "I can't post in the blacklist request channel."
            ))

        await _reply(ctx, success_embed(
            f"Blacklist request submitted. Staff will review it in {channel.mention}."
        ))


    # ------------------------------------------------------------------
    # Embeds
    # ------------------------------------------------------------------
    @commands.hybrid_group(
        name="embeds", aliases=["embed"], description="Build and edit embeds",
        invoke_without_command=True,
    )
    async def embeds(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await _reply(ctx, info_embed(
                "Use `embeds create` to post a new embed, or "
                "`embeds edit <message_id>` to edit one the bot already posted."
            ))

    @embeds.command(name="create", description="Open a form to build and post an embed")
    @command_check()
    async def embeds_create(self, ctx: commands.Context):
        if ctx.interaction is not None:
            return await ctx.interaction.response.send_modal(_EmbedModal(ctx.channel))
        # Prefix: modals need an interaction, so hand out a button instead.
        view = _EmbedFormView(ctx.author.id, ctx.channel)
        view.message = await ctx.send("Click the button to open the embed form.", view=view)

    @embeds.command(name="edit", description="Edit an embed the bot posted (message ID or link)")
    @command_check()
    async def embeds_edit(self, ctx: commands.Context, message_id: str):
        ref = (message_id or "").strip()
        channel, mid = ctx.channel, None
        link = re.search(r"channels/(\d+)/(\d+)/(\d+)", ref)
        if link:
            if int(link.group(1)) != ctx.guild.id:
                return await _reply(ctx, error_embed("That message link is from another server."))
            channel = ctx.guild.get_channel_or_thread(int(link.group(2)))
            mid = int(link.group(3))
        elif ref.isdigit():
            mid = int(ref)
        if channel is None or mid is None:
            return await _reply(ctx, error_embed(
                "Give me the message ID of the embed (or a message link)."
            ))

        try:
            msg = await channel.fetch_message(mid)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return await _reply(ctx, error_embed(
                "I couldn't find that message. Run the command in the same channel as the "
                "embed, or paste the message link."
            ))
        if msg.author.id != self.bot.user.id:
            return await _reply(ctx, error_embed("I can only edit embeds that I posted."))
        if not msg.embeds:
            return await _reply(ctx, error_embed("That message has no embed."))

        if ctx.interaction is not None:
            return await ctx.interaction.response.send_modal(_EmbedEditModal(msg))
        view = _EmbedFormView(
            ctx.author.id, ctx.channel,
            modal_factory=lambda on_done: _EmbedEditModal(msg, on_done=on_done),
        )
        view.message = await ctx.send("Click the button to open the edit form.", view=view)

    # ------------------------------------------------------------------
    # Reaction roles
    # ------------------------------------------------------------------
    @commands.hybrid_group(
        name="reactionrole", description="Reaction roles", invoke_without_command=True
    )
    async def reactionrole(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await _reply(ctx, info_embed(
                "Use `reactionrole add <message_id> <emoji> <role> [channel]`, "
                "`reactionrole remove <message_id> <emoji>`, `reactionrole list`, "
                "`reactionrole mode <message_id> <normal|unique|verify>` or "
                "`reactionrole clear <message_id>`."
            ))

    @reactionrole.command(name="add", description="React-to-get-a-role on an existing message")
    @command_check()
    async def reactionrole_add(self, ctx: commands.Context, message_id: str, emoji: str,
                               role: discord.Role, channel: discord.TextChannel = None):
        try:
            mid = int(message_id)
        except ValueError:
            return await _reply(ctx, error_embed("`message_id` must be a number (Copy Message ID)."))

        parsed = _parse_emoji(emoji)
        if parsed is None:
            return await _reply(ctx, error_embed("I couldn't read that emoji."))
        emoji_key, display = parsed

        problem = _role_problem(ctx, role)
        if problem:
            return await _reply(ctx, error_embed(problem))

        channel = channel or ctx.channel
        try:
            target = await channel.fetch_message(mid)
        except discord.NotFound:
            return await _reply(ctx, error_embed(
                f"I couldn't find that message in {channel.mention}. "
                "If it's in another channel, add the channel at the end."
            ))
        except (discord.Forbidden, discord.HTTPException):
            return await _reply(ctx, error_embed(f"I can't read messages in {channel.mention}."))

        try:
            react_with = discord.PartialEmoji.from_str(display) if display.startswith("<") else display
            await target.add_reaction(react_with)
        except (discord.Forbidden, discord.HTTPException):
            return await _reply(ctx, error_embed(
                "I couldn't react with that emoji. It may be invalid, or a custom emoji "
                "from a server I'm not in."
            ))

        await models.upsert_reaction_role(
            guild_id=ctx.guild.id, channel_id=channel.id, message_id=mid,
            emoji_key=emoji_key, emoji_display=display, role_id=role.id,
            created_by=ctx.author.id,
        )
        # A new emoji on a message that already has a mode (unique / verify) joins that mode.
        mode = await models.get_reaction_role_mode(ctx.guild.id, mid)
        if mode != "normal":
            await models.set_reaction_role_mode(ctx.guild.id, mid, mode)
        await _reply(ctx, success_embed(
            f"Reacting with {display} on [that message]({target.jump_url}) now gives {role.mention}."
            + (f"\nThis message is in **{mode}** mode." if mode != "normal" else "")
        ))

    @reactionrole.command(name="remove", description="Remove a reaction role from a message")
    @command_check()
    async def reactionrole_remove(self, ctx: commands.Context, message_id: str, emoji: str):
        try:
            mid = int(message_id)
        except ValueError:
            return await _reply(ctx, error_embed("`message_id` must be a number (Copy Message ID)."))
        parsed = _parse_emoji(emoji)
        if parsed is None:
            return await _reply(ctx, error_embed("I couldn't read that emoji."))
        emoji_key, display = parsed

        removed = await models.delete_reaction_role(ctx.guild.id, mid, emoji_key)
        if not removed:
            return await _reply(ctx, info_embed("There's no reaction role for that emoji on that message."))

        # Best effort: take the bot's own reaction off the message.
        try:
            ch = ctx.guild.get_channel(int(removed["channel_id"]))
            if ch is not None:
                msg = await ch.fetch_message(mid)
                react_with = (discord.PartialEmoji.from_str(removed["emoji_display"])
                              if removed["emoji_display"].startswith("<") else removed["emoji_display"])
                await msg.remove_reaction(react_with, ctx.guild.me)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        await _reply(ctx, success_embed(f"Removed the {removed['emoji_display']} reaction role."))

    @reactionrole.command(name="list", description="List this server's reaction roles")
    @command_check()
    async def reactionrole_list(self, ctx: commands.Context):
        rows = await models.get_guild_reaction_roles(ctx.guild.id)
        if not rows:
            return await _reply(ctx, info_embed("No reaction roles set up yet."))
        lines = []
        for r in rows:
            role = ctx.guild.get_role(int(r["role_id"]))
            role_txt = role.mention if role else "*(deleted role)*"
            link = f"https://discord.com/channels/{ctx.guild.id}/{r['channel_id']}/{r['message_id']}"
            mode = r.get("mode")
            mode_txt = f" · {mode}" if mode in ("unique", "verify") else ""
            lines.append(f"{r['emoji_display']} → {role_txt} · [message]({link}){mode_txt}")
        desc = "\n".join(lines)
        if len(desc) > 4000:
            desc = desc[:3990].rsplit("\n", 1)[0] + "\n…"
        await _reply(ctx, base_embed(title="🎭 Reaction roles", description=desc,
                                     color=constants.COLOR_INFO), ephemeral=True, delete_after=30)

    @reactionrole.command(name="mode", description="Change how a message's reaction roles behave")
    @app_commands.describe(
        message_id="The message the reaction roles are on",
        mode="normal = un-reacting removes the role, unique = one role at a time, "
             "verify = un-reacting keeps the role")
    @command_check()
    async def reactionrole_mode(self, ctx: commands.Context, message_id: str,
                                mode: Literal["normal", "unique", "verify"]):
        try:
            mid = int(message_id)
        except ValueError:
            return await _reply(ctx, error_embed("`message_id` must be a number (Copy Message ID)."))
        count = await models.set_reaction_role_mode(ctx.guild.id, mid, mode)
        if not count:
            return await _reply(ctx, error_embed(
                "That message has no reaction roles. Add one first with `/reactionrole add`."))
        blurb = {
            "normal": "Reacting gives the role and removing the reaction takes it away.",
            "unique": "Members can hold only one role from this message - picking another swaps it.",
            "verify": "Reacting gives the role, and removing the reaction keeps it.",
        }[mode]
        note = ""
        if mode == "unique":
            rows = await models.get_message_reaction_roles(ctx.guild.id, mid)
            ch = ctx.guild.get_channel(int(rows[0]["channel_id"])) if rows else None
            if ch is not None and not ch.permissions_for(ctx.guild.me).manage_messages:
                note = ("\n-# I don't have Manage Messages there, so I can swap roles but I can't "
                        "remove the member's old reaction.")
        await _reply(ctx, success_embed(
            f"**{count}** reaction role(s) on that message set to **{mode}**.\n{blurb}{note}"))

    @reactionrole.command(name="clear", description="Remove every reaction role from a message")
    @command_check()
    async def reactionrole_clear(self, ctx: commands.Context, message_id: str):
        try:
            mid = int(message_id)
        except ValueError:
            return await _reply(ctx, error_embed("`message_id` must be a number (Copy Message ID)."))
        rows = await models.clear_message_reaction_roles(ctx.guild.id, mid)
        if not rows:
            return await _reply(ctx, info_embed("That message has no reaction roles."))
        # Best effort: take the bot's own reactions off the message.
        try:
            ch = ctx.guild.get_channel(int(rows[0]["channel_id"]))
            if ch is not None:
                msg = await ch.fetch_message(mid)
                for r in rows:
                    disp = r["emoji_display"]
                    try:
                        await msg.remove_reaction(
                            discord.PartialEmoji.from_str(disp) if disp.startswith("<") else disp,
                            ctx.guild.me)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        await _reply(ctx, success_embed(f"Removed **{len(rows)}** reaction role(s) from that message."))

    async def _unique_swap(self, payload: discord.RawReactionActionEvent,
                           guild: discord.Guild, member: discord.Member, keep_key: str):
        """Unique mode: drop the member's other roles (and reactions) from the same message."""
        rows = await models.get_message_reaction_roles(guild.id, payload.message_id)
        channel = guild.get_channel(payload.channel_id)
        for row in rows:
            if row["emoji_key"] == keep_key:
                continue
            other = guild.get_role(int(row["role_id"]))
            if other is not None and other in member.roles:
                try:
                    await member.remove_roles(other, reason="Reaction role (unique mode)")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            if channel is not None:
                disp = row["emoji_display"]
                try:
                    await channel.get_partial_message(payload.message_id).remove_reaction(
                        discord.PartialEmoji.from_str(disp) if disp.startswith("<") else disp,
                        member)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

    async def _handle_reaction_role(self, payload: discord.RawReactionActionEvent, add: bool):
        if payload.guild_id is None or payload.user_id == self.bot.user.id:
            return
        mapping = await models.get_reaction_role(
            payload.guild_id, payload.message_id, _payload_key(payload.emoji)
        )
        if not mapping:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        role = guild.get_role(int(mapping["role_id"]))
        if role is None or role.managed or role >= guild.me.top_role:
            return
        member = payload.member if (add and payload.member) else guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except (discord.NotFound, discord.HTTPException):
                return
        if member.bot:
            return
        mode = mapping.get("mode") or "normal"
        try:
            if add:
                if mode == "unique":
                    await self._unique_swap(payload, guild, member, _payload_key(payload.emoji))
                await member.add_roles(role, reason="Reaction role")
            elif mode != "verify":   # verify: un-reacting keeps the role
                await member.remove_roles(role, reason="Reaction role removed")
        except (discord.Forbidden, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._handle_reaction_role(payload, add=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._handle_reaction_role(payload, add=False)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.guild_id is not None:
            await models.delete_reaction_roles_for_message(payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if payload.guild_id is not None:
            for mid in payload.message_ids:
                await models.delete_reaction_roles_for_message(mid)


    # ------------------------------------------------------------------
    # /temprole  —  add / remove / list time-limited roles
    # ------------------------------------------------------------------

    @commands.hybrid_group(name="temprole", description="Manage temporary roles",
                           invoke_without_command=True)
    async def temprole(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await _reply(ctx, info_embed(
                "Use `/temprole add <user> <role> <duration>`, "
                "`/temprole remove <user> <role>`, or `/temprole list [user]`."
            ))

    @temprole.command(name="add",
                      description="Give a member a role that expires automatically "
                                  "(e.g. 30m, 2h, 7d)")
    @command_check()
    async def temprole_add(self, ctx: commands.Context,
                           member: discord.Member,
                           role: discord.Role,
                           duration: str):
        # --- role safety checks (same as /role add user) ---
        if role.is_default():
            return await _reply(ctx, error_embed("Can't assign @everyone."))
        if role.managed:
            return await _reply(ctx, error_embed(
                "That role is managed by an integration and can't be assigned manually."))
        if role >= ctx.guild.me.top_role:
            return await _reply(ctx, error_embed(
                "That role is at or above my top role — I can't manage it."))
        if (not ctx.author.guild_permissions.administrator
                and ctx.author.id != ctx.guild.owner_id
                and role >= ctx.author.top_role):
            return await _reply(ctx, error_embed(
                "You can't assign a role that's at or above your own top role."))

        # --- parse duration ---
        from cogs.moderation import parse_duration, format_duration
        seconds = parse_duration(duration)
        if not seconds or seconds <= 0:
            return await _reply(ctx, error_embed(
                "Invalid duration. Use `30s`, `10m`, `2h`, `7d`, `1w`, etc."))

        from datetime import datetime, timezone, timedelta
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)

        # --- check for existing entry ---
        existing = await models.get_temp_role(ctx.guild.id, member.id, role.id)

        # --- apply role ---
        try:
            if role not in member.roles:
                await member.add_roles(role, reason=f"Temp role by {ctx.author} for {duration}")
        except discord.Forbidden:
            return await _reply(ctx, error_embed("Missing permission to add that role."))
        except discord.HTTPException as e:
            return await _reply(ctx, error_embed(f"Discord error: {e}"))

        # --- persist ---
        await models.add_temp_role(ctx.guild.id, member.id, role.id, expires_at)

        ts = int(expires_at.timestamp())
        verb = "extended" if existing else "added"
        await _reply(ctx, success_embed(
            f"{verb.capitalize()} {role.mention} for {member.mention}.\n"
            f"Expires <t:{ts}:R> (<t:{ts}:f>)."
        ))

    @temprole.command(name="remove",
                      description="Remove a temporary role from a member early")
    @command_check()
    async def temprole_remove(self, ctx: commands.Context,
                              member: discord.Member,
                              role: discord.Role):
        doc = await models.remove_temp_role(ctx.guild.id, member.id, role.id)
        if not doc:
            return await _reply(ctx, error_embed(
                f"{member.mention} has no active temp entry for {role.mention}."))

        # Remove the actual role if they still have it
        if role in member.roles:
            try:
                await member.remove_roles(
                    role, reason=f"Temp role cancelled early by {ctx.author}")
            except (discord.Forbidden, discord.HTTPException):
                pass  # Role gone or no perms; DB entry already deleted

        await _reply(ctx, success_embed(
            f"Removed {role.mention} from {member.mention} and cancelled the timer."))

    @temprole.command(name="list",
                      description="List active temporary roles in this server (or for one member)")
    @command_check()
    async def temprole_list(self, ctx: commands.Context,
                            member: discord.Member = None):
        from datetime import datetime, timezone
        if member:
            docs = await models.get_user_temp_roles(ctx.guild.id, member.id)
            title = f"Temp roles for {member.display_name}"
        else:
            docs = await models.get_guild_temp_roles(ctx.guild.id)
            title = "Active temp roles"

        if not docs:
            return await _reply(ctx, info_embed(
                f"No active temp roles{f' for {member.mention}' if member else ''}."))

        lines = []
        for doc in docs[:20]:  # cap display at 20
            uid = int(doc["user_id"])
            rid = int(doc["role_id"])
            ts = int(doc["expires_at"].timestamp())
            m = ctx.guild.get_member(uid)
            name = m.mention if m else f"<@{uid}>"
            role_mention = f"<@&{rid}>"
            lines.append(f"{name} → {role_mention} — expires <t:{ts}:R>")

        embed = info_embed("\n".join(lines), title=title)
        if len(docs) > 20:
            embed.set_footer(text=f"Showing 20 of {len(docs)}")
        await _reply(ctx, embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /statchannel  -  channels whose name shows a live server stat
    # ------------------------------------------------------------------
    @commands.hybrid_group(name="statchannel", description="Live stat channels (member count etc.)",
                           invoke_without_command=True)
    async def statchannel(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await _reply(ctx, info_embed(
                "Use `statchannel add <stat> [channel] [text]`, `statchannel remove <channel>`, "
                "`statchannel list` or `statchannel refresh`."))

    @statchannel.command(name="add", description="Show a live server stat as a channel's name")
    @app_commands.describe(
        stat="What to count",
        channel="A voice channel to rename (leave empty and I'll create a locked one)",
        text="Channel name with {count} where the number goes, e.g. Members: {count}")
    @command_check()
    async def statchannel_add(self, ctx: commands.Context,
                              stat: Literal["members", "humans", "bots", "boosts",
                                            "roles", "channels", "voice"],
                              channel: discord.VoiceChannel = None, *, text: str = None):
        guild = ctx.guild
        text = (text or STAT_DEFAULT_FORMATS[stat]).strip()
        if "{count}" not in text:
            return await _reply(ctx, error_embed(
                "The text needs `{count}` where the number should go, e.g. `Members: {count}`."))
        if len(text) > 90:
            return await _reply(ctx, error_embed("Keep the text under 90 characters."))

        rows = await models.get_stat_channels(guild.id)
        if channel is None and len(rows) >= MAX_STAT_CHANNELS:
            return await _reply(ctx, error_embed(f"You can have at most {MAX_STAT_CHANNELS} stat channels."))

        row = {"stat": stat, "format": text}
        name = _stat_name(guild, row)
        try:
            if channel is None:
                channel = await guild.create_voice_channel(
                    name,
                    overwrites={guild.default_role: discord.PermissionOverwrite(
                        view_channel=True, connect=False)},
                    reason=f"Stat channel by {ctx.author}")
            else:
                if not channel.permissions_for(guild.me).manage_channels:
                    return await _reply(ctx, error_embed(
                        f"I need Manage Channels on {channel.mention} to rename it."))
                await channel.edit(name=name, reason=f"Stat channel by {ctx.author}")
        except discord.Forbidden:
            return await _reply(ctx, error_embed("I'm missing the Manage Channels permission."))
        except discord.HTTPException as e:
            return await _reply(ctx, error_embed(f"Discord error: {e}"))

        row["channel_id"] = str(channel.id)
        rows = [r for r in rows if r.get("channel_id") != row["channel_id"]] + [row]
        await models.set_stat_channels(guild.id, rows)
        await _reply(ctx, success_embed(
            f"{channel.mention} now shows **{name}** and updates every 10 minutes."))

    @statchannel.command(name="remove", description="Stop updating a stat channel")
    @command_check()
    async def statchannel_remove(self, ctx: commands.Context, channel: discord.VoiceChannel):
        rows = await models.get_stat_channels(ctx.guild.id)
        kept = [r for r in rows if r.get("channel_id") != str(channel.id)]
        if len(kept) == len(rows):
            return await _reply(ctx, info_embed(f"{channel.mention} isn't a stat channel."))
        await models.set_stat_channels(ctx.guild.id, kept)
        await _reply(ctx, success_embed(
            f"{channel.mention} is no longer updated. The channel itself is untouched - "
            "delete or rename it yourself."))

    @statchannel.command(name="list", description="List this server's stat channels")
    @command_check()
    async def statchannel_list(self, ctx: commands.Context):
        rows = await models.get_stat_channels(ctx.guild.id)
        if not rows:
            return await _reply(ctx, info_embed("No stat channels yet. Use `/statchannel add`."))
        lines = []
        for r in rows:
            ch = ctx.guild.get_channel(int(r["channel_id"]))
            lines.append(f"{ch.mention if ch else '*(deleted)*'} · **{r.get('stat')}** · `{r.get('format')}`")
        await _reply(ctx, base_embed(title="📊 Stat channels", description="\n".join(lines),
                                     color=constants.COLOR_INFO), delete_after=30)

    @statchannel.command(name="refresh", description="Update the stat channels right now")
    @command_check()
    async def statchannel_refresh(self, ctx: commands.Context):
        changed = await self._refresh_stat_channels(ctx.guild)
        await _reply(ctx, success_embed(
            f"Updated **{changed}** channel(s)." if changed else "Everything is already up to date."))

    async def _refresh_stat_channels(self, guild: discord.Guild) -> int:
        rows = await models.get_stat_channels(guild.id)
        if not rows:
            return 0
        kept, changed = [], 0
        for row in rows:
            ch = guild.get_channel(int(row["channel_id"]))
            if ch is None:           # channel was deleted: forget it
                continue
            kept.append(row)
            name = _stat_name(guild, row)
            if ch.name != name:
                try:
                    await ch.edit(name=name, reason="Stat channel update")
                    changed += 1
                except (discord.Forbidden, discord.HTTPException):
                    pass
        if len(kept) != len(rows):
            await models.set_stat_channels(guild.id, kept)
        return changed

    @tasks.loop(minutes=10)
    async def _update_stat_channels(self):
        for guild in list(self.bot.guilds):
            try:
                await self._refresh_stat_channels(guild)
            except Exception as e:
                print(f"[statchannel] {guild.id}: {e}")

    @_update_stat_channels.before_loop
    async def _before_update_stat_channels(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Background task: expire temp roles
    # ------------------------------------------------------------------

    @tasks.loop(seconds=30)
    async def _check_temp_roles(self):
        from datetime import datetime, timezone
        try:
            now = datetime.now(timezone.utc)
            for doc in await models.get_due_temp_roles(now):
                try:
                    guild = self.bot.get_guild(int(doc["guild_id"]))
                    if guild:
                        member = guild.get_member(int(doc["user_id"]))
                        role = guild.get_role(int(doc["role_id"]))
                        if member and role and role in member.roles:
                            await member.remove_roles(
                                role, reason="Temp role expired")
                except Exception as e:
                    print(f"[temprole] failed to expire {doc.get('_id')}: {e}")
                finally:
                    # Always clean up the DB entry, even if the member left
                    await models.delete_temp_role_by_id(doc["_id"])
        except Exception as e:
            print(f"[temprole] loop error: {e}")

    @_check_temp_roles.before_loop
    async def _before_check_temp_roles(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Utilities(bot))