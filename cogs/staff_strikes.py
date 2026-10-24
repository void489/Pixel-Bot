"""
Staff strikes cog - strikes per Change type, with optional automatic demotion.

Commands (hybrid, so slash AND prefix, all @command_check() = admin-only until enabled on the
dashboard's Command Permissions page):
    /strike add <member> <type> [reason]   give a strike in a type
    /strike remove <number> [reason]       delete one strike by its number
    /strike list <member> [type]           a member's strikes, grouped by type

Types are the Change types from the dashboard's Changes page (staff, builder, ...). A strike
only counts inside its own type. The card is posted in that type's announce channel.

Everything else is on the dashboard's Strikes tab, per type:
    - rules: "at N strikes -> demote X rank(s)" (the highest rule reached wins)
    - tick: reset strikes on promotion / tick: reset strikes on demotion
    - delete a strike, reset one member, reset a whole type
    - "strikes expire after N days" (0 = never) with an optional DM; a sweep every 3 hours
      removes expired strikes and records them as "expired" in the strike audit. Expiry only
      lowers the count for future limits, it never undoes a demotion that already happened.

Rules only run when a NEW strike is added (never when settings change). After a rule demotes
someone, their strikes in that type are reset to 0 so the same rule can't fire again on the
next strike. If the member is on LOA (ranks are paused), isn't in the server, or the demotion
fails, the strike is still recorded and the reply says the demotion was skipped.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.changes import _on_loa, _reply, _type_autocomplete, demote_member
from config import constants
from database import models
from database.mongo import staff_strikes_col, guild_settings_col
from utils import strikes_core as sc
from utils.embeds import base_embed, error_embed, info_embed, success_embed
from utils.permissions import command_check
from utils import changes_core as core


def _ts(dt) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


async def post_strike_card(guild: discord.Guild, user: discord.abc.User, ctype: dict,
                           count: int, number: int, reason: str, issuer: discord.abc.User) -> str:
    """Post the strike card in the type's channel. Returns "" on success, otherwise a short
    human reason it wasn't posted."""
    channel_id = ctype.get("channel_id")
    if not channel_id:
        return "No channel is set for this type on the Changes page, so nothing was announced."
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        return "This type's channel no longer exists, so nothing was announced."
    embed = base_embed(
        title="⚠️ Staff strike",
        description=f"{user.mention} received a strike in **{ctype['name']}**.",
        color=constants.COLOR_ERROR,
    )
    embed.add_field(name="Active strikes", value=f"**{count}**")
    embed.add_field(name="Reason", value=reason or "No reason given.", inline=False)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=f"{ctype['name']} · Strike #{number} · By {issuer.name}")
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        return "I couldn't post in this type's channel (missing permissions?)."
    return ""


def until_demotion_text(rules, count: int) -> str:
    """'2 more strikes until you are demoted 1 rank' / '' when no rule is ahead."""
    ahead = sc.next_rule(rules, count)
    if not ahead:
        return ""
    left = ahead["at"] - count
    return (f"**{left}** more strike{'' if left == 1 else 's'} until you are demoted "
            f"{sc.rank_word(ahead['demote'])}")


def strike_dm_embed(guild: discord.Guild, ctype: dict, reason: str, number: int, count: int,
                    demoted: bool, rules, title: str, description: str) -> discord.Embed:
    dm = base_embed(title=title, description=description, color=constants.COLOR_ERROR)
    dm.add_field(name="Reason", value=reason or "No reason given.", inline=False)
    if demoted:
        dm.add_field(name="Result",
                     value=f"You reached the strike limit, so your **{ctype['name']}** "
                           "rank was lowered and your strikes were reset.", inline=False)
    else:
        dm.add_field(name="Active strikes", value=f"**{count}**")
        left = until_demotion_text(rules, count)
        if left:
            dm.add_field(name="Until demotion", value=left, inline=False)
    dm.set_footer(text=f"{guild.name} · {ctype['name']} · Strike #{number}")
    return dm


