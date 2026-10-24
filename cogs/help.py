"""
Help commands - /help, /staffhelp and /admin help (all also work with the server's
prefix, e.g. !help, !staffhelp, !admin help).

  /help          member commands (rank, profile, info, utility, community and fun commands)
  /staffhelp     staff commands (tickets, moderation, staff tools, blacklists, giveaways, polls)
  /admin help    every other command (staff management, roles, vouch setup, xp, panels...)

All three are @command_check() commands, so they appear on the dashboard's Command
Permissions page by themselves (blocked for non-admins until you enable them there and
pick the roles). Each one replies publicly in the channel with a category dropdown. The
dropdown never expires (it survives restarts) and ANYONE can use it, not just the person
who ran the command: each category opens as a private message only the clicker sees, and
it lists the commands THAT person can ACTUALLY run (it follows the dashboard's Command
Permissions page; admins see all).
LIVE MENUS: the menu itself lists every category with its command count (the same for everyone);
the per-person filtering happens when someone picks a category. Menus posted by an admin (or
adopted when an admin picks from an old one) are tracked in the `help_panels` collection and
edited automatically every few minutes when commands are added/removed, so nobody has to
re-post them. The same loop re-registers new/removed commands on the Command Permissions page
without needing a restart.
The lists below say which command belongs where; the usage text is read from the
commands themselves, so anything new you add shows up under /admin help ("Other" unless
you give its root command a category in ADMIN_CATEGORIES).
"""
import asyncio
import hashlib

import discord
from discord.ext import commands, tasks

from database import models
from utils.embeds import base_embed, error_embed
from utils.permissions import command_check, register_all_commands

# ---------------------------------------------------------------------------
# Which command belongs to which help
# ---------------------------------------------------------------------------
# MEMBER_COMMANDS / STAFF_COMMANDS live in config/command_groups.py so the dashboard's
# Command Permissions page groups commands exactly the same way.
from config.command_groups import MEMBER_COMMANDS, STAFF_COMMANDS  # noqa: E402

# Everything else (any command with @command_check) is listed under /admin help,
# grouped by its first word.
ADMIN_CATEGORIES = {
    "promote": "Staff management", "demote": "Staff management",
    "strike": "Staff management",
    "activitycheck": "Staff management",
    "loa": "LOA",
    "ticket": "Tickets",
    "suggestions": "Roles & utilities", "birthday": "Roles & utilities",
    "steal": "Roles & utilities",
    "role": "Roles & utilities", "temprole": "Roles & utilities",
    "reactionrole": "Roles & utilities", "nick": "Roles & utilities",
    "statchannel": "Roles & utilities", "invitetracker": "Roles & utilities",
    "sticky": "Roles & utilities", "unsticky": "Roles & utilities",
    "embeds": "Roles & utilities", "count": "Roles & utilities",
    "vouches": "Vouches", "vouch": "Vouches", "config": "Vouches",
    "xp": "Leveling",
    "spawner": "Panels", "applications": "Panels",
    "taskbook": "Taskbooks",
    "modlog": "Moderation", "nuke": "Moderation", "lockdown": "Moderation",
    "unlockdown": "Moderation", "tempban": "Moderation", "tempbans": "Moderation",
}
ADMIN_ORDER = ["Staff management", "LOA", "Tickets", "Roles & utilities", "Vouches",
               "Leveling", "Panels", "Taskbooks", "Moderation", "Other"]

EMOJI = {
    "Members": "🙋", "Tickets": "🎫", "Moderation": "🔨", "Blacklists": "🚫",
    "Giveaways & polls": "🎉", "Staff management": "🧑‍💼", "Roles & utilities": "🧰",
    "Vouches": "🤝", "Leveling": "⭐", "Panels": "📋", "Taskbooks": "📒", "Other": "📁",
    "Info": "ℹ️", "Utility": "🛠️", "Community": "💬", "Fun": "🎲", "Staff tools": "🧑‍💻", "LOA": "🏖️",
}

