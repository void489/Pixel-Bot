"""
Spawner buy/sell panel.

Commands (all hybrid: slash AND the server's custom prefix, all @command_check so they
show up on the dashboard's Command Permissions page and are admin-only until enabled):

  /spawner panel                                   post the live price panel in this channel
  /spawner setprice <type> <sell|buy> <price>      set a price ("off" stops that side)
  /spawner add <type> <emoji>                      add a spawner type (or change its emoji)
  /spawner remove <type>                           remove a spawner type
  /spawnercalc <type> <sell|buy> <amount>          price calculator (uses the server's prices)

Prices are from the CUSTOMER's point of view, matching the panel text:
  sell = what the customer RECEIVES when they sell to us   ("YOU SELL TO US")
  buy  = what the customer PAYS when they buy from us      ("WE SELL TO YOU")

Numbers accept k / m / b everywhere: 64, 1,152, 100k, 1.5m, 2b (see utils/spawner_math.py).

Panel: an embed with the prices + the free-text notes from the dashboard, and two
buttons, Sell Spawners and Buy Spawners. The buttons are DynamicItems registered once
in cog_load, so they keep working across restarts. Every panel message is remembered
(guild_settings.spawners.panels) and edited in place whenever a price or the notes change.

Button -> (type picker, only if several types are priced for that side) -> modal asking
for IGN + amount -> a ticket channel. Spawner tickets are stored in the normal `tickets`
collection with kind="spawner", so /ticket add, remove, rename and the Close button all
work on them (/ticket move refuses them, like claim tickets). The ticket stores the order
(side, type, amount, unit price, total).

Pause (dashboard only): each side of a type has a "pause" tick. A paused side keeps its price
and stays on the panel as ~~price~~ PAUSED, but it is hidden from the type picker and
tickets for it are refused (also if the picker was already open). Open tickets are untouched.

Price change: /spawner setprice edits the panels, then every OPEN ticket for that exact
type + side is recalculated: its welcome embed is refreshed and a "Spawner Price Updated"
embed (old -> new unit and total) is posted pinging the ticket opener. Tickets for other
spawners or the other side are not touched, and neither are tickets whose price is already
current.

Dashboard (Spawners tab): ticket category, allow roles (see + reply, count as staff for
the Close button), ping roles (pinged when a ticket opens) and the notes shown under the
prices. Saving there makes the bot refresh the live panels via a dashboard task.
"""
import asyncio
import re
from datetime import datetime, timezone
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import models
from utils.embeds import success_embed, error_embed, info_embed
from utils.permissions import command_check
from utils import ticket_core
from utils.spawner_math import parse_amount, parse_price, format_short, format_both
from cogs.tickets import (
    TicketControlView, category_lock, find_category_by_name, _can_close_ticket,
)

SELL = "sell"
BUY = "buy"

PANEL_COLOR = 0x2ECC71      # green bar, like the reference panel
UPDATE_COLOR = 0xFEE75C     # yellow bar for "price updated"

REFRESH_TASK = "refresh_spawner_panels"   # enqueued by the dashboard

_CUSTOM_EMOJI = re.compile(r"^<a?:\w{2,32}:\d{15,25}>$")
_TRAILING_SPAWNERS = re.compile(r"\s*spawners?\s*$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _price(t: dict, side: str):
    """Price for one side as an int, or None when it isn't set."""
    value = (t or {}).get(f"{side}_price")
    return int(value) if value else None


def _paused(t: dict, side: str) -> bool:
    """True when staff ticked "pause" for this side of the type on the dashboard.
    The price is kept; the side just can't be ordered until it's unticked."""
    return bool((t or {}).get(f"{side}_paused"))


def _min_amount(t: dict, side: str) -> int:
    """Smallest amount a customer may order on this side (0 = no minimum)."""
    try:
        return max(0, int((t or {}).get(f"{side}_min") or 0))
    except (TypeError, ValueError):
        return 0


def _min_message(t: dict, side: str, minimum: int) -> str:
    verb = "sell" if side == SELL else "buy"
    return f"The minimum to {verb} **{_label(t)}** is **{minimum:,}**."


def _orderable_price(t: dict, side: str):
    """Price customers can order at right now: None when it isn't set OR the side is paused.
    Use this for anything a customer does (picker, modal, ticket creation, calculator).
    `_price` stays the raw saved price (used to recalculate tickets that are already open)."""
    if _paused(t, side):
        return None
    return _price(t, side)


def _label(t: dict) -> str:
    return f"{t['name']} Spawners"


def _clean_name(text: str) -> str:
    """'Skeleton Spawners' -> 'Skeleton' (we add 'Spawners' ourselves when displaying)."""
    return _TRAILING_SPAWNERS.sub("", (text or "").strip()).strip()


def _valid_emoji(text: str) -> bool:
    text = (text or "").strip()
    if _CUSTOM_EMOJI.match(text):
        return True
    # Unicode emoji: short, and no letters / digits / spaces in it.
    return 0 < len(text) <= 16 and not any(c.isalnum() or c.isspace() for c in text)


def _slug(text: str, fallback: str, limit: int) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:limit].strip("-")
    return out or fallback


