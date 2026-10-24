"""Staff automation - configured entirely on the dashboard (Staff Automation page). No commands.

1. PROMOTION SUGGESTIONS  Per ladder rung you set requirements on taskbook stats (handled,
   closed, minimum average rating). Every 30 minutes the bot looks for members who hold the
   rung below and meet them, and posts ONE card in the management channel with Promote /
   Dismiss buttons. It never promotes anyone by itself and skips people on LOA. Promote runs
   the normal promote routine. Dismiss stays dismissed until the NEXT rung's requirements.
2. PROBATION  Per type, N days from the taskbook's creation date (or a date you set on the
   dashboard). When it ends the bot posts a review card (stats, strikes) with Pass / Extend.
3. WEEKLY REPORT  Posted on the day and hour you set, or on demand from the dashboard.
   Taskbook counters have no history, so the bot saves a snapshot each report and shows the
   difference since the previous one.

Buttons are persistent (they keep working after a restart); all state is in Mongo.
"""
from datetime import datetime, timedelta, timezone

import discord
from bson import ObjectId
from discord.ext import commands, tasks
from pymongo.errors import DuplicateKeyError

from cogs.changes import _on_loa, promote_member
from database import models
from database.mongo import (
    guild_settings_col, taskbooks_col, staff_strikes_col, ticket_ratings_col,
    staff_activity_checks_col, loa_requests_col, promotion_history_col,
    dashboard_tasks_col, staff_suggestions_col, staff_probation_col,
    staff_report_state_col, staff_report_snapshots_col, staff_strike_audit_col,
)
from utils import changes_core as cc
from utils import staff_automation_core as core
from utils.embeds import base_embed, error_embed

POST_NOW_TASK = "staff_report_now"
SNAPSHOT_KEEP = 12


async def get_settings(guild_id):
    doc = await guild_settings_col().find_one({"_id": str(guild_id)}) or {}
    cfg = core.normalize(doc.get("staff_automation"))
    types = cc.normalize_types((doc.get("changes") or {}).get("types"))
    return cfg, types


def _mgmt_channel(guild: discord.Guild, cfg: dict):
    if not cfg["mgmt_channel_id"]:
        return None
    ch = guild.get_channel(int(cfg["mgmt_channel_id"]))
    return ch if isinstance(ch, discord.TextChannel) else None


def _ping(cfg: dict):
    if cfg["ping_role_id"]:
        return (f"<@&{cfg['ping_role_id']}>",
                discord.AllowedMentions(roles=[discord.Object(id=int(cfg["ping_role_id"]))],
                                        users=False, everyone=False))
    return None, discord.AllowedMentions.none()


def _rank_label(guild, ladder, idx):
    entry = cc.entry_at(ladder, idx)
    if not entry:
        return "?"
    role = guild.get_role(int(entry["role_id"]))
    return cc.label(entry, role.name if role else None)


# ---------------------------------------------------------------------------
# Persistent buttons
# ---------------------------------------------------------------------------
class ReviewButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"staffauto:(?P<kind>sug|prob):(?P<id>[0-9a-f]{24}):(?P<act>promote|dismiss|pass|extend)",
):
    LOOK = {
        "promote": ("Promote", "⬆️", discord.ButtonStyle.success),
        "dismiss": ("Dismiss", "✖️", discord.ButtonStyle.secondary),
        "pass": ("Pass probation", "✅", discord.ButtonStyle.success),
        "extend": (f"Extend {core.DEFAULT_EXTEND_DAYS} days", "⏳", discord.ButtonStyle.primary),
    }

    def __init__(self, kind: str, doc_id: str, act: str):
        label, emoji, style = self.LOOK[act]
        super().__init__(discord.ui.Button(
            label=label, emoji=emoji, style=style,
            custom_id=f"staffauto:{kind}:{doc_id}:{act}"))
        self.kind, self.doc_id, self.act = kind, doc_id, act

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["kind"], match["id"], match["act"])

    async def callback(self, interaction: discord.Interaction):
        perms = interaction.user.guild_permissions
        if not (perms.administrator or perms.manage_roles):
            return await interaction.response.send_message(
                embed=error_embed("You need the Manage Roles permission to use these buttons."),
                ephemeral=True)
        cog = interaction.client.get_cog("StaffAutomation")
        if cog is None:
            return await interaction.response.send_message(
                embed=error_embed("This feature isn't loaded right now."), ephemeral=True)
        if self.kind == "sug":
            await cog.handle_suggestion(interaction, ObjectId(self.doc_id), self.act)
        else:
            await cog.handle_probation(interaction, ObjectId(self.doc_id), self.act)


