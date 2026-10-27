"""
Ticket system with ticket types.

/ticket rename <name>
/ticket close [reason]       -> opens a modal for a reason (optional)
/ticket request-close [reason] -> posts an embed pinging the ticket opener with
                                Close Ticket / Don't Close buttons (only the opener
                                can press them)
/ticket add <member or role>
/ticket remove <member or role>
/ticket move                 -> staff pick a type; bot asks the ticket creator to confirm;
                                creator clicks Move -> modal with the target type's
                                questions -> on submit, ticket is moved + re-welcomed
/ticket panel                -> select a panel; posts its embed + buttons immediately
/ticket lookup <member or text> -> staff: a member's past tickets (mention / ID) or a search
                                by ticket name, person or reason; summary + dashboard links

Panel buttons open a modal with the type's questions. Every ticket also gets a
Close Ticket button which opens a reason modal. Blank reason -> "No Further Action Required".
"""
import asyncio
import json
import mimetypes
import re
from datetime import datetime, timezone
from typing import Literal, Union

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import models
from database.mongo import tickets_col
from utils.embeds import success_embed, error_embed, info_embed, base_embed
from utils.permissions import command_check
from utils import ticket_autoclose as ac
from utils import ticket_container as tcont
from utils import ticket_core
from utils import ticket_stats
from utils import ticket_settings as ts
from config import constants
from config.settings import dashboard_base_url


DEFAULT_CLOSE_REASON = "No Further Action Required"

BUTTON_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------

async def _allowed_role_ids(guild: discord.Guild, ticket_doc: dict) -> set:
    """Roles that count as staff for this ticket: the server's support roles plus
    the allowed roles of whatever created the ticket (type / claim / spawner /
    application)."""
    settings = await models.get_guild_settings(guild.id)
    allowed = {str(s) for s in (settings.get("tickets", {}).get("support_role_ids", []) or [])}

    kind = ticket_doc.get("kind")
    if kind == "claim":
        cfg = await models.get_claim_ticket_config(guild.id)
        allowed |= {str(r) for r in cfg["allow_role_ids"]}
    elif kind == "spawner":
        cfg = await models.get_spawner_config(guild.id)
        allowed |= {str(r) for r in cfg["allow_role_ids"]}
    elif kind == "application":
        try:
            app = await models.get_application(guild.id, ticket_doc.get("application_id"))
        except Exception:
            app = None
        if app:
            allowed |= {str(r) for r in (app.get("ticket_allow_role_ids") or [])}
    elif ticket_doc.get("ticket_type_id"):
        try:
            tt = await models.get_ticket_type(ticket_doc["ticket_type_id"])
        except Exception:
            tt = None
        if tt:
            allowed |= {str(r) for r in (tt.get("allow_role_ids") or [])}
    return allowed


async def _is_ticket_staff(guild: discord.Guild, member: discord.Member,
                           ticket_doc: dict) -> bool:
    """The one rule for every button inside a ticket (Close, Claim, Unclaim, Change
    Amount) on every ticket type (normal, claim, spawner, application, giveaway):
    administrators, or someone holding one of the ticket's allowed roles.
    Nobody else - not even the person who opened the ticket."""
    if member.guild_permissions.administrator:
        return True
    allowed = await _allowed_role_ids(guild, ticket_doc)
    return bool(allowed & {str(r.id) for r in member.roles})


async def _can_close_ticket(guild: discord.Guild, member: discord.Member,
                            ticket_doc: dict) -> bool:
    return await _is_ticket_staff(guild, member, ticket_doc)


async def _can_claim_ticket(guild: discord.Guild, member: discord.Member,
                            ticket_doc: dict) -> bool:
    """Same staff rule, but a non-admin can't claim a ticket they opened themselves."""
    if member.guild_permissions.administrator:
        return True
    if str(member.id) == str(ticket_doc.get("opener_id")):
        return False
    return await _is_ticket_staff(guild, member, ticket_doc)


# ---------------------------------------------------------------------------
# Category resolution (dashboard can store "__new__:<name>")
# ---------------------------------------------------------------------------

_category_locks: dict[int, asyncio.Lock] = {}


def category_lock(guild_id: int) -> asyncio.Lock:
    lock = _category_locks.get(guild_id)
    if lock is None:
        lock = _category_locks[guild_id] = asyncio.Lock()
    return lock


def find_category_by_name(guild: discord.Guild, name: str):
    wanted = (name or "").strip().lower()
    if not wanted:
        return None
    for cat in guild.categories:
        if cat.name.strip().lower() == wanted:
            return cat
    return None


async def _resolve_category(guild: discord.Guild, ticket_type: dict):
    raw = ticket_type.get("category_id")
    if not raw:
        return None

    if isinstance(raw, str) and raw.startswith("__new__:"):
        name = raw[len("__new__:"):].strip() or "Tickets"
        category = find_category_by_name(guild, name)
        if category is None:
            try:
                category = await guild.create_category(name=name, reason="Ticket type setup")
            except discord.HTTPException:
                return None
        await models.update_ticket_type(str(ticket_type["_id"]), {"category_id": str(category.id)})
        return category

    try:
        category = guild.get_channel(int(raw))
    except (TypeError, ValueError):
        category = None
    return category if isinstance(category, discord.CategoryChannel) else None


async def _cleanup_empty_category(guild: discord.Guild, category, gone_ids=()):
    if category is None:
        return
    try:
        async with category_lock(guild.id):
            category = guild.get_channel(category.id)
            if not isinstance(category, discord.CategoryChannel):
                return
            if any(c.id not in gone_ids for c in category.channels):
                return

            cid = str(category.id)
            pending = f"__new__:{category.name}"
            all_types = await models.get_ticket_types(guild.id)
            main_types = [t for t in all_types if str(t.get("category_id")) == cid]
            spare_types = [t for t in all_types
                           if cid in [str(x) for x in (t.get("overflow_category_ids") or [])]]
            claim_cfg = await models.get_claim_ticket_config(guild.id)
            claim_uses_it = str(claim_cfg.get("category_id")) == cid
            claim_spares = [str(x) for x in (claim_cfg.get("overflow_category_ids") or [])]
            claim_spare_uses_it = cid in claim_spares
            spawner_cfg = await models.get_spawner_config(guild.id)
            spawner_uses_it = str(spawner_cfg.get("category_id")) == cid
            spawner_spares = [str(x) for x in (spawner_cfg.get("overflow_category_ids") or [])]
            spawner_spare_uses_it = cid in spawner_spares
            apps = [a for a in await models.get_applications(guild.id)
                    if str(a.get("ticket_category_id")) == cid]
            fixed_entry, server_spares = await ticket_core.server_category_block(guild.id)
            fixed_uses_it = fixed_entry == cid
            server_spare_uses_it = cid in server_spares
            if not (main_types or spare_types or claim_uses_it or spawner_uses_it or apps
                    or fixed_uses_it or server_spare_uses_it
                    or claim_spare_uses_it or spawner_spare_uses_it):
                return

            # Main, spare and server-wide categories all work the same: whoever used this
            # category is switched back to "__new__:<name>" (created again when needed).
            for t in main_types:
                await models.update_ticket_type(str(t["_id"]), {"category_id": pending})
            for t in spare_types:
                spares = [pending if str(x) == cid else str(x) for x in (t.get("overflow_category_ids") or [])]
                await models.update_ticket_type(str(t["_id"]), {"overflow_category_ids": spares})
            for a in apps:
                await models.update_application(guild.id, str(a["_id"]), {"ticket_category_id": pending})
            if claim_uses_it:
                await models.update_guild_settings(guild.id, {"claim_tickets.category_id": pending})
            if spawner_uses_it:
                await models.update_guild_settings(guild.id, {"spawners.category_id": pending})
            if claim_spare_uses_it:
                await models.update_guild_settings(guild.id, {
                    "claim_tickets.overflow_category_ids": [pending if x == cid else x for x in claim_spares]})
            if spawner_spare_uses_it:
                await models.update_guild_settings(guild.id, {
                    "spawners.overflow_category_ids": [pending if x == cid else x for x in spawner_spares]})
            if fixed_uses_it:
                await models.update_guild_settings(guild.id, {"tickets.fixed_category_id": pending})
            if server_spare_uses_it:
                await models.update_guild_settings(guild.id, {
                    "tickets.overflow_category_ids": [pending if x == cid else x for x in server_spares]})

            try:
                await category.delete(reason="Ticket category is empty")
            except discord.HTTPException:
                pass
    except Exception as e:
        print(f"[tickets] category cleanup failed: {e}")


# ---------------------------------------------------------------------------
# Shared overwrite builder
# ---------------------------------------------------------------------------

def _ticket_overwrites(guild: discord.Guild, opener: discord.Member, ticket_type: dict) -> dict:
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True
        ),
    }
    if opener is not None:
        overwrites[opener] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )
    for role_id in ticket_type.get("allow_role_ids", []) or []:
        try:
            role = guild.get_role(int(role_id))
        except (TypeError, ValueError):
            role = None
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )
    return overwrites


def _name_slug(text, limit: int = None) -> str:
    """Channel-name-safe: lowercase, spaces -> dashes, only letters/digits/_/-."""
    text = re.sub(r"\s+", "-", str(text or "").strip().lower())
    text = re.sub(r"[^\w-]", "", text)
    text = re.sub(r"-{2,}", "-", text)
    if limit:
        text = text[:limit]
    return text.strip("-")


def _question_required(ticket_type: dict, index: int) -> bool:
    """Per-question Required flag from the dashboard (older types: only question 1)."""
    flags = ticket_type.get("questions_required")
    if isinstance(flags, list) and index < len(flags):
        return bool(flags[index])
    return index == 0


def _auto_rename_name(ticket_type: dict, user: discord.abc.User, answers: list, number=None):
    """Channel name from the type's Auto rename pattern, or None if it has none / it
    comes out empty. Tokens: {q1}-{q5} (answers), {username}, {user}, {type} / {panel},
    {number} (0007), {id} (the member's ID). Empty answers are skipped (no double dashes)."""
    pattern = (ticket_type.get("auto_rename") or "").strip()
    if not pattern:
        return None
    answers = answers or []

    def fill(m):
        key = m.group(1).lower()
        if re.fullmatch(r"q[1-5]", key):
            i = int(key[1]) - 1
            value = answers[i] if i < len(answers) else ""
            return _name_slug(value, 40)
        values = {
            "username": getattr(user, "name", ""),
            "user": getattr(user, "display_name", "") or getattr(user, "name", ""),
            "type": ticket_type.get("name", ""),
            "panel": ticket_type.get("name", ""),
            "number": ts.format_number(number),
            "id": str(getattr(user, "id", "")),
        }
        return _name_slug(values.get(key, ""), 40)

    name = _name_slug(re.sub(r"\{(\w+)\}", fill, pattern), 100)
    return name or None


def _channel_name_for(ticket_type: dict, user: discord.abc.User, answers: list = None,
                      number=None) -> str:
    """Auto rename pattern if the type has one, else "<type>-<username>".
    (The type's emoji is only used on the panel button, never in the channel name.)"""
    return (
        _auto_rename_name(ticket_type, user, answers, number)
        or _name_slug(f"{ticket_type['name']}-{user.name}", 100)
        or "ticket"
    )


