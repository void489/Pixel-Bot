"""Join Ad - DMs new members an ad.

Configured entirely from the dashboard (Join Ad page) - there are no commands.

Flow for every human who joins:
  1. quick checks (feature on, account old enough),
  2. wait the configured delay,
  3. join that server's send queue (one DM every few seconds, so a raid can't
     make the bot mass-DM),
  4. at send time: still in the server? inside the schedule window? skip role?
     messaged too recently? daily cap? -> then pick ONE active ad at random
     (weighted) and DM it.
Campaign limits (N days / N members) switch an ad off by itself and can post a note
to a staff channel.

Counters live in the join_ad_stats collection:
  _id "<guild>:<ad id>"  -> sent, failed, recent_failures[]
  _id "<guild>:_global"  -> skipped, reasons{}
"""
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks
from pymongo import ReturnDocument

from database import models
from database.mongo import get_db, guild_settings_col
from utils import join_ads as ja

GLOBAL = "_global"
MAX_RECENT_FAILURES = 10
BACKOFF_SECONDS = 90  # after Discord says we're opening DMs too fast


def _stats_col():
    return get_db()["join_ad_stats"]


def _sent_col():
    return get_db()["join_ad_sent"]


def _daily_col():
    return get_db()["join_ad_daily"]


class JoinAds(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._queues: dict[int, asyncio.Queue] = {}
        self._workers: dict[int, asyncio.Task] = {}
        self._pending: set[asyncio.Task] = set()

    async def cog_load(self):
        # Old bookkeeping cleans itself up.
        try:
            await _sent_col().create_index("at", expireAfterSeconds=400 * 86400)
            await _daily_col().create_index("at", expireAfterSeconds=3 * 86400)
        except Exception as e:
            print(f"[join_ads] couldn't create indexes: {e}")
        self._expiry_loop.start()

    async def cog_unload(self):
        self._expiry_loop.cancel()
        for t in list(self._pending) + list(self._workers.values()):
            t.cancel()

    # ------------------------------------------------------------------
    # config / stats helpers
    # ------------------------------------------------------------------

    async def _config(self, guild_id: int) -> dict:
        settings = await models.get_guild_settings(guild_id)
        return ja.load_config(settings.get("join_ads"))

    async def _sent_by_ad(self, guild_id: int) -> dict:
        out = {}
        async for d in _stats_col().find({"guild_id": str(guild_id)}):
            out[d.get("ad_id")] = int(d.get("sent") or 0)
        return out

    async def _skip(self, guild_id: int, reason: str):
        await _stats_col().update_one(
            {"_id": f"{guild_id}:{GLOBAL}"},
            {"$inc": {"skipped": 1, f"reasons.{reason}": 1},
             "$set": {"guild_id": str(guild_id), "ad_id": GLOBAL}},
            upsert=True,
        )

    async def _record_sent(self, guild_id: int, ad_id: str) -> int:
        doc = await _stats_col().find_one_and_update(
            {"_id": f"{guild_id}:{ad_id}"},
            {"$inc": {"sent": 1}, "$set": {"guild_id": str(guild_id), "ad_id": ad_id}},
            upsert=True, return_document=ReturnDocument.AFTER,
        )
        return int((doc or {}).get("sent") or 0)

    async def _record_failed(self, guild_id: int, ad: dict, member: discord.Member, reason: str):
        entry = {
            "user": f"{member} ({member.id})",
            "reason": reason,
            "ad": ad.get("name", ""),
            "at": datetime.now(timezone.utc),
        }
        await _stats_col().update_one(
            {"_id": f"{guild_id}:{ad['id']}"},
            {"$inc": {"failed": 1},
             "$set": {"guild_id": str(guild_id), "ad_id": ad["id"]},
             "$push": {"recent_failures": {"$each": [entry], "$slice": -MAX_RECENT_FAILURES}}},
            upsert=True,
        )

    async def _finish(self, guild: discord.Guild | None, guild_id: int, ad: dict,
                      reason: str, notify_channel_id, sent: int):
        """Switch an ad off (only once) and optionally tell staff."""
        res = await guild_settings_col().update_one(
            {"_id": str(guild_id)},
            {"$set": {"join_ads.ads.$[a].enabled": False,
                      "join_ads.ads.$[a].finished_reason": reason}},
            array_filters=[{"a.id": ad["id"], "a.enabled": True}],
        )
        if not res.modified_count or guild is None or not notify_channel_id:
            return
        channel = guild.get_channel(int(notify_channel_id))
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(
                    f"📣 Join ad **{ad.get('name', 'ad')}** has finished: {reason}. "
                    f"It sent {sent} DM{'s' if sent != 1 else ''}. "
                    "Open the Join Ad page on the dashboard to restart it.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

    # ------------------------------------------------------------------
    # joins
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        try:
            cfg = await self._config(member.guild.id)
            if not cfg["enabled"] or not cfg["ads"]:
                return
            min_age = cfg["min_account_age_days"]
            if min_age and (ja.utcnow() - member.created_at).days < min_age:
                await self._skip(member.guild.id, "young_account")
                return
            self._schedule(member, cfg["delay_seconds"])
        except Exception as e:
            print(f"[join_ads] join handling failed in {member.guild.id}: {e}")

    def _schedule(self, member: discord.Member, delay: int):
        async def _go():
            if delay:
                await asyncio.sleep(delay)
            await self._queue_for(member.guild.id).put(member)

        task = asyncio.create_task(_go())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def _queue_for(self, guild_id: int) -> asyncio.Queue:
        q = self._queues.get(guild_id)
        if q is None:
            q = self._queues[guild_id] = asyncio.Queue()
            self._workers[guild_id] = asyncio.create_task(self._worker(guild_id, q))
        return q

    async def _worker(self, guild_id: int, q: asyncio.Queue):
        while True:
            try:
                member = await asyncio.wait_for(q.get(), timeout=300)
            except asyncio.TimeoutError:
                if q.empty():  # nothing slipped in: shut this worker down
                    self._queues.pop(guild_id, None)
                    self._workers.pop(guild_id, None)
                    return
                continue
            try:
                wait = await self._process(member)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[join_ads] processing failed in {guild_id}: {e}")
                wait = 0
            if wait:
                await asyncio.sleep(wait)

    # ------------------------------------------------------------------
    # the actual send (returns seconds to wait before the next DM, 0 if none was tried)
    # ------------------------------------------------------------------

    async def _process(self, member: discord.Member) -> int:
        guild = member.guild
        gid = guild.id
        cfg = await self._config(gid)  # fresh: dashboard changes apply immediately
        if not cfg["enabled"]:
            return 0

        current = guild.get_member(member.id)
        if current is None:
            await self._skip(gid, "left")
            return 0
        member = current

        now = ja.utcnow()
        if not ja.in_window(cfg, now):
            await self._skip(gid, "window")
            return 0

        if cfg["skip_role_ids"] and any(str(r.id) in cfg["skip_role_ids"] for r in member.roles):
            await self._skip(gid, "role")
            return 0

        key = f"{gid}:{member.id}"
        if cfg["cooldown_days"]:
            rec = await _sent_col().find_one({"_id": key})
            last = rec.get("at") if rec else None
            if last is not None:
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if now - last < timedelta(days=cfg["cooldown_days"]):
                    await self._skip(gid, "cooldown")
                    return 0

        sent_by_ad = await self._sent_by_ad(gid)

        # Finish anything that ran out (time limit) before picking.
        for a in cfg["ads"]:
            state, reason = ja.campaign_state(a, sent_by_ad.get(a["id"], 0), now)
            if state == "finished" and a.get("enabled"):
                await self._finish(guild, gid, a, reason, cfg["notify_channel_id"],
                                   sent_by_ad.get(a["id"], 0))

        ad = ja.pick_ad(cfg["ads"], sent_by_ad, now)
        if ad is None:
            await self._skip(gid, "no_ad")
            return 0

        if cfg["daily_cap"]:
            day = await _daily_col().find_one_and_update(
                {"_id": f"{gid}:{now:%Y%m%d}"},
                {"$inc": {"n": 1}, "$set": {"at": now}},
                upsert=True, return_document=ReturnDocument.AFTER,
            )
            if int((day or {}).get("n") or 0) > cfg["daily_cap"]:
                await self._skip(gid, "cap")
                return 0

        msg = ja.build_message(
            ad, user_mention=member.mention, username=member.name,
            server=guild.name, member_count=guild.member_count,
        )
        view = None
        if msg["buttons"]:
            view = discord.ui.View()
            for label, url in msg["buttons"]:
                view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label=label, url=url))

        kwargs = {}
        if msg["content"]:
            kwargs["content"] = msg["content"]
        if msg["embed"]:
            kwargs["embed"] = discord.Embed.from_dict(msg["embed"])
        if view is not None:
            kwargs["view"] = view
        if msg["suppress_embeds"]:
            kwargs["suppress_embeds"] = True

        wait = cfg["pace_seconds"]
        try:
            await member.send(**kwargs)
        except discord.Forbidden:
            await self._record_failed(gid, ad, member, "DMs closed")
            return wait
        except discord.HTTPException as e:
            if e.code == 40003:  # opening DMs too fast
                wait = max(wait, BACKOFF_SECONDS)
            await self._record_failed(gid, ad, member, f"Discord error {e.code or e.status}")
            return wait

        await _sent_col().update_one({"_id": key}, {"$set": {"at": now}}, upsert=True)
        sent = await self._record_sent(gid, ad["id"])
        state, reason = ja.campaign_state(ad, sent, now)
        if state == "finished":
            await self._finish(guild, gid, ad, reason, cfg["notify_channel_id"], sent)
        return wait

    # ------------------------------------------------------------------
    # housekeeping: finish campaigns that ran out of time even when nobody joins
    # ------------------------------------------------------------------

    @tasks.loop(minutes=10)
    async def _expiry_loop(self):
        try:
            async for doc in guild_settings_col().find({"join_ads.ads.enabled": True}):
                gid = int(doc["_id"])
                cfg = ja.load_config(doc.get("join_ads"))
                sent_by_ad = await self._sent_by_ad(gid)
                for a in cfg["ads"]:
                    sent = sent_by_ad.get(a["id"], 0)
                    state, reason = ja.campaign_state(a, sent)
                    if state == "finished" and a.get("enabled"):
                        await self._finish(self.bot.get_guild(gid), gid, a, reason,
                                           cfg["notify_channel_id"], sent)
        except Exception as e:
            print(f"[join_ads] expiry check failed: {e}")

    @_expiry_loop.before_loop
    async def _before_expiry(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(JoinAds(bot))