async def _apply_rule(guild: discord.Guild, member: Optional[discord.Member],
                      ctype: dict, types: list, rule: dict, count: int):
    """Run a threshold rule. Returns (message, demoted)."""
    what = f"{sc.rank_word(rule['demote'])}"
    if member is None:
        return (f"They reached **{count}** strikes (rule: demote {what}) but aren't in the "
                "server, so nobody was demoted."), False
    if len(ctype["ladder"]) < 2:
        return (f"They reached **{count}** strikes (rule: demote {what}) but the "
                f"**{ctype['name']}** ladder has fewer than 2 roles, so nobody was demoted."), False
    if await _on_loa(guild.id, member.id):
        return (f"They reached **{count}** strikes (rule: demote {what}) but they're on LOA "
                f"(ranks are paused), so the demotion was skipped. Use `/demote` in "
                f"**{ctype['name']}** once the LOA ends."), False

    res = await demote_member(
        guild, member, ctype, types,
        audit_reason=f"Reached {count} strikes ({ctype['name']}, {rule['at']}-strike rule)",
        reason=f"Reached {count} strikes",
        footer=f"{ctype['name']} · Strike limit reached",
        ranks=rule["demote"], source="strike",
    )
    if res.status == "error":
        return (f"They reached **{count}** strikes (rule: demote {what}) but the demotion "
                f"failed: {res.error}"), False
    if res.status == "excepted":
        return (f"They reached **{count}** strikes (rule: demote {what}) but they hold an "
                f"exception role for **{ctype['name']}**, so nobody was demoted."), False
    if res.status in ("none", "base"):
        return (f"They reached **{count}** strikes (rule: demote {what}) but they have no "
                f"**{ctype['name']}** rank to lose, so nothing changed."), False

    cleared = await models.clear_user_strikes(guild.id, ctype["id"], member.id)
    msg = (f"⬇️ Strike limit reached ({count}): demoted from **{res.old_label}** to "
           f"**{res.new_label}**. Their strikes in **{ctype['name']}** were reset to 0"
           f"{f' ({cleared} cleared)' if cleared else ''}.")
    if res.card_note:
        msg += f"\n⚠️ {res.card_note}"
    if res.extras_warning:
        msg += f"\n⚠️ {res.extras_warning}"
    return msg, True


@dataclass
class StrikeResult:
    number: int
    count: int            # strikes right after adding (before any reset)
    shown: int            # active strikes now (0 after a demotion reset)
    demoted: bool
    rule_msg: str
    card_note: str
    dm_ok: bool
    next_text: str        # "Next rule: ..." for the staff reply ("" if none / demoted)


async def give_strike(guild: discord.Guild, user: discord.abc.User, ctype: dict, types: list,
                      reason: str, issuer: discord.abc.User, *,
                      dm_title: str = None, dm_description: str = None) -> StrikeResult:
    """The one strike routine: used by /strike add and by failed activity checks.

    Records the strike, posts the card, runs the threshold rule (demotion + reset) and
    DMs the member how many strikes they have and how many are left until demotion."""
    doc = await models.add_strike(
        guild.id, ctype["id"], ctype["name"], user.id, reason, issuer.id, issuer.name)
    count = await models.count_strikes(guild.id, ctype["id"], user.id)
    gm = guild.get_member(user.id)
    scfg = await models.get_strike_type_config(guild.id, ctype["id"])
    card_note = await post_strike_card(guild, user, ctype, count, doc["number"], reason, issuer)

    rule = sc.rule_for_count(scfg["rules"], count)
    rule_msg, demoted = ("", False)
    if rule:
        rule_msg, demoted = await _apply_rule(guild, gm, ctype, types, rule, count)

    shown = 0 if demoted else count
    dm_ok = True
    try:
        await user.send(embed=strike_dm_embed(
            guild, ctype, reason, doc["number"], shown, demoted, scfg["rules"],
            dm_title or f"⚠️ You received a strike in {guild.name}",
            dm_description or f"You were given a strike in **{ctype['name']}**."))
    except (discord.Forbidden, discord.HTTPException):
        dm_ok = False

    next_text = ""
    if not rule_msg and not demoted:
        ahead = sc.next_rule(scfg["rules"], count)
        if ahead:
            next_text = f"Next rule: {sc.describe_rule(ahead)}."
    return StrikeResult(doc["number"], count, shown, demoted, rule_msg, card_note, dm_ok, next_text)


