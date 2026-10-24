"""
Changes cog (promotions / demotions), one ladder per TYPE.

Commands (hybrid, so slash AND prefix, both @command_check()):
    /promote <member> <type> [index] [reason] -> gives the member the next role up that type's
                                          ladder (the old role is NOT removed). With an index,
                                          jumps straight there and gives EVERY role up to it.
    /demote  <member> <type> [index] [reason] -> removes the highest role of that type's ladder
                                          the member has (never the base rung 0). With an index,
                                          drops to it and removes every role above it.

Setup is all on the dashboard's Changes page: create types (staff, builder, ...), each
with its own announce channel and role ladder (index 0 = base rank / Member).
A type with no channel set still changes roles but announces nothing.

Extra roles: each type can list up to 10 "extra" roles on the Changes page (a Staff Team
role, a separator...). They are added silently when a member crosses 0 -> 1 in that type
(or enters straight at rung 1+) and removed silently when they drop back to the base rung.
1->2 / 3->2 never touch them, and no rank -> rung 0 is not an entry. A role shared by two
types stays until the member has left both. A failure to add/remove one never blocks the
promotion/demotion; the reply to the person running the command just warns about it.

Application hook: accepting an application linked to a type calls promote_application()
below, which promotes the applicant in that type exactly like /promote, except that they
always end up on rung 1 or higher (never just rung 0). No type linked = no roles at all.

Members currently on LOA can't be promoted or demoted (their ranks are paused and are
given back when the LOA ends - see cogs/loa.py).

Replies to the person running the command are EPHEMERAL on slash and auto-delete after
5s on prefix. The card itself is public in the type's channel.
"""
import asyncio
import io
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import models
from database.mongo import dashboard_tasks_col, promotion_history_col
from database.mongo import loa_requests_col
from utils import promotioncard
from utils import changes_core as core
from utils.embeds import success_embed, error_embed, info_embed, base_embed
from utils.permissions import command_check
from config import constants


async def _reply(ctx: commands.Context, embed: discord.Embed, *, delete_after: float = 5):
    if ctx.interaction is not None:
        return await ctx.send(embed=embed, ephemeral=True)
    return await ctx.send(embed=embed, delete_after=delete_after)


def _role_label(guild: discord.Guild, entry) -> str:
    """Nick if set, else the role's name, else a placeholder."""
    if entry is None:
        return core.label(None)
    role = guild.get_role(int(entry["role_id"]))
    return core.label(entry, role.name if role else None)


async def post_staff_change(guild: discord.Guild, member: discord.Member, kind: str,
                            old_label: str, new_label: str, reason: str = None,
                            footer: str = None, channel_id=None) -> str:
    """Post the card in the type's channel.

    Returns "" on success, otherwise a short human reason it wasn't posted
    (so the caller can tell the person running the command)."""
    if not channel_id:
        return "No channel is set for this type on the Changes page, so nothing was announced."
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        return "This type's channel no longer exists, so nothing was announced."

    up = kind in ("promotion", "promoted")
    verb = "promoted" if up else "demoted"
    desc = f"{member.mention} was **{verb}** from **{old_label}** to **{new_label}**."
    if reason:
        desc += f"\n**Reason:** {reason[:1000]}"
    embed = base_embed(
        description=desc,
        color=constants.COLOR_SUCCESS if up else constants.COLOR_ERROR,
    )

    file = None
    try:
        try:
            avatar = await member.display_avatar.replace(size=256, format="png").read()
        except (discord.HTTPException, ValueError):
            avatar = None
        png = await asyncio.to_thread(
            promotioncard.render, avatar, member.name, kind, old_label, new_label,
            reason, footer,
        )
        file = discord.File(io.BytesIO(png), filename="staffchange.png")
        embed.set_image(url="attachment://staffchange.png")
    except Exception as e:  # card is a nicety - fall back to the plain embed
        print(f"[changes] card render failed: {e}")
        file = None

    try:
        kwargs = {"embed": embed, "allowed_mentions": discord.AllowedMentions.none()}
        if file is not None:
            kwargs["file"] = file
        await channel.send(**kwargs)
    except (discord.Forbidden, discord.HTTPException):
        return "I couldn't post in this type's channel (missing permissions?)."
    return ""