def _channel_name(side: str, amount: int, type_name: str) -> str:
    """e.g. "sell-128-skeleton": side, amount, type - nothing else."""
    return f"{side}-{amount}-{_slug(type_name, 'spawner', 40)}"[:100]


def _roles_from_ids(guild: discord.Guild, role_ids: list) -> list:
    roles = []
    for rid in role_ids or []:
        try:
            role = guild.get_role(int(rid))
        except (TypeError, ValueError):
            role = None
        if role:
            roles.append(role)
    return roles


def order_phrase(side: str, amount: int, t: dict) -> str:
    """'buying 128 💀 skeleton spawners' (customer's point of view)."""
    verb = "selling" if side == SELL else "buying"
    parts = [verb, f"{amount:,}", t.get("emoji") or "", f"{t['name'].lower()} spawners"]
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Embeds
# ---------------------------------------------------------------------------

def build_panel_embed(cfg: dict) -> discord.Embed:
    types = list(cfg["types"].values())

    def lines_for(side):
        out = []
        for t in types:
            price = _price(t, side)
            if price:
                emoji = f"{t['emoji']} " if t.get("emoji") else ""
                minimum = _min_amount(t, side)
                tail = f" · min {minimum:,}" if minimum else ""
                if _paused(t, side):
                    out.append(f"| {emoji}{_label(t)} ~~{format_short(price)}~~ **PAUSED**")
                else:
                    out.append(f"| {emoji}{_label(t)} **{format_short(price)}**{tail}")
        return out

    sections = []
    sells, buys = lines_for(SELL), lines_for(BUY)
    if sells:
        sections.append("**YOU SELL TO US**\n" + "\n".join(sells))
    if buys:
        sections.append("**WE SELL TO YOU**\n" + "\n".join(buys))
    if not sections:
        sections.append("*No prices have been set yet.*")

    description = "\n\n".join(sections)
    notes = (cfg.get("notes") or "").strip()
    if notes:
        room = 4096 - len(description) - 2
        if room > 0:
            description += "\n\n" + notes[:room]

    emoji = next((t.get("emoji") for t in types if t.get("emoji")), "")
    title = f"{emoji} SPAWNER PRICES {emoji}" if emoji else "SPAWNER PRICES"
    return discord.Embed(
        title=title, description=description, color=PANEL_COLOR,
        timestamp=datetime.now(timezone.utc),
    )


def build_order_embed(*, side: str, amount: int, t: dict, ign: str,
                      unit_price: int, total: int, opener_id) -> discord.Embed:
    title = "💰 Selling Spawners" if side == SELL else "🛒 Buying Spawners"
    description = (
        f"Ticket opened by <@{opener_id}>.\n"
        "Staff will be with you shortly. Use the button below to close it when resolved.\n\n"
        f"**IGN:** `{ign}`\n"
        f"**Order:** {order_phrase(side, amount, t)}\n"
        f"**Unit Price:** {format_both(unit_price)}\n"
        f"**Total Price:** {format_both(total)}"
    )
    return discord.Embed(
        title=title, description=description, color=PANEL_COLOR,
        timestamp=datetime.now(timezone.utc),
    )


def build_update_embed(*, side: str, amount: int, t: dict, old_unit: int, new_unit: int,
                       old_total: int, new_total: int) -> discord.Embed:
    emoji = f"{t['emoji']} " if t.get("emoji") else ""
    description = (
        f"**Order:** {order_phrase(side, amount, t)}\n"
        f"**Unit Price:** {format_short(old_unit)} → {format_short(new_unit)}\n"
        f"**Total Price:** {format_short(old_total)} → {format_both(new_total)}"
    )
    return discord.Embed(
        title=f"{emoji}Spawner Price Updated", description=description,
        color=UPDATE_COLOR, timestamp=datetime.now(timezone.utc),
    )