# Clearer wording than the short description for a few commands.
OVERRIDES = {
    "blacklist add": "Request to blacklist someone. Type is giveaway, ticket or application; "
                     "proof is required and staff approve or deny it.",
    "blacklist remove": "Request to unblacklist someone (type: giveaway, ticket or application). "
                        "Staff approve or deny it.",
    "giveaway rps": "Giveaway where the 2 winners play Rock Paper Scissors for the prize.",
    "giveaway splitsteal": "Giveaway where the 2 winners play Split or Steal for the prize.",
    "giveaway membercount": "Giveaway with no timer - it ends when the server reaches the member count you set.",
    "giveaway fastestfinger": "Starts instantly: a code is posted and the first person to type it in any channel (or press the button) wins. Optional mode: number (type only) or button (button only).",
    "giveaway guessflag": "Posts a random flag - the first person to type the country in any channel wins.",
    "giveaway guessthelanguage": "Posts a sentence in a random language - the first person to type its name in any channel wins.",
    "giveaway doubleornothing": "Giveaway where the winner can double the prize (2x, 4x...) but each double is riskier - or take it.",
    "ticket priority": "Set this ticket's priority: low, normal, high or urgent (staff). Shown in the ticket, on the dashboard and in the transcript.",
    "ticket request-close": "Ask the ticket opener to confirm closing - they get Close / Don't Close buttons.",
    "ticket lookup": "Look up a member's past tickets (mention or ID), or search closed tickets by name, staff or reason. Shows a summary with dashboard links.",
    "poll end": "End a poll and lock its buttons.",
    "embeds edit": "Edit an embed the bot posted (message ID or link).",
    "warnings remove": "Remove a warn by its case number.",
    "tempban": "Ban a member for a set time (30m, 2h, 7d, up to 365d); they're unbanned automatically.",
    "tempbans": "See who is temp banned and when they're unbanned.",
    "invites": "How many people you (or someone) invited, and who invited them.",
    "topinviters": "The members who have invited the most people who are still here.",
    "reactionrole mode": "normal = un-reacting removes the role, unique = one role per message, verify = un-reacting keeps it.",
    "reactionrole clear": "Remove every reaction role from one message.",
    "statchannel add": "Make a channel show a live count (members, bots, boosts...) in its name. Leave the channel empty and I'll create a locked one.",
    "statchannel remove": "Stop updating a stat channel (the channel itself stays).",
    "statchannel list": "List the stat channels.",
    "statchannel refresh": "Update the stat channels right now (they also update every 10 minutes).",
    "invitetracker toggle": "Start or stop tracking who invited who (needs Manage Server).",
    "invitetracker channel": "Log every join and its inviter in a channel (empty = off).",
    "invitetracker status": "Show whether invite tracking is on and working.",
    "warns": "See a member's warns.",
    "report": "Report a user with evidence.",
    "afk": "Set yourself AFK (reason optional). People who ping you are told, and it clears when you talk again.",
    "membercount": "See the server's total, member and bot counts.",
    "lookup": "See a user's reputation (vouches and scammer status).",
    "profile": "Your rank, vouches and dates in one card (warns are only shown to you and staff).",
    "vouches top": "The most-vouched members in this server.",
    "remindme": "Get a DM reminder after a delay, e.g. 30m, 2h, 1d12h.",
    "translate": "Translate text into another language (name or code, e.g. spanish / es).",
    "rolelist": "The roles you can get by reacting, with links to where.",
    "suggest": "Post a suggestion for the server; people vote with 👍 / 👎.",
    "giveaways": "See the giveaways that are running right now.",
    "ticketstatus": "See your open ticket and your recent applications.",
    "birthday set": "Save your birthday (day + month number) so the bot can announce it.",
    "roll": "Roll dice: NdM like 2d6, or just a number of sides.",
    "choose": "Pick one option at random. Separate them with | or commas.",
    "slowmode": "Set a channel's slowmode: seconds, 30s / 5m / 1h, or off.",
    "history": "A member's full moderation history (every case type, removed ones struck through).",
    "clearwarns": "Remove ALL of a member's active warns at once.",
    "note add": "Add a private staff note about a member.",
    "note list": "See the staff notes on a member.",
    "note remove": "Delete a staff note by its number.",
    "staffprofile": "A staff member's taskbook stats (tickets, blacklists, sponsored) and number of mod cases.",
    "suggestions approve": "Approve a suggestion by number (optional reason). The author is told.",
    "suggestions deny": "Deny a suggestion by number (optional reason). The author is told.",
    "steal": "Copy custom emoji(s) from another server into this one (paste them; add a name after one emoji to rename it).",
    "nuke": "Clone a channel and delete the old one (confirmation required).",
    "lockdown": "Stop @everyone talking in every public channel. Undo with /unlockdown.",
    "loa request": "Request a leave of absence (reason + duration like 3d, 1w). Staff accept or deny it in the request channel; while it runs your ranks above Member (and their extra roles) are paused.",
    "loa end": "End a member's active LOA early: the LOA role is removed and their paused ranks and extra roles come back.",
    "loa add": "Put a member on LOA directly, no request needed (duration like 3d, 1w). Their ladder ranks above Member, and those types' extra roles, are paused until it ends.",
    "loa remove": "Delete a member's active LOA or pending request, taking the LOA role back and restoring paused ranks and extra roles.",
    "promote": "Promote a member one rung up a Changes type's ladder (type = staff, builder...). Going from the base rung up to #1 also gives the type's extra roles. Not allowed while they're on LOA.",
    "demote": "Remove a member's highest role from a Changes type's ladder (never the base Member rung). Dropping back to the base rung also removes the type's extra roles. Not allowed while they're on LOA.",
    "unlockdown": "Undo /lockdown, restoring only the channels it changed.",
}

