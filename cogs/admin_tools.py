"""Admin tools - applies the bot's activity/status chosen on the dashboard's /admin page.

How it works
  * /admin saves the wanted presence in Mongo (`bot_settings`, _id = "presence")
    and queues a `bot_presence_apply` task.
  * This cog picks the task up within a few seconds and calls change_presence().
  * It's applied again every time the bot starts, so it survives restarts/deploys.
  * Empty text = no activity (only the online/idle/dnd/invisible status is set).
  * The text can use {servers} (how many servers the bot is in) and {members} (total
    members across all of those servers). They are filled in when the presence is applied
    and refreshed every 5 minutes, so the numbers stay current.
"""
import asyncio
import collections
import logging
import math
import time
import traceback
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import constants
from database import models
from database.logging_config import get_logging_config
from database.mongo import admin_broadcasts_col, bot_errors_col, bot_settings_col

PRESENCE_TASK = "bot_presence_apply"
BROADCAST_TASK = "admin_broadcast"
HEARTBEAT_SECONDS = 30
CONTROLS_TTL = 10                 # seconds a maintenance / feature switch is cached
ERROR_KEEP_SECONDS = 7 * 24 * 3600
DEFAULT_MAINTENANCE_MESSAGE = "🛠️ The bot is being updated right now. Please try again in a little while."

# Mistakes by the person using a command, not bugs: they don't go on the Errors tab.
_USER_ERRORS = (commands.CheckFailure, commands.CommandNotFound, commands.UserInputError,
                commands.CommandOnCooldown, commands.DisabledCommand,
                app_commands.CheckFailure, app_commands.CommandNotFound)


class Refused(commands.CheckFailure):
    """A command was refused because of maintenance mode or a switched-off feature."""


class _ErrorCapture(logging.Handler):
    """Copies ERROR-level log records (discord.py logs every unhandled command / event error
    this way) into a buffer; the cog saves the buffer to Mongo every few seconds."""

    def __init__(self, buffer: collections.deque):
        super().__init__(level=logging.ERROR)
        self.buffer = buffer

    def emit(self, record: logging.LogRecord):
        try:
            exc = record.exc_info[1] if record.exc_info else None
            inner = getattr(exc, "original", exc)
            if isinstance(exc, _USER_ERRORS) or isinstance(inner, _USER_ERRORS):
                return
            tb = ""
            if record.exc_info:
                tb = "".join(traceback.format_exception(*record.exc_info))[-4000:]
            self.buffer.append({
                "created_at": datetime.now(timezone.utc),
                "logger": record.name,
                "level": record.levelname,
                "message": record.getMessage()[:500],
                "exc_type": type(inner).__name__ if inner is not None else "",
                "traceback": tb,
            })
        except Exception:  # a logging handler must never raise
            pass

_TYPES = {
    "playing": discord.ActivityType.playing,
    "watching": discord.ActivityType.watching,
    "listening": discord.ActivityType.listening,
    "competing": discord.ActivityType.competing,
    "streaming": discord.ActivityType.streaming,
}
_STATUSES = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.dnd,
    "invisible": discord.Status.invisible,
}


PLACEHOLDERS = ("{servers}", "{members}")


