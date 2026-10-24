"""Admin tools - applies the bot's activity/status chosen on the dashboard's /admin page.

How it works
  * /admin saves the wanted presence in Mongo (`bot_settings`, _id = "presence")
    and queues a `bot_presence_apply` task.
  * This cog picks the task up within a few seconds and calls change_presence().
  * It's applied again every time the bot starts, so it survives restarts/deploys.
  * Empty text = no activity (only the online/idle/dnd/invisible status is set).
"""
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from database import models
from database.mongo import bot_settings_col

PRESENCE_TASK = "bot_presence_apply"

_TYPES = {
    "playing": discord.ActivityType.playing,
    "watching": discord.ActivityType.watching,
    "listening": discord.ActivityType.listening,
    "competing": discord.ActivityType.competing,
}
_STATUSES = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.dnd,
    "invisible": discord.Status.invisible,
}


async def apply_presence(bot: commands.Bot) -> tuple:
    """Apply the saved presence. Returns (ok, message)."""
    doc = await bot_settings_col().find_one({"_id": "presence"})
    if not doc:
        return True, "Nothing saved yet."

    text = (doc.get("text") or "").strip()
    status = _STATUSES.get(doc.get("status"), discord.Status.online)
    activity = None
    if text:
        kind = _TYPES.get(doc.get("type"), discord.ActivityType.playing)
        activity = discord.Activity(type=kind, name=text[:128])

    try:
        await bot.change_presence(status=status, activity=activity)
        msg, ok = "Applied.", True
    except Exception as e:
        print(f"[admin_tools] change_presence failed: {e}")
        msg, ok = f"Failed: {e}", False

    await bot_settings_col().update_one(
        {"_id": "presence"},
        {"$set": {"last_result": {"ok": ok, "message": msg, "at": datetime.now(timezone.utc)}}},
    )
    return ok, msg


class AdminTools(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self._poll.start()
        self._startup.start()

    async def cog_unload(self):
        self._poll.cancel()
        self._startup.cancel()

    @tasks.loop(seconds=3)
    async def _poll(self):
        try:
            batch = await models.take_pending_dashboard_tasks(limit=10, types=[PRESENCE_TASK])
            if batch:
                await apply_presence(self.bot)  # always applies the latest saved value
        except Exception as e:
            print(f"[admin_tools] poller error: {e}")

    @_poll.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    @tasks.loop(count=1)
    async def _startup(self):
        try:
            await apply_presence(self.bot)
        except Exception as e:
            print(f"[admin_tools] startup apply error: {e}")

    @_startup.before_loop
    async def _before_startup(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminTools(bot))