async def _rename_channel_quietly(channel: discord.TextChannel, name: str, reason: str):
    """Discord allows ~2 renames per 10 min per channel: try briefly, never hang."""
    if channel.name == name:
        return
    try:
        await asyncio.wait_for(channel.edit(name=name, reason=reason), timeout=10)
    except Exception as e:
        print(f"[tickets] couldn't rename {channel.id}: {e}")


async def _rename_ok(channel, name: str, reason: str) -> bool:
    """Like _rename_channel_quietly, but says whether the rename went through (Discord
    allows ~2 renames per 10 min per channel, so it can fail). Works on a thread too."""
    if channel.name == name:
        return True
    try:
        await asyncio.wait_for(channel.edit(name=name, reason=reason), timeout=10)
        return True
    except Exception as e:
        print(f"[tickets] couldn't rename {channel.id}: {e}")
        return False


async def _sync_channel_perms(channel: discord.TextChannel, guild: discord.Guild,
                              opener: discord.Member, ticket_type: dict) -> None:
    new_overwrites = _ticket_overwrites(guild, opener, ticket_type)
    existing_targets = set(channel.overwrites.keys())
    new_targets = set(new_overwrites.keys())
    for target in existing_targets - new_targets:
        try:
            await channel.set_permissions(target, overwrite=None)
        except discord.HTTPException:
            pass
    for target, ow in new_overwrites.items():
        try:
            await channel.set_permissions(target, overwrite=ow)
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------------------
# Welcome embed builder (used at open + after move)
# ---------------------------------------------------------------------------

def _placeholders(guild, opener, channel, ticket_type: dict, number=None) -> dict:
    """Values for {user} {username} {server} {channel} {panel} {number} in the panel's
    welcome / "ticket created" messages."""
    return {
        "user": opener.mention if opener else "",
        "username": getattr(opener, "name", "") if opener else "",
        "server": guild.name if guild else "",
        "channel": channel.mention if channel else "",
        "panel": ticket_type.get("name", ""),
        "number": ts.format_number(number),
    }


async def _linked_type_names(ticket_type: dict) -> list:
    """Names of the panels this one is linked to (deleted ones are skipped)."""
    names = []
    for tid in ts.settings_for(ticket_type)["linked_type_ids"][:ts.MAX_LINKED]:
        try:
            linked = await models.get_ticket_type(tid)
        except Exception:
            linked = None
        if linked:
            names.append(f"{linked.get('emoji') or '🎫'} {linked['name']}")
    return names


def _build_welcome_embed(ticket_type: dict, opener: discord.Member, answers: list, *,
                         guild=None, channel=None, number=None, linked_names=None,
                         priority=None) -> discord.Embed:
    color = ticket_type.get("color") or 0x5865F2
    questions = (ticket_type.get("questions") or [])[:5] or ["Describe your issue"]

    custom = ts.settings_for(ticket_type)["welcome_message"].strip()
    if custom:
        description = ts.render(custom, _placeholders(guild, opener, channel, ticket_type, number))[:4000]
    else:
        description = (
            f"Ticket opened by {opener.mention}.\n"
            "Use the button below to close it when resolved."
        )
    embed = discord.Embed(
        title=f"{ticket_type.get('emoji', '🎫')} {ticket_type['name']}",
        description=description,
        color=color,
    )
    for i, q in enumerate(questions):
        ans = answers[i] if i < len(answers) and answers[i] else "*(no answer)*"
        embed.add_field(name=f"{i + 1}. {q}"[:250], value=str(ans)[:1000], inline=False)
    if linked_names:
        embed.add_field(name="🔗 Related panels", value=", ".join(linked_names)[:1000], inline=False)
    if priority:
        embed.insert_field_at(0, name="Priority", value=ts.priority_label(priority), inline=True)
    return embed


def _ping_content(ticket_type: dict, guild: discord.Guild, opener: discord.Member) -> str:
    content = opener.mention if opener else ""
    if ticket_type.get("ping_role_id"):
        try:
            role = guild.get_role(int(ticket_type["ping_role_id"]))
        except (TypeError, ValueError):
            role = None
        if role:
            content = (content + f" {role.mention}").strip()
    return content


# ---------------------------------------------------------------------------
# Close-ticket flow
# ---------------------------------------------------------------------------