def fill_placeholders(text: str, bot: commands.Bot) -> str:
    """{servers} -> number of servers the bot is in, {members} -> members across them all."""
    if "{servers}" in text:
        text = text.replace("{servers}", str(len(bot.guilds)))
    if "{members}" in text:
        text = text.replace("{members}", str(sum((g.member_count or 0) for g in bot.guilds)))
    return text


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
        name = fill_placeholders(text, bot)[:128]
        url = (doc.get("url") or "").strip()
        if kind == discord.ActivityType.streaming and url:
            activity = discord.Streaming(name=name, url=url)
        elif kind == discord.ActivityType.streaming:
            activity = discord.Activity(type=discord.ActivityType.playing, name=name)  # no link saved: show as Playing
        else:
            activity = discord.Activity(type=kind, name=name)

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
        self._started_at = datetime.now(timezone.utc)
        self._errors = collections.deque(maxlen=300)
        self._capture = _ErrorCapture(self._errors)
        self._controls_at = -1e9
        self._controls_data = {"maintenance": False, "maintenance_message": DEFAULT_MAINTENANCE_MESSAGE,
                               "disabled_cogs": set()}

    async def cog_load(self):
        logging.getLogger().addHandler(self._capture)
        self.bot.add_check(self._prefix_check)                  # maintenance / switched-off features
        self.bot.tree.interaction_check = self._tree_check      # ... and the same for slash commands
        try:
            await bot_errors_col().create_index("created_at", expireAfterSeconds=ERROR_KEEP_SECONDS)
        except Exception as e:
            print(f"[admin_tools] error index: {e}")
        self._poll.start()
        self._poll_broadcast.start()
        self._startup.start()
        self._refresh_counts.start()
        self._heartbeat.start()
        self._flush_errors.start()

    async def cog_unload(self):
        logging.getLogger().removeHandler(self._capture)
        self.bot.remove_check(self._prefix_check)
        try:
            del self.bot.tree.interaction_check                 # back to discord.py's own
        except AttributeError:
            pass
        self._poll.cancel()
        self._poll_broadcast.cancel()
        self._startup.cancel()
        self._refresh_counts.cancel()
        self._heartbeat.cancel()
        self._flush_errors.cancel()

    # ------------------------------------------------------------------
    # Maintenance mode + feature switches
    # ------------------------------------------------------------------

    async def _controls(self) -> dict:
        """The maintenance / feature switches, cached for a few seconds so a command doesn't
        cost a database read. If Mongo can't be reached nothing is blocked."""
        now = time.monotonic()
        if now - self._controls_at < CONTROLS_TTL:
            return self._controls_data
        try:
            docs = {}
            async for d in bot_settings_col().find({"_id": {"$in": ["maintenance", "features"]}}):
                docs[d["_id"]] = d
            maint = docs.get("maintenance") or {}
            self._controls_data = {
                "maintenance": bool(maint.get("enabled")),
                "maintenance_message": (maint.get("message") or "").strip() or DEFAULT_MAINTENANCE_MESSAGE,
                "disabled_cogs": set((docs.get("features") or {}).get("disabled_cogs") or []),
            }
        except Exception as e:
            print(f"[admin_tools] couldn't read the maintenance switches: {e}")
        self._controls_at = now
        return self._controls_data

    async def _refusal(self, user, cog_name: str):
        """The message to answer with, or None when the command may run."""
        c = await self._controls()
        off = bool(cog_name) and cog_name in c["disabled_cogs"]
        if not c["maintenance"] and not off:
            return None
        try:
            if await self.bot.is_owner(user):
                return None
        except Exception:
            pass
        return c["maintenance_message"] if c["maintenance"] else \
            "This feature is turned off right now. Please try again later."

    async def _prefix_check(self, ctx: commands.Context) -> bool:
        """Global check for prefix commands (slash commands go through _tree_check)."""
        if ctx.interaction is not None:
            return True
        reason = await self._refusal(ctx.author, ctx.cog.qualified_name if ctx.cog else None)
        if reason is None:
            return True
        try:
            await ctx.send(reason)
        except discord.HTTPException:
            pass
        raise Refused(reason)

    async def _tree_check(self, interaction: discord.Interaction) -> bool:
        """The same for slash commands (and the slash side of hybrid commands)."""
        if interaction.type is not discord.InteractionType.application_command:
            return True
        binding = getattr(interaction.command, "binding", None)
        cog_name = binding.qualified_name if isinstance(binding, commands.Cog) else None
        reason = await self._refusal(interaction.user, cog_name)
        if reason is None:
            return True
        try:
            await interaction.response.send_message(reason, ephemeral=True)
        except discord.HTTPException:
            pass
        return False

    # ------------------------------------------------------------------
    # Blocked servers
    # ------------------------------------------------------------------

    async def _blocked_ids(self) -> set:
        doc = await bot_settings_col().find_one({"_id": "blocked_guilds"}) or {}
        return {str(x) for x in (doc.get("ids") or [])}

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        try:
            if str(guild.id) in await self._blocked_ids():
                print(f"[admin_tools] {guild.name} ({guild.id}) is blocked: leaving")
                await guild.leave()
        except Exception as e:
            print(f"[admin_tools] blocked-server check failed for {guild.id}: {e}")

    async def _leave_blocked(self):
        for guild in list(self.bot.guilds):
            if str(guild.id) in await self._blocked_ids():
                print(f"[admin_tools] {guild.name} ({guild.id}) is blocked: leaving")
                try:
                    await guild.leave()
                except discord.HTTPException as e:
                    print(f"[admin_tools] couldn't leave {guild.id}: {e}")

    # ------------------------------------------------------------------
    # Heartbeat + error capture
    # ------------------------------------------------------------------

    @tasks.loop(seconds=HEARTBEAT_SECONDS)
    async def _heartbeat(self):
        try:
            guilds = list(self.bot.guilds)
            latency = self.bot.latency
            now = datetime.now(timezone.utc)
            await bot_settings_col().update_one({"_id": "heartbeat"}, {"$set": {
                "updated_at": now,
                "started_at": self._started_at,
                "latency_ms": int(latency * 1000) if math.isfinite(latency) else None,
                "guilds": len(guilds),
                "members": sum((g.member_count or 0) for g in guilds),
            }}, upsert=True)
            info = [{
                "id": str(g.id), "name": g.name, "members": g.member_count or 0,
                "joined_at": g.me.joined_at.isoformat() if g.me and g.me.joined_at else None,
            } for g in guilds]
            await bot_settings_col().update_one({"_id": "guild_info"},
                                                {"$set": {"guilds": info, "updated_at": now}}, upsert=True)
        except Exception as e:
            print(f"[admin_tools] heartbeat error: {e}")

    @_heartbeat.before_loop
    async def _before_heartbeat(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=15)
    async def _flush_errors(self):
        docs = []
        while self._errors:
            try:
                docs.append(self._errors.popleft())
            except IndexError:
                break
        if not docs:
            return
        try:
            await bot_errors_col().insert_many(docs, ordered=False)
        except Exception as e:
            print(f"[admin_tools] couldn't save {len(docs)} error(s): {e}")

    # ------------------------------------------------------------------
    # Broadcast (from the /admin Broadcast tab)
    # ------------------------------------------------------------------

    async def _broadcast_channel(self, guild: discord.Guild):
        """(channel, None) or (None, why not). The server's log channel, else its system channel."""
        candidates = []
        try:
            cid = (await get_logging_config(guild.id)).get("log_channel_id")
            if cid:
                candidates.append(guild.get_channel(int(cid)))
        except Exception:
            pass
        candidates.append(guild.system_channel)
        for ch in candidates:
            if isinstance(ch, discord.TextChannel):
                perms = ch.permissions_for(guild.me)
                if perms.view_channel and perms.send_messages and perms.embed_links:
                    return ch, None
        return None, "No log channel set and no usable system channel"

    async def _run_broadcast(self, task: dict):
        from bson import ObjectId
        col = admin_broadcasts_col()
        try:
            doc = await col.find_one({"_id": ObjectId((task.get("payload") or {}).get("broadcast_id"))})
        except Exception:
            return
        if not doc:
            return
        await col.update_one({"_id": doc["_id"]},
                             {"$set": {"status": "sending", "started_at": datetime.now(timezone.utc)}})
        if doc.get("all"):
            targets = [(str(g.id), g) for g in self.bot.guilds]
        else:
            targets = [(str(gid), self.bot.get_guild(int(gid))) for gid in (doc.get("guild_ids") or [])]

        embed = discord.Embed(title=(doc.get("title") or "Message from the bot owner")[:256],
                              description=doc.get("text") or "", color=constants.COLOR_DEFAULT,
                              timestamp=datetime.now(timezone.utc))
        results = []
        for gid, guild in targets:
            if guild is None:
                results.append({"guild_id": gid, "name": gid, "ok": False, "reason": "The bot isn't in that server"})
                continue
            channel, why = await self._broadcast_channel(guild)
            if channel is None:
                results.append({"guild_id": gid, "name": guild.name, "ok": False, "reason": why})
                continue
            try:
                await channel.send(embed=embed)
                results.append({"guild_id": gid, "name": guild.name, "ok": True, "channel": channel.name})
            except discord.Forbidden:
                results.append({"guild_id": gid, "name": guild.name, "ok": False, "reason": "Missing permission"})
            except discord.HTTPException as e:
                results.append({"guild_id": gid, "name": guild.name, "ok": False, "reason": f"Discord error: {e}"[:200]})
            await asyncio.sleep(1.0)        # stay well inside Discord's rate limits

        await col.update_one({"_id": doc["_id"]}, {"$set": {
            "status": "done", "results": results,
            "sent": sum(1 for r in results if r["ok"]),
            "failed": sum(1 for r in results if not r["ok"]),
            "finished_at": datetime.now(timezone.utc),
        }})

    @tasks.loop(seconds=5)
    async def _poll_broadcast(self):
        try:
            for task in await models.take_pending_dashboard_tasks(limit=1, types=[BROADCAST_TASK]):
                await self._run_broadcast(task)
        except Exception as e:
            print(f"[admin_tools] broadcast error: {e}")

    @_poll_broadcast.before_loop
    async def _before_poll_broadcast(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def _refresh_counts(self):
        """Keep {servers} / {members} current. Only touches Discord if the text uses one."""
        try:
            doc = await bot_settings_col().find_one({"_id": "presence"}) or {}
            text = doc.get("text") or ""
            if any(ph in text for ph in PLACEHOLDERS):
                await apply_presence(self.bot)
        except Exception as e:
            print(f"[admin_tools] count refresh error: {e}")

    @_refresh_counts.before_loop
    async def _before_refresh_counts(self):
        await self.bot.wait_until_ready()

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
        try:
            await self._leave_blocked()
        except Exception as e:
            print(f"[admin_tools] blocked-server sweep error: {e}")

    @_startup.before_loop
    async def _before_startup(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminTools(bot))