def build_amount_embed(*, side: str, old_amount: int, new_amount: int, t: dict,
                       unit_price: int, old_total: int, new_total: int, by) -> discord.Embed:
    emoji = f"{t['emoji']} " if t.get("emoji") else ""
    description = (
        f"**Order:** {order_phrase(side, new_amount, t)}\n"
        f"**Amount:** {old_amount:,} → {new_amount:,}\n"
        f"**Unit Price:** {format_short(unit_price)}\n"
        f"**Total Price:** {format_short(old_total)} → {format_both(new_total)}\n\n"
        f"Changed by {by.mention}"
    )
    return discord.Embed(
        title=f"{emoji}Spawner Amount Updated", description=description,
        color=UPDATE_COLOR, timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Live panels + price-change notifications (shared by the commands and the
# dashboard task poller)
# ---------------------------------------------------------------------------

async def refresh_panels(guild: discord.Guild) -> int:
    """Edit every remembered panel message in place. Returns how many were updated.
    Panels whose message or channel no longer exists are forgotten."""
    cfg = await models.get_spawner_config(guild.id)
    embed = build_panel_embed(cfg)
    updated = 0
    for p in cfg["panels"]:
        channel = guild.get_channel_or_thread(int(p["channel_id"]))
        if channel is None:
            await models.remove_spawner_panel(guild.id, p["message_id"])
            continue
        try:
            msg = await channel.fetch_message(int(p["message_id"]))
            await msg.edit(embed=embed)   # buttons stay as they are
            updated += 1
        except discord.NotFound:
            await models.remove_spawner_panel(guild.id, p["message_id"])
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[spawners] couldn't refresh panel {p['message_id']} in {guild.id}: {e}")
    return updated


async def _sync_ticket(guild: discord.Guild, doc: dict, t: dict, new_unit: int) -> bool:
    """Bring ONE open spawner ticket up to `new_unit`. Returns True if it was changed.
    Safe to call twice: the compare-and-set below means only one caller announces."""
    old_unit = int(doc.get("unit_price") or 0)
    if old_unit == new_unit:
        return False
    channel = guild.get_channel(int(doc["channel_id"]))
    if channel is None:
        return False

    amount = int(doc.get("amount") or 0)
    old_total = int(doc.get("total") or old_unit * amount)
    new_total = new_unit * amount
    if not await models.reprice_spawner_ticket(channel.id, old_unit, new_unit, new_total):
        return False   # someone else already repriced it

    side = doc["side"]
    # Refresh the welcome embed so the ticket's top message shows the current price.
    welcome_id = doc.get("welcome_message_id")
    if welcome_id:
        try:
            welcome = await channel.fetch_message(int(welcome_id))
            await welcome.edit(embed=build_order_embed(
                side=side, amount=amount, t=t, ign=doc.get("ign") or "?",
                unit_price=new_unit, total=new_total, opener_id=doc["opener_id"],
            ))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    try:
        await channel.send(
            content=f"<@{doc['opener_id']}> Spawner prices changed and your ticket was recalculated.",
            embed=build_update_embed(
                side=side, amount=amount, t=t, old_unit=old_unit, new_unit=new_unit,
                old_total=old_total, new_total=new_total,
            ),
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"[spawners] couldn't post price update in {channel.id}: {e}")
    return True


async def sync_ticket_prices(guild: discord.Guild, key: str, side: str) -> int:
    """Recalculate every open ticket for this type + side against the CURRENT price."""
    cfg = await models.get_spawner_config(guild.id)
    t = cfg["types"].get(key)
    new_unit = _price(t, side) if t else None
    if not new_unit:
        return 0
    changed = 0
    for doc in await models.get_open_spawner_tickets(guild.id, key, side):
        try:
            if await _sync_ticket(guild, doc, t, new_unit):
                changed += 1
        except Exception as e:
            print(f"[spawners] failed to reprice ticket {doc.get('channel_id')}: {e}")
    return changed


# ---------------------------------------------------------------------------
# Ticket creation
# ---------------------------------------------------------------------------

async def _resolve_category(guild: discord.Guild, cfg: dict):
    """The dashboard stores either a category ID or '__new__:<name>' (create on first use).
    Call this while holding category_lock(guild.id)."""
    raw = cfg.get("category_id")
    if not raw:
        return None

    if str(raw).startswith("__new__:"):
        fresh = (await models.get_spawner_config(guild.id)).get("category_id")
        if fresh and not str(fresh).startswith("__new__:"):
            raw = fresh
        else:
            name = str(raw)[len("__new__:"):].strip() or "Spawner Tickets"
            category = find_category_by_name(guild, name)
            if category is None:
                try:
                    category = await guild.create_category(name=name, reason="Spawner ticket setup")
                except discord.HTTPException:
                    return None
            await models.update_guild_settings(guild.id, {"spawners.category_id": str(category.id)})
            return category

    try:
        category = guild.get_channel(int(raw))
    except (TypeError, ValueError):
        return None
    return category if isinstance(category, discord.CategoryChannel) else None


async def _existing_ticket_channel(guild: discord.Guild, user_id: int, key: str, side: str):
    """The user's open ticket for this type + side, if its channel still exists."""
    for doc in await models.get_open_spawner_tickets(guild.id, key, side, user_id):
        channel = guild.get_channel(int(doc["channel_id"]))
        if channel is not None:
            return channel
    return None


async def create_spawner_ticket(interaction: discord.Interaction, key: str, side: str,
                                ign: str, amount_text: str):
    """Runs after the modal was submitted and the interaction was deferred (ephemeral)."""
    guild, user = interaction.guild, interaction.user

    async def fail(message: str):
        await interaction.followup.send(embed=error_embed(message), ephemeral=True)

    try:
        amount = parse_amount(amount_text)
    except ValueError as e:
        return await fail(str(e))

    ign = (ign or "").replace("`", "").strip()
    if not ign:
        return await fail("Please enter your Minecraft IGN.")

    # Read the price now (not when the button was pressed): staff may have changed it
    # while the modal was open.
    cfg = await models.get_spawner_config(guild.id)
    t = cfg["types"].get(key)
    if t and _paused(t, side) and _price(t, side):
        return await fail("That spawner is paused right now, so tickets can't be opened for it.")
    unit = _orderable_price(t, side) if t else None
    if not unit:
        return await fail("That spawner isn't available right now.")
    minimum = _min_amount(t, side)
    if minimum and amount < minimum:
        return await fail(_min_message(t, side, minimum))
    total = unit * amount

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True
        ),
        user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
    }
    for role in _roles_from_ids(guild, cfg["allow_role_ids"]):
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )

    channel_name = _channel_name(side, amount, t["name"])

    try:
        # One lock for the whole check-and-create, so a double-submit can't open two tickets.
        async with category_lock(guild.id):
            existing = await _existing_ticket_channel(guild, user.id, key, side)
            if existing is not None:
                return await fail(f"You already have an open ticket for this: {existing.mention}")

            category, overflow = await ticket_core.kind_category_plan(
                guild, await _resolve_category(guild, cfg)
            )
            channel = await ticket_core.create_ticket_channel(
                guild, category, overflow,
                name=channel_name,
                overwrites=overwrites,
                topic=f"Spawner {side} | {user} | {amount:,} {t['name']}"[:1024],
                reason=f"Spawner {side} ticket by {user}",
            )
            try:
                await models.create_ticket(
                    guild.id, channel.id, user.id,
                    panel_name=f"Spawner {side.title()}", ticket_type_id=None, kind="spawner",
                    extra={
                        "spawner_key": key,
                        "spawner_name": t["name"],
                        "spawner_emoji": t.get("emoji") or "",
                        "side": side,
                        "ign": ign,
                        "amount": amount,
                        "unit_price": unit,
                        "total": total,
                    },
                )
            except Exception:
                try:
                    await channel.delete(reason="Spawner ticket database write failed")
                except discord.HTTPException:
                    pass
                raise
    except ticket_core.CategoriesFull as e:
        await ticket_core.alert_staff_full(guild, e.names, "spawner ticket")
        return await fail(ticket_core.FULL_MESSAGE)
    except discord.HTTPException as e:
        return await fail(f"Couldn't create your ticket: {e}")

    ping = user.mention
    for role in _roles_from_ids(guild, cfg["ping_role_ids"]):
        ping += f" {role.mention}"

    welcome = await channel.send(
        content=ping,
        embed=build_order_embed(
            side=side, amount=amount, t=t, ign=ign, unit_price=unit, total=total,
            opener_id=user.id,
        ),
        view=SpawnerTicketView(),
    )
    await models.update_ticket(channel.id, welcome_message_id=str(welcome.id))

    await interaction.followup.send(
        embed=success_embed(f"Your ticket has been created: {channel.mention}"), ephemeral=True
    )

    # If staff changed the price in the moments while this ticket was being created, the
    # price-change pass may have run before the ticket existed. Catch up now (no-op if current).
    try:
        await sync_ticket_prices(guild, key, side)
    except Exception as e:
        print(f"[spawners] post-create price check failed: {e}")