# Never listed: the help commands themselves.
HIDDEN = {"help", "staffhelp", "admin", "admin help"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def command_line(cmd) -> str:
    usage = f"/{cmd.qualified_name} {cmd.signature}".strip()
    desc = (OVERRIDES.get(cmd.qualified_name) or cmd.description or cmd.short_doc or "").strip()
    return f"`{usage}`\n-# {desc}" if desc else f"`{usage}`"


def admin_category(cmd) -> str:
    return ADMIN_CATEGORIES.get(cmd.qualified_name.split()[0], "Other")


async def can_run(cmd, member: discord.Member) -> bool:
    """Same rules as @command_check(): ungated commands are open, gated ones follow
    the dashboard's Command Permissions (admins always pass)."""
    if not getattr(getattr(cmd, "callback", None), "__command_check__", False):
        return True
    if member.guild_permissions.administrator:
        return True
    perm = await models.get_command_permission(member.guild.id, cmd.qualified_name)
    if perm.get("admin_only", True) or not perm.get("enabled", False):
        return False
    allowed = {str(r) for r in perm.get("allowed_roles", [])}
    return bool(allowed & {str(r.id) for r in member.roles})


async def filter_runnable(cmds: list, member: discord.Member) -> list:
    results = await asyncio.gather(*(can_run(c, member) for c in cmds))
    return [c for c, ok in zip(cmds, results) if ok]


# ---------------------------------------------------------------------------
# The three menus
# ---------------------------------------------------------------------------
KINDS = {
    "member": ("📖 Help", "Commands for everyone.",
               "There are no member commands available to you right now."),
    "staff": ("🛡️ Staff help", "Pick a category to see its commands and what they do.",
              "You don't have access to any staff commands."),
    "admin": ("⚙️ Admin help", "Setup and management commands.",
              "You don't have access to any admin commands."),
}


async def _named_pages(bot, member, groups: dict) -> dict:
    """{category: [names]} -> {category: [lines of commands this member can run]}
    (member=None: every command in the category, no permission filtering)"""
    pages = {}
    for category, names in groups.items():
        cmds = [c for c in (bot.get_command(n) for n in names) if c]
        if member is not None:
            cmds = await filter_runnable(cmds, member)
        pages[category] = [command_line(c) for c in cmds]
    return pages


async def build_pages(bot, kind: str, member: discord.Member = None) -> dict:
    """{category: [command lines]} for one help menu, as seen by `member`.
    With no member it's the full list (what the live, posted menus are built from)."""
    if kind == "member":
        pages = await _named_pages(bot, member, MEMBER_COMMANDS)
    elif kind == "staff":
        pages = await _named_pages(bot, member, STAFF_COMMANDS)
    else:  # admin: every other @command_check command
        listed = {n for names in (*MEMBER_COMMANDS.values(), *STAFF_COMMANDS.values()) for n in names}
        cmds = [
            c for c in bot.walk_commands()
            if not isinstance(c, commands.Group)
            and getattr(c.callback, "__command_check__", False)
            and c.qualified_name not in listed
            and c.qualified_name not in HIDDEN
        ]
        cmds = sorted(cmds, key=lambda c: c.qualified_name)
        if member is not None:
            cmds = await filter_runnable(cmds, member)
        grouped: dict = {}
        for c in cmds:
            grouped.setdefault(admin_category(c), []).append(command_line(c))
        pages = {k: grouped[k] for k in ADMIN_ORDER if k in grouped}
        pages.update({k: v for k, v in grouped.items() if k not in pages})
    return {k: v for k, v in pages.items() if v}


def _count(n: int) -> str:
    return f"{n} command{'' if n == 1 else 's'}"


def overview_embed(kind: str, pages: dict, footer: str) -> discord.Embed:
    title, intro, _ = KINDS[kind]
    rows = [f"{EMOJI.get(n, '📁')} **{n}** - {_count(len(l))}" for n, l in pages.items()]
    embed = base_embed(title=title, description=f"{intro}\n\n" + "\n".join(rows))
    embed.set_footer(text=footer)
    return embed


def category_embed(name: str, lines: list, footer: str) -> discord.Embed:
    body = "\n\n".join(lines)
    if len(body) > 4000:
        body = body[:3990].rsplit("\n\n", 1)[0] + "\n\n…"
    embed = base_embed(title=f"{EMOJI.get(name, '📁')} {name}", description=body)
    embed.set_footer(text=footer)
    return embed


def _default_options(kind: str) -> list:
    names = {"member": list(MEMBER_COMMANDS), "staff": list(STAFF_COMMANDS),
             "admin": ADMIN_ORDER}[kind]
    return [discord.SelectOption(label=n, value=n, emoji=EMOJI.get(n, "📁")) for n in names]


def _footer(prefix: str) -> str:
    return f"Use / or {prefix} before a command. Only commands you can run are shown."


def _options_for(pages: dict) -> list:
    return [
        discord.SelectOption(label=name, value=name, emoji=EMOJI.get(name, "📁"),
                             description=_count(len(lines)))
        for name, lines in list(pages.items())[:25]
    ]


def _panel_sig(kind: str, pages: dict, prefix: str) -> str:
    """Fingerprint of what a posted menu shows (categories, counts, prefix)."""
    text = kind + "|" + prefix + "|" + "|".join(f"{k}:{len(v)}" for k, v in pages.items())
    return hashlib.sha1(text.encode()).hexdigest()


def _can_manage(member) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.manage_guild))