async def _on_loa(guild_id: int, user_id: int) -> bool:
    return bool(await loa_requests_col().find_one(
        {"guild_id": guild_id, "user_id": user_id, "status": "active"}))


async def sync_extras(guild: discord.Guild, member: discord.Member, ctype: dict, types: list,
                      change, held_role_ids, reason: str) -> str:
    """Add / remove a type's extra roles after a rank change. Completely silent.

    `change` is core.crossing(old_idx, new_idx): "enter" adds the extras, "leave" removes
    them (overlap-safe), anything else does nothing. `held_role_ids` = the member's role ids
    AFTER the ladder role change (the cached member isn't updated yet, so pass them in).
    Returns "" on success, else a short warning naming what failed."""
    if change not in ("enter", "leave"):
        return ""
    adding = change == "enter"
    if adding:
        ids = list(ctype.get("extra_role_ids") or [])
    else:
        ids = core.extras_to_remove(ctype, types, held_role_ids)
    held = {int(r) for r in held_role_ids}
    failed = []
    for rid in ids:
        role = guild.get_role(int(rid))
        if role is None:
            failed.append(f"`{rid}` (deleted)")
            continue
        if (role.id in held) == adding:
            continue  # already how we want it
        if role.managed or role.is_default() or role >= guild.me.top_role:
            failed.append(role.mention)
            continue
        try:
            if adding:
                await member.add_roles(role, reason=reason)
            else:
                await member.remove_roles(role, reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            failed.append(role.mention)
    if not failed:
        return ""
    return (f"Couldn't {'add' if adding else 'remove'} these extra roles "
            f"(check my role hierarchy): {', '.join(failed)}")


@dataclass
class PromoteResult:
    status: str                 # "ok" | "top" | "error"
    error: str = ""             # when status == "error"
    cur: int = -1               # rung before
    old_label: str = ""
    new_label: str = ""
    card_note: str = ""         # why the card wasn't posted ("" = posted)
    extras_warning: str = ""    # extra roles that couldn't be changed ("" = fine)
    strikes_reset: int = 0      # strikes cleared by the type's "reset on promotion" tick
    steps: list = field(default_factory=list)   # labels of every rung given, lowest first


@dataclass
class DemoteResult:
    status: str                 # "ok" | "none" (no ladder role) | "base" (already lowest) | "excepted" | "error"
    error: str = ""             # when status == "error"
    cur: int = -1               # rung before
    old_label: str = ""
    new_label: str = ""
    card_note: str = ""         # why the card wasn't posted ("" = posted)
    extras_warning: str = ""    # extra roles that couldn't be changed ("" = fine)
    strikes_reset: int = 0      # strikes cleared by the type's "reset on demotion" tick
    steps: list = field(default_factory=list)   # labels of every rung taken away, highest first


def pick_nick_rung(types: list, role_ids):
    """The rung a member's nickname should show, judged from `role_ids`.
    Returns (index, type) or None if they hold no rung 1+ in any auto-nick type.

    1. The type with "Nick priority" (if any) wins whenever the member holds rung 1+ in it,
       even if another type has a higher index.
    2. Otherwise the highest rung (#1+) across all types with the auto nick tick on wins;
       the first type on a tie."""
    auto = [t for t in types if t.get("auto_nick")]
    if not auto:
        return None
    prio = next((t for t in auto if t.get("nick_priority")), None)
    if prio is not None:
        cur = core.highest_index(role_ids, prio["ladder"])
        if cur >= 1:
            return cur, prio
    best = None
    for t in auto:
        cur = core.highest_index(role_ids, t["ladder"])
        if cur >= 1 and (best is None or cur > best[0]):
            best = (cur, t)
    return best


def plain_base_name(member: discord.Member, labels, font) -> str:
    """The member's own name with our rank prefix removed and any OTHER font undone, ready
    for build_nick to restyle. `font` = the font about to be applied (None = plain look).
    A name that was only restyled keeps its real capitals when it still matches their
    Discord name (small caps can't remember capitals)."""
    name, stripped = core.strip_rank_prefix_ex(member.display_name, labels)
    if not stripped and font is None:
        return name                     # their own hand-made name: untouched
    plain = core.unfont(name, keep=font)
    if plain != name:
        real = member.global_name or member.name
        if plain.lower() == real.lower():
            return real
    return plain


async def apply_auto_nick(guild: discord.Guild, member: discord.Member, ctype: dict,
                          types: list, new_idx: int, audit_reason: str,
                          role_ids=None) -> str:
    """The type's "Auto nick change" tick. After a promote/demote in `ctype`, the nickname is
    set from the member's best rung across ALL auto-nick types (see pick_nick_rung), so a
    base-rung change in one type can't wipe a rank they still hold in another. If they hold
    no rank anywhere, the nickname is reset. `role_ids` = the roles the member holds AFTER the
    change. Returns a warning string (\"\" = fine / tick off). Never raises."""
    if not ctype.get("auto_nick"):
        return ""
    try:
        if role_ids is None:
            role_ids = [r.id for r in member.roles]
        labels = [_role_label(guild, e) for t in types for e in t["ladder"]]
        pick = pick_nick_rung(types, role_ids)
        if pick is None:
            if member.nick is None:
                return ""
            await member.edit(nick=None, reason=audit_reason)
            return ""
        idx, ptype = pick
        font = core.nick_font_for(ptype)
        base = plain_base_name(member, labels, font)
        rank = _role_label(guild, core.entry_at(ptype["ladder"], idx))
        nick = core.build_nick(rank, base, font)
        if member.nick != nick:
            await member.edit(nick=nick, reason=audit_reason)
        return ""
    except discord.Forbidden:
        return "Couldn't change their nickname (I can't rename them: owner or above my top role)."
    except discord.HTTPException as e:
        return f"Couldn't change their nickname: {e}"
    except Exception as e:
        print(f"[changes] auto nick failed: {e}")
        return "Couldn't change their nickname."


SYNC_NICKS_TASK = "changes_sync_nicks"


def auto_nick_target(guild: discord.Guild, member: discord.Member, types: list, role_ids):
    """What an auto-nick type set wants for this member right now, judged from `role_ids`.
    Returns ("set", nick) | ("reset", None) | None (nothing to do).
    Uses pick_nick_rung (nick priority type first, then highest rung).
    With no such rung, a nickname is only reset if it carries one of our rank prefixes,
    so a hand-made nickname is never wiped by the automatic fixer."""
    auto = [t for t in types if t.get("auto_nick")]
    if not auto:
        return None
    best = pick_nick_rung(types, role_ids)
    labels = [_role_label(guild, e) for t in types for e in t["ladder"]]
    if best is not None:
        font = core.nick_font_for(best[1])
        base = plain_base_name(member, labels, font)
        rank = _role_label(guild, core.entry_at(best[1]["ladder"], best[0]))
        return "set", core.build_nick(rank, base, font)
    base = core.strip_rank_prefix(member.display_name, labels)
    if base != member.display_name and member.nick is not None:
        return "reset", None
    return None


async def sync_member_nick(guild: discord.Guild, member: discord.Member, types: list,
                           reason: str) -> str:
    """Apply auto_nick_target. Returns "changed" | "same" | "failed"."""
    target = auto_nick_target(guild, member, types, [r.id for r in member.roles])
    if target is None:
        return "same"
    action, nick = target
    want = nick if action == "set" else None
    if member.nick == want:
        return "same"
    try:
        await member.edit(nick=want, reason=reason)
        return "changed"
    except (discord.Forbidden, discord.HTTPException):
        return "failed"


async def _reset_strikes(guild_id: int, ctype: dict, user_id: int, flag: str) -> int:
    """Clear a member's strikes in this type if the type's `flag` tick
    ("reset_on_promotion" / "reset_on_demotion") is on. Never raises into a rank change."""
    try:
        cfg = await models.get_strike_type_config(guild_id, ctype["id"])
        if not cfg.get(flag):
            return 0
        return await models.clear_user_strikes(guild_id, ctype["id"], user_id)
    except Exception as e:
        print(f"[changes] strike reset failed: {e}")
        return 0


async def _log_history(guild: discord.Guild, member: discord.Member, ctype: dict, kind: str,
                       res, *, reason: str = None, by=None, source: str = "command") -> None:
    """Write one promotion / demotion to the history shown on the dashboard.
    kind = "promotion" | "demotion". `by` = whoever did it (a Discord user), or None for the bot
    itself (strike limit). Never raises into a rank change."""
    try:
        await promotion_history_col().insert_one({
            "guild_id": str(guild.id),
            "member_id": str(member.id),
            "member_name": member.display_name[:100],
            "type_id": str(ctype["id"]),
            "type_name": str(ctype["name"]),
            "kind": kind,
            "old_label": res.old_label,
            "new_label": res.new_label,
            "steps": list(res.steps or []),
            "reason": (reason or "")[:1000],
            "by_id": str(by.id) if by is not None else "",
            "by_name": ((getattr(by, "display_name", None) or getattr(by, "name", None) or "Bot")
                        if by is not None else "Bot"),
            "source": source,
            "at": datetime.now(timezone.utc),
        })
    except Exception as e:
        print(f"[changes] history log failed: {e}")


async def promote_member(guild: discord.Guild, member: discord.Member, ctype: dict, types: list,
                         *, audit_reason: str, reason: str = None, footer: str = None,
                         min_target: int = 0, to_index: Optional[int] = None,
                         actor=None, source: str = "command") -> PromoteResult:
    """The one promotion routine, used by /promote and accepted applications.

    Gives the next role up the ladder (the old one is kept), posts the card in the type's
    channel and handles the extra roles. `min_target` is the lowest rung to land on:
    /promote uses 0, applications use 1 so the applicant always gets a real rank.
    `to_index` (optional) jumps straight to that rung: every ladder role from the bottom up
    to it that the member doesn't have yet is given in one go, and the card shows one
    change (old rank -> rung `to_index`).
    Does no permission / LOA / self checks - callers do those first."""
    ladder = ctype["ladder"]
    held_ids = [r.id for r in member.roles]
    cur = core.highest_index(held_ids, ladder)
    if cur >= len(ladder) - 1:
        return PromoteResult("top", cur=cur,
                             old_label=_role_label(guild, core.entry_at(ladder, cur)))

    if to_index is not None:
        if to_index < 0 or to_index > len(ladder) - 1:
            return PromoteResult("error", error=f"Index must be between 0 and {len(ladder) - 1} "
                                                f"for the **{ctype['name']}** ladder.")
        if to_index <= cur:
            return PromoteResult("error", error=(
                f"They're already at index **{cur}** "
                f"(**{_role_label(guild, core.entry_at(ladder, cur))}**). Pick an index above that "
                "to promote, or use `/demote` to go down."))
        target = core.entry_at(ladder, to_index)
    else:
        target = core.entry_at(ladder, max(cur + 1, min_target))
    if target is None:
        return PromoteResult("error", error="This ladder isn't set up yet. Add at least 2 roles "
                                            "on the dashboard's **Changes** page.")

    held_set = {str(r) for r in held_ids}
    if to_index is not None:
        wanted = [e for e in ladder if e["index"] <= to_index and e["role_id"] not in held_set]
    else:
        wanted = [target]
    roles = []
    for entry in wanted:
        role = guild.get_role(int(entry["role_id"]))
        if role is None:
            return PromoteResult("error", error=(
                "The next role on the ladder no longer exists. Fix it on the dashboard's "
                "**Changes** page." if to_index is None else
                f"The role for index {entry['index']} no longer exists. Fix it on the "
                "dashboard's **Changes** page."))
        if role.managed or role.is_default():
            return PromoteResult("error", error=f"{role.mention} can't be assigned manually.")
        if role >= guild.me.top_role:
            return PromoteResult("error",
                                 error=f"{role.mention} is at or above my top role, so I can't give it.")
        roles.append(role)
    try:
        await member.add_roles(*roles, reason=audit_reason)
    except discord.Forbidden:
        return PromoteResult("error", error="Missing permission to add that role.")
    except discord.HTTPException as e:
        return PromoteResult("error", error=f"Discord error: {e}")
    role = roles[-1]

    res = PromoteResult("ok", cur=cur,
                        old_label=_role_label(guild, core.entry_at(ladder, cur)),
                        new_label=_role_label(guild, target))
    res.steps = [_role_label(guild, e) for e in wanted]
    res.extras_warning = await sync_extras(
        guild, member, ctype, types, core.crossing(cur, target["index"]),
        held_ids + [r.id for r in roles], audit_reason)
    nick_warn = await apply_auto_nick(guild, member, ctype, types, target["index"], audit_reason,
                                      role_ids=held_ids + [r.id for r in roles])
    if nick_warn:
        res.extras_warning = (res.extras_warning + "\n" + nick_warn).strip()
    res.strikes_reset = await _reset_strikes(guild.id, ctype, member.id, "reset_on_promotion")
    res.card_note = await post_staff_change(
        guild, member, "promotion", res.old_label, res.new_label,
        reason=reason, footer=footer, channel_id=ctype.get("channel_id"),
    )
    await _log_history(guild, member, ctype, "promotion", res,
                       reason=reason, by=actor, source=source)
    return res


async def demote_member(guild: discord.Guild, member: discord.Member, ctype: dict, types: list,
                        *, audit_reason: str, reason: str = None, footer: str = None,
                        ranks: int = 1, to_index: Optional[int] = None,
                        actor=None, source: str = "command") -> DemoteResult:
    """The one demotion routine, used by /demote and by strike thresholds.

    Takes away the member's highest `ranks` ladder roles at rung 1+ (never the base rung),
    handles the extra roles, posts the card in the type's channel and clears strikes if the
    type's "reset on demotion" tick is on. Does no permission / LOA / self checks - callers
    do those first. Nothing is removed unless every role can be removed.
    `to_index` (optional) drops straight to that rung instead: every ladder role above it
    that the member holds is removed in one go (overrides `ranks`)."""
    ladder = ctype["ladder"]
    held_ids = [r.id for r in member.roles]
    if core.is_excepted(held_ids, ctype):
        # Exception role (Changes page): never demoted, by command, strike limit or anything else.
        return DemoteResult("excepted")
    cur = core.highest_index(held_ids, ladder)
    if cur < 0:
        return DemoteResult("none", cur=cur)
    if cur == 0:
        return DemoteResult("base", cur=0,
                            old_label=_role_label(guild, core.entry_at(ladder, 0)))

    if to_index is not None:
        if to_index < 0:
            return DemoteResult("error", cur=cur, error="Index can't be negative.")
        if to_index >= cur:
            return DemoteResult("error", cur=cur, error=(
                f"They're at index **{cur}** "
                f"(**{_role_label(guild, core.entry_at(ladder, cur))}**). Pick an index below "
                "that to demote, or use `/promote` to go up."))
    held = {str(r) for r in held_ids}
    floor = 1 if to_index is None else max(to_index + 1, 1)
    steps = sorted((e for e in ladder if e["index"] >= floor and e["role_id"] in held),
                   key=lambda e: -e["index"])
    if to_index is None:
        steps = steps[:max(1, int(ranks))]
    roles = []
    for entry in steps:
        role = guild.get_role(int(entry["role_id"]))
        if role is None:
            return DemoteResult("error", cur=cur,
                                error="That role no longer exists. Fix it on the dashboard's "
                                      "**Changes** page.")
        if role >= guild.me.top_role:
            return DemoteResult("error", cur=cur,
                                error=f"{role.mention} is at or above my top role, so I can't "
                                      "remove it.")
        roles.append(role)

    try:
        await member.remove_roles(*roles, reason=audit_reason)
    except discord.Forbidden:
        return DemoteResult("error", cur=cur, error="Missing permission to remove that role.")
    except discord.HTTPException as e:
        return DemoteResult("error", cur=cur, error=f"Discord error: {e}")

    # Where they land = the highest ladder role they still hold.
    removed_ids = {r.id for r in roles}
    remaining = [rid for rid in held_ids if rid not in removed_ids]
    new_idx = core.highest_index(remaining, ladder)
    res = DemoteResult("ok", cur=cur,
                       old_label=_role_label(guild, core.entry_at(ladder, cur)),
                       new_label=_role_label(guild, core.entry_at(ladder, new_idx)))
    res.steps = [_role_label(guild, e) for e in steps]
    # Back on the base rung -> take the extra roles away (silently, overlap-safe).
    res.extras_warning = await sync_extras(
        guild, member, ctype, types, core.crossing(cur, new_idx), remaining, audit_reason)
    nick_warn = await apply_auto_nick(guild, member, ctype, types, new_idx, audit_reason,
                                      role_ids=remaining)
    if nick_warn:
        res.extras_warning = (res.extras_warning + "\n" + nick_warn).strip()
    res.strikes_reset = await _reset_strikes(guild.id, ctype, member.id, "reset_on_demotion")
    res.card_note = await post_staff_change(
        guild, member, "demotion", res.old_label, res.new_label,
        reason=reason, footer=footer, channel_id=ctype.get("channel_id"),
    )
    await _log_history(guild, member, ctype, "demotion", res,
                       reason=reason, by=actor, source=source)
    return res


async def promote_application(guild: discord.Guild, member, change_type_id, decided_by,
                              reason: str = None) -> tuple:
    """Called by applications.py when an application is accepted.

    With a Change type linked, the applicant is promoted in it exactly like /promote, but
    lands on rung 1 or higher (one above their current rung if they already outrank 1).
    Already at the top = nothing happens. No type linked = nothing happens at all.
    Never raises into the accept flow.

    Returns (role_warnings, notes):
      role_warnings - role failures; shown to the reviewer AND put in the applicant's DM
                      (same spot as the old accept-roles warnings)
      notes         - reviewer-only warnings (LOA, type gone, not in the server...)"""
    role_warnings, notes = [], []
    if not change_type_id:
        return role_warnings, notes
    try:
        cfg = await models.get_changes_config(guild.id)
        ctype = core.find_type(cfg["types"], change_type_id)
        if ctype is None:
            notes.append("The linked Change type no longer exists, so the applicant wasn't promoted.")
        elif len(ctype["ladder"]) < 2:
            notes.append(f"The **{ctype['name']}** ladder has fewer than 2 roles, so the applicant "
                         "wasn't promoted.")
        elif member is None:
            notes.append("Applicant isn't in the server, so they weren't promoted.")
        elif await _on_loa(guild.id, member.id):
            notes.append(f"{member.mention} is on LOA, so they weren't promoted. Promote them with "
                         f"`/promote` in **{ctype['name']}** once the LOA ends.")
        else:
            footer = f"{ctype['name']} · Application accepted"
            if decided_by is not None:
                footer += f" · by {decided_by.name}"
            res = await promote_member(
                guild, member, ctype, cfg["types"],
                audit_reason=f"Application accepted by {decided_by}",
                reason=reason, footer=footer, min_target=1,
                actor=decided_by, source="application",
            )
            if res.status == "error":
                role_warnings.append(res.error)
            if res.extras_warning:
                role_warnings.append(res.extras_warning)
    except Exception as e:
        print(f"[changes] application promotion failed: {e}")
        notes.append("Something went wrong promoting the applicant. Use `/promote` instead.")
    return role_warnings, notes


async def _type_autocomplete(interaction: discord.Interaction, current: str):
    if interaction.guild is None:
        return []
    cfg = await models.get_changes_config(interaction.guild.id)
    wanted = (current or "").lower()
    return [app_commands.Choice(name=t["name"][:100], value=t["name"][:100])
            for t in cfg["types"] if wanted in t["name"].lower()][:25]


class Changes(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self._poll_dashboard_tasks.start()

    async def cog_unload(self):
        self._poll_dashboard_tasks.cancel()

    # ── Auto nick: keep nicknames in step with ladder roles ───────────────
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """A ladder role was added/removed (by hand, a role panel, another bot...) ->
        fix the nickname for types with "Auto nick change" on. Only reacts to ROLE
        changes, never to nickname changes, and does nothing if the nick is already right."""
        try:
            if after.bot:
                return
            changed = {r.id for r in before.roles} ^ {r.id for r in after.roles}
            if not changed:
                return
            cfg = await models.get_changes_config(after.guild.id)
            types = cfg["types"]
            watched = {int(rid) for t in types if t.get("auto_nick")
                       for rid in (e["role_id"] for e in t["ladder"])}
            if not (changed & watched):
                return
            await sync_member_nick(after.guild, after, types, "Auto nick change (role changed)")
        except Exception as e:
            print(f"[changes] auto nick listener failed: {e}")

    # ── Dashboard: "Sync nicknames" button ────────────────────────────────
    @tasks.loop(seconds=3)
    async def _poll_dashboard_tasks(self):
        try:
            batch = await models.take_pending_dashboard_tasks(limit=5, types=[SYNC_NICKS_TASK])
            for t in batch:
                error, result = "", ""
                try:
                    guild = self.bot.get_guild(int(t["guild_id"]))
                    if guild is None:
                        error = "The bot isn't in that server."
                    else:
                        result = await self._sync_type_nicks(
                            guild, (t.get("payload") or {}).get("type_id"))
                except Exception as e:
                    error = f"Unexpected error: {e}"
                    print(f"[changes] nick sync task {t.get('_id')} failed: {e}")
                await dashboard_tasks_col().update_one(
                    {"_id": t["_id"]}, {"$set": {"error": error, "result": result}})
        except Exception as e:
            print(f"[changes] dashboard poller error: {e}")

    @_poll_dashboard_tasks.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()
        try:
            await promotion_history_col().create_index([("guild_id", 1), ("at", -1)])
            await promotion_history_col().create_index([("guild_id", 1), ("member_id", 1), ("at", -1)])
        except Exception as e:
            print(f"[changes] history index failed: {e}")

    async def _sync_type_nicks(self, guild: discord.Guild, type_id) -> str:
        cfg = await models.get_changes_config(guild.id)
        types = cfg["types"]
        ctype = core.find_type(types, type_id)
        if ctype is None:
            return "That type no longer exists."
        if not ctype.get("auto_nick"):
            return f"Auto nick change is off for **{ctype['name']}**."
        ladder_ids = {int(e["role_id"]) for e in ctype["ladder"]}
        fixed = same = failed = 0
        for member in list(guild.members):
            if member.bot or not (ladder_ids & {r.id for r in member.roles}):
                continue
            res = await sync_member_nick(guild, member, types, "Auto nick change (sync)")
            if res == "changed":
                fixed += 1
                await asyncio.sleep(1.2)       # stay well inside Discord's rate limits
            elif res == "failed":
                failed += 1
            else:
                same += 1
        return (f"Nick refresh for {ctype['name']}: {fixed} updated, {same} already correct, "
                f"{failed} couldn't be renamed.")

    async def _precheck(self, ctx: commands.Context, member: discord.Member, type_name: str):
        """Shared validation. Returns (type, all_types), or (None, None) after replying with the error."""
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
        if len(ctype["ladder"]) < 2:
            await _reply(ctx, error_embed(
                f"The **{ctype['name']}** ladder isn't set up yet. Add at least 2 roles on the "
                "dashboard's **Changes** page."))
            return None, None
        if member.bot:
            await _reply(ctx, error_embed("You can't do that to a bot."))
            return None, None
        if member.id == ctx.author.id:
            await _reply(ctx, error_embed("You can't promote or demote yourself."))
            return None, None
        if await _on_loa(ctx.guild.id, member.id):
            await _reply(ctx, error_embed(
                f"{member.mention} is on LOA right now. End the LOA first "
                "(their ranks are paused and come back when it ends)."))
            return None, None
        return ctype, cfg["types"]

    # ------------------------------------------------------------------
    # /promote
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="promote",
                             description="Promote a member one level up a type's ladder")
    @app_commands.describe(member="Who to promote", type="Which ladder (staff, builder...)",
                           index="Optional: jump straight to this rung and get every role up to it",
                           reason="Why (optional)")
    @command_check()
    async def promote(self, ctx: commands.Context, member: discord.Member, type: str,
                      index: Optional[int] = None, *, reason: str = None):
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)

        ctype, types = await self._precheck(ctx, member, type)
        if ctype is None:
            return

        res = await promote_member(
            ctx.guild, member, ctype, types,
            audit_reason=f"Promotion ({ctype['name']}) by {ctx.author}"
                         + (f": {reason}" if reason else ""),
            reason=reason, footer=f"{ctype['name']} · By {ctx.author.name}",
            to_index=index, actor=ctx.author, source="command",
        )
        if res.status == "top":
            return await _reply(ctx, info_embed(
                f"{member.mention} is at the highest **{ctype['name']}** role right now "
                f"(**{res.old_label}**)."
            ))
        if res.status == "error":
            return await _reply(ctx, error_embed(res.error))

        if index is not None:
            msg = (f"Promoted {member.mention} from **{res.old_label}** (index {max(res.cur, 0)}) "
                   f"to **{res.new_label}** (index {index}) ({ctype['name']}).")
            if len(res.steps) > 1:
                msg += f"\nRoles given: {' → '.join(res.steps)}"
        else:
            msg = f"Promoted {member.mention} to **{res.new_label}** ({ctype['name']})."
        if res.strikes_reset:
            msg += f"\n🧹 Cleared {res.strikes_reset} strike(s) in **{ctype['name']}**."
        if res.card_note:
            msg += f"\n⚠️ {res.card_note}"
        if res.extras_warning:
            msg += f"\n⚠️ {res.extras_warning}"
        await _reply(ctx, success_embed(msg))

    # ------------------------------------------------------------------
    # /demote
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="demote",
                             description="Demote a member (removes their highest role of a type's ladder)")
    @app_commands.describe(member="Who to demote", type="Which ladder (staff, builder...)",
                           index="Optional: drop straight to this rung and lose every role above it",
                           reason="Why (optional)")
    @command_check()
    async def demote(self, ctx: commands.Context, member: discord.Member, type: str,
                     index: Optional[int] = None, *, reason: str = None):
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)

        ctype, types = await self._precheck(ctx, member, type)
        if ctype is None:
            return
        res = await demote_member(
            ctx.guild, member, ctype, types,
            audit_reason=f"Demotion ({ctype['name']}) by {ctx.author}"
                         + (f": {reason}" if reason else ""),
            reason=reason, footer=f"{ctype['name']} · By {ctx.author.name}",
            to_index=index, actor=ctx.author, source="command",
        )
        if res.status == "excepted":
            return await _reply(ctx, error_embed(
                f"{member.mention} has an exception role for **{ctype['name']}**, so they "
                "can't be demoted. Remove the role from the Changes page first."))
        if res.status == "none":
            return await _reply(ctx, info_embed(
                f"{member.mention} doesn't have any **{ctype['name']}** ladder role to remove."
            ))
        if res.status == "base":
            return await _reply(ctx, info_embed(
                f"{member.mention} is already at the lowest **{ctype['name']}** role "
                f"(**{res.old_label}**) and can't be demoted any lower."
            ))
        if res.status == "error":
            return await _reply(ctx, error_embed(res.error))

        msg = (f"Demoted {member.mention} from **{res.old_label}** to "
               f"**{res.new_label}** ({ctype['name']}).")
        if index is not None and len(res.steps) > 1:
            msg += f"\nRoles removed: {' → '.join(res.steps)}"
        if res.strikes_reset:
            msg += f"\n🧹 Cleared {res.strikes_reset} strike(s) in **{ctype['name']}**."
        if res.card_note:
            msg += f"\n⚠️ {res.card_note}"
        if res.extras_warning:
            msg += f"\n⚠️ {res.extras_warning}"
        await _reply(ctx, success_embed(msg))

    @promote.autocomplete("type")
    async def _promote_type_ac(self, interaction: discord.Interaction, current: str):
        return await _type_autocomplete(interaction, current)

    @demote.autocomplete("type")
    async def _demote_type_ac(self, interaction: discord.Interaction, current: str):
        return await _type_autocomplete(interaction, current)


async def setup(bot: commands.Bot):
    await bot.add_cog(Changes(bot))