# ---------------------------------------------------------------------------
# Order flow UI: button -> (type picker) -> modal
# ---------------------------------------------------------------------------
# Change Amount button (inside a spawner ticket)
# ---------------------------------------------------------------------------

_bg_tasks: set = set()


async def _rename_ticket_channel(channel: discord.TextChannel, **changes):
    """Discord only allows ~2 channel renames per 10 minutes. Waiting that out would hang,
    so try for a few seconds and give up quietly - the ticket itself is already updated."""
    try:
        await asyncio.wait_for(
            channel.edit(reason="Spawner ticket amount changed", **changes), timeout=10
        )
    except Exception as e:
        print(f"[spawners] couldn't rename {channel.id} after amount change: {e}")


async def change_spawner_amount(interaction: discord.Interaction, amount_text: str):
    """Runs after the Change Amount modal was submitted and deferred (ephemeral)."""
    guild, user, channel = interaction.guild, interaction.user, interaction.channel

    async def reply(embed):
        await interaction.followup.send(embed=embed, ephemeral=True)

    doc = await models.get_ticket(channel.id)
    if not doc or doc.get("kind") != "spawner":
        return await reply(error_embed("This isn't an open spawner ticket."))
    # Same rule as the Close button.
    if not await _can_close_ticket(guild, user, doc):
        return await reply(error_embed("You don't have permission to change this ticket's amount."))

    try:
        new_amount = parse_amount(amount_text)
    except ValueError as e:
        return await reply(error_embed(str(e)))

    # The customer can't drop below the type's minimum; staff may set any amount.
    if str(user.id) == str(doc.get("opener_id")):
        cfg0 = await models.get_spawner_config(guild.id)
        t0 = cfg0["types"].get(doc.get("spawner_key"))
        minimum = _min_amount(t0, doc.get("side")) if t0 else 0
        if minimum and new_amount < minimum:
            return await reply(error_embed(_min_message(t0, doc["side"], minimum)))

    old_amount = int(doc.get("amount") or 0)
    unit = int(doc.get("unit_price") or 0)
    if new_amount == old_amount:
        return await reply(info_embed(f"The amount is already {old_amount:,}."))
    old_total = int(doc.get("total") or unit * old_amount)
    new_total = unit * new_amount

    if not await models.set_spawner_ticket_amount(channel.id, old_amount, unit, new_amount, new_total):
        return await reply(error_embed(
            "This ticket changed while you were editing it (price or amount). "
            "Press Change Amount again."
        ))

    cfg = await models.get_spawner_config(guild.id)
    t = dict(cfg["types"].get(doc.get("spawner_key")) or {})
    t.setdefault("name", doc.get("spawner_name") or "Spawner")
    t.setdefault("emoji", doc.get("spawner_emoji") or "")
    side = doc["side"]

    # Refresh the ticket's top message so it shows the new order.
    welcome_id = doc.get("welcome_message_id")
    if welcome_id:
        try:
            welcome = await channel.fetch_message(int(welcome_id))
            await welcome.edit(embed=build_order_embed(
                side=side, amount=new_amount, t=t, ign=doc.get("ign") or "?",
                unit_price=unit, total=new_total, opener_id=doc["opener_id"],
            ))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    try:
        await channel.send(
            embed=build_amount_embed(
                side=side, old_amount=old_amount, new_amount=new_amount, t=t,
                unit_price=unit, old_total=old_total, new_total=new_total, by=user,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"[spawners] couldn't post amount update in {channel.id}: {e}")

    # Keep the channel name / topic in step with the order (in the background).
    changes = {"name": _channel_name(side, new_amount, t["name"])}
    opener = guild.get_member(int(doc["opener_id"]))
    if opener is not None:
        changes["topic"] = f"Spawner {side} | {opener} | {new_amount:,} {t['name']}"[:1024]
    task = asyncio.create_task(_rename_ticket_channel(channel, **changes))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)

    await reply(success_embed(f"Amount changed to **{new_amount:,}**. New total: {format_both(new_total)}."))


