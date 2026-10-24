"""Scheduled announcements - configured entirely on the dashboard (Announcements page).

No commands. A background loop checks every 30 seconds for schedules whose `next_run` has
passed and posts them. Schedules live in Mongo, so they survive restarts. If the bot was down
at the scheduled time the announcement is posted late, once (repeating ones skip the missed
occurrences and carry on from the next one) and the dashboard marks it "posted late".

Claiming: before posting, the loop moves `next_run` forward (or marks a one-off "sent") with a
compare-and-set update, so two loop runs - or two bot processes - can never post the same
occurrence twice.
"""
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from database.mongo import scheduled_announcements_col
from utils import announce_core as core

KEEP_DAYS = 90        # finished / cancelled schedules older than this are deleted


def build_message(doc: dict):
    """(content, embed, allowed_mentions) for one schedule doc."""
    role_id = doc.get("ping_role_id")
    ping = f"<@&{role_id}>" if role_id else ""
    text = (doc.get("message") or "").strip()
    image = doc.get("image_url") or ""
    embed = None
    if doc.get("use_embed"):
        embed = discord.Embed(
            title=(doc.get("title") or None),
            description=text[:core.MAX_EMBED_TEXT] or None,
            color=int(doc.get("color") or 0x5865F2),
        )
        if image:
            embed.set_image(url=image)
        content = ping or None
    else:
        content = "\n".join(x for x in (ping, text, image) if x)[:core.MAX_TEXT] or None
    mentions = discord.AllowedMentions(
        everyone=False, users=False,
        roles=[discord.Object(id=int(role_id))] if role_id else False)
    return content, embed, mentions


class Announcements(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._ticks = 0

    async def cog_load(self):
        self._loop.start()

    async def cog_unload(self):
        self._loop.cancel()

    @tasks.loop(seconds=30)
    async def _loop(self):
        try:
            col = scheduled_announcements_col()
            now = datetime.now(timezone.utc)
            due = await col.find({"status": "scheduled", "next_run": {"$lte": now}}) \
                .sort("next_run", 1).to_list(25)
            for doc in due:
                try:
                    await self._fire(doc, now)
                except Exception as e:
                    print(f"[announcements] {doc.get('_id')} failed: {e}")
            self._ticks += 1
            if self._ticks % 120 == 1:          # roughly hourly (and once at startup)
                await col.delete_many({
                    "status": {"$in": ["sent", "failed", "cancelled"]},
                    "updated_at": {"$lt": now - timedelta(days=KEEP_DAYS)}})
        except Exception as e:
            print(f"[announcements] loop error: {e}")

    @_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        try:
            await scheduled_announcements_col().create_index(
                [("status", 1), ("next_run", 1)])
            await scheduled_announcements_col().create_index(
                [("guild_id", 1), ("next_run", 1)])
        except Exception as e:
            print(f"[announcements] index failed: {e}")

    async def _fire(self, doc: dict, now: datetime):
        col = scheduled_announcements_col()
        guild = self.bot.get_guild(int(doc["guild_id"]))
        if guild is None:
            return                              # not ready / not in that server: try next tick

        due_at = core.aware(doc["next_run"])
        repeat = doc.get("repeat") or "once"
        nxt = None if repeat == "once" else core.next_run(
            doc.get("first_local"), doc.get("tz"), repeat, now)

        # Claim this occurrence (compare-and-set on next_run).
        claim = ({"$set": {"next_run": nxt, "updated_at": now}} if nxt is not None
                 else {"$set": {"status": "sent", "updated_at": now}})
        res = await col.update_one(
            {"_id": doc["_id"], "status": "scheduled", "next_run": doc["next_run"]}, claim)
        if not res.modified_count:
            return                              # someone else claimed it

        error = ""
        channel = guild.get_channel(int(doc["channel_id"]))
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            error = "The channel no longer exists."
        else:
            content, embed, mentions = build_message(doc)
            if content is None and embed is None:
                error = "The announcement is empty."
            else:
                try:
                    await channel.send(content=content, embed=embed, allowed_mentions=mentions)
                except discord.Forbidden:
                    error = "I don't have permission to post in that channel."
                except discord.HTTPException as e:
                    error = f"Discord error: {e}"

        update = {"last_error": error, "updated_at": now}
        if not error:
            update["last_sent_at"] = now
            update["late"] = core.is_late(due_at, now)
        elif nxt is None:
            update["status"] = "failed"         # a one-off that couldn't be posted
        await col.update_one(
            {"_id": doc["_id"]},
            {"$set": update, **({} if error else {"$inc": {"run_count": 1}})})


async def setup(bot: commands.Bot):
    await bot.add_cog(Announcements(bot))
