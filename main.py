"""Bot entrypoint - creates client, loads cogs, connects DB, starts bot AND the dashboard.

Runs as a single process (one Railway service): the dashboard's Flask app runs in a
background thread, the Discord bot runs on the main asyncio event loop. This avoids
needing Railway's Procfile worker/web split, which requires two separate services to
actually both start - a single Railway service only runs one process type.
"""
import asyncio
import logging
import os
import threading

import discord
from discord.ext import commands

from config import settings
from utils.permissions import get_prefix_for_guild, register_all_commands

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True  # for voice join/leave logs

bot = commands.Bot(command_prefix=get_prefix_for_guild, intents=intents, help_command=None)

COGS = [
    "cogs.events",
    "cogs.tickets",
    "cogs.ticket_automations",
    "cogs.temp_cleanup",
    "cogs.help",
    "cogs.giveaways",
    "cogs.logs",
    "cogs.vouches",
    "cogs.applications",
    "cogs.moderation",
    "cogs.automod",
    "cogs.invite_tracker",
    "cogs.anti_nuke",
    "cogs.utilities",
    "cogs.changes",
    "cogs.staff_strikes",
    "cogs.welcome",
    "cogs.join_ads",
    "cogs.taskbook",
    "cogs.ticket_ratings",
    "cogs.bot_profile",
    "cogs.staff_activity",
    "cogs.loa",
    "cogs.leveling",
    "cogs.admin_tools",
    "cogs.spawner_panel",
    "cogs.member_tools",
    "cogs.staff_tools",
    "cogs.role_panels",
    "cogs.announcements",
    "cogs.staff_automation",
]


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"Slash sync failed: {e}")

    try:
        await register_all_commands(bot)
    except Exception as e:
        print(f"Command registration failed: {e}")


@bot.event
async def on_guild_join(guild):
    try:
        await register_all_commands(bot, [guild.id])
    except Exception as e:
        print(f"Command registration failed for {guild.id}: {e}")


def run_dashboard():
    """Runs the Flask dashboard in this background thread. Railway injects PORT -
    the dashboard must bind to that, not DASHBOARD_PORT, when running on Railway."""
    from dashboard.app import app
    port = int(os.getenv("PORT", settings.DASHBOARD_PORT))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


async def main():
    threading.Thread(target=run_dashboard, daemon=True).start()

    async with bot:
        for cog in COGS:
            try:
                await bot.load_extension(cog)
                print(f"Loaded {cog}")
            except Exception as e:
                print(f"Failed to load {cog}: {e}")
        await bot.start(settings.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())