class ChangeAmountModal(discord.ui.Modal):
    def __init__(self, current: int):
        super().__init__(title="Change Amount")
        self.amount = discord.ui.TextInput(
            label="New amount", placeholder="e.g. 64, 1k, 2.5k", default=f"{current}",
            required=True, min_length=1, max_length=15,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await change_spawner_amount(interaction, self.amount.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        import traceback
        traceback.print_exception(type(error), error, error.__traceback__)
        try:
            embed = error_embed("Something went wrong changing the amount.")
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass


class ChangeAmountButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Change Amount", emoji="✏️", style=discord.ButtonStyle.secondary,
            custom_id="spawner_change_amount_button",
        )

    async def callback(self, interaction: discord.Interaction):
        doc = await models.get_ticket(interaction.channel.id)
        if not doc or doc.get("kind") != "spawner":
            return await interaction.response.send_message(
                embed=error_embed("This isn't an open spawner ticket."), ephemeral=True)
        # Same rule as the Close button.
        if not await _can_close_ticket(interaction.guild, interaction.user, doc):
            return await interaction.response.send_message(
                embed=error_embed("You don't have permission to change this ticket's amount."),
                ephemeral=True)
        await interaction.response.send_modal(ChangeAmountModal(int(doc.get("amount") or 1)))


class SpawnerTicketView(TicketControlView):
    """Close / Claim / Unclaim (same as every ticket) + Change Amount."""
    def __init__(self):
        super().__init__()
        self.add_item(ChangeAmountButton())


class ChangeAmountView(discord.ui.View):
    """Only used to re-register the button after a restart (the other three are
    re-registered by TicketControlView in the events cog)."""
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ChangeAmountButton())


# ---------------------------------------------------------------------------

