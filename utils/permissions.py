"""
Reusable command-permission template.

Drop @command_check() on top of ANY hybrid/slash command in ANY cog and you get,
for free:
  1. Auto-registration into the dashboard "Command Permissions" page on first run
     (so you never have to manually add a command to the dashboard).
  2. Enforcement: command is BLOCKED for everyone except server Administrators
     until it is explicitly turned on + given allowed roles in the dashboard.
  3. Works identically whether the command is invoked as a slash command (/)
     or as a text command using the guild's custom prefix (?, !, etc).

Usage in a cog:

    from utils.permissions import command_check

    @commands.hybrid_group(name="ticket")
    async def ticket(self, ctx):
        ...

    @ticket.command(name="close")
    @command_check()
    async def ticket_close(self, ctx, reason: str = None):
        ...

The decorator infers the dashboard-visible command name from the command's
qualified name (e.g. "ticket close") and the cog it belongs to automatically -
nothing else needs to be passed in for the common case.
"""
import functools
from discord.ext import commands
from database import models
from utils.embeds import error_embed


def command_check():
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(self, ctx, *args, **kwargs):
            guild_id = ctx.guild.id if ctx.guild else None
            command_name = ctx.command.qualified_name

            if guild_id is None:
                await ctx.send(embed=error_embed("This command can only be used in a server."))
                return

            # Make sure it's registered so it shows up on the dashboard
            await models.register_command(guild_id, command_name, self.__class__.__name__)

            perm = await models.get_command_permission(guild_id, command_name)

            is_admin = ctx.author.guild_permissions.administrator

            if perm.get("admin_only", True) or not perm.get("enabled", False):
                # Not yet configured on the dashboard - admins only
                if not is_admin:
                    await ctx.send(embed=error_embed(
                        "This command hasn't been enabled yet. Ask an admin to "
                        "turn it on in the dashboard's **Command Permissions** page."
                    ))
                    return
            else:
                allowed_roles = set(perm.get("allowed_roles", []))
                user_role_ids = {str(r.id) for r in ctx.author.roles}
                if not is_admin and not (allowed_roles & user_role_ids):
                    await ctx.send(embed=error_embed("You don't have permission to use this command."))
                    return

            return await func(self, ctx, *args, **kwargs)
        wrapper.__command_check__ = True
        return wrapper
    return decorator


async def get_prefix_for_guild(bot, message):
    """Used by main.py as the bot's command_prefix callable - pulls the
    per-guild custom prefix from the dashboard/mongo, falls back to '/'."""
    if message.guild is None:
        return commands.when_mentioned_or("/")(bot, message)
    prefix = await models.get_prefix(message.guild.id)
    return commands.when_mentioned_or(prefix)(bot, message)


async def register_all_commands(bot, guild_ids=None):
    """Register every @command_check() command for the given guilds (default: all
    guilds the bot is in), so they all show on the dashboard's Command Permissions
    page without anyone having to run them first. Safe to call repeatedly -
    models.register_command never overwrites an existing entry."""
    targets = []
    for cmd in bot.walk_commands():
        if getattr(cmd.callback, "__command_check__", False):
            cog_name = cmd.cog.__class__.__name__ if cmd.cog else "Unknown"
            targets.append((cmd.qualified_name, cog_name))

    if guild_ids is None:
        guild_ids = [g.id for g in bot.guilds]

    for gid in guild_ids:
        for name, cog_name in targets:
            await models.register_command(gid, name, cog_name)
    try:
        await models.purge_removed_commands()
    except Exception as e:
        print(f"[permissions] couldn't clean up removed commands: {e}")
    print(f"[permissions] registered {len(targets)} command(s) for {len(guild_ids)} guild(s)")
