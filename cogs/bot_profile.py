"""
Bot Profile - set the bot's per-server nickname, bio, avatar and banner from the
dashboard (sidebar -> Bot Profile). Only changes how the bot looks in THAT server;
its global profile is never touched.

How it works
  * The dashboard saves the wanted profile (text + image bytes) in Mongo
    (`bot_profiles`, one doc per server, _id = guild id) and queues a
    `bot_profile_apply` task.
  * This cog picks the task up and sends it to Discord with
    PATCH /guilds/{id}/members/@me  (called directly, so it doesn't depend on which
    discord.py version is installed). Only the fields that changed are sent, so the
    strict avatar/banner rate limits aren't wasted.
  * What was last applied is remembered in the doc (`applied`), together with the
    outcome (`last_result`) which the dashboard shows.

Surviving restarts
  * Discord keeps the profile itself, and the settings + images live in Mongo
    (Railway's disk is wiped on every deploy, so nothing is stored in files).
  * On startup the bot compares each server's saved profile with `applied` and only
    re-applies if they differ (e.g. an apply failed, or the bot was offline when you
    saved). If nothing changed it does nothing.
  * A save made while the bot is offline stays queued and runs when it comes back.

Needs the "Change Nickname" permission for the nickname to apply.
"""
import asyncio
import base64
import hashlib

import discord
from discord.ext import commands, tasks
from discord.http import Route

from database import models
from database.mongo import bot_profiles_col, dashboard_tasks_col
from datetime import datetime, timezone

APPLY_TASK = "bot_profile_apply"
_UNSET = object()
_locks: dict = {}


def _norm(value):
    if isinstance(value, str):
        value = value.strip()
    return value or None


def _data_uri(img: dict) -> str:
    b64 = base64.b64encode(bytes(img["data"])).decode("ascii")
    return f"data:{img.get('mime') or 'image/png'};base64,{b64}"


def compute_diff(doc: dict):
    """(payload to send to Discord, what to remember as applied) for a profile doc."""
    applied = doc.get("applied") or {}
    payload, marks = {}, {}

    for key in ("nick", "bio"):
        want = _norm(doc.get(key))
        if applied.get(key, _UNSET) != want:
            payload[key] = want
            marks[key] = want

    for kind in ("avatar", "banner"):
        img = doc.get(kind)
        want_hash = img.get("hash") if img else None
        if applied.get(f"{kind}_hash", _UNSET) != want_hash:
            payload[kind] = _data_uri(img) if img else None
            marks[f"{kind}_hash"] = want_hash
    return payload, marks


async def _save_result(guild_id: str, ok: bool, message: str, marks: dict = None):
    update = {"last_result": {"ok": ok, "message": message, "at": datetime.now(timezone.utc)}}
    for k, v in (marks or {}).items():
        update[f"applied.{k}"] = v
    await bot_profiles_col().update_one({"_id": guild_id}, {"$set": update})


async def apply_profile(bot: commands.Bot, guild_id) -> tuple:
    """Apply the saved profile of one server. Returns (ok, message)."""
    gid = str(guild_id)
    lock = _locks.setdefault(gid, asyncio.Lock())
    async with lock:
        doc = await bot_profiles_col().find_one({"_id": gid})
        if not doc:
            return True, "Nothing saved for this server."
        guild = bot.get_guild(int(gid))
        if guild is None:
            msg = "The bot isn't in that server."
            await _save_result(gid, False, msg)
            return False, msg

        payload, marks = compute_diff(doc)
        if not payload:
            return True, "Already up to date."

        try:
            await bot.http.request(
                Route("PATCH", "/guilds/{guild_id}/members/@me", guild_id=guild.id),
                json=payload,
                reason="Bot profile updated from the dashboard",
            )
        except discord.Forbidden as e:
            msg = (f"Discord refused it (missing permission). The bot needs Change Nickname "
                   f"for the nickname. Details: {getattr(e, 'text', None) or e}")
            await _save_result(gid, False, msg)
            return False, msg
        except discord.HTTPException as e:
            msg = f"Discord said: {getattr(e, 'text', None) or e}"
            await _save_result(gid, False, msg)
            return False, msg
        except Exception as e:
            msg = f"Unexpected error: {e}"
            print(f"[bot_profile] apply failed for {gid}: {e}")
            await _save_result(gid, False, msg)
            return False, msg

        sent = ", ".join(payload.keys())
        await _save_result(gid, True, f"Applied ({sent}).", marks)
        return True, "Applied."


class BotProfile(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self._poll.start()
        self._startup_sync.start()

    async def cog_unload(self):
        self._poll.cancel()
        self._startup_sync.cancel()

    @tasks.loop(seconds=3)
    async def _poll(self):
        try:
            batch = await models.take_pending_dashboard_tasks(limit=10, types=[APPLY_TASK])
            for t in batch:
                try:
                    await apply_profile(self.bot, t["guild_id"])
                except Exception as e:
                    print(f"[bot_profile] task {t.get('_id')} failed: {e}")
        except Exception as e:
            print(f"[bot_profile] poller error: {e}")

    @_poll.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    @tasks.loop(count=1)
    async def _startup_sync(self):
        """After a (re)start: re-apply only the servers whose saved profile differs
        from what was last applied."""
        try:
            in_guilds = {str(g.id) for g in self.bot.guilds}
            async for doc in bot_profiles_col().find({}):
                if doc["_id"] not in in_guilds:
                    continue
                payload, _ = compute_diff(doc)
                if not payload:
                    continue
                await apply_profile(self.bot, doc["_id"])
                await asyncio.sleep(3)  # stay well inside Discord's rate limits
        except Exception as e:
            print(f"[bot_profile] startup sync error: {e}")

    @_startup_sync.before_loop
    async def _before_sync(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(BotProfile(bot))