class SpawnerOrderModal(discord.ui.Modal):
    def __init__(self, key: str, side: str, t: dict):
        verb = "Sell" if side == SELL else "Buy"
        super().__init__(title=f"{verb} {_label(t)}"[:45])
        self.key = key
        self.side = side
        self.ign = discord.ui.TextInput(
            label="Minecraft IGN", placeholder="Your in-game name",
            required=True, min_length=1, max_length=32,
        )
        minimum = _min_amount(t, side)
        self.amount = discord.ui.TextInput(
            label=f"Amount (minimum {minimum:,})" if minimum else "Amount",
            placeholder="e.g. 64, 1k, 2.5k",
            required=True, min_length=1, max_length=15,
        )
        self.add_item(self.ign)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await create_spawner_ticket(
            interaction, self.key, self.side, self.ign.value, self.amount.value
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        import traceback
        traceback.print_exception(type(error), error, error.__traceback__)
        try:
            embed = error_embed("Something went wrong creating your ticket.")
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass


async def _start_order(interaction: discord.Interaction, key: str, side: str):
    """Validate, then open the IGN + amount modal as the response to `interaction`."""
    guild = interaction.guild
    cfg = await models.get_spawner_config(guild.id)
    t = cfg["types"].get(key)
    if t and _paused(t, side) and _price(t, side):
        return await interaction.response.send_message(
            embed=error_embed("That spawner is paused right now."), ephemeral=True
        )
    if not t or not _orderable_price(t, side):
        return await interaction.response.send_message(
            embed=error_embed("That spawner isn't available right now."), ephemeral=True
        )
    existing = await _existing_ticket_channel(guild, interaction.user.id, key, side)
    if existing is not None:
        return await interaction.response.send_message(
            embed=error_embed(f"You already have an open ticket for this: {existing.mention}"),
            ephemeral=True,
        )
    await interaction.response.send_modal(SpawnerOrderModal(key, side, t))


class TypePickerSelect(discord.ui.Select):
    def __init__(self, side: str, available: list):
        side_ = side
        options = [
            discord.SelectOption(
                label=_label(t)[:100], value=key,
                description=(f"{format_short(price)} each"
                             + (f" · min {_min_amount(t, side_):,}" if _min_amount(t, side_) else ""))[:100],
            )
            for key, t, price in available[:models.MAX_SPAWNER_TYPES]
        ]
        super().__init__(
            placeholder="Choose a spawner...", options=options, min_values=1, max_values=1
        )
        self.side = side

    async def callback(self, interaction: discord.Interaction):
        await _start_order(interaction, self.values[0], self.side)


class TypePickerView(discord.ui.View):
    def __init__(self, side: str, available: list):
        super().__init__(timeout=180)
        self.add_item(TypePickerSelect(side, available))


class SpawnerOpenButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"spawner_open:(?P<side>sell|buy)"
):
    def __init__(self, side: str):
        sell = side == SELL
        super().__init__(discord.ui.Button(
            label="Sell Spawners" if sell else "Buy Spawners",
            emoji="💰" if sell else "🛒",
            style=discord.ButtonStyle.success if sell else discord.ButtonStyle.primary,
            custom_id=f"spawner_open:{side}",
        ))
        self.side = side

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match, /):
        return cls(match["side"])

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        side = self.side

        # Same blacklist the normal ticket panels use
        blacklist = await models.get_blacklist_config(guild.id)
        bl_role_id = blacklist["ticket_role_id"]
        if bl_role_id:
            member = guild.get_member(interaction.user.id)
            if member and any(str(r.id) == str(bl_role_id) for r in member.roles):
                return await interaction.response.send_message(
                    embed=error_embed("You're blacklisted from opening tickets."), ephemeral=True
                )

        cfg = await models.get_spawner_config(guild.id)
        available = [
            (key, t, _orderable_price(t, side)) for key, t in cfg["types"].items()
            if _orderable_price(t, side)
        ]
        if not available:
            what = "buying" if side == SELL else "selling"
            if any(_paused(t, side) and _price(t, side) for t in cfg["types"].values()):
                return await interaction.response.send_message(
                    embed=error_embed(f"**{what.capitalize()}** spawners is paused right now."),
                    ephemeral=True,
                )
            return await interaction.response.send_message(
                embed=error_embed(f"We aren't {what} any spawners right now."), ephemeral=True
            )

        if len(available) == 1:
            return await _start_order(interaction, available[0][0], side)

        await interaction.response.send_message(
            embed=info_embed("Which spawner?"), view=TypePickerView(side, available), ephemeral=True
        )