# ---------------------------------------------------------------------------
# Live menus: help messages posted by admins are tracked and edited automatically when the
# command list changes, so nobody has to re-post them.
# ---------------------------------------------------------------------------
async def refresh_panels(bot) -> int:
    """Edit every tracked help message whose categories/counts changed. Returns edits made."""
    panels = await models.get_help_panels()
    if not panels:
        return 0
    built: dict = {}
    edited = 0
    for p in panels:
        kind = p.get("kind")
        if kind not in KINDS:
            continue
        guild = bot.get_guild(int(p["guild_id"]))
        if guild is None:
            continue                       # not ready / bot left; try again next time
        if kind not in built:
            built[kind] = await build_pages(bot, kind)
        pages = built[kind]
        if not pages:
            continue
        prefix = await models.get_prefix(guild.id)
        sig = _panel_sig(kind, pages, prefix)
        if sig == p.get("sig"):
            continue
        channel = guild.get_channel_or_thread(int(p["channel_id"]))
        if channel is None:
            await models.delete_help_panel(p["_id"])
            continue
        try:
            await channel.get_partial_message(int(p["_id"])).edit(
                embed=overview_embed(kind, pages, _footer(prefix)),
                view=HelpView(kind, _options_for(pages)),
            )
        except discord.NotFound:
            await models.delete_help_panel(p["_id"])   # the message was deleted
            continue
        except (discord.HTTPException, AttributeError) as e:
            print(f"[help] couldn't refresh panel {p['_id']}: {e}")
            continue
        await models.set_help_panel_sig(p["_id"], sig)
        edited += 1
        await asyncio.sleep(0.5)           # be gentle with the API
    return edited


# ---------------------------------------------------------------------------
# The menu (embed + category dropdown) - persistent, usable by anyone
# ---------------------------------------------------------------------------
class HelpView(discord.ui.View):
    """timeout=None + a fixed custom_id per menu type, and registered once at startup, so
    the dropdown on any help message keeps working forever (even after a restart) and for
    every member. What a click shows is worked out from the CLICKER's permissions."""

    def __init__(self, kind: str, options: list = None):
        super().__init__(timeout=None)
        self.kind = kind
        self.select = discord.ui.Select(
            placeholder="Pick a category",
            options=(options or _default_options(kind))[:25],
            custom_id=f"help_menu:{kind}",
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                embed=error_embed("Help only works inside a server."), ephemeral=True)

        # The category goes out as a private message only the clicker sees.
        await interaction.response.defer(ephemeral=True)
        name = self.select.values[0] if self.select.values else None
        pages = await build_pages(interaction.client, self.kind, interaction.user)
        prefix = await models.get_prefix(interaction.guild.id)
        footer = _footer(prefix)

        if name in pages:
            embed = category_embed(name, pages[name], footer)
        else:
            embed = error_embed("You don't have access to any commands in this category.")
        await interaction.followup.send(embed=embed, ephemeral=True)

        # An admin picking from an old menu makes it a live one (kept up to date from now on).
        if _can_manage(interaction.user) and interaction.message is not None:
            try:
                await models.save_help_panel(interaction.guild.id, interaction.channel_id,
                                             interaction.message.id, self.kind)
            except Exception as e:
                print(f"[help] couldn't track panel: {e}")

        # Re-send the public menu untouched so the same category can be picked again.
        try:
            await interaction.message.edit(
                view=HelpView(self.kind, _options_from_message(interaction.message, self.kind)))
        except Exception:
            pass