async def post_strike_removed_card(guild: discord.Guild, user: discord.abc.User, ctype: dict,
                                   left: int, number: int, reason: str,
                                   remover: discord.abc.User) -> str:
    """Card in the type's channel when a strike is deleted. "" on success, else why not."""
    channel_id = ctype.get("channel_id")
    if not channel_id:
        return "No channel is set for this type on the Changes page, so nothing was announced."
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        return "This type's channel no longer exists, so nothing was announced."
    embed = base_embed(
        title="✅ Strike removed",
        description=f"A strike was removed from {user.mention} in **{ctype['name']}**.",
        color=constants.COLOR_SUCCESS,
    )
    embed.add_field(name="Active strikes", value=f"**{left}**")
    embed.add_field(name="Reason", value=reason or "No reason given.", inline=False)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=f"{ctype['name']} · Strike #{number} · By {remover.name}")
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        return "I couldn't post in this type's channel (missing permissions?)."
    return ""


class StaffStrikes(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self._expire_sweep.start()
        try:
            await staff_strikes_col().create_index(
                [("guild_id", 1), ("type_id", 1), ("user_id", 1)])
            await staff_strikes_col().create_index([("guild_id", 1), ("number", 1)], unique=True)
        except Exception as e:  # indexes are an optimisation, never block the cog
            print(f"[staff_strikes] couldn't create indexes: {e}")

    async def cog_unload(self):
        self._expire_sweep.cancel()

    # ------------------------------------------------------------------
    # strike expiry
    # ------------------------------------------------------------------
    @tasks.loop(hours=3)
    async def _expire_sweep(self):
        try:
            async for gdoc in guild_settings_col().find({"strikes.types": {"$exists": True}}):
                try:
                    await self._expire_guild(gdoc)
                except Exception as e:
                    print(f"[staff_strikes] expiry sweep failed for {gdoc.get('_id')}: {e}")
        except Exception as e:
            print(f"[staff_strikes] expiry sweep error: {e}")

    @_expire_sweep.before_loop
    async def _before_expire(self):
        await self.bot.wait_until_ready()

    async def _expire_guild(self, gdoc: dict):
        guild = self.bot.get_guild(int(gdoc["_id"]))
        if guild is None:
            return                      # never touch data of a server the bot isn't in
        now = datetime.now(timezone.utc)
        for type_id, raw in ((gdoc.get("strikes") or {}).get("types") or {}).items():
            cfg = sc.normalize_type_config(raw)
            days = cfg["expire_days"]
            if days <= 0:
                continue
            cutoff = now - timedelta(days=days)
            old = await staff_strikes_col().find(
                {"guild_id": str(guild.id), "type_id": str(type_id),
                 "created_at": {"$lt": cutoff}}).to_list(None)
            per_user = {}
            for d in old:
                # delete by _id so a strike removed by hand a moment ago isn't counted twice
                res = await staff_strikes_col().delete_one({"_id": d["_id"]})
                if not res.deleted_count:
                    continue
                per_user.setdefault(d["user_id"], []).append(d)
                await models.add_strike_audit(
                    guild.id, "expired", type_id, d.get("type_name") or "?", d["user_id"],
                    "Auto expiry", f"Strike #{d.get('number')} expired after {days} day(s)")
            if not cfg["expire_dm"]:
                continue
            for uid, gone in per_user.items():
                member = guild.get_member(int(uid))
                if member is None:
                    continue
                left = await models.count_strikes(guild.id, type_id, int(uid))
                name = gone[0].get("type_name") or "staff"
                try:
                    await member.send(embed=info_embed(
                        f"{len(gone)} of your **{name}** strike(s) in **{guild.name}** expired "
                        f"(strikes last {days} days). You now have **{left}** active."))
                except discord.HTTPException:
                    pass                # DMs closed; the audit trail still has it

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    async def _find_type(self, ctx: commands.Context, type_name: str):
        """(type, all_types) or (None, None) after replying with the error."""
        cfg = await models.get_changes_config(ctx.guild.id)
        if not cfg["types"]:
            await _reply(ctx, error_embed(
                "No change types exist yet. Create one on the dashboard's **Changes** page."))
            return None, None
        ctype = core.find_type(cfg["types"], type_name)
        if ctype is None:
            known = ", ".join(f"`{t['name']}`" for t in cfg["types"])
            await _reply(ctx, error_embed(
                f"I couldn't find a type called **{type_name}**. Your types: {known}."))
            return None, None
        return ctype, cfg["types"]

    # ------------------------------------------------------------------
    # /strike
    # ------------------------------------------------------------------
    @commands.hybrid_group(name="strike", description="Staff strikes (per Change type)",
                           invoke_without_command=True)
    async def strike(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await _reply(ctx, info_embed(
                "Use `/strike add <member> <type> [reason]`, `/strike remove <number>` or "
                "`/strike list <member>`."))

    @strike.command(name="add", description="Give a staff member a strike in a type")
    @app_commands.describe(member="Who gets the strike", type="Which type (staff, builder...)",
                           reason="Why (optional)")
    @command_check()
    async def strike_add(self, ctx: commands.Context, member: discord.User, type: str, *,
                         reason: str = None):
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)

        ctype, types = await self._find_type(ctx, type)
        if ctype is None:
            return
        if member.bot:
            return await _reply(ctx, error_embed("You can't strike a bot."))
        if member.id == ctx.author.id:
            return await _reply(ctx, error_embed("You can't strike yourself."))
        gm = ctx.guild.get_member(member.id)
        if gm is not None and core.is_excepted([r.id for r in gm.roles], ctype):
            return await _reply(ctx, error_embed(
                f"{member.mention} has an exception role for **{ctype['name']}**, so they "
                "can't be given strikes."))

        reason = sc.clean_reason(reason)
        r = await give_strike(ctx.guild, member, ctype, types, reason, ctx.author)

        msg = (f"Gave {member.mention} strike `#{r.number}` in **{ctype['name']}**. "
               f"They now have **{r.shown}** active strike{'' if r.shown == 1 else 's'}.")
        if r.rule_msg:
            msg += f"\n{r.rule_msg}"
        elif r.next_text:
            msg += f"\n{r.next_text}"
        if r.card_note:
            msg += f"\n⚠️ {r.card_note}"
        if not r.dm_ok:
            msg += "\n⚠️ Couldn't DM them (their DMs are closed or they share no server with me)."
        await _reply(ctx, success_embed(msg), delete_after=15)

    @strike.command(name="remove", description="Delete one strike by its number")
    @app_commands.describe(number="The strike number (shown on the card and in /strike list)",
                           reason="Why it's being removed (optional)")
    @command_check()
    async def strike_remove(self, ctx: commands.Context, number: int, *, reason: str = None):
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        doc = await models.delete_strike(ctx.guild.id, number)
        if doc is None:
            return await _reply(ctx, error_embed(f"There's no strike numbered `#{number}` here."))
        reason = sc.clean_reason(reason)
        await models.add_strike_audit(
            ctx.guild.id, "delete_strike", doc["type_id"], doc.get("type_name", "?"),
            doc["user_id"], ctx.author.name,
            f"Strike #{doc['number']}" + (f" - {reason}" if reason else ""))
        uid = int(doc["user_id"])
        left = await models.count_strikes(ctx.guild.id, doc["type_id"], uid)
        msg = (f"Deleted strike `#{doc['number']}` (<@{uid}>, **{doc.get('type_name', '?')}**). "
               f"They have **{left}** active strike{'' if left == 1 else 's'} left in that type.")

        # Same treatment as /strike add: card in the type's channel + DM to the member.
        cfg = await models.get_changes_config(ctx.guild.id)
        ctype = core.find_type(cfg["types"], doc["type_id"])
        user = ctx.guild.get_member(uid)
        if user is None:
            try:
                user = await self.bot.fetch_user(uid)
            except discord.HTTPException:
                user = None
        if ctype is None:
            msg += "\n⚠️ That type no longer exists, so nothing was announced and they weren't DMed."
        elif user is None:
            msg += "\n⚠️ I couldn't find that user, so nothing was announced and they weren't DMed."
        else:
            scfg = await models.get_strike_type_config(ctx.guild.id, ctype["id"])
            ahead = sc.next_rule(scfg["rules"], left)
            if ahead:
                msg += f"\nNext rule: {sc.describe_rule(ahead)}."
            note = await post_strike_removed_card(
                ctx.guild, user, ctype, left, doc["number"], reason, ctx.author)
            if note:
                msg += f"\n⚠️ {note}"
            try:
                dm = base_embed(
                    title=f"✅ A strike was removed in {ctx.guild.name}",
                    description=f"Strike `#{doc['number']}` in **{ctype['name']}** was removed.",
                    color=constants.COLOR_SUCCESS)
                dm.add_field(name="Reason", value=reason or "No reason given.", inline=False)
                dm.add_field(name="Active strikes", value=f"**{left}**")
                until = until_demotion_text(scfg["rules"], left)
                if until:
                    dm.add_field(name="Until demotion", value=until, inline=False)
                dm.set_footer(text=f"{ctx.guild.name} · {ctype['name']} · Strike #{doc['number']}")
                await user.send(embed=dm)
            except (discord.Forbidden, discord.HTTPException):
                msg += "\n⚠️ Couldn't DM them (their DMs are closed or they share no server with me)."
        await _reply(ctx, success_embed(msg), delete_after=15)

    @strike.command(name="list", description="Show a member's strikes")
    @app_commands.describe(member="Whose strikes to show", type="Only this type (optional)")
    @command_check()
    async def strike_list(self, ctx: commands.Context, member: discord.User,
                          type: Optional[str] = None):
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        cfg = await models.get_changes_config(ctx.guild.id)
        type_id = None
        if type:
            ctype = core.find_type(cfg["types"], type)
            if ctype is None:
                return await _reply(ctx, error_embed(f"I couldn't find a type called **{type}**."))
            type_id = ctype["id"]

        strikes = await models.get_user_strikes(ctx.guild.id, member.id, type_id)
        if not strikes:
            return await _reply(ctx, info_embed(f"{member.mention} has no active strikes."))

        names = {t["id"]: t["name"] for t in cfg["types"]}
        grouped = {}
        for s in strikes:
            grouped.setdefault(s["type_id"], []).append(s)

        embed = base_embed(title=f"⚠️ Strikes - {member.display_name}", color=constants.COLOR_ERROR)
        embed.set_thumbnail(url=member.display_avatar.url)
        for tid, items in grouped.items():
            name = names.get(tid) or items[-1].get("type_name") or "?"
            lines = []
            for s in items[-8:]:
                line = f"`#{s['number']}` <t:{_ts(s['created_at'])}:R> by <@{s['issuer_id']}>"
                if s.get("reason"):
                    line += f" - {s['reason'][:120]}"
                lines.append(line)
            if len(items) > 8:
                lines.insert(0, f"*...{len(items) - 8} older not shown*")
            value = "\n".join(lines)
            embed.add_field(name=f"{name} ({len(items)})", value=value[:1024], inline=False)
        await _reply(ctx, embed, delete_after=30)

    @strike_add.autocomplete("type")
    async def _add_type_ac(self, interaction: discord.Interaction, current: str):
        return await _type_autocomplete(interaction, current)

    @strike_list.autocomplete("type")
    async def _list_type_ac(self, interaction: discord.Interaction, current: str):
        return await _type_autocomplete(interaction, current)


async def setup(bot: commands.Bot):
    await bot.add_cog(StaffStrikes(bot))