def build_panel_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(SpawnerOpenButton(SELL))
    view.add_item(SpawnerOpenButton(BUY))
    return view


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Spawners(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_dynamic_items(SpawnerOpenButton)
        self.bot.add_view(ChangeAmountView())
        self._poll_dashboard_tasks.start()

    async def cog_unload(self):
        self._poll_dashboard_tasks.cancel()

    # ------------------------------------------------------------------
    # Dashboard task poller: saving the Spawners tab refreshes the live panels
    # ------------------------------------------------------------------
    @tasks.loop(seconds=3)
    async def _poll_dashboard_tasks(self):
        try:
            batch = await models.take_pending_dashboard_tasks(limit=10, types=[REFRESH_TASK])
            for t in batch:
                try:
                    guild = self.bot.get_guild(int(t["guild_id"]))
                    if guild is not None:
                        await refresh_panels(guild)
                        # Prices changed on the dashboard -> recalc open tickets + ping
                        # openers, exactly like /spawner setprice does.
                        for ch in (t.get("payload") or {}).get("price_changes") or []:
                            await sync_ticket_prices(guild, str(ch.get("key")), str(ch.get("side")))
                except Exception as e:
                    print(f"[spawners] dashboard task {t.get('_id')} failed: {e}")
        except Exception as e:
            print(f"[spawners] dashboard poller error: {e}")

    @_poll_dashboard_tasks.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _resolve_type(self, ctx: commands.Context, text: str):
        """Find a spawner type from what staff typed. Sends the error itself and
        returns None when it can't."""
        cfg = await models.get_spawner_config(ctx.guild.id)
        types = cfg["types"]
        cleaned = _clean_name(text)
        key = models.spawner_key(cleaned)

        if key in types:
            return key, types[key]
        if key:
            starts = [k for k, t in types.items()
                      if k.startswith(key) or t["name"].lower().startswith(cleaned.lower())]
            if len(starts) == 1:
                return starts[0], types[starts[0]]

        if types:
            known = ", ".join(f"`{t['name']}`" for t in types.values())
            await ctx.send(embed=error_embed(
                f"I don't know a spawner called **{text}**. Known spawners: {known}.\n"
                "Add one with `spawner add`."
            ), ephemeral=True)
        else:
            await ctx.send(embed=error_embed(
                "No spawner types exist yet. Add one first with `spawner add <type> <emoji>`."
            ), ephemeral=True)
        return None

    async def _type_autocomplete(self, interaction: discord.Interaction, current: str):
        if interaction.guild is None:
            return []
        cfg = await models.get_spawner_config(interaction.guild.id)
        wanted = (current or "").lower()
        return [
            app_commands.Choice(name=_label(t)[:100], value=t["name"][:100])
            for key, t in cfg["types"].items()
            if wanted in t["name"].lower() or wanted in key
        ][:25]

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @commands.hybrid_command(
        name="spawnercalc",
        description="Calculate what a spawner order costs, using this server's prices",
    )
    @app_commands.describe(
        spawner_type="Which spawner",
        side="sell = you sell to us, buy = you buy from us",
        amount="How many, e.g. 64, 1k, 2.5k",
    )
    @app_commands.rename(spawner_type="type")
    @command_check()
    async def spawnercalc(self, ctx: commands.Context, spawner_type: str,
                          side: Literal["sell", "buy"], amount: str):
        found = await self._resolve_type(ctx, spawner_type)
        if found is None:
            return
        _, t = found

        try:
            qty = parse_amount(amount)
        except ValueError as e:
            return await ctx.send(embed=error_embed(str(e)), ephemeral=True)

        if _paused(t, side) and _price(t, side):
            return await ctx.send(embed=error_embed(
                f"**{side.capitalize()}** for **{_label(t)}** is paused right now."
            ), ephemeral=True)

        unit = _price(t, side)
        if unit is None:
            return await ctx.send(embed=error_embed(
                f"**{_label(t)}** has no **{side}** price set, so there's nothing to calculate."
            ), ephemeral=True)
        minimum = _min_amount(t, side)
        if minimum and qty < minimum:
            return await ctx.send(embed=error_embed(
                _min_message(t, side, minimum) + " Try a bigger amount."), ephemeral=True)

        embed = discord.Embed(
            title="Spawner Calculator", color=PANEL_COLOR, timestamp=datetime.now(timezone.utc),
            description=(
                f"**Spawner:** {t.get('emoji') + ' ' if t.get('emoji') else ''}{_label(t)}\n"
                f"**Side:** {side.capitalize()}\n"
                f"**Amount:** {qty:,}\n"
                f"**Price each:** {format_short(unit)}\n"
                f"**Total:** {format_both(unit * qty)}"
            ),
        )
        await ctx.send(embed=embed)

    @spawnercalc.autocomplete("spawner_type")
    async def spawnercalc_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._type_autocomplete(interaction, current)

    @commands.hybrid_group(name="spawner", description="Manage the spawner buy/sell panel")
    async def spawner(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=info_embed(
                "Use `spawner panel / setprice / add / remove`."
            ))

    @spawner.command(name="panel", description="Post the spawner price panel in this channel")
    @command_check()
    async def spawner_panel(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        cfg = await models.get_spawner_config(ctx.guild.id)
        try:
            msg = await ctx.channel.send(embed=build_panel_embed(cfg), view=build_panel_view())
        except (discord.Forbidden, discord.HTTPException) as e:
            return await ctx.send(embed=error_embed(
                f"I couldn't post the panel in this channel: {e}"
            ), ephemeral=True)

        await models.add_spawner_panel(ctx.guild.id, ctx.channel.id, msg.id)

        note = "It updates by itself whenever a price or the notes change."
        if not any(_price(t, SELL) or _price(t, BUY) for t in cfg["types"].values()):
            note += ("\nNo prices are set yet - use `spawner add` and `spawner setprice`, "
                     "the panel will fill in.")
        await ctx.send(embed=success_embed(f"Panel posted. {note}"), ephemeral=True)

    @spawner.command(name="setprice", description="Set a spawner's sell or buy price")
    @app_commands.describe(
        spawner_type="Which spawner",
        side="sell = what customers get when they sell to you, buy = what customers pay",
        price="e.g. 8.2m, 8600000, 1.5b - or 'off' to stop this side",
    )
    @command_check()
    async def spawner_setprice(self, ctx: commands.Context, spawner_type: str,
                               side: Literal["sell", "buy"], price: str):
        await ctx.defer(ephemeral=True)

        found = await self._resolve_type(ctx, spawner_type)
        if found is None:
            return
        key, t = found

        if price.strip().lower() in ("off", "none", "clear", "remove"):
            new_price = None
        else:
            try:
                new_price = parse_price(price)
            except ValueError as e:
                return await ctx.send(embed=error_embed(str(e)), ephemeral=True)

        ok, old_price = await models.set_spawner_price(ctx.guild.id, key, side, new_price)
        if not ok:
            return await ctx.send(embed=error_embed(
                "That spawner was removed while you were setting the price."
            ), ephemeral=True)

        who = "Customers selling to you receive" if side == SELL else "Customers buying from you pay"
        if new_price is None:
            headline = (f"**{_label(t)}**: the **{side}** side is now off. "
                        "It's gone from the panel and no new tickets can be opened for it.")
        elif new_price == old_price:
            headline = f"**{_label(t)}**: the {side} price was already **{format_short(new_price)}**."
        else:
            was = f" (was {format_short(old_price)})" if old_price else ""
            headline = f"**{_label(t)}**: {who} **{format_short(new_price)}** each{was}."

        panels = await refresh_panels(ctx.guild)
        tickets = 0
        if new_price is not None and new_price != old_price:
            tickets = await sync_ticket_prices(ctx.guild, key, side)

        await ctx.send(embed=success_embed(
            f"{headline}\nPanels updated: **{panels}**  |  Open tickets recalculated: **{tickets}**"
        ), ephemeral=True)

    @spawner_setprice.autocomplete("spawner_type")
    async def spawner_setprice_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._type_autocomplete(interaction, current)

    @spawner.command(name="add", description="Add a spawner type (or change its emoji)")
    @app_commands.describe(
        spawner_type="Name, e.g. Skeleton (the word Spawners is added for you)",
        emoji="The emoji shown next to it",
    )
    @command_check()
    async def spawner_add(self, ctx: commands.Context, spawner_type: str, emoji: str):
        await ctx.defer(ephemeral=True)

        name = _clean_name(spawner_type)
        key = models.spawner_key(name)
        if not key:
            return await ctx.send(embed=error_embed(
                "Give the spawner a name with letters or numbers in it."
            ), ephemeral=True)
        if len(name) > 40:
            return await ctx.send(embed=error_embed("Keep the name under 40 characters."),
                                  ephemeral=True)

        emoji = emoji.strip()
        if not _valid_emoji(emoji):
            return await ctx.send(embed=error_embed(
                "That doesn't look like an emoji. Use the emoji itself (like 💀) "
                "or a custom emoji from a server the bot is in."
            ), ephemeral=True)

        cfg = await models.get_spawner_config(ctx.guild.id)
        if key not in cfg["types"] and len(cfg["types"]) >= models.MAX_SPAWNER_TYPES:
            return await ctx.send(embed=error_embed(
                f"You can have at most {models.MAX_SPAWNER_TYPES} spawner types."
            ), ephemeral=True)

        created = await models.add_spawner_type(ctx.guild.id, key, name, emoji)
        await refresh_panels(ctx.guild)

        if created:
            text = (f"Added {emoji} **{name} Spawners**.\n"
                    f"Set its prices with `spawner setprice {name} sell <price>` "
                    f"and `spawner setprice {name} buy <price>`.")
        else:
            text = f"Updated {emoji} **{name} Spawners** (its prices were kept)."
        await ctx.send(embed=success_embed(text), ephemeral=True)

    @spawner.command(name="remove", description="Remove a spawner type")
    @app_commands.describe(spawner_type="Which spawner to remove")
    @command_check()
    async def spawner_remove(self, ctx: commands.Context, spawner_type: str):
        await ctx.defer(ephemeral=True)

        found = await self._resolve_type(ctx, spawner_type)
        if found is None:
            return
        key, t = found

        if not await models.remove_spawner_type(ctx.guild.id, key):
            return await ctx.send(embed=error_embed("That spawner was already removed."),
                                  ephemeral=True)
        await refresh_panels(ctx.guild)
        await ctx.send(embed=success_embed(
            f"Removed **{_label(t)}**. Tickets that are already open keep their price."
        ), ephemeral=True)

    @spawner_remove.autocomplete("spawner_type")
    async def spawner_remove_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._type_autocomplete(interaction, current)


async def setup(bot: commands.Bot):
    await bot.add_cog(Spawners(bot))