def _options_from_message(message: discord.Message, kind: str) -> list:
    """The dropdown options the message was posted with (what its runner could see)."""
    try:
        comp = message.components[0].children[0]
        return [discord.SelectOption(label=o.label, value=o.value, emoji=o.emoji,
                                     description=o.description) for o in comp.options]
    except Exception:
        return _default_options(kind)


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self._cmd_sig = None

    async def cog_load(self):
        # One registration per menu type keeps every old help message working.
        for kind in KINDS:
            self.bot.add_view(HelpView(kind))
        self.live_sync.start()

    async def cog_unload(self):
        self.live_sync.cancel()

    @tasks.loop(minutes=5)
    async def live_sync(self):
        """Keep things live without a restart: when the set of commands (or servers) changes,
        re-register them on the Command Permissions page, then update the posted help menus."""
        try:
            sig = (
                frozenset(c.qualified_name for c in self.bot.walk_commands()
                          if getattr(c.callback, "__command_check__", False)),
                frozenset(g.id for g in self.bot.guilds),
            )
            if sig != self._cmd_sig:
                await register_all_commands(self.bot)
                self._cmd_sig = sig
            await refresh_panels(self.bot)
        except Exception as e:
            print(f"[help] live sync failed: {e}")

    @live_sync.before_loop
    async def _before_live_sync(self):
        await self.bot.wait_until_ready()

    async def _send(self, ctx, kind: str, pages: dict):
        _, _, empty_msg = KINDS[kind]
        if not pages:
            await ctx.send(embed=error_embed(empty_msg))
            return
        prefix = await models.get_prefix(ctx.guild.id)
        msg = await ctx.send(embed=overview_embed(kind, pages, _footer(prefix)),
                             view=HelpView(kind, _options_for(pages)))
        # A menu an admin posts (e.g. in a bot-info channel) stays up to date by itself.
        if _can_manage(ctx.author) and msg is not None:
            try:
                await models.save_help_panel(ctx.guild.id, ctx.channel.id, msg.id, kind,
                                             _panel_sig(kind, pages, prefix))
            except Exception as e:
                print(f"[help] couldn't track panel: {e}")

    async def _guard(self, ctx) -> bool:
        if ctx.guild is None:
            await ctx.send(embed=error_embed("Help only works inside a server."))
            return False
        if ctx.interaction is not None:
            await ctx.defer()   # permission lookups can take a moment
        return True

    # -- /help ---------------------------------------------------------------
    @commands.hybrid_command(name="help", description="Show the commands you can use")
    @command_check()
    async def help_cmd(self, ctx: commands.Context):
        if not await self._guard(ctx):
            return
        await self._send(ctx, "member", await build_pages(self.bot, "member"))

    # -- /staffhelp ----------------------------------------------------------
    @commands.hybrid_command(name="staffhelp", description="Staff commands you can use and what they're for")
    @command_check()
    async def staffhelp(self, ctx: commands.Context):
        if not await self._guard(ctx):
            return
        await self._send(ctx, "staff", await build_pages(self.bot, "staff"))

    # -- /admin help ---------------------------------------------------------
    @commands.hybrid_group(name="admin", description="Admin tools", invoke_without_command=True)
    async def admin(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=base_embed(description="Use `admin help` to see the admin commands."))

    @admin.command(name="help", description="Every other command (staff management, roles, vouch setup, xp, panels...)")
    @command_check()
    async def admin_help(self, ctx: commands.Context):
        if not await self._guard(ctx):
            return
        await self._send(ctx, "admin", await build_pages(self.bot, "admin"))


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
