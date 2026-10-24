"""
Staff activity checks - as many as you like, each for its own role.

On the dashboard's "Staff Activity" page you create checks. Each one has a name,
a role, a channel, an interval, a reaction window and an emoji. Every INTERVAL_DAYS
the bot posts an embed in that check's channel, pings its role, and asks everyone
with the role to react with the emoji within WINDOW_HOURS. When the window closes
it posts a results embed listing who didn't react.

Config lives in "staff_activity_configs", running/finished checks in
"staff_activity_checks" (each one snapshots its role/channel/emoji, so editing a
check on the dashboard never changes one that is already running). Everything is
timestamp-driven from Mongo, so a restart never loses an open check or skips
closing one that is already due.

    /activitycheck <name | all>   start a check right now (permission: Command
                                  Permissions page; administrators always can)
"""
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.mongo import staff_activity_checks_col, staff_activity_configs_col
from utils.embeds import success_embed, error_embed
from database import models
from utils import changes_core as core
from utils import strikes_core as sc
from utils.permissions import command_check
from cogs.staff_strikes import give_strike

DEFAULT_EMOJI = "💎"
BLURPLE = 0x5865F2
MAX_LISTED = 40  # mentions shown in the results embed before "+N more"


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def expected_members(role, exempt_role_ids) -> list:
    """IDs of the members who must react: everyone holding `role` except bots and anyone who
    also holds one of the check's exempt roles (e.g. high staff). Exempt wins."""
    exempt = {int(x) for x in (exempt_role_ids or [])}
    return [m.id for m in role.members
            if not m.bot and not any(r.id in exempt for r in m.roles)]


