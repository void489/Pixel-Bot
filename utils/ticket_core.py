"""Shared ticket helpers used by every ticket kind (normal, claim, spawner, application).

Step 1 - overflow categories
    Every ticket target has a MAIN category plus up to 5 spare ones (each one an existing
    category or a name that is created when needed and removed again when empty). Discord allows 50
    channels per category, so when the main one is full the next one with room is used.
    If every category is full, CategoriesFull is raised and the caller tells the member
    (see FULL_MESSAGE / alert_staff_full).

    Where the categories are set (all optional, nothing changes until they are filled in):
        ticket type   -> category_id + overflow_category_ids        (Tickets page, per type)
        server-wide   -> tickets.fixed_category_id + tickets.overflow_category_ids
                         (Tickets page; used by claim / spawner / application tickets, and by
                         any type that has no category of its own)

    ALWAYS call create_ticket_channel() while holding category_lock(guild.id) from
    cogs/tickets.py, so two people can't both take the last free slot.

This module has no cog imports, so it can be imported from anywhere.
"""
import time

import discord

from database import models
from utils.ticket_settings import MAX_OVERFLOW, clean_overflow_ids  # noqa: F401 (re-exported)

CATEGORY_CHANNEL_LIMIT = 50     # Discord's hard limit per category

FULL_MESSAGE = ("All ticket categories are full right now. "
                "Staff have been told - please try again a bit later.")

_FULL_ALERT_EVERY = 600         # seconds between "categories full" alerts per server
_last_alert: dict = {}          # (guild_id, key) -> last time


class CategoriesFull(Exception):
    """Every category (main + spares) is at Discord's 50-channel limit."""

    def __init__(self, names=None):
        self.names = list(names or [])
        super().__init__("all ticket categories are full")


# ---------------------------------------------------------------------------
# Category entries
#
# A spare (or the server-wide main) category is stored either as a real category ID or as
# "__new__:Name" - same as a ticket type's main category. A "__new__" entry means "use the
# category with this name, or create it when it's first needed"; once it exists the entry
# is swapped for its ID. When a ticket closes and the category is empty, cogs/tickets.py
# deletes it and puts "__new__:Name" back (see _cleanup_empty_category).
# ---------------------------------------------------------------------------

NEW_PREFIX = "__new__:"


def pending_name(entry):
    """The name inside a "__new__:Name" entry, else None."""
    entry = str(entry or "")
    return entry[len(NEW_PREFIX):].strip() if entry.startswith(NEW_PREFIX) else None


def _find_by_name(guild: discord.Guild, name: str):
    wanted = (name or "").strip().lower()
    for cat in guild.categories:
        if cat.name.strip().lower() == wanted:
            return cat
    return None


def _as_category(guild: discord.Guild, raw):
    try:
        cat = guild.get_channel(int(raw))
    except (TypeError, ValueError):
        return None
    return cat if isinstance(cat, discord.CategoryChannel) else None


async def _make_category(guild: discord.Guild, name: str):
    try:
        return await guild.create_category(name=name[:100], reason="Ticket category (created on demand)")
    except discord.HTTPException as e:       # includes Forbidden
        print(f"[tickets] couldn't create category '{name}': {e}")
        return None


async def _persist_overflow(owner, old_entry: str, new_id: str) -> None:
    """Swap a "__new__:Name" spare for the real category ID where it's stored.
    owner = ("type", ticket_type_id) or ("server", guild_id). Never raises."""
    try:
        kind, ref = owner
        if kind == "type":
            tt = await models.get_ticket_type(str(ref))
            if not tt:
                return
            current = tt.get("overflow_category_ids") or []
        else:
            current = ((await models.get_guild_settings(int(ref))).get("tickets") or {}).get(
                "overflow_category_ids") or []
        updated = []
        for x in current:
            x = new_id if str(x) == old_entry else str(x)
            if x not in updated:
                updated.append(x)
        if kind == "type":
            await models.update_ticket_type(str(ref), {"overflow_category_ids": updated})
        else:
            await models.update_guild_settings(int(ref), {"tickets.overflow_category_ids": updated})
    except Exception as e:
        print(f"[tickets] couldn't save spare category: {e}")


async def _resolve_entry(guild: discord.Guild, entry, owner):
    """A spare entry -> its category. An ID whose category was deleted gives None; a
    "__new__:Name" entry finds the category by name or creates it right now."""
    name = pending_name(entry)
    if name is None:
        return _as_category(guild, entry)
    if not name:
        return None
    cat = _find_by_name(guild, name) or await _make_category(guild, name)
    if cat is not None:
        await _persist_overflow(owner, str(entry), str(cat.id))
    return cat


async def _resolve_fixed(guild: discord.Guild, raw):
    """The server-wide main category (an ID or "__new__:Name"); created on demand."""
    name = pending_name(raw)
    if name is None:
        return _as_category(guild, raw)
    if not name:
        return None
    cat = _find_by_name(guild, name) or await _make_category(guild, name)
    if cat is not None:
        try:
            await models.update_guild_settings(guild.id, {"tickets.fixed_category_id": str(cat.id)})
        except Exception as e:
            print(f"[tickets] couldn't save main category: {e}")
    return cat