def _format_duration(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = [(days, "d"), (hours, "h"), (minutes, "m"), (secs, "s")]
    parts = [f"{v}{u}" for v, u in parts if v]
    return " ".join(parts[:2]) if parts else "0s"


async def _dm_ticket_closed(guild: discord.Guild, channel: discord.TextChannel,
                            ticket_doc: dict, closer: discord.Member, reason: str,
                            transcript_id: str = None):
    opener_id = ticket_doc.get("opener_id")
    if not opener_id:
        return
    opener = guild.get_member(int(opener_id))
    if opener is None:
        try:
            opener = await guild.fetch_member(int(opener_id))
        except (discord.NotFound, discord.HTTPException):
            return

    opened_at = channel.created_at
    open_for = _format_duration((discord.utils.utcnow() - opened_at).total_seconds())

    embed = discord.Embed(title="🔒 Ticket Closed", color=0xD72638,
                          timestamp=discord.utils.utcnow())
    embed.add_field(name="🎫 Ticket", value=f"`{channel.name}`", inline=False)
    embed.add_field(name="👤 Opened by", value=opener.mention, inline=False)
    embed.add_field(name="🔒 Closed by", value=closer.mention, inline=False)
    embed.add_field(name="🕒 Opened", value=discord.utils.format_dt(opened_at, "R"), inline=False)
    embed.add_field(name="⏳ Open for", value=open_for, inline=False)
    embed.add_field(name="❓ Reason", value=reason[:1000], inline=False)
    embed.set_footer(text=guild.name, icon_url=guild.icon.url if guild.icon else None)

    # Ticket rating: 1-5 buttons, credited to whoever claimed the ticket (see cogs/ticket_ratings.py).
    view = None
    try:
        from cogs.ticket_ratings import build_rating_view
        view = await build_rating_view(guild, opener, ticket_doc, channel.name, transcript_id)
    except Exception as e:
        print(f"[tickets] couldn't prepare the rating buttons: {e}")
    if view is not None:
        embed.add_field(name="⭐ How did we do?",
                        value="Rate how this ticket was handled, 1 (poor) to 5 (great).",
                        inline=False)

    # Panels set to "staff + opener" let the member read their own transcript (Discord login).
    try:
        base = dashboard_base_url()
        if transcript_id and base:
            tt = await _ticket_type_of(ticket_doc)
            if ts.settings_for(tt)["transcript_access"] == "opener":
                embed.add_field(
                    name="📄 Transcript",
                    value=f"[Read this ticket's transcript]({base}/guild/{guild.id}/transcripts/{transcript_id})",
                    inline=False)
    except Exception as e:
        print(f"[tickets] couldn't add the transcript link: {e}")

    try:
        if view is not None:
            await opener.send(embed=embed, view=view)
        else:
            await opener.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        if view is not None:
            from cogs.ticket_ratings import discard_rating
            await discard_rating(view)


# ---------------------------------------------------------------------------
# Transcripts (structured; rendered by the dashboard)
# ---------------------------------------------------------------------------

_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_MAX_IMAGE_BYTES = 8 * 1024 * 1024            # per image
_MAX_TRANSCRIPT_IMAGE_BYTES = 50 * 1024 * 1024  # all images in one transcript
_MAX_DOC_BYTES = 14 * 1000 * 1000             # Mongo's limit is 16 MB per document
_NORMAL_MESSAGE_TYPES = {
    discord.MessageType.default, discord.MessageType.reply,
    discord.MessageType.chat_input_command, discord.MessageType.context_menu_command,
}


def _serialize_embed(e: discord.Embed) -> dict:
    return {
        "title": e.title,
        "description": e.description,
        "url": e.url,
        "color": f"#{e.color.value:06x}" if e.color else None,
        "author": e.author.name if e.author and e.author.name else None,
        "fields": [{"name": f.name, "value": f.value, "inline": bool(f.inline)} for f in e.fields],
        "footer": e.footer.text if e.footer and e.footer.text else None,
        "image": e.image.url if e.image and e.image.url else None,
        "thumbnail": e.thumbnail.url if e.thumbnail and e.thumbnail.url else None,
    }


async def _build_transcript_messages(channel: discord.TextChannel, transcript_id: str):
    """Walk the channel and return (messages, saved_image_count).

    Images are downloaded NOW (the channel is about to be deleted and Discord's
    attachment links expire) and stored in GridFS. A failure to save an image
    never blocks closing the ticket."""
    out = []
    saved_images = 0
    total_bytes = 0

    async for msg in channel.history(limit=None, oldest_first=True):
        author = msg.author
        color = None
        if isinstance(author, discord.Member) and author.color.value:
            color = f"#{author.color.value:06x}"

        reply = None
        ref = msg.reference.resolved if msg.reference else None
        if isinstance(ref, discord.Message):
            reply = {
                "author": ref.author.display_name,
                "snippet": (ref.clean_content or "[attachment or embed]")[:100],
            }

        atts = []
        for att in msg.attachments:
            ctype = (att.content_type or mimetypes.guess_type(att.filename)[0] or "")
            ctype = ctype.split(";")[0].strip().lower()
            entry = {
                "filename": att.filename, "size": att.size, "content_type": ctype,
                "is_image": ctype in _IMAGE_TYPES, "file_id": None,
            }
            if (entry["is_image"] and att.size <= _MAX_IMAGE_BYTES
                    and total_bytes + att.size <= _MAX_TRANSCRIPT_IMAGE_BYTES):
                try:
                    data = await att.read()
                    entry["file_id"] = await models.save_transcript_file(
                        transcript_id, channel.guild.id, att.filename, ctype, data
                    )
                    total_bytes += len(data)
                    saved_images += 1
                except Exception as e:  # never block closing on an image
                    print(f"[tickets] couldn't save transcript image {att.filename}: {e}")
            atts.append(entry)

        out.append({
            "id": str(msg.id),
            "author": {
                "id": str(author.id),
                "name": author.display_name,
                "avatar": author.display_avatar.with_size(64).url,
                "bot": author.bot,
                "color": color,
            },
            "at": msg.created_at.isoformat(),
            "edited": bool(msg.edited_at),
            "content": msg.clean_content or "",
            "system": (msg.system_content or "") if msg.type not in _NORMAL_MESSAGE_TYPES else None,
            "reply": reply,
            "embeds": [_serialize_embed(e) for e in msg.embeds],
            "attachments": atts,
        })
    return out, saved_images


def _fit_transcript(messages: list):
    """Drop the oldest messages if the transcript would exceed Mongo's doc limit."""
    trimmed = 0
    size = len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
    while size > _MAX_DOC_BYTES and len(messages) > 1:
        drop = max(1, len(messages) // 10)
        messages = messages[drop:]
        trimmed += drop
        size = len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
    return messages, trimmed


async def _resolve_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.HTTPException):
            return str(user_id)
    return member.name


def _transcript_embed(guild: discord.Guild, channel: discord.TextChannel, *,
                      opener_id: int, opener_name: str, closer: discord.Member,
                      reason: str, message_count: int, image_count: int,
                      transcript_id: str) -> discord.Embed:
    opened_at = channel.created_at
    open_for = _format_duration((discord.utils.utcnow() - opened_at).total_seconds())

    embed = discord.Embed(title="🎫 Ticket Closed", color=0xD72638,
                          timestamp=discord.utils.utcnow())
    embed.add_field(name="🎫 Ticket", value=f"`{channel.name}`", inline=True)
    embed.add_field(name="👤 Opened by",
                    value=f"<@{opener_id}>\n`{opener_name}`", inline=True)
    embed.add_field(name="🔒 Closed by",
                    value=f"{closer.mention}\n`{closer.name}`", inline=True)
    embed.add_field(name="❓ Reason", value=(reason or DEFAULT_CLOSE_REASON)[:1000], inline=False)
    embed.add_field(name="🕒 Opened", value=discord.utils.format_dt(opened_at, "R"), inline=True)
    embed.add_field(name="⏳ Open for", value=open_for, inline=True)
    msgs = f"{message_count}" + (f" ({image_count} image{'s' if image_count != 1 else ''} saved)"
                                 if image_count else "")
    embed.add_field(name="💬 Messages", value=msgs, inline=True)
    embed.set_footer(text=f"Transcript ID: {transcript_id}",
                     icon_url=guild.icon.url if guild.icon else None)
    return embed


async def close_ticket_with_reason(guild: discord.Guild, channel,
                                   closer: discord.Member, reason: str):
    """`channel` is the ticket's text channel or private thread."""
    if not reason or not reason.strip():
        reason = DEFAULT_CLOSE_REASON

    ticket_doc = await models.get_ticket(channel.id)
    if not ticket_doc:
        return

    # An archived thread arrives as a bare stub; swap in the real one. It can be read and
    # deleted while archived, so there's no need to reopen it.
    channel = await tcont.resolve(guild, channel)

    ticket_type = await _ticket_type_of(ticket_doc)
    save_transcript = ts.settings_for(ticket_type)["save_transcripts"]

    opener_id = int(ticket_doc["opener_id"])
    opener_name = await _resolve_name(guild, opener_id)

    claimer_id = ticket_doc.get("claimed_by")
    claimer_name = await _resolve_name(guild, int(claimer_id)) if claimer_id else None

    if save_transcript:
        transcript_oid = models.new_transcript_id()
        messages, image_count = await _build_transcript_messages(channel, str(transcript_oid))
        messages, trimmed = _fit_transcript(messages)
        transcript_id = await models.save_structured_transcript(
            transcript_oid, guild.id, channel.id, channel.name,
            opener_id, closer.id, reason, opener_name, closer.name, messages, trimmed,
            claimed_by_id=claimer_id, claimed_by_name=claimer_name,
            panel_name=ticket_doc.get("panel_name"),
            priority=ts.clean_priority(ticket_doc.get("priority")),
            ticket_type_id=str(ticket_doc["ticket_type_id"]) if ticket_doc.get("ticket_type_id") else None,
        )
    else:
        # this panel doesn't keep transcripts (ratings and the log line still work)
        messages, image_count, transcript_id = [], 0, None
    await models.close_ticket(channel.id)
    await ticket_stats.record_close(ticket_doc, closer, claimer_name)

    # Taskbooks: closed counter.
    try:
        from cogs.taskbook import record_ticket_close as tb_close
        await tb_close(guild, closer)
    except Exception as e:
        print(f"[tickets] taskbook settle failed: {e}")

    settings = await models.get_guild_settings(guild.id)
    ticket_cfg = settings.get("tickets", {})
    logging_cfg = settings.get("logging", {})

    transcript_channel_id = (
        logging_cfg.get("transcript_channel_id")
        or logging_cfg.get("log_channel_id")
        or ticket_cfg.get("transcript_channel_id")
    )
    if transcript_channel_id and transcript_id:
        transcript_channel = guild.get_channel(int(transcript_channel_id))
        if transcript_channel:
            embed = _transcript_embed(
                guild, channel, opener_id=opener_id, opener_name=opener_name,
                closer=closer, reason=reason, message_count=len(messages),
                image_count=image_count, transcript_id=transcript_id,
            )
            view = None
            base = dashboard_base_url()
            if base:
                view = discord.ui.View()
                view.add_item(discord.ui.Button(
                    style=discord.ButtonStyle.link, label="View Transcript", emoji="📄",
                    url=f"{base}/guild/{guild.id}/transcripts/{transcript_id}",
                ))
            try:
                await transcript_channel.send(embed=embed, view=view)
            except (discord.Forbidden, discord.HTTPException) as e:
                print(f"[tickets] couldn't post transcript embed: {e}")

    log_channel_id = ticket_cfg.get("log_channel_id")
    if log_channel_id:
        log_channel = guild.get_channel(int(log_channel_id))
        if log_channel:
            await log_channel.send(embed=info_embed(
                f"🔒 Ticket **#{channel.name}** closed by {closer.mention}. Reason: {reason}"
            ))

    await _dm_ticket_closed(guild, channel, ticket_doc, closer, reason, transcript_id)

    in_thread = tcont.is_thread(channel)
    category = None if in_thread else channel.category      # a thread's "category" is its parent's
    if not await tcont.delete_container(channel, f"Ticket closed by {closer}: {reason}"):
        return

    if not in_thread:
        await _cleanup_empty_category(guild, category, {channel.id})


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class QuestionModal(discord.ui.Modal):
    def __init__(self, ticket_type: dict, panel_name: str):
        super().__init__(title=f"{ticket_type.get('emoji', '🎫')} {ticket_type['name']}"[:45])
        self.ticket_type = ticket_type
        self.panel_name = panel_name

        questions = (ticket_type.get("questions") or [])[:5]
        if not questions:
            questions = ["Describe your issue"]

        for i, q in enumerate(questions, 1):
            self.add_item(discord.ui.TextInput(
                label=q[:45],
                placeholder="Type your answer here...",
                style=discord.TextStyle.paragraph,
                required=_question_required(ticket_type, i - 1),
                max_length=1000,
                custom_id=f"q{i}",
            ))

    async def on_submit(self, interaction: discord.Interaction):
        await _create_ticket_from_modal(
            interaction, self.ticket_type, self.panel_name,
            [child.value for child in self.children],
        )


class CloseReasonModal(discord.ui.Modal):
    def __init__(self, guild: discord.Guild, channel: discord.TextChannel,
                 closer: discord.Member):
        super().__init__(title="Close Ticket")
        self.guild = guild
        self.channel = channel
        self.closer = closer

        self.add_item(discord.ui.TextInput(
            label="Reason (optional)",
            placeholder=DEFAULT_CLOSE_REASON,
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        reason = (self.children[0].value or "").strip() or DEFAULT_CLOSE_REASON
        await interaction.response.send_message(
            embed=info_embed("Closing ticket and generating transcript..."),
            ephemeral=True,
        )
        await close_ticket_with_reason(self.guild, self.channel, self.closer, reason)


class MoveQuestionsModal(discord.ui.Modal):
    def __init__(self, new_type: dict, ticket_channel: discord.TextChannel,
                 opener: discord.Member, old_welcome_message_id: int,
                 approval_message_id: int = None):
        super().__init__(title=f"Move → {new_type.get('name', 'Ticket')}"[:45])
        self.new_type = new_type
        self.ticket_channel = ticket_channel
        self.opener = opener
        self.old_welcome_message_id = old_welcome_message_id
        self.approval_message_id = approval_message_id

        questions = (new_type.get("questions") or [])[:5]
        if not questions:
            questions = ["Describe your issue"]

        for i, q in enumerate(questions, 1):
            self.add_item(discord.ui.TextInput(
                label=q[:45],
                placeholder="Type your answer here...",
                style=discord.TextStyle.paragraph,
                required=_question_required(new_type, i - 1),
                max_length=1000,
                custom_id=f"q{i}",
            ))

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel = self.ticket_channel
        new_type = self.new_type
        opener = self.opener

        ticket_doc = await models.get_ticket(channel.id)
        if not ticket_doc:
            return await interaction.response.send_message(
                embed=error_embed("This is no longer an active ticket."),
                ephemeral=True,
            )

        await _sync_channel_perms(channel, guild, opener, new_type)

        old_category = channel.category
        moved = False
        full = None
        async with category_lock(guild.id):
            category = await _resolve_category(guild, new_type)
            if category is None:
                category, overflow = await ticket_core.kind_category_plan(guild, None)
                owner = ("server", guild.id)
            else:
                overflow = ticket_core.clean_overflow_ids(
                    new_type.get("overflow_category_ids"), category.id
                )
                owner = ("type", str(new_type["_id"]))
            category, all_full, tried = await ticket_core.first_with_room(
                guild, category, overflow, old_category, owner)
            if all_full:
                # every category is full: the ticket stays where it is
                full = tried
            else:
                try:
                    await channel.edit(
                        category=category,
                        reason=f"Ticket moved to type '{new_type['name']}' by {interaction.user}",
                    )
                    moved = True
                except discord.HTTPException:
                    pass
        if full is not None:
            await ticket_core.alert_staff_full(guild, full, "moved ticket")
        if moved and old_category is not None and (category is None or category.id != old_category.id):
            await _cleanup_empty_category(guild, old_category, {channel.id})

        await models.update_ticket(
            channel.id,
            ticket_type_id=str(new_type["_id"]),
            panel_name=new_type["name"],
        )

        if self.old_welcome_message_id:
            try:
                old_msg = await channel.fetch_message(int(self.old_welcome_message_id))
                await old_msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        if self.approval_message_id:
            try:
                approval_msg = await channel.fetch_message(int(self.approval_message_id))
                await approval_msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        answers = [child.value for child in self.children]
        prio = ts.clean_priority(ticket_doc.get("priority")) or (
            "normal" if ts.settings_for(new_type)["priority_enabled"] else None)
        if prio and not ticket_doc.get("priority"):
            await models.update_ticket(channel.id, priority=prio)
        new_embed = _build_welcome_embed(
            new_type, opener, answers, guild=guild, channel=channel,
            number=ticket_doc.get("number"), linked_names=await _linked_type_names(new_type),
            priority=prio,
        )
        new_ping = _ping_content(new_type, guild, opener)

        new_msg = await _send_welcome(channel, new_ping, new_embed, new_type)
        await models.update_ticket(channel.id, welcome_message_id=str(new_msg.id))

        await interaction.response.send_message(
            embed=success_embed(
                f"Ticket moved to **{new_type['name']}** — permission and category synced."
            ),
            ephemeral=True,
        )

        # Only if the new type has an Auto rename pattern (otherwise the name is left alone).
        new_name = _auto_rename_name(new_type, opener, answers, ticket_doc.get("number"))
        if new_name:
            # keep the priority prefix / emoji if the new panel uses one
            prefix = ts.priority_prefix(prio, ts.settings_for(new_type)["priority_rename"])
            new_name = (prefix + new_name)[:100]
            if await _rename_ok(channel, new_name, f"Ticket moved to type '{new_type['name']}'"):
                await models.update_ticket(channel.id, priority_prefix=prefix)


# ---------------------------------------------------------------------------
# Panel buttons + views
# ---------------------------------------------------------------------------

async def _open_gate_error(guild: discord.Guild, member, ticket_type: dict):
    """Why this member can't open a ticket of this type right now, or None.

    1. Required roles (per panel): they need at least one. If every listed role was deleted
       the rule is ignored, so a deleted role can never lock everybody out.
    2. Open-ticket limit: with a per-panel limit, only tickets of THIS panel count; with
       none set the old rule applies (one open ticket per member across the server)."""
    cfg = ts.settings_for(ticket_type)

    req_roles = []
    for rid in cfg["required_role_ids"]:
        try:
            role = guild.get_role(int(rid))
        except (TypeError, ValueError):
            role = None
        if role:
            req_roles.append(role)
    if req_roles:
        have = {r.id for r in getattr(member, "roles", [])}
        if not any(r.id in have for r in req_roles):
            return "You need one of these roles to open this ticket: " + ", ".join(
                r.mention for r in req_roles[:10])

    limit = cfg["max_open_tickets"]
    if limit:
        docs = await models.get_open_tickets_for_user_type(guild.id, member.id, str(ticket_type["_id"]))
        live = []
        for d in docs:
            found = await tcont.get_container(guild, d["channel_id"])   # finds archived threads too
            if found is not None:
                live.append((d, found))
        if len(live) >= limit:
            ch = live[0][1]
            if limit == 1:
                return f"You already have an open ticket: {ch.mention}"
            return f"You already have {len(live)} open tickets here (max {limit}): {ch.mention}"
    else:
        existing = await models.get_open_ticket_for_user(guild.id, member.id)
        if existing:
            ch = await tcont.get_container(guild, existing["channel_id"])
            if ch is not None:
                return f"You already have an open ticket: {ch.mention}"
    return None


async def _start_ticket_flow(interaction: discord.Interaction, type_id: str, panel_name: str):
    """What happens when a member picks a ticket type, from a button OR a dropdown:
    type still exists, open-ticket limit + required roles, blacklist check, then the
    question modal."""
    guild = interaction.guild
    fresh = await models.get_ticket_type(str(type_id))
    if not fresh:
        return await interaction.response.send_message(
            embed=error_embed("This ticket type no longer exists."), ephemeral=True
        )

    problem = await _open_gate_error(guild, interaction.user, fresh)
    if problem:
        return await interaction.response.send_message(
            embed=error_embed(problem), ephemeral=True
        )

    # Blacklist check
    blacklist = await models.get_blacklist_config(guild.id)
    bl_role_id = blacklist["ticket_role_id"]
    if bl_role_id and not fresh.get("allow_blacklisted", False):
        member = guild.get_member(interaction.user.id)
        if member and any(str(r.id) == str(bl_role_id) for r in member.roles):
            return await interaction.response.send_message(
                embed=error_embed("You're blacklisted from opening tickets."),
                ephemeral=True,
            )

    await interaction.response.send_modal(QuestionModal(fresh, panel_name))


class OpenTicketButton(discord.ui.Button):
    def __init__(self, ticket_type: dict, panel_name: str):
        super().__init__(
            label=(ts.settings_for(ticket_type)["button_label"] or ticket_type["name"])[:80],
            emoji=ticket_type.get("emoji") or None,
            style=BUTTON_STYLES.get(ticket_type.get("button_style", "primary"),
                                    discord.ButtonStyle.primary),
            custom_id=f"open_ticket_type:{ticket_type['_id']}",
        )
        self.ticket_type = ticket_type
        self.panel_name = panel_name

    async def callback(self, interaction: discord.Interaction):
        await _start_ticket_flow(interaction, str(self.ticket_type["_id"]), self.panel_name)


MAX_PANEL_BUTTONS = 5
MAX_PANEL_DROPDOWN = 25     # a Discord select menu holds 25 options


class TicketPanelSelect(discord.ui.Select):
    """Dropdown version of a panel: one option per ticket type, with its description."""

    def __init__(self, ticket_types: list, panel_name: str, panel_id: str = None,
                 placeholder: str = None):
        options = []
        for tt in ticket_types[:MAX_PANEL_DROPDOWN]:
            options.append(discord.SelectOption(
                label=tt["name"][:100],
                value=str(tt["_id"]),
                description=((tt.get("description") or "").strip()[:100] or None),
                emoji=tt.get("emoji") or None,
            ))
        super().__init__(
            placeholder=(placeholder or "Select a ticket type...")[:150],
            options=options,
            min_values=1, max_values=1,
            # one stable id per panel so the menu keeps working after a restart
            custom_id=f"ticket_panel_select:{panel_id or panel_name}",
        )
        self.panel_name = panel_name

    async def callback(self, interaction: discord.Interaction):
        try:
            await _start_ticket_flow(interaction, self.values[0], self.panel_name)
        finally:
            # put the menu back to "nothing selected" so the same option can be picked again
            try:
                await interaction.message.edit(view=self.view)
            except discord.HTTPException:
                pass


class TicketPanelView(discord.ui.View):
    def __init__(self, panel_name: str, ticket_types: list, display_style: str = "buttons",
                 placeholder: str = None, panel_id: str = None):
        super().__init__(timeout=None)
        self.panel_name = panel_name
        if display_style == "dropdown":
            self.add_item(TicketPanelSelect(ticket_types, panel_name, panel_id, placeholder))
        else:
            for tt in ticket_types[:MAX_PANEL_BUTTONS]:
                self.add_item(OpenTicketButton(tt, panel_name))


async def _set_priority(channel, guild: discord.Guild, ticket_doc: dict, level: str) -> list:
    """Save a ticket's priority, update the Priority field of its welcome embed and (if the
    panel asks for it) put a priority prefix / emoji in the channel name.

    `channel` is a text channel or a thread. Never raises; returns short notes about
    anything that couldn't be done (e.g. the rename hit Discord's rate limit)."""
    level = ts.clean_priority(level, "normal")
    notes = []
    await models.update_ticket(channel.id, priority=level)

    try:
        wid = ticket_doc.get("welcome_message_id")
        if wid:
            msg = await channel.fetch_message(int(wid))
            if msg.embeds:
                embed = msg.embeds[0]
                label = ts.priority_label(level)
                for i, field in enumerate(embed.fields):
                    if field.name == "Priority":
                        embed.set_field_at(i, name="Priority", value=label, inline=True)
                        break
                else:
                    embed.insert_field_at(0, name="Priority", value=label, inline=True)
                await msg.edit(embed=embed)
    except discord.HTTPException:
        pass        # welcome message deleted / not ours: the priority is still saved

    try:
        tt = await _ticket_type_of(ticket_doc)
        mode = ts.settings_for(tt)["priority_rename"]
        if mode != "off":
            old_prefix = ticket_doc.get("priority_prefix") or ""
            base = channel.name
            if old_prefix and base.startswith(old_prefix):
                base = base[len(old_prefix):]          # only ever strips what we added
            new_prefix = ts.priority_prefix(level, mode)
            new_name = new_prefix + base[:100 - len(new_prefix)]
            if await _rename_ok(channel, new_name, f"Ticket priority set to {level}"):
                await models.update_ticket(channel.id, priority_prefix=new_prefix)
            else:
                notes.append("I couldn't rename the channel right now (Discord limits renames).")
    except Exception as e:
        print(f"[tickets] priority rename failed: {e}")
    return notes


class PrioritySelect(discord.ui.Select):
    def __init__(self, current: str):
        super().__init__(
            placeholder="Choose a priority",
            options=[
                discord.SelectOption(label=name, value=key, emoji=emoji, default=(key == current))
                for key, (emoji, name) in ts.PRIORITY_INFO.items()
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        ticket_doc = await models.get_ticket(interaction.channel.id)
        if not ticket_doc or not await _is_ticket_staff(interaction.guild, interaction.user, ticket_doc):
            return await interaction.response.edit_message(
                embed=error_embed("You can't change this ticket's priority."), view=None)
        await interaction.response.defer()
        level = self.values[0]
        notes = await _set_priority(interaction.channel, interaction.guild, ticket_doc, level)
        await interaction.edit_original_response(
            embed=success_embed(f"Priority set to {ts.priority_label(level)}. " + " ".join(notes)), view=None)
        try:
            await interaction.channel.send(
                embed=info_embed(f"{ts.priority_label(level)} priority set by {interaction.user.mention}."),
                allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


class PrioritySelectView(discord.ui.View):
    def __init__(self, current: str):
        super().__init__(timeout=60)
        self.add_item(PrioritySelect(current))


class PriorityButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Priority", emoji="🚩", style=discord.ButtonStyle.secondary,
            custom_id="ticket_priority_button",
        )

    async def callback(self, interaction: discord.Interaction):
        ticket_doc = await models.get_ticket(interaction.channel.id)
        if not ticket_doc:
            return await interaction.response.send_message(
                embed=error_embed("This is no longer an active ticket."), ephemeral=True)
        if not await _is_ticket_staff(interaction.guild, interaction.user, ticket_doc):
            return await interaction.response.send_message(
                embed=error_embed("Only staff can change the priority."), ephemeral=True)
        current = ts.clean_priority(ticket_doc.get("priority"), "normal")
        await interaction.response.send_message(
            embed=info_embed("Choose this ticket's priority."),
            view=PrioritySelectView(current), ephemeral=True)


class TicketPriorityView(discord.ui.View):
    """Registered at startup (cogs/events.py) so the Priority button keeps working on
    messages sent before a restart; the custom_id is what matters."""
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PriorityButton())


def _button_kwargs(ticket_type, key: str) -> dict:
    """label / emoji / style for a ticket button, from the panel's customisation
    (defaults = the original look). The custom_id is never changed, so old messages
    keep working."""
    look = ts.button_look(ticket_type, key)
    return {
        "label": look["label"],
        "emoji": look["emoji"],
        "style": BUTTON_STYLES.get(look["style"], discord.ButtonStyle.secondary),
    }


async def _ticket_type_of(ticket_doc: dict):
    """The ticket type document a ticket was opened from (None for claim / spawner /
    application tickets, or if the type was deleted)."""
    tid = (ticket_doc or {}).get("ticket_type_id")
    if not tid:
        return None
    try:
        return await models.get_ticket_type(str(tid))
    except Exception:
        return None


async def _apply_claim_visibility(channel, guild: discord.Guild, ticket_doc: dict,
                                  ticket_type, claimer, claimed: bool) -> None:
    """Per-panel option "only the claimer can see the ticket after claiming".
    On claim the staff roles lose View Channel and the claimer gets their own access;
    on unclaim everything is put back. Text channels only; never raises."""
    try:
        if ticket_type is None or not ts.settings_for(ticket_type)["claim_only_visible"]:
            return
        if not isinstance(channel, discord.TextChannel):
            return
        staff_ids = await _allowed_role_ids(guild, ticket_doc)
        for target in list(channel.overwrites):
            if isinstance(target, discord.Role) and str(target.id) in staff_ids:
                ow = (discord.PermissionOverwrite(view_channel=False) if claimed else
                      discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                  read_message_history=True))
                await channel.set_permissions(target, overwrite=ow, reason="Ticket claim visibility")
        if isinstance(claimer, discord.Member):
            if claimed:
                await channel.set_permissions(
                    claimer, reason="Ticket claim visibility",
                    overwrite=discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, read_message_history=True))
            elif str(claimer.id) != str(ticket_doc.get("opener_id")):
                await channel.set_permissions(claimer, overwrite=None, reason="Ticket claim visibility")
    except Exception as e:
        print(f"[tickets] claim visibility update failed: {e}")


class CloseTicketButton(discord.ui.Button):
    def __init__(self, ticket_type: dict = None):
        super().__init__(
            custom_id="close_ticket_button",
            **_button_kwargs(ticket_type, "close"),
        )

    async def callback(self, interaction: discord.Interaction):
        ticket_doc = await models.get_ticket(interaction.channel.id)
        if not ticket_doc:
            return await interaction.response.send_message(
                embed=error_embed("This is not an active ticket."), ephemeral=True
            )
        if not await _can_close_ticket(interaction.guild, interaction.user, ticket_doc):
            return await interaction.response.send_message(
                embed=error_embed("You don't have permission to close this ticket."),
                ephemeral=True,
            )
        await interaction.response.send_modal(
            CloseReasonModal(interaction.guild, interaction.channel, interaction.user)
        )


async def _tb_adjust_handled(guild, user_id, delta):
    try:
        from cogs.taskbook import adjust_handled
        await adjust_handled(guild, user_id, delta)
    except Exception as e:
        print(f"[tickets] taskbook handled update failed: {e}")


class ClaimTicketButton(discord.ui.Button):
    def __init__(self, ticket_type: dict = None):
        super().__init__(
            custom_id="claim_ticket_button",
            **_button_kwargs(ticket_type, "claim"),
        )

    async def callback(self, interaction: discord.Interaction):
        ticket_doc = await models.get_ticket(interaction.channel.id)
        if not ticket_doc:
            return await interaction.response.send_message(
                embed=error_embed("This is not an active ticket."), ephemeral=True)
        if not await _can_claim_ticket(interaction.guild, interaction.user, ticket_doc):
            return await interaction.response.send_message(
                embed=error_embed("You don't have permission to claim this ticket."),
                ephemeral=True)
        tt = await _ticket_type_of(ticket_doc)
        if tt is not None and not ts.settings_for(tt)["claiming_enabled"]:
            return await interaction.response.send_message(
                embed=error_embed("Claiming is turned off for this panel."), ephemeral=True)

        # Atomic: only succeeds while nobody holds the claim.
        res = await tickets_col().update_one(
            {"channel_id": str(interaction.channel.id), "status": "open",
             "claimed_by": {"$in": [None]}},
            {"$set": {"claimed_by": str(interaction.user.id)}},
        )
        if not res.modified_count:
            fresh = await models.get_ticket(interaction.channel.id) or {}
            who = fresh.get("claimed_by")
            return await interaction.response.send_message(
                embed=error_embed(f"This ticket is already claimed by <@{who}>." if who
                                  else "This ticket can't be claimed right now."),
                ephemeral=True)

        await interaction.response.send_message(
            embed=info_embed(f"🙋 Ticket claimed by {interaction.user.mention}."),
            allowed_mentions=discord.AllowedMentions.none())
        await _tb_adjust_handled(interaction.guild, interaction.user.id, +1)
        await ticket_stats.record_claim(ticket_doc, interaction.user)
        await _apply_claim_visibility(interaction.channel, interaction.guild, ticket_doc, tt,
                                      interaction.user, True)


class UnclaimTicketButton(discord.ui.Button):
    def __init__(self, ticket_type: dict = None):
        super().__init__(
            custom_id="unclaim_ticket_button",
            **_button_kwargs(ticket_type, "unclaim"),
        )

    async def callback(self, interaction: discord.Interaction):
        ticket_doc = await models.get_ticket(interaction.channel.id)
        if not ticket_doc:
            return await interaction.response.send_message(
                embed=error_embed("This is not an active ticket."), ephemeral=True)

        if not await _is_ticket_staff(interaction.guild, interaction.user, ticket_doc):
            return await interaction.response.send_message(
                embed=error_embed("You don't have permission to use this button."),
                ephemeral=True)

        claimer = ticket_doc.get("claimed_by")
        if not claimer:
            return await interaction.response.send_message(
                embed=error_embed("Nobody has claimed this ticket, so there's nothing to unclaim."),
                ephemeral=True)
        is_admin = interaction.user.guild_permissions.administrator
        if str(claimer) != str(interaction.user.id) and not is_admin:
            return await interaction.response.send_message(
                embed=error_embed(f"Only <@{claimer}> can unclaim this ticket."),
                ephemeral=True)

        res = await tickets_col().update_one(
            {"channel_id": str(interaction.channel.id), "status": "open",
             "claimed_by": str(claimer)},
            {"$set": {"claimed_by": None}},
        )
        if not res.modified_count:
            return await interaction.response.send_message(
                embed=error_embed("This ticket was already unclaimed."), ephemeral=True)

        if str(claimer) == str(interaction.user.id):
            text = f"↩️ Ticket unclaimed by {interaction.user.mention}."
        else:  # an admin releasing someone else's claim
            text = f"↩️ Ticket unclaimed by {interaction.user.mention} (was claimed by <@{claimer}>)."
        await interaction.response.send_message(
            embed=info_embed(text), allowed_mentions=discord.AllowedMentions.none())
        await _tb_adjust_handled(interaction.guild, int(claimer), -1)
        await ticket_stats.record_unclaim(ticket_doc)
        await _apply_claim_visibility(
            interaction.channel, interaction.guild, ticket_doc, await _ticket_type_of(ticket_doc),
            interaction.guild.get_member(int(claimer)), False)


class TicketControlView(discord.ui.View):
    """Close / Claim / Unclaim. With a ticket type, uses that panel's button look and hides
    Claim / Unclaim when claiming is switched off; without one it is the original view
    (this is also the one registered at startup - the custom_ids are what matter)."""
    def __init__(self, ticket_type: dict = None):
        super().__init__(timeout=None)
        self.add_item(CloseTicketButton(ticket_type))
        if ts.settings_for(ticket_type)["claiming_enabled"]:
            self.add_item(ClaimTicketButton(ticket_type))
            self.add_item(UnclaimTicketButton(ticket_type))
        if ts.settings_for(ticket_type)["priority_enabled"]:
            self.add_item(PriorityButton())


# ---------------------------------------------------------------------------
# Move flow
# ---------------------------------------------------------------------------

class MoveApprovalView(discord.ui.View):
    def __init__(self, new_type: dict, ticket_channel: discord.TextChannel,
                 opener: discord.Member, old_welcome_message_id: int,
                 staff: discord.Member, approval_message_id: int = None):
        super().__init__(timeout=600)
        self.new_type = new_type
        self.ticket_channel = ticket_channel
        self.opener = opener
        self.old_welcome_message_id = old_welcome_message_id
        self.staff = staff
        self.approval_message_id = approval_message_id

    @discord.ui.button(label="Move Ticket", style=discord.ButtonStyle.success, emoji="🔁")
    async def move_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opener.id and \
           not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                embed=error_embed("Only the ticket creator can confirm the move."),
                ephemeral=True,
            )
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        await interaction.response.send_modal(
            MoveQuestionsModal(
                self.new_type, self.ticket_channel, self.opener,
                self.old_welcome_message_id, self.approval_message_id,
            )
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opener.id and \
           not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                embed=error_embed("Only the ticket creator can cancel the move."),
                ephemeral=True,
            )
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        try:
            await interaction.message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass


class MoveTicketSelect(discord.ui.Select):
    def __init__(self, ticket_types: list, ticket_channel: discord.TextChannel,
                 opener: discord.Member, old_welcome_message_id: int,
                 staff: discord.Member, current_type_id: str = None):
        options = []
        for tt in ticket_types[:25]:
            if current_type_id and str(tt["_id"]) == str(current_type_id):
                continue
            options.append(discord.SelectOption(
                label=tt["name"][:100],
                value=str(tt["_id"]),
                emoji=tt.get("emoji") or None,
            ))
        super().__init__(placeholder="Choose a ticket type to move to...", options=options)
        self.ticket_types = {str(tt["_id"]): tt for tt in ticket_types}
        self.ticket_channel = ticket_channel
        self.opener = opener
        self.old_welcome_message_id = old_welcome_message_id
        self.staff = staff

    async def callback(self, interaction: discord.Interaction):
        new_type = self.ticket_types[self.values[0]]

        approval_embed = discord.Embed(
            title=f"🔁 Move request → {new_type.get('emoji', '🎫')} {new_type['name']}",
            description=(
                f"{self.opener.mention}, staff **{self.staff.display_name}** has requested "
                f"this ticket be moved to **{new_type['name']}**.\n\n"
                "Click **Move Ticket** below to confirm and fill in the required questions."
            ),
            color=new_type.get("color") or 0x5865F2,
        )
        approval_view = MoveApprovalView(
            new_type, self.ticket_channel, self.opener,
            self.old_welcome_message_id, self.staff,
        )

        approval_msg = await self.ticket_channel.send(
            content=self.opener.mention,
            embed=approval_embed,
            view=approval_view,
        )
        approval_view.approval_message_id = approval_msg.id

        await interaction.response.send_message(
            embed=success_embed(
                f"Move request for **{new_type['name']}** sent to {self.opener.mention}."
            ),
            ephemeral=True,
        )


class MoveTicketView(discord.ui.View):
    def __init__(self, ticket_types: list, ticket_channel: discord.TextChannel,
                 opener: discord.Member, old_welcome_message_id: int,
                 staff: discord.Member, current_type_id: str = None):
        super().__init__(timeout=180)
        self.add_item(MoveTicketSelect(
            ticket_types, ticket_channel, opener,
            old_welcome_message_id, staff, current_type_id,
        ))


# ---------------------------------------------------------------------------
# Panel picker
# ---------------------------------------------------------------------------

class PanelPickerSelect(discord.ui.Select):
    def __init__(self, panels: list):
        options = [
            discord.SelectOption(label=p["name"][:100], value=str(p["_id"]))
            for p in panels[:25]
        ]
        super().__init__(placeholder="Choose a panel to post...", options=options)
        self.panels = {str(p["_id"]): p for p in panels}

    async def callback(self, interaction: discord.Interaction):
        panel = self.panels[self.values[0]]

        ticket_types = []
        for tid in panel.get("ticket_type_ids", []):
            tt = await models.get_ticket_type(tid)
            if tt:
                ticket_types.append(tt)

        if not ticket_types:
            return await interaction.response.send_message(
                embed=error_embed(f"Panel **{panel['name']}** has no valid ticket types assigned."),
                ephemeral=True,
            )

        style = "dropdown" if panel.get("display_style") == "dropdown" else "buttons"
        default_text = ("Choose an option from the menu below to open a ticket."
                        if style == "dropdown" else "Click a button below to open a ticket.")
        embed = discord.Embed(
            title=panel.get("title") or "🎫 Support",
            description=panel.get("description") or default_text,
            color=panel.get("color") or 0x5865F2,
        )
        view = TicketPanelView(panel["name"], ticket_types, style,
                               panel.get("placeholder"), str(panel["_id"]))
        message = await interaction.channel.send(embed=embed, view=view)
        await models.save_ticket_panel(
            interaction.guild.id, panel["name"], message.id, interaction.channel.id,
            [str(tt["_id"]) for tt in ticket_types],
            title=panel.get("title"),
            description=panel.get("description"),
            color=panel.get("color"),
            color_hex=panel.get("color_hex"),
            display_style=style,
            placeholder=panel.get("placeholder") or "",
        )
        await interaction.response.send_message(
            embed=success_embed(f"Panel **{panel['name']}** posted."),
            ephemeral=True,
        )


class PanelPickerView(discord.ui.View):
    def __init__(self, panels: list):
        super().__init__(timeout=180)
        self.add_item(PanelPickerSelect(panels))


# ---------------------------------------------------------------------------
# Ticket creation from modal answers
# ---------------------------------------------------------------------------

async def _send_welcome(channel, content: str, embed: discord.Embed, ticket_type: dict):
    """Welcome message with the panel's buttons. If Discord rejects a custom button
    (bad emoji), it is sent again with the default buttons instead of failing."""
    try:
        return await channel.send(content=content, embed=embed, view=TicketControlView(ticket_type))
    except discord.HTTPException as e:
        if e.status != 400:
            raise
        print(f"[tickets] custom buttons rejected for '{ticket_type.get('name')}': {e}")
        return await channel.send(content=content, embed=embed, view=TicketControlView())


async def _prepare_thread_ticket(guild: discord.Guild, thread: discord.Thread,
                                 opener: discord.Member, ticket_type: dict) -> None:
    """Thread tickets: give the support roles access and warn if the member may not be able
    to see the parent channel. Never raises; problems go to staff, not to the member."""
    try:
        parent = thread.parent
        # the panel's support roles + the server's ticket support roles (no extra DB read: we
        # already have the ticket type)
        settings = await models.get_guild_settings(guild.id)
        ids = {str(r) for r in ((settings.get("tickets") or {}).get("support_role_ids") or [])}
        ids |= {str(r) for r in (ticket_type.get("allow_role_ids") or [])}
        roles = [r for r in (guild.get_role(int(i)) for i in ids) if r and not r.is_default()]
        failed = await tcont.ensure_staff_access(parent, roles)
        if failed:
            await ticket_core.alert_staff(
                guild,
                f"⚠️ I couldn't give {', '.join(r.mention for r in failed)} access to {parent.mention}, so "
                "they may not see thread tickets there. Give those roles **Manage Threads** on that channel "
                "(I need Manage Permissions to do it myself).",
                key=f"thread-access:{ticket_type['_id']}")
        if not parent.permissions_for(opener).view_channel:
            await thread.send(embed=error_embed(
                f"{opener.mention} can't view {parent.mention}, so they may not be able to see this ticket. "
                "Allow **View Channel** for members on that channel (and deny Send Messages there)."),
                allowed_mentions=discord.AllowedMentions.none())
    except Exception as e:
        print(f"[tickets] thread setup extras failed: {e}")


async def _create_ticket_from_modal(interaction: discord.Interaction, ticket_type: dict,
                                    panel_name: str, answers: list):
    guild = interaction.guild
    if guild is None:
        return

    problem = await _open_gate_error(guild, interaction.user, ticket_type)
    if problem:
        return await interaction.response.send_message(
            embed=error_embed(problem), ephemeral=True,
        )

    overwrites = _ticket_overwrites(guild, interaction.user, ticket_type)
    tcfg = ts.settings_for(ticket_type)

    forbidden = False
    failed = False
    full = None
    channel = None
    number = None
    thread_problem = None          # why a thread ticket couldn't be opened (for staff)
    start_priority = None
    async with category_lock(guild.id):
        # Checked again inside the lock: two quick submits can't both pass the limit.
        problem = await _open_gate_error(guild, interaction.user, ticket_type)
        if not problem:
            number = await models.next_ticket_number(guild.id)
            start_priority = "normal" if tcfg["priority_enabled"] else None
            name = _channel_name_for(ticket_type, interaction.user, answers, number)
            reason = f"Ticket ({ticket_type['name']}) opened by {interaction.user}"
            extra = {"number": number, "opener_name": interaction.user.display_name,
                     **({"priority": start_priority} if start_priority else {})}

            if tcfg["thread_mode"]:
                # ---- private thread in the panel's chosen channel (no categories involved)
                parent = guild.get_channel(int(tcfg["thread_parent_id"]))
                if not isinstance(parent, discord.TextChannel):
                    thread_problem = "the channel chosen for thread tickets doesn't exist or isn't a text channel"
                else:
                    missing = tcont.missing_thread_perms(parent, guild.me)
                    if missing:
                        thread_problem = f"I'm missing these permissions in {parent.mention}: {', '.join(missing)}"
                    else:
                        try:
                            channel = await tcont.create_ticket_thread(
                                parent, name, tcfg["thread_auto_archive"], tcfg["thread_invitable"], reason)
                            await channel.add_user(interaction.user)
                        except discord.HTTPException as e:
                            thread_problem = f"Discord refused to open the thread ({getattr(e, 'text', None) or e})"
                            if channel is not None:       # no thread without its member
                                await tcont.delete_container(channel, "Couldn't add the ticket opener")
                                channel = None
                extra.update({"is_thread": True, "thread_parent_id": str(tcfg["thread_parent_id"])})
            else:
                category = await _resolve_category(guild, ticket_type)
                if category is None:
                    # no category of its own: fall back to the server-wide one (Tickets page)
                    category, overflow = await ticket_core.kind_category_plan(guild, None)
                    owner = ("server", guild.id)
                else:
                    overflow = ticket_core.clean_overflow_ids(
                        ticket_type.get("overflow_category_ids"), category.id
                    )
                    owner = ("type", str(ticket_type["_id"]))
                try:
                    channel = await ticket_core.create_ticket_channel(
                        guild, category, overflow, owner=owner,
                        name=name, overwrites=overwrites, reason=reason,
                    )
                except discord.Forbidden:
                    forbidden = True
                except ticket_core.CategoriesFull as e:
                    full = e
            if channel is not None:
                try:
                    await models.create_ticket(
                        guild.id, channel.id, interaction.user.id,
                        panel_name=panel_name,
                        ticket_type_id=str(ticket_type["_id"]),
                        extra=extra,
                    )
                except Exception as e:
                    print(f"[tickets] couldn't save the ticket: {e}")
                    failed = True
                    await tcont.delete_container(channel, "Ticket database write failed")
    if problem:
        return await interaction.response.send_message(
            embed=error_embed(problem), ephemeral=True,
        )
    if thread_problem:
        await ticket_core.alert_staff(
            guild, f"⚠️ A member couldn't open a **{ticket_type['name']}** ticket: {thread_problem}.",
            key=f"thread:{ticket_type['_id']}")
        return await interaction.response.send_message(
            embed=error_embed("I can't open tickets for this panel right now. Staff have been told."),
            ephemeral=True,
        )
    if full is not None:
        await ticket_core.alert_staff_full(guild, full.names)
        return await interaction.response.send_message(
            embed=error_embed(ticket_core.FULL_MESSAGE), ephemeral=True,
        )
    if forbidden:
        return await interaction.response.send_message(
            embed=error_embed("I don't have permission to create ticket channels."),
            ephemeral=True,
        )
    if failed:
        return await interaction.response.send_message(
            embed=error_embed("Couldn't create your ticket. Please try again."),
            ephemeral=True,
        )

    if tcont.is_thread(channel):
        await _prepare_thread_ticket(guild, channel, interaction.user, ticket_type)

    welcome_embed = _build_welcome_embed(
        ticket_type, interaction.user, answers, guild=guild, channel=channel, number=number,
        linked_names=await _linked_type_names(ticket_type), priority=start_priority,
    )
    ping_content = _ping_content(ticket_type, guild, interaction.user)

    welcome_msg = await _send_welcome(channel, ping_content, welcome_embed, ticket_type)
    await models.update_ticket(channel.id, welcome_message_id=str(welcome_msg.id))

    created = ts.settings_for(ticket_type)["created_message"].strip()
    if created:
        text = ts.render(created, _placeholders(guild, interaction.user, channel, ticket_type, number))
    else:
        text = f"Your ticket has been created: {channel.mention}"
    await interaction.response.send_message(
        embed=success_embed(text[:ts.CREATED_MAX * 2]),
        ephemeral=True,
    )

    # Automations ("a ticket is opened"): lazy and non-fatal, like the other hooks
    try:
        from cogs import ticket_automations
        new_doc = await models.get_ticket(channel.id)
        if new_doc:
            await ticket_automations.on_ticket_opened(guild, channel, new_doc, ticket_type)
    except Exception as e:
        print(f"[tickets] automation hook failed: {e}")


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

OPENER_LEFT_REASON = "Ticket opener left the server"


# ---------------------------------------------------------------------------
# /ticket request-close: the opener confirms (or declines) closing
# ---------------------------------------------------------------------------

class CloseRequestButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ticket_reqclose:(?P<cid>[0-9]+):(?P<action>close|keep)",
):
    """Persistent, so the buttons keep working after a bot restart."""

    def __init__(self, channel_id, action: str):
        if action == "close":
            button = discord.ui.Button(
                label="Close Ticket", emoji="🔒", style=discord.ButtonStyle.danger,
                custom_id=f"ticket_reqclose:{channel_id}:close",
            )
        else:
            button = discord.ui.Button(
                label="Don't Close", emoji="🔓", style=discord.ButtonStyle.secondary,
                custom_id=f"ticket_reqclose:{channel_id}:keep",
            )
        super().__init__(button)
        self.channel_id = int(channel_id)
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match, /):
        return cls(match["cid"], match["action"])

    async def callback(self, interaction: discord.Interaction):
        ticket_doc = await models.get_ticket(self.channel_id)
        if not ticket_doc:
            return await interaction.response.send_message(
                embed=error_embed("This ticket is already closed."), ephemeral=True
            )
        if str(interaction.user.id) != str(ticket_doc.get("opener_id")):
            return await interaction.response.send_message(
                embed=error_embed("Only the person who opened this ticket can answer this."),
                ephemeral=True,
            )

        guild = interaction.guild
        channel = interaction.channel
        requester_id = ticket_doc.get("close_request_by")
        reason = (ticket_doc.get("close_request_reason") or "").strip()

        if self.action == "keep":
            await models.update_ticket(
                self.channel_id, close_request_by=None, close_request_reason=None
            )
            await interaction.response.edit_message(
                embed=info_embed(f"{interaction.user.mention} chose to **keep this ticket open**."),
                view=None, content=None,
            )
            if requester_id:
                try:
                    await channel.send(
                        f"<@{requester_id}> - {interaction.user.mention} doesn't want this "
                        "ticket closed yet.",
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                except discord.HTTPException:
                    pass
            return

        await interaction.response.edit_message(
            embed=info_embed(
                f"{interaction.user.mention} confirmed - closing ticket and generating "
                "transcript..."
            ),
            view=None, content=None,
        )
        closer = None
        if requester_id:
            closer = guild.get_member(int(requester_id))
        if closer is None:
            closer = guild.me
        await close_ticket_with_reason(
            guild, channel, closer,
            reason or "Close requested by staff and confirmed by the ticket opener",
        )


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._closing: set = set()      # channel ids being auto-closed right now
        self._swept = False

    async def cog_load(self):
        self.bot.add_dynamic_items(CloseRequestButton)
        self._inactivity_sweep.start()
        # tickets still waiting for a staff reply: loaded in the background so startup
        # never waits on it (a ticket missing from the list just isn't timed)
        self._pending_load = asyncio.create_task(ticket_stats.load_pending())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Analytics: time to the first staff reply. Messages outside tickets that are still
        waiting for one are skipped with a dict lookup, so this costs nothing elsewhere."""
        waiting = ticket_stats.PENDING_REPLY.get(message.channel.id)
        if waiting is None or message.guild is None or message.author.bot:
            return
        if message.author.id == waiting[1] or message.type not in _NORMAL_MESSAGE_TYPES:
            return
        try:
            ticket_doc = await models.get_ticket(message.channel.id)
            if not ticket_doc:
                ticket_stats.PENDING_REPLY.pop(message.channel.id, None)
                return
            member = message.author if isinstance(message.author, discord.Member) \
                else message.guild.get_member(message.author.id)
            if member and await _is_ticket_staff(message.guild, member, ticket_doc):
                await ticket_stats.record_first_reply(message.channel.id, member, message.created_at)
        except Exception as e:
            print(f"[tickets] first-reply tracking failed: {e}")

    async def cog_unload(self):
        self._inactivity_sweep.cancel()

    # ── Auto-close inactive tickets (per-panel setting, see utils/ticket_autoclose.py) ──
    @tasks.loop(minutes=10)
    async def _inactivity_sweep(self):
        for guild in list(self.bot.guilds):
            try:
                await self._inactivity_guild(guild)
            except Exception as e:
                print(f"[tickets] inactivity sweep failed for {guild.id}: {e}")

    @_inactivity_sweep.before_loop
    async def _before_inactivity(self):
        await self.bot.wait_until_ready()

    async def _inactivity_guild(self, guild: discord.Guild):
        panels = await models.get_ticket_panels(guild.id)
        cfgs = {}
        for p in panels:
            cfg = ac.normalize(p)
            if cfg["warn_hours"] > 0 and p.get("name"):
                cfgs[p["name"]] = cfg
        if not cfgs:
            return
        docs = await tickets_col().find({
            "guild_id": str(guild.id), "status": "open",
            "panel_name": {"$in": list(cfgs)},
        }).to_list(None)
        now = datetime.now(timezone.utc)
        for doc in docs:
            cid = int(doc["channel_id"])
            if cid in self._closing:
                continue
            channel = await tcont.get_container(guild, cid)     # text channel or thread (even archived)
            if channel is None:
                continue
            cfg = cfgs[doc["panel_name"]]
            last_id = channel.last_message_id
            last_activity = (discord.utils.snowflake_time(last_id) if last_id
                             else ac.aware(doc.get("created_at")) or now)
            warn_id = doc.get("inactivity_warn_msg_id")
            action = ac.decide(
                now, last_activity, doc.get("inactivity_warned_at"),
                int(warn_id) if warn_id else None, last_id, cfg,
                claimed=bool(doc.get("claimed_by")))
            if action == "reset":
                await tickets_col().update_one(
                    {"_id": doc["_id"]},
                    {"$unset": {"inactivity_warned_at": "", "inactivity_warn_msg_id": ""}})
            elif action == "warn":
                await self._inactivity_warn(guild, channel, doc, cfg)
            elif action == "close":
                await self._inactivity_close(guild, channel, doc, cfg)
            await asyncio.sleep(1)

    async def _inactivity_warn(self, guild, channel, doc, cfg):
        grace = ac.hours_text(cfg["grace_hours"])
        await tcont.ensure_unarchived(channel)
        try:
            msg = await channel.send(
                content=f"<@{doc['opener_id']}>",
                embed=info_embed(
                    f"⏳ This ticket has had no activity for a while. It will be **closed "
                    f"automatically in {grace}** unless someone replies here."),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except discord.HTTPException as e:
            print(f"[tickets] couldn't post inactivity warning in {channel.id}: {e}")
            return
        await tickets_col().update_one(
            {"_id": doc["_id"], "status": "open"},
            {"$set": {"inactivity_warned_at": datetime.now(timezone.utc),
                      "inactivity_warn_msg_id": str(msg.id)}})

    async def _inactivity_close(self, guild, channel, doc, cfg):
        cid = channel.id
        self._closing.add(cid)
        try:
            await close_ticket_with_reason(
                guild, channel, guild.me,
                f"Closed automatically: no reply after the inactivity warning "
                f"({ac.hours_text(cfg['grace_hours'])})")
        except Exception as e:
            print(f"[tickets] inactivity close of {cid} failed: {e}")
        finally:
            self._closing.discard(cid)

    # ── Auto-close when the opener leaves the server ──────────────────────
    async def _close_orphan_ticket(self, guild: discord.Guild, ticket_doc: dict):
        cid = int(ticket_doc["channel_id"])
        if cid in self._closing:
            return
        channel = await tcont.get_container(guild, cid)
        if channel is None:
            return
        self._closing.add(cid)
        try:
            await tcont.ensure_unarchived(channel)
            try:
                await channel.send(embed=info_embed(
                    "The person who opened this ticket left the server. Closing it now..."))
            except discord.HTTPException:
                pass
            await close_ticket_with_reason(guild, channel, guild.me, OPENER_LEFT_REASON)
        except Exception as e:
            print(f"[tickets] auto-close of {cid} failed: {e}")
        finally:
            self._closing.discard(cid)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot:
            return
        try:
            docs = await tickets_col().find({
                "guild_id": str(member.guild.id),
                "opener_id": str(member.id),
                "status": "open",
            }).to_list(None)
            for doc in docs:
                await self._close_orphan_ticket(member.guild, doc)
        except Exception as e:
            print(f"[tickets] member-leave handler failed: {e}")

    @commands.Cog.listener()
    async def on_ready(self):
        """Once per start: catch tickets whose opener left while the bot was offline.
        Each opener is checked with a real API lookup, never the member cache, so a
        cache gap can't close a ticket by mistake."""
        if self._swept:
            return
        self._swept = True
        try:
            for guild in self.bot.guilds:
                docs = await tickets_col().find(
                    {"guild_id": str(guild.id), "status": "open"}).to_list(None)
                checked: dict = {}
                for doc in docs:
                    opener = doc.get("opener_id")
                    if not opener:
                        continue
                    if opener not in checked:
                        try:
                            await guild.fetch_member(int(opener))
                            checked[opener] = True
                        except discord.NotFound:
                            checked[opener] = False
                        except discord.HTTPException:
                            checked[opener] = True   # unsure -> leave it open
                        await asyncio.sleep(0.5)
                    if checked[opener] is False:
                        await self._close_orphan_ticket(guild, doc)
        except Exception as e:
            print(f"[tickets] startup sweep failed: {e}")

    @commands.hybrid_group(name="ticket", description="Manage support tickets")
    async def ticket(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=info_embed(
                "Use `ticket rename / close / request-close / priority / add / remove / move / lookup / panel`."
            ))

    async def _get_ticket_or_fail(self, ctx: commands.Context):
        ticket_doc = await models.get_ticket(ctx.channel.id)
        if not ticket_doc:
            await ctx.send(embed=error_embed(
                "This command can only be used inside a ticket channel."
            ))
            return None
        return ticket_doc

    @ticket.command(name="rename", description="Rename this ticket channel")
    @command_check()
    async def ticket_rename(self, ctx: commands.Context, *, name: str):
        if not await self._get_ticket_or_fail(ctx):
            return
        try:
            channel = await tcont.resolve(ctx.guild, ctx.channel)
            await tcont.ensure_unarchived(channel)
            await channel.edit(name=name[:100])
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed(f"Couldn't rename: {e}"))
        try:
            from cogs.taskbook import record_ticket_rename as tb_rename
            await tb_rename(ctx.guild, ctx.channel, ctx.author)
        except Exception as e:
            print(f"[tickets] taskbook rename hook failed: {e}")
        await ctx.send(embed=success_embed(f"Ticket renamed to **{name}**."))

    @ticket.command(name="priority", description="Set this ticket's priority (staff)")
    @command_check()
    async def ticket_priority(self, ctx: commands.Context, level: Literal["low", "normal", "high", "urgent"]):
        ticket_doc = await self._get_ticket_or_fail(ctx)
        if not ticket_doc:
            return
        if not await _is_ticket_staff(ctx.guild, ctx.author, ticket_doc):
            return await ctx.send(
                embed=error_embed("Only staff can change the priority."), ephemeral=True)
        notes = await _set_priority(ctx.channel, ctx.guild, ticket_doc, level)
        await ctx.send(
            embed=success_embed(
                f"{ts.priority_label(level)} priority set by {ctx.author.mention}. " + " ".join(notes)),
            allowed_mentions=discord.AllowedMentions.none())

    @ticket.command(name="close", description="Close this ticket and save a transcript")
    @command_check()
    async def ticket_close(self, ctx: commands.Context, *, reason: str = None):
        ticket_doc = await self._get_ticket_or_fail(ctx)
        if not ticket_doc:
            return

        await ctx.send(embed=info_embed("Closing ticket and generating transcript..."))
        await close_ticket_with_reason(ctx.guild, ctx.channel, ctx.author, reason)

    @ticket.command(
        name="request-close",
        description="Ask the ticket opener to confirm closing this ticket",
    )
    @command_check()
    async def ticket_request_close(self, ctx: commands.Context, *, reason: str = None):
        ticket_doc = await self._get_ticket_or_fail(ctx)
        if not ticket_doc:
            return
        opener_id = ticket_doc.get("opener_id")
        if not opener_id:
            return await ctx.send(
                embed=error_embed("I can't tell who opened this ticket."), ephemeral=True
            )
        reason = (reason or "").strip()[:500] or None

        await models.update_ticket(
            ctx.channel.id, close_request_by=str(ctx.author.id), close_request_reason=reason
        )
        embed = discord.Embed(
            title="🔒 Close this ticket?",
            description=(
                f"{ctx.author.mention} would like to close this ticket.\n\n"
                + (f"**Reason:** {reason}\n\n" if reason else "")
                + "<@" + str(opener_id) + ">, is your issue sorted? "
                "Press **Close Ticket** to close it (a transcript is saved) or "
                "**Don't Close** to keep it open."
            ),
            color=0xED4245,
        )
        view = discord.ui.View(timeout=None)
        view.add_item(CloseRequestButton(ctx.channel.id, "close"))
        view.add_item(CloseRequestButton(ctx.channel.id, "keep"))
        await ctx.send(
            content=f"<@{opener_id}>", embed=embed, view=view,
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    @ticket.command(name="unclaim", description="Force-unclaim this ticket (admins only)")
    @command_check()
    async def ticket_unclaim(self, ctx: commands.Context):
        if not ctx.author.guild_permissions.administrator:
            return await ctx.send(embed=error_embed("Only administrators can use this."),
                                  ephemeral=True)
        ticket_doc = await self._get_ticket_or_fail(ctx)
        if not ticket_doc:
            return
        claimer = ticket_doc.get("claimed_by")
        if not claimer:
            return await ctx.send(embed=error_embed(
                "Nobody has claimed this ticket, so there's nothing to unclaim."), ephemeral=True)

        res = await tickets_col().update_one(
            {"channel_id": str(ctx.channel.id), "status": "open", "claimed_by": claimer},
            {"$set": {"claimed_by": None}},
        )
        if not res.modified_count:
            return await ctx.send(embed=error_embed("This ticket was already unclaimed."),
                                  ephemeral=True)
        await ctx.send(embed=info_embed(
            f"↩️ Ticket unclaimed by {ctx.author.mention} (was claimed by <@{claimer}>)."),
            allowed_mentions=discord.AllowedMentions.none())
        await _tb_adjust_handled(ctx.guild, int(claimer), -1)
        await ticket_stats.record_unclaim(ticket_doc)
        await _apply_claim_visibility(
            ctx.channel, ctx.guild, ticket_doc, await _ticket_type_of(ticket_doc),
            ctx.guild.get_member(int(claimer)), False)

    @ticket.command(name="lookup", description="Look up a member's past tickets or search closed tickets")
    @app_commands.describe(query="A member (mention or ID), or part of a ticket name, staff name or reason")
    @command_check()
    async def ticket_lookup(self, ctx: commands.Context, *, query: str):
        query = (query or "").strip()
        if not query:
            return await ctx.send(embed=error_embed("Give me a member or something to search for."),
                                  ephemeral=True)
        guild = ctx.guild
        m = re.fullmatch(r"<@!?(\d{15,25})>|(\d{15,25})", query)
        user_id = (m.group(1) or m.group(2)) if m else None

        flt = {"guild_id": str(guild.id)}
        if user_id:
            flt["opener_id"] = user_id
            subject = f"<@{user_id}>"
        else:
            rx = {"$regex": re.escape(query[:80]), "$options": "i"}
            flt["$or"] = [{f: rx} for f in ("channel_name", "opener_name", "closer_name",
                                            "claimed_by_name", "reason", "panel_name")]
            subject = f"\"{discord.utils.escape_markdown(query[:80])}\""
        col = models.transcripts_col()
        total = await col.count_documents(flt)
        docs = await col.find(flt, {"messages": 0, "html": 0}).sort("created_at", -1).limit(10).to_list(10)

        open_lines = []
        if user_id:
            async for t in tickets_col().find(
                    {"guild_id": str(guild.id), "opener_id": user_id, "status": "open"}).limit(5):
                open_lines.append(f"<#{t['channel_id']}>")

        if not docs and not open_lines:
            return await ctx.send(embed=info_embed(f"No tickets found for {subject}."), ephemeral=True)

        base = dashboard_base_url()

        def link(d):
            return f"{base}/guild/{guild.id}/transcripts/{d['_id']}" if base else None

        def when(d):
            at = d.get("created_at")
            return discord.utils.format_dt(at, "d") if at else "?"

        def stars(d):
            return f" · {'⭐' * int(d['rating'])}" if d.get("rating") else ""

        embed = base_embed(title="🔎 Ticket lookup", description=f"Results for {subject}")
        if open_lines:
            embed.add_field(name="🟢 Open now", value=", ".join(open_lines), inline=False)

        if len(docs) == 1 and total == 1:
            d = docs[0]
            esc = discord.utils.escape_markdown
            embed.add_field(name="🎫 Ticket", value=f"#{esc(d.get('channel_name') or '?')}"
                            + (f"\n-# {esc(d['panel_name'])}" if d.get("panel_name") else ""))
            embed.add_field(name="👤 Opened by", value=esc(d.get("opener_name") or d.get("opener_id") or "?"))
            embed.add_field(name="🙋 Claimed by", value=esc(d.get("claimed_by_name") or "Nobody"))
            embed.add_field(name="🔒 Closed by", value=esc(d.get("closer_name") or d.get("closed_by_id") or "?"))
            embed.add_field(name="🕒 Closed", value=when(d))
            embed.add_field(name="💬 Messages", value=str(d.get("message_count", "?")))
            embed.add_field(name="❓ Reason", value=(d.get("reason") or "No Further Action Required")[:1000],
                            inline=False)
            if d.get("rating"):
                rating = f"{'⭐' * int(d['rating'])} ({int(d['rating'])}/5)"
                if d.get("rating_comment"):
                    rating += f"\n> {d['rating_comment'][:500]}"
                embed.add_field(name="Rating", value=rating, inline=False)
        elif docs:
            esc = discord.utils.escape_markdown
            lines = []
            for d in docs:
                who = "" if user_id else f" · {esc(d.get('opener_name') or '?')}"
                claimer = f" · claimed by {esc(d['claimed_by_name'])}" if d.get("claimed_by_name") else ""
                url = link(d)
                name = f"[#{esc(d.get('channel_name') or '?')}]({url})" if url else f"#{esc(d.get('channel_name') or '?')}"
                lines.append(f"{name}{who} · {when(d)}{claimer}{stars(d)}")
            embed.add_field(name=f"📄 Closed tickets (latest {len(docs)} of {total})",
                            value="\n".join(lines)[:1024], inline=False)
        if len(docs) == 1 and link(docs[0]):
            embed.add_field(name="Transcript", value=f"[Open in the dashboard]({link(docs[0])})", inline=False)
        elif base and total > len(docs):
            embed.set_footer(text="Search the full history on the dashboard's Transcripts page.")
        await ctx.send(embed=embed, ephemeral=True)

    @ticket.command(name="add", description="Add a member or a role to this ticket")
    @command_check()
    async def ticket_add(self, ctx: commands.Context,
                         member_or_role: Union[discord.Member, discord.Role]):
        if not await self._get_ticket_or_fail(ctx):
            return
        if isinstance(member_or_role, discord.Role) and member_or_role.is_default():
            return await ctx.send(embed=error_embed(
                "You can't add @everyone to a ticket."), ephemeral=True)
        channel = await tcont.resolve(ctx.guild, ctx.channel)
        if tcont.is_thread(channel):
            if isinstance(member_or_role, discord.Role):
                return await ctx.send(embed=error_embed(
                    "A role can't be added to a thread ticket. Add the member, or give the role "
                    "**Manage Threads** on the thread channel."), ephemeral=True)
            try:
                await tcont.add_member(channel, member_or_role)
            except discord.HTTPException as e:
                return await ctx.send(embed=error_embed(f"Couldn't add them: {e}"), ephemeral=True)
        else:
            await channel.set_permissions(
                member_or_role, view_channel=True, send_messages=True, read_message_history=True
            )
        kind = "Role" if isinstance(member_or_role, discord.Role) else "Member"
        await ctx.send(
            embed=success_embed(f"{kind} {member_or_role.mention} added to the ticket."),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ticket.command(name="remove", description="Remove a member or a role from this ticket")
    @command_check()
    async def ticket_remove(self, ctx: commands.Context,
                            member_or_role: Union[discord.Member, discord.Role]):
        if not await self._get_ticket_or_fail(ctx):
            return
        if isinstance(member_or_role, discord.Role) and member_or_role.is_default():
            return await ctx.send(embed=error_embed(
                "@everyone is already locked out of tickets."), ephemeral=True)
        channel = await tcont.resolve(ctx.guild, ctx.channel)
        if tcont.is_thread(channel):
            if isinstance(member_or_role, discord.Role):
                return await ctx.send(embed=error_embed(
                    "Roles aren't part of a thread ticket, so there's nothing to remove. "
                    "Remove the member instead."), ephemeral=True)
            try:
                await tcont.remove_member(channel, member_or_role)
            except discord.HTTPException as e:
                return await ctx.send(embed=error_embed(f"Couldn't remove them: {e}"), ephemeral=True)
        else:
            await channel.set_permissions(member_or_role, overwrite=None)
        kind = "Role" if isinstance(member_or_role, discord.Role) else "Member"
        await ctx.send(
            embed=success_embed(f"{kind} {member_or_role.mention} removed from the ticket."),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ticket.command(name="move", description="Move this ticket to a different ticket type")
    @command_check()
    async def ticket_move(self, ctx: commands.Context):
        ticket_doc = await self._get_ticket_or_fail(ctx)
        if not ticket_doc:
            return

        if ticket_doc.get("kind") == "claim":
            return await ctx.send(embed=error_embed(
                "Prize claim tickets can't be moved to another ticket type."
            ))
        if ticket_doc.get("kind") == "spawner":
            return await ctx.send(embed=error_embed(
                "Spawner tickets can't be moved to another ticket type."
            ))
        if tcont.is_thread(ctx.channel):
            return await ctx.send(embed=error_embed(
                "Thread tickets can't be moved to another ticket type."
            ))

        ticket_types = await models.get_ticket_types(ctx.guild.id)
        if not ticket_types:
            return await ctx.send(embed=error_embed(
                "No ticket types configured for this server. Set them up in the dashboard first."
            ))

        opener = None
        if ticket_doc.get("opener_id"):
            opener = ctx.guild.get_member(int(ticket_doc["opener_id"]))
        if opener is None:
            return await ctx.send(embed=error_embed(
                "The ticket creator isn't in the server anymore, so they can't confirm the move."
            ))

        old_welcome_id = ticket_doc.get("welcome_message_id")
        current_type_id = ticket_doc.get("ticket_type_id")

        # A panel with linked panels only offers those; none linked = every type (as before).
        current_type = await _ticket_type_of(ticket_doc)
        linked = ts.settings_for(current_type)["linked_type_ids"] if current_type else []
        if linked:
            ticket_types = [t for t in ticket_types
                            if str(t["_id"]) in linked or str(t["_id"]) == str(current_type_id)]

        if not [t for t in ticket_types if str(t["_id"]) != str(current_type_id)]:
            return await ctx.send(embed=error_embed(
                "There's no other ticket type to move this ticket to."
            ))
        view = MoveTicketView(
            ticket_types, ctx.channel, opener, old_welcome_id, ctx.author, current_type_id
        )

        if ctx.interaction is not None:
            await ctx.interaction.response.send_message(view=view, ephemeral=True)
        else:
            await ctx.send(view=view, delete_after=180)

    @ticket.command(name="panel", description="Post a ticket panel")
    @command_check()
    async def ticket_panel(self, ctx: commands.Context):
        panels = await models.get_ticket_panels(ctx.guild.id)
        if not panels:
            return await ctx.send(embed=error_embed(
                "No panels configured for this server. Create one in the dashboard first."
            ))
        view = PanelPickerView(panels)
        if ctx.interaction is not None:
            return await ctx.interaction.response.send_message(view=view, ephemeral=True)
        else:
            return await ctx.send(view=view, delete_after=180)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))