def _buttons(kind, doc_id, acts):
    view = discord.ui.View(timeout=None)
    for a in acts:
        view.add_item(ReviewButton(kind, str(doc_id), a))
    return view


def _closed_embed(message: discord.Message, text: str, color: int) -> discord.Embed:
    e = message.embeds[0] if message.embeds else base_embed()
    e = discord.Embed.from_dict(e.to_dict())
    e.color = color
    e.set_footer(text=text)
    return e


# ---------------------------------------------------------------------------
class StaffAutomation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_dynamic_items(ReviewButton)
        try:
            await staff_suggestions_col().create_index(
                [("guild_id", 1), ("user_id", 1), ("type_id", 1), ("target", 1)], unique=True)
            await staff_probation_col().create_index(
                [("guild_id", 1), ("user_id", 1), ("type_id", 1)], unique=True)
            await staff_report_snapshots_col().create_index([("guild_id", 1), ("at", -1)])
        except Exception as e:
            print(f"[staff_automation] index creation failed: {e}")
        self._review_sweep.start()
        self._report_loop.start()
        self._task_poll.start()

    async def cog_unload(self):
        self._review_sweep.cancel()
        self._report_loop.cancel()
        self._task_poll.cancel()

    # ------------------------------------------------------------------ sweeps
    @tasks.loop(minutes=30)
    async def _review_sweep(self):
        for guild in list(self.bot.guilds):
            try:
                cfg, types = await get_settings(guild.id)
                # Only the type ticked "Show in Staff Automation" (or all while none is).
                types = cc.automation_types(types)
                if not types:
                    continue
                if cfg["suggestions"]["enabled"]:
                    await self.suggestions_for_guild(guild, cfg, types)
                if cfg["probation"]["enabled"]:
                    await self.probation_for_guild(guild, cfg, types)
            except Exception as e:
                print(f"[staff_automation] sweep failed for {guild.id}: {e}")

    @_review_sweep.before_loop
    async def _before_sweep(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ suggestions
    async def suggestions_for_guild(self, guild, cfg, types) -> int:
        channel = _mgmt_channel(guild, cfg)
        if channel is None:
            return 0
        posted = 0
        books = await taskbooks_col().find({"guild_id": str(guild.id)}).to_list(None)
        for ctype in types:
            tcfg = cfg["suggestions"]["types"].get(ctype["id"])
            if not tcfg or not tcfg["rungs"]:
                continue
            for tb in books:
                member = guild.get_member(int(tb["user_id"]))
                if member is None or member.bot:
                    continue
                if cc.is_excepted([r.id for r in member.roles], ctype):
                    continue                      # exception role: no suggestions
                cur = cc.highest_index([r.id for r in member.roles], ctype["ladder"])
                target = cur + 1
                if cur < 0 or target >= len(ctype["ladder"]):
                    continue
                req = tcfg["rungs"].get(str(target))
                if not req:
                    continue
                stats = core.stats_from_taskbook(tb, tcfg["use_lifetime"])
                ok, _ = core.meets(req, stats)
                if not ok or await _on_loa(guild.id, member.id):
                    continue
                if await self._post_suggestion(guild, channel, cfg, ctype, member, cur, target, stats):
                    posted += 1
        return posted

    async def _post_suggestion(self, guild, channel, cfg, ctype, member, cur, target, stats) -> bool:
        doc = {"guild_id": str(guild.id), "user_id": str(member.id), "type_id": ctype["id"],
               "target": target, "status": "pending", "created_at": datetime.now(timezone.utc)}
        try:
            res = await staff_suggestions_col().insert_one(doc)   # unique: one per rung, ever
        except DuplicateKeyError:
            return False
        ladder = ctype["ladder"]
        embed = base_embed(
            "📈 Promotion suggestion",
            f"{member.mention} meets the requirements for **{_rank_label(guild, ladder, target)}** "
            f"in **{ctype['name']}**.", discord.Color.blurple().value)
        embed.add_field(name="Current rank", value=_rank_label(guild, ladder, cur))
        embed.add_field(name="Suggested rank", value=_rank_label(guild, ladder, target))
        embed.add_field(name="Stats", value=core.describe_stats(stats), inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        content, mentions = _ping(cfg)
        try:
            msg = await channel.send(content=content, embed=embed, allowed_mentions=mentions,
                                     view=_buttons("sug", res.inserted_id, ("promote", "dismiss")))
        except discord.HTTPException as e:
            print(f"[staff_automation] couldn't post suggestion: {e}")
            await staff_suggestions_col().delete_one({"_id": res.inserted_id})   # retry next sweep
            return False
        await staff_suggestions_col().update_one(
            {"_id": res.inserted_id}, {"$set": {"channel_id": str(channel.id), "message_id": str(msg.id)}})
        return True

    async def handle_suggestion(self, interaction: discord.Interaction, doc_id, act: str):
        col = staff_suggestions_col()
        doc = await col.find_one_and_update(
            {"_id": doc_id, "status": "pending"}, {"$set": {"status": "working"}})
        if not doc:
            return await interaction.response.send_message(
                embed=error_embed("This suggestion was already handled."), ephemeral=True)
        by = interaction.user
        if act == "dismiss":
            await col.update_one({"_id": doc_id}, {"$set": {"status": "dismissed", "by": str(by.id)}})
            return await interaction.response.edit_message(
                embed=_closed_embed(interaction.message, f"Dismissed by {by.display_name}", 0x95A5A6),
                view=None)

        guild = interaction.guild
        cfg, types = await get_settings(guild.id)
        ctype = next((t for t in types if t["id"] == doc["type_id"]), None)
        member = guild.get_member(int(doc["user_id"]))
        problem = ""
        if ctype is None or member is None:
            problem = "that type or member no longer exists"
        elif cc.highest_index([r.id for r in member.roles], ctype["ladder"]) != doc["target"] - 1:
            problem = "their rank has changed since this was posted"
        elif await _on_loa(guild.id, member.id):
            # Not a dead end: drop the record so the next sweep suggests them again once
            # they're back from LOA.
            await col.delete_one({"_id": doc_id})
            return await interaction.response.edit_message(
                embed=_closed_embed(interaction.message,
                                    "They're on LOA. A new suggestion will be posted when they're back.",
                                    0x95A5A6), view=None)
        if problem:
            await col.update_one({"_id": doc_id}, {"$set": {"status": "stale"}})
            return await interaction.response.edit_message(
                embed=_closed_embed(interaction.message, f"No longer applies: {problem}", 0x95A5A6),
                view=None)
        await interaction.response.defer()
        res = await promote_member(
            guild, member, ctype, types,
            audit_reason=f"Promotion suggestion approved by {by}",
            reason="Met the promotion requirements",
            footer=f"Approved by {by.display_name}",
            to_index=doc["target"], actor=by, source="suggestion")
        if res.status != "ok":
            await col.update_one({"_id": doc_id}, {"$set": {"status": "pending"}})   # try again
            msg = res.error or "They're already at the top of the ladder."
            return await interaction.followup.send(embed=error_embed(msg), ephemeral=True)
        await col.update_one({"_id": doc_id}, {"$set": {"status": "promoted", "by": str(by.id)}})
        await interaction.message.edit(
            embed=_closed_embed(interaction.message,
                                f"Promoted to {res.new_label} by {by.display_name}", 0x2ECC71),
            view=None)

    # ------------------------------------------------------------------ probation
    async def probation_for_guild(self, guild, cfg, types) -> int:
        channel = _mgmt_channel(guild, cfg)
        if channel is None:
            return 0
        now = datetime.now(timezone.utc)
        posted = 0
        books = {tb["user_id"]: tb for tb in
                 await taskbooks_col().find({"guild_id": str(guild.id)}).to_list(None)}
        for ctype in types:
            days = cfg["probation"]["types"].get(ctype["id"], 0)
            if days <= 0:
                continue
            for uid, tb in books.items():
                member = guild.get_member(int(uid))
                if member is None or member.bot:
                    continue
                if cc.is_excepted([r.id for r in member.roles], ctype):
                    continue                      # exception role: no probation review
                if cc.highest_index([r.id for r in member.roles], ctype["ladder"]) < 1:
                    continue                      # not staff in this type
                pdoc = await staff_probation_col().find_one(
                    {"guild_id": str(guild.id), "user_id": uid, "type_id": ctype["id"]}) or {}
                if pdoc.get("status") in ("passed", "review", "working"):
                    continue
                start = pdoc.get("backdate") or tb.get("created_at")
                end = core.probation_end(start, days, pdoc.get("extended_until"))
                if end is None or end > now:
                    continue
                if await self._post_probation(guild, channel, cfg, ctype, member, tb, end):
                    posted += 1
        return posted

    async def _post_probation(self, guild, channel, cfg, ctype, member, tb, end) -> bool:
        key = {"guild_id": str(guild.id), "user_id": str(member.id), "type_id": ctype["id"]}
        claimed = await staff_probation_col().find_one_and_update(
            {**key, "status": {"$nin": ["passed", "review", "working"]}},
            {"$set": {"status": "review", "review_at": datetime.now(timezone.utc)}},
            upsert=False)
        if claimed is None:
            try:
                await staff_probation_col().insert_one(
                    {**key, "status": "review",
                     "review_at": datetime.now(timezone.utc)})
            except DuplicateKeyError:
                return False
        strikes = await models.count_strikes(guild.id, ctype["id"], member.id)
        stats = core.stats_from_taskbook(tb, lifetime=False)
        embed = base_embed(
            "🧪 Probation review",
            f"{member.mention}'s probation as **{ctype['name']}** ended "
            f"<t:{int(end.timestamp())}:R>.", discord.Color.gold().value)
        embed.add_field(name="Stats (taskbook)", value=core.describe_stats(stats), inline=False)
        embed.add_field(name="Active strikes", value=str(strikes))
        embed.set_thumbnail(url=member.display_avatar.url)
        content, mentions = _ping(cfg)
        try:
            doc = await staff_probation_col().find_one(key)
            msg = await channel.send(content=content, embed=embed, allowed_mentions=mentions,
                                     view=_buttons("prob", doc["_id"], ("pass", "extend")))
        except discord.HTTPException as e:
            print(f"[staff_automation] couldn't post probation review: {e}")
            await staff_probation_col().update_one(key, {"$set": {"status": "active"}})
            return False
        await staff_probation_col().update_one(
            key, {"$set": {"channel_id": str(channel.id), "message_id": str(msg.id)}})
        return True

    async def handle_probation(self, interaction: discord.Interaction, doc_id, act: str):
        col = staff_probation_col()
        doc = await col.find_one_and_update(
            {"_id": doc_id, "status": "review"}, {"$set": {"status": "working"}})
        if not doc:
            return await interaction.response.send_message(
                embed=error_embed("This review was already handled."), ephemeral=True)
        by = interaction.user
        if act == "pass":
            await col.update_one({"_id": doc_id}, {"$set": {"status": "passed", "by": str(by.id)}})
            text, color = f"Passed probation (approved by {by.display_name})", 0x2ECC71
        else:
            until = datetime.now(timezone.utc) + timedelta(days=core.DEFAULT_EXTEND_DAYS)
            await col.update_one({"_id": doc_id}, {"$set": {
                "status": "active", "by": str(by.id), "extended_until": until}})
            text, color = (f"Extended by {core.DEFAULT_EXTEND_DAYS} days "
                           f"({by.display_name}); a new review follows"), 0xF1C40F
        await interaction.response.edit_message(
            embed=_closed_embed(interaction.message, text, color), view=None)

    # ------------------------------------------------------------------ weekly report
    @tasks.loop(minutes=5)
    async def _report_loop(self):
        now = datetime.now(timezone.utc)
        for guild in list(self.bot.guilds):
            try:
                cfg, _ = await get_settings(guild.id)
                rep = cfg["report"]
                if not (rep["enabled"] and rep["channel_id"]):
                    continue
                due = core.last_due(now, rep["weekday"], rep["hour"], rep["tz"])
                state = await staff_report_state_col().find_one({"_id": str(guild.id)})
                last = core.aware(state["last_due"]) if state else None
                if last is not None and last >= due:
                    continue
                # Claim this slot first so two runs can never both post it.
                await staff_report_state_col().update_one(
                    {"_id": str(guild.id)}, {"$set": {"last_due": due}}, upsert=True)
                # First time enabled: just remember the slot; the first report is next week's.
                # Missed slot (bot was down): post it late, but not if it's over a day old.
                if last is None or (now - due) > timedelta(days=1):
                    continue
                await self.post_report(guild, cfg, late=(now - due) > timedelta(minutes=10))
            except Exception as e:
                print(f"[staff_automation] report failed for {guild.id}: {e}")

    @_report_loop.before_loop
    async def _before_report(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=5)
    async def _task_poll(self):
        try:
            batch = await models.take_pending_dashboard_tasks(limit=5, types=[POST_NOW_TASK])
            for t in batch:
                error = ""
                try:
                    guild = self.bot.get_guild(int(t["guild_id"]))
                    cfg, _ = await get_settings(t["guild_id"])
                    if guild is None:
                        error = "The bot isn't in that server."
                    elif not cfg["report"]["channel_id"]:
                        error = "Pick a report channel first and save."
                    else:
                        error = await self.post_report(guild, cfg, preview=True) or ""
                except Exception as e:
                    error = f"Unexpected error: {e}"
                await dashboard_tasks_col().update_one(
                    {"_id": t["_id"]}, {"$set": {"error": error, "result": "" if error else "Posted."}})
        except Exception as e:
            print(f"[staff_automation] task poll error: {e}")

    @_task_poll.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    async def post_report(self, guild, cfg, *, late=False, preview=False) -> str:
        """Build and post the report. A scheduled report also saves the snapshot the NEXT
        report compares against; a "post now" preview does not, so it can't skew the weeks."""
        channel = guild.get_channel(int(cfg["report"]["channel_id"] or 0))
        if not isinstance(channel, discord.TextChannel):
            return "The report channel no longer exists."
        embeds = await self.build_report(guild, preview=preview)
        if late:
            embeds[0].set_footer(text="Posted late: the bot was offline at the scheduled time.")
        try:
            await channel.send(embeds=embeds)
        except discord.Forbidden:
            return "I can't post in the report channel."
        except discord.HTTPException as e:
            return f"Discord error: {e}"
        return ""

    async def build_report(self, guild: discord.Guild, *, preview: bool) -> list:
        now = datetime.now(timezone.utc)
        gid = str(guild.id)
        books = await taskbooks_col().find({"guild_id": gid}).to_list(None)
        cur = {b["user_id"]: {f: int(b.get(f) or 0) for f in
                              core.STAT_FIELDS + ("rating_sum", "rating_count")} for b in books}
        # Members with an exception role (in any type) are left out of the report's rows. The
        # snapshot below still saves everyone, so losing the role later can't dump lifetime
        # numbers into one week.
        _, rtypes = await get_settings(guild.id)
        skip = set()
        for uid in cur:
            m = guild.get_member(int(uid))
            if m is not None and cc.is_excepted_anywhere([r.id for r in m.roles], rtypes):
                skip.add(uid)
        shown = {u: c for u, c in cur.items() if u not in skip}
        prev = await staff_report_snapshots_col().find_one({"guild_id": gid}, sort=[("at", -1)])
        since = core.aware(prev["at"]) if prev else now - timedelta(days=7)

        name = lambda uid: (guild.get_member(int(uid)).display_name
                            if guild.get_member(int(uid)) else f"<@{uid}>")
        embed = base_embed("📋 Weekly staff report",
                           f"Since <t:{int(since.timestamp())}:f> ({(now - since).days or 1} day(s)).",
                           discord.Color.blurple().value)

        # -- taskbook activity (difference between snapshots)
        if prev:
            diffs = {u: core.week_diff((prev.get("users") or {}).get(u, {}), c) for u, c in shown.items()}
            top = sorted(diffs.items(), key=lambda kv: (-kv[1]["handled"], -kv[1]["closed"]))
            lines = [f"**{name(u)}**: {d['handled']} handled · {d['closed']} closed"
                     for u, d in top[:8] if d["handled"] or d["closed"]]
            tot_h = sum(d["handled"] for d in diffs.values())
            tot_c = sum(d["closed"] for d in diffs.values())
            embed.add_field(name=f"Tickets · {tot_h} handled, {tot_c} closed",
                            value="\n".join(lines) or "No ticket activity.", inline=False)
        else:
            top = sorted(shown.items(), key=lambda kv: -kv[1]["handled"])
            lines = [f"**{name(u)}**: {c['handled']} handled · {c['closed']} closed" for u, c in top[:5]]
            embed.add_field(name="Tickets (running totals)",
                            value=("\n".join(lines) or "No taskbooks yet.") +
                                  "\n-# First report: weekly numbers start next time.", inline=False)

        # -- ratings
        ratings = await ticket_ratings_col().find(
            {"guild_id": gid, "rated_at": {"$gte": since}, "rating": {"$ne": None}}).to_list(None)
        if ratings:
            avg = sum(r["rating"] for r in ratings) / len(ratings)
            embed.add_field(name="Ratings", value=f"{len(ratings)} new · average **{avg:.2f}** / 5")
        else:
            embed.add_field(name="Ratings", value="None this week.")

        # -- strikes
        new_strikes = await staff_strikes_col().count_documents(
            {"guild_id": gid, "created_at": {"$gte": since}})
        expired = await staff_strike_audit_col().count_documents(
            {"guild_id": gid, "action": "expired", "at": {"$gte": since}})
        embed.add_field(name="Strikes", value=f"{new_strikes} given · {expired} expired")

        # -- activity checks
        checks = await staff_activity_checks_col().count_documents(
            {"guild_id": guild.id, "opened_at": {"$gte": since}})
        embed.add_field(name="Activity checks", value=f"{checks} held")

        # -- promotions / demotions
        changes = await promotion_history_col().find(
            {"guild_id": gid, "at": {"$gte": since}}).sort("at", 1).to_list(20)
        if changes:
            lines = [f"{'⬆️' if c['kind'] == 'promotion' else '⬇️'} **{c['member_name']}**: "
                     f"{c['old_label']} → {c['new_label']}" for c in changes[:10]]
            more = f"\n…and {len(changes) - 10} more" if len(changes) > 10 else ""
            embed.add_field(name="Promotions & demotions", value="\n".join(lines) + more, inline=False)
        else:
            embed.add_field(name="Promotions & demotions", value="None.", inline=False)

        # -- LOA right now
        loas = await loa_requests_col().find({"guild_id": guild.id, "status": "active"}).to_list(15)
        embed.add_field(name=f"On LOA now ({len(loas)})",
                        value=", ".join(name(d["user_id"]) for d in loas)[:1000] or "Nobody.",
                        inline=False)

        if not preview:
            await staff_report_snapshots_col().insert_one({"guild_id": gid, "at": now, "users": cur})
            old = await staff_report_snapshots_col().find({"guild_id": gid}).sort("at", -1) \
                .skip(SNAPSHOT_KEEP).to_list(None)
            if old:
                await staff_report_snapshots_col().delete_many({"_id": {"$in": [o["_id"] for o in old]}})
        return [embed]


async def setup(bot: commands.Bot):
    await bot.add_cog(StaffAutomation(bot))