async def server_category_block(guild_id: int) -> tuple:
    """(fixed category entry or None, [spare entries]) - the server-wide block, as stored."""
    settings = await models.get_guild_settings(guild_id)
    cfg = settings.get("tickets") or {}
    fixed = str(cfg["fixed_category_id"]) if cfg.get("fixed_category_id") else None
    return fixed, clean_overflow_ids(cfg.get("overflow_category_ids"), fixed)


async def kind_category_plan(guild: discord.Guild, own_primary):
    """For claim / spawner / application tickets (and types without a category).

    own_primary is the category that kind already resolves for itself (or None). If the
    server-wide block is filled in, its main category wins; otherwise the kind keeps using
    its own. The server-wide spares apply either way.
    Returns (primary CategoryChannel or None, [spare entries]). Call under category_lock:
    a "__new__" main category is created here."""
    fixed, overflow = await server_category_block(guild.id)
    primary = await _resolve_fixed(guild, fixed) if fixed else None
    if primary is None:
        primary = own_primary
    return primary, overflow


# ---------------------------------------------------------------------------
# Picking + creating
# ---------------------------------------------------------------------------

def _is_full(category: discord.CategoryChannel) -> bool:
    return len(category.channels) >= CATEGORY_CHANNEL_LIMIT


def _is_category_full_error(e: discord.HTTPException) -> bool:
    """Discord refused because the category already holds 50 channels (our cache was stale)."""
    text = str(getattr(e, "text", "") or e)
    return e.status == 400 and ("MAX_CHANNELS" in text or "parent_id" in text)


async def iter_categories(guild: discord.Guild, primary, overflow_entries, owner=None):
    """The main category, then each spare in order. A "__new__" spare is created only when
    the loop actually reaches it (every earlier category was full). Deleted ones are skipped."""
    owner = owner or ("server", guild.id)
    seen = set()
    if isinstance(primary, discord.CategoryChannel):
        seen.add(primary.id)
        yield primary
    for entry in (overflow_entries or []):
        cat = await _resolve_entry(guild, entry, owner)
        if cat is not None and cat.id not in seen:
            seen.add(cat.id)
            yield cat


async def create_ticket_channel(guild: discord.Guild, primary, overflow_ids, owner=None, **kwargs):
    """guild.create_text_channel() in the first category with room.

    Call while holding category_lock(guild.id). `kwargs` go straight to
    create_text_channel (name, overwrites, topic, reason...) - don't pass `category`.
    `owner` says where the spare list is stored, so a created spare can be saved:
    ("type", ticket_type_id), or leave it out for the server-wide list.
    With no category configured at all it behaves exactly like before (no category).
    Raises CategoriesFull when every configured category is full; discord.Forbidden and
    other HTTP errors are raised as usual for the caller to handle."""
    tried = []
    async for cat in iter_categories(guild, primary, overflow_ids, owner):
        tried.append(cat.name)
        if _is_full(cat):
            continue
        try:
            return await guild.create_text_channel(category=cat, **kwargs)
        except discord.HTTPException as e:
            if _is_category_full_error(e):
                continue            # stale cache: Discord says it's full, try the next one
            raise
    if not tried:
        return await guild.create_text_channel(category=None, **kwargs)
    raise CategoriesFull(tried)


async def first_with_room(guild: discord.Guild, primary, overflow_ids, current=None, owner=None):
    """For moving an existing channel: (category, all_full, names_tried).

    A category the channel is already in always counts as having room. With nothing
    configured it returns (None, False, []), same as before (no category)."""
    tried = []
    async for cat in iter_categories(guild, primary, overflow_ids, owner):
        tried.append(cat.name)
        if (current is not None and cat.id == current.id) or not _is_full(cat):
            return cat, False, tried
    return None, bool(tried), tried


# ---------------------------------------------------------------------------
# "Everything is full" messages
# ---------------------------------------------------------------------------

async def alert_staff(guild: discord.Guild, text: str, key: str = "general") -> None:
    """Post `text` to the ticket log channel, pinging the support roles. At most one alert
    per `key` every 10 minutes per server, so a broken setup can't spam. Never raises."""
    try:
        now = time.monotonic()
        if now - _last_alert.get((guild.id, key), 0) < _FULL_ALERT_EVERY:
            return
        _last_alert[(guild.id, key)] = now

        settings = await models.get_guild_settings(guild.id)
        cfg = settings.get("tickets") or {}
        log_id = cfg.get("log_channel_id") or (settings.get("logging") or {}).get("log_channel_id")
        channel = guild.get_channel(int(log_id)) if log_id else None
        if channel is None:
            return

        roles = []
        for rid in (cfg.get("support_role_ids") or []):
            role = guild.get_role(int(rid))
            if role:
                roles.append(role.mention)
        await channel.send(
            content=((" ".join(roles) + "\n") if roles else "") + text,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
    except Exception as e:
        print(f"[tickets] could not alert staff ({key}): {e}")


async def alert_staff_full(guild: discord.Guild, names=None, what: str = "ticket") -> None:
    """Tell staff the categories are full (see alert_staff). Never raises."""
    listed = ", ".join(f"**{n}**" for n in (names or []))
    await alert_staff(
        guild,
        f"⚠️ A member couldn't open a {what}: every ticket category is full"
        + (f" ({listed})" if listed else "")
        + ". Close old tickets or add a spare category on the dashboard (Tickets page).",
        key="full")