class StaffActivity(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        try:
            await staff_activity_checks_col().create_index([("config_id", 1), ("closed", 1)])
            await staff_activity_configs_col().create_index("guild_id")
        except Exception as e:
            print(f"[staff_activity] index creation failed: {e}")
        self.loop_task.start()

    async def cog_unload(self):
        self.loop_task.cancel()

    # ── Open a new check ─────────────────────────────────────────────────
    async def open_check(self, guild: discord.Guild, cfg: dict) -> bool:
        role = guild.get_role(int(cfg["role_id"])) if cfg.get("role_id") else None
        channel = guild.get_channel(int(cfg["channel_id"])) if cfg.get("channel_id") else None
        if role is None or not isinstance(channel, discord.TextChannel):
            return False

        emoji = (cfg.get("emoji") or DEFAULT_EMOJI).strip() or DEFAULT_EMOJI
        interval_days = max(1, int(cfg.get("interval_days") or 2))
        window_hours = max(1, int(cfg.get("window_hours") or 24))
        name = cfg.get("name") or "Staff"

        now = datetime.now(timezone.utc)
        deadline = now + timedelta(hours=window_hours)
        # Everyone holding the role is expected to react, except members with an Exception role
        # (set on the Changes page: the check's strike type, or any type when it has none).
        # (LOA strips ladder roles, so people on LOA normally don't hold the role any more.)
        expected = expected_members(role, [])
        exc_ids = []
        try:
            ccfg = await models.get_changes_config(guild.id)
            stype = (core.find_type(ccfg["types"], cfg["strike_type_id"])
                     if cfg.get("strike_type_id") else None)
            scope = [stype] if stype is not None else ccfg["types"]
            exc_ids = sorted({int(x) for t in scope for x in (t.get("exception_role_ids") or [])
                              if guild.get_role(int(x)) is not None})
            expected = [uid for uid in expected
                        if guild.get_member(uid) is None
                        or not core.is_excepted_anywhere(
                            [r.id for r in guild.get_member(uid).roles], scope)]
        except Exception as e:
            print(f"[staff_activity] exception roles lookup failed: {e}")

        embed = discord.Embed(
            title=f"Staff Activity Check • {name}",
            description=f"React with {emoji} within {window_hours} hours to confirm activity.",
            color=BLURPLE,
        )
        embed.add_field(name="Team", value=role.mention, inline=False)
        if exc_ids:
            embed.add_field(name="Exempt (no need to react)",
                            value=" ".join(f"<@&{rid}>" for rid in exc_ids)[:1000],
                            inline=False)
        embed.add_field(name="Deadline", value=f"<t:{int(deadline.timestamp())}:F>", inline=True)
        embed.add_field(name="Check closes", value=f"<t:{int(deadline.timestamp())}:R>", inline=True)
        embed.set_footer(text=f"Runs every {interval_days} day{'' if interval_days == 1 else 's'}")

        try:
            msg = await channel.send(
                content=role.mention, embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
            await msg.add_reaction(emoji)
        except discord.HTTPException as e:
            print(f"[staff_activity] Failed to post check '{name}' in guild {guild.id}: {e}")
            return False

        await staff_activity_checks_col().insert_one({
            "guild_id": guild.id,
            "config_id": str(cfg["_id"]),
            "name": name,
            "channel_id": channel.id,
            "message_id": msg.id,
            "role_id": role.id,
            "emoji": emoji,
            "expected_reactors": expected,
            # Snapshotted like role/emoji: editing the check later never changes a running one.
            "strike_on_miss": bool(cfg.get("strike_on_miss")) and bool(cfg.get("strike_type_id")),
            "strike_type_id": str(cfg["strike_type_id"]) if cfg.get("strike_type_id") else None,
            "opened_at": now,
            "deadline": deadline,
            "closed": False,
        })
        await staff_activity_configs_col().update_one(
            {"_id": cfg["_id"]}, {"$set": {"last_opened_at": now}})
        return True

    # ── Close a due check and post results ───────────────────────────────
    async def close_check(self, check: dict):
        # Claim it first so two loop runs can never post results twice.
        claimed = await staff_activity_checks_col().update_one(
            {"_id": check["_id"], "closed": False}, {"$set": {"closed": True}})
        if not claimed.modified_count:
            return

        guild = self.bot.get_guild(check["guild_id"])
        if guild is None:
            return

        channel = guild.get_channel(check["channel_id"])
        role = guild.get_role(check["role_id"])
        expected = set(check.get("expected_reactors", []))
        reacted = set()
        message_gone = False
        name = check.get("name") or "Staff"

        if channel:
            try:
                msg = await channel.fetch_message(check["message_id"])
                for reaction in msg.reactions:
                    if str(reaction.emoji) != check["emoji"]:
                        continue
                    async for user in reaction.users():
                        if not user.bot:
                            reacted.add(user.id)
            except discord.NotFound:
                message_gone = True
            except discord.HTTPException as e:
                print(f"[staff_activity] Failed to fetch check message: {e}")

        role_text = role.mention if role else "@unknown role"

        if message_gone:
            embed = discord.Embed(
                title=f"⚠️ Staff Activity Check Results • {name}", color=0xF1C40F,
                description="The check message was deleted before it closed, so there are no results.",
            )
            missing, matched = set(), set()
        else:
            missing = expected - reacted
            matched = expected & reacted
            icon = "✅" if not missing else "❌"
            embed = discord.Embed(
                title=f"{icon} Staff Activity Check Results • {name}",
                color=0x2ECC71 if not missing else 0xE74C3C,
            )
            if missing:
                listed = sorted(missing)
                lines = "\n".join(f"<@{uid}>" for uid in listed[:MAX_LISTED])
                if len(listed) > MAX_LISTED:
                    lines += f"\n…and {len(listed) - MAX_LISTED} more"
                embed.description = (
                    f"Missing reactions from {len(missing)} member(s) with {role_text}:\n\n{lines}")
            else:
                embed.description = f"All members with {role_text} reacted in time. 🎉"
            embed.add_field(name="Expected Reactors", value=str(len(expected)), inline=True)
            embed.add_field(name="Reacted", value=str(len(matched)), inline=True)
            embed.add_field(name="Missing", value=str(len(missing)), inline=True)
            if missing and check.get("strike_on_miss") and check.get("strike_type_id"):
                embed.add_field(name="Strikes", value=await self._strike_missing(
                    guild, check, name, missing), inline=False)

        if channel:
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as e:
                print(f"[staff_activity] Failed to post results: {e}")

        await staff_activity_checks_col().update_one(
            {"_id": check["_id"]},
            {"$set": {"closed_at": datetime.now(timezone.utc),
                      "reacted": list(matched), "missing": list(missing)}},
        )

    # ── Strike everyone who missed the check (one strike per check) ────────
    async def _strike_missing(self, guild: discord.Guild, check: dict, name: str,
                              missing: set) -> str:
        """Give each missing member one strike in the check's type and DM them.
        Returns a short summary line for the results embed. Never raises."""
        try:
            cfg = await models.get_changes_config(guild.id)
            ctype = core.find_type(cfg["types"], check["strike_type_id"])
            if ctype is None:
                return "⚠️ The strike type no longer exists, so nobody was struck."
            struck, no_dm, gone = 0, 0, 0
            for uid in sorted(missing):
                member = guild.get_member(uid)
                if member is None:
                    gone += 1  # left the server
                    continue
                if core.is_excepted([r.id for r in member.roles], ctype):
                    continue  # got an exception role after the check opened
                try:
                    r = await give_strike(
                        guild, member, ctype, cfg["types"],
                        sc.clean_reason(f"Missed activity check: {name}"), self.bot.user,
                        dm_title=f"⚠️ You were struck in {guild.name}",
                        dm_description=(f"You didn't react to the **{name}** activity check in "
                                        f"time, so you received a strike in **{ctype['name']}**."))
                except Exception as e:
                    print(f"[staff_activity] strike failed for {uid}: {e}")
                    continue
                struck += 1
                if not r.dm_ok:
                    no_dm += 1
            line = f"Struck **{struck}** member(s) in **{ctype['name']}**."
            if no_dm:
                line += f" {no_dm} couldn't be DMed."
            if gone:
                line += f" {gone} left the server."
            return line
        except Exception as e:
            print(f"[staff_activity] strike step failed: {e}")
            return "⚠️ Something went wrong while giving strikes."

    # ── Scheduler (restart-safe: everything is timestamp-driven from Mongo) ──
    @tasks.loop(minutes=5)
    async def loop_task(self):
        try:
            now = datetime.now(timezone.utc)

            # Close every check whose deadline has passed.
            due = await staff_activity_checks_col().find(
                {"closed": False, "deadline": {"$lte": now}}).to_list(None)
            for check in due:
                await self.close_check(check)

            # Open new checks for every enabled config that is due.
            configs = await staff_activity_configs_col().find({"enabled": True}).to_list(None)
            for cfg in configs:
                guild = self.bot.get_guild(int(cfg["guild_id"]))
                if guild is None or not cfg.get("role_id") or not cfg.get("channel_id"):
                    continue
                if await staff_activity_checks_col().find_one(
                        {"config_id": str(cfg["_id"]), "closed": False}):
                    continue
                last = _aware(cfg.get("last_opened_at"))
                interval = max(1, int(cfg.get("interval_days") or 2))
                if last is None or now >= last + timedelta(days=interval):
                    await self.open_check(guild, cfg)
        except Exception as e:
            print(f"[staff_activity] loop error: {e}")

    @loop_task.before_loop
    async def _before_loop(self):
        await self.bot.wait_until_ready()

    # ── Manual trigger ────────────────────────────────────────────────────
    @commands.hybrid_command(name="activitycheck",
                             description="Manually start a staff activity check now")
    @app_commands.describe(check="Which check to start, or 'all'")
    @command_check()
    async def activitycheck(self, ctx: commands.Context, check: str):
        await ctx.defer(ephemeral=True)
        configs = await staff_activity_configs_col().find(
            {"guild_id": ctx.guild.id}).sort("created_at", 1).to_list(None)
        if not configs:
            return await ctx.send(embed=error_embed(
                "No staff activity checks exist yet. Create one on the dashboard first."),
                ephemeral=True)

        wanted = check.strip().lower()
        if wanted == "all":
            chosen = configs
        else:
            chosen = [c for c in configs if (c.get("name") or "").lower() == wanted]
            if not chosen:
                chosen = [c for c in configs if (c.get("name") or "").lower().startswith(wanted)]
            if len(chosen) != 1:
                known = ", ".join(f"`{c['name']}`" for c in configs)
                return await ctx.send(embed=error_embed(
                    f"I couldn't find a single check called **{check}**. Your checks: {known}."),
                    ephemeral=True)

        started, skipped = [], []
        for cfg in chosen:
            if await staff_activity_checks_col().find_one(
                    {"config_id": str(cfg["_id"]), "closed": False}):
                skipped.append(f"**{cfg['name']}** (already running)")
            elif await self.open_check(ctx.guild, cfg):
                started.append(f"**{cfg['name']}**")
            else:
                skipped.append(f"**{cfg['name']}** (couldn't post - check its role, channel and my permissions)")

        lines = []
        if started:
            lines.append("Started: " + ", ".join(started))
        if skipped:
            lines.append("Skipped: " + ", ".join(skipped))
        embed = success_embed("\n".join(lines)) if started else error_embed("\n".join(lines))
        await ctx.send(embed=embed, ephemeral=True)

    @activitycheck.autocomplete("check")
    async def _check_autocomplete(self, interaction: discord.Interaction, current: str):
        if interaction.guild is None:
            return []
        configs = await staff_activity_configs_col().find(
            {"guild_id": interaction.guild.id}).sort("created_at", 1).to_list(25)
        wanted = (current or "").lower()
        names = ["all"] + [c["name"] for c in configs]
        return [app_commands.Choice(name=n[:100], value=n[:100])
                for n in names if wanted in n.lower()][:25]


async def setup(bot: commands.Bot):
    await bot.add_cog(StaffActivity(bot))
