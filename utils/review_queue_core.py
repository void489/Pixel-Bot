"""
Shared machinery for the review queues.

There are THREE separate queues, one per type, and each one belongs to its own cog:
    applications  -> cogs/applications.py   (/applications setqueue | removequeue)
    blacklist     -> cogs/utilities.py      (/blacklist setqueue | removequeue)
    reports       -> cogs/vouches.py        (/vouches setreportqueue | removereportqueue)

A queue is ONE box (like the ticket panels) in a channel: the pending list, Accept All /
Deny All buttons, and a dropdown. Every pending item of that type is in the dropdown.
Picking one opens a private (ephemeral) copy of the normal review embed with its normal
buttons; the existing persistent buttons do the deciding. Accept All / Deny All ask for a
confirmation first, then decide every pending item through the cog's `bulk_decide`.
On a discord.py without Components V2 it falls back to the old embed + dropdown.

This file only holds what the three share. It knows nothing about any cog: each cog
builds a QueueSpec with its own gather() and open_item() and passes it in.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

import discord

from database import models
from config import constants
from database.logging_config import get_logging_config
from utils.embeds import base_embed, success_embed, error_embed, info_embed, warning_embed

MAX_OPTIONS = 25


@dataclass
class QueueSpec:
    key: str            # config key: "applications" | "blacklist" | "reports"
    select_id: str      # persistent custom_id of this queue's dropdown
    title: str          # e.g. "Application Queue"
    emoji: str          # e.g. "📝"
    noun: str           # e.g. "applications" (used in text)
    gather: Callable[[discord.Guild], Awaitable[list]]
    # async (interaction, guild, item_id) -> sends the private review message
    open_item: Callable[[discord.Interaction, discord.Guild, str], Awaitable[None]]
    # Accept All / Deny All. async (client, guild, staff_member, item_id, accept: bool)
    #   -> ("done" | "skipped" | "failed", short note or None). None = no bulk buttons.
    bulk_decide: Callable = None
    # optional async (guild, member) -> bool, checked before the confirmation is shown
    can_bulk: Callable = None
    # extra warning line shown in the confirmation (what accepting really does)
    bulk_note: str = ""


_locks: dict = {}
_bulk_running: set = set()      # (queue key, guild id) with an Accept All / Deny All in progress

_BOX_PARTS = ("LayoutView", "Container", "TextDisplay", "Separator", "ActionRow")


def box_supported() -> bool:
    """The one-box style needs Discord's Components V2 classes (discord.py 2.6+)."""
    return all(hasattr(discord.ui, n) for n in _BOX_PARTS)


_LayoutBase = getattr(discord.ui, "LayoutView", discord.ui.View)


def _lock(spec: QueueSpec, guild_id) -> asyncio.Lock:
    return _locks.setdefault((spec.key, str(guild_id)), asyncio.Lock())


# ---------------------------------------------------------------------------
# Small helpers the cogs use when building items
# ---------------------------------------------------------------------------

def cut(text, n: int) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


def age(ts) -> str:
    if not isinstance(ts, datetime):
        return "?"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    secs = max(0, int((datetime.now(timezone.utc) - ts).total_seconds()))
    if secs < 3600:
        return f"{max(1, secs // 60)}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def display_name(guild: discord.Guild, user_id) -> str:
    member = guild.get_member(int(user_id)) if user_id else None
    return member.display_name if member else str(user_id)


# ---------------------------------------------------------------------------
# Server log channel
# ---------------------------------------------------------------------------

async def send_log(guild: discord.Guild, embed: discord.Embed) -> None:
    """Post to the server's log channel (Logs cog setting). Silent if there isn't one."""
    try:
        cfg = await get_logging_config(guild.id)
        cid = cfg.get("log_channel_id")
        channel = guild.get_channel(int(cid)) if cid else None
        if isinstance(channel, discord.TextChannel):
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except Exception as e:  # noqa: BLE001
        print(f"[review_queue] log post failed: {e}")


# ---------------------------------------------------------------------------
# Channel / notify
# ---------------------------------------------------------------------------

async def get_channel(guild: discord.Guild, spec: QueueSpec):
    """The queue channel if this queue is on and the channel still exists, else None."""
    cfg = await models.get_review_queue_config(guild.id, spec.key)
    if not cfg["channel_id"]:
        return None
    return guild.get_channel(int(cfg["channel_id"]))


async def notify_new(bot, guild: discord.Guild, spec: QueueSpec, role_ids=None, text: str = None):
    """Optional short ping in the queue channel (auto-deletes), then refresh the queue."""
    channel = await get_channel(guild, spec)
    if channel is None:
        return
    pings = []
    for rid in (role_ids or []):
        role = guild.get_role(int(rid))
        if role:
            pings.append(role.mention)
    if pings:
        try:
            await channel.send(
                content=f"{' '.join(pings)} {text or 'New submission to review.'}",
                allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
                delete_after=30,
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
    await refresh(bot, guild.id, spec)


# ---------------------------------------------------------------------------
# Embed + view
# ---------------------------------------------------------------------------

def build_embed(spec: QueueSpec, items: list) -> discord.Embed:
    if not items:
        embed = base_embed(
            title=f"{spec.emoji} {spec.title}",
            description=f"✅ No {spec.noun} waiting for review.",
            color=constants.COLOR_SUCCESS,
        )
        return embed

    lines = [f"{k + 1}. {cut(i['label'], 70)} · {i['desc'].split('• ')[-1]}"
             for k, i in enumerate(items[:10])]
    if len(items) > 10:
        lines.append(f"…and {len(items) - 10} more")
    embed = base_embed(
        title=f"{spec.emoji} {spec.title} — {len(items)} pending",
        description="Pick one from the dropdown to review it. Only you will see it.\n\n"
                    + "\n".join(lines),
        color=constants.COLOR_WARNING,
    )
    if len(items) > MAX_OPTIONS:
        embed.set_footer(text=f"Dropdown shows the oldest {MAX_OPTIONS}. Decide some to see the rest.")
    return embed


class _QueueSelect(discord.ui.Select):
    def __init__(self, spec: QueueSpec, items: list = None):
        self.spec = spec
        options = [
            discord.SelectOption(
                label=cut(i["label"], 100),
                description=cut(i["desc"], 100),
                value=str(i["id"]),
            )
            for i in (items or [])[:MAX_OPTIONS]
        ]
        disabled = False
        if not options:
            # Discord needs at least one option, so show a disabled placeholder.
            options = [discord.SelectOption(label="Nothing to review", value="none")]
            disabled = True
        super().__init__(
            custom_id=spec.select_id,
            placeholder=f"Pick {spec.noun[:-1] if spec.noun.endswith('s') else spec.noun} to review…",
            min_values=1, max_values=1,
            options=options, disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0] if self.values else "none"
        guild = interaction.guild
        if guild is None:
            return
        if value == "none":
            return await interaction.response.send_message(
                embed=info_embed(f"No {self.spec.noun} to review."), ephemeral=True
            )
        try:
            await self.spec.open_item(interaction, guild, value)
        finally:
            # Resets the dropdown selection and drops anything already decided.
            await refresh(interaction.client, guild.id, self.spec)


class QueueView(discord.ui.View):
    """Old look (embed + dropdown). Only used when Components V2 isn't available."""
    def __init__(self, spec: QueueSpec, items: list = None):
        super().__init__(timeout=None)
        self.add_item(_QueueSelect(spec, items))


def queue_text(spec: QueueSpec, items: list) -> str:
    if not items:
        return f"## {spec.emoji} {spec.title}\n✅ No {spec.noun} waiting for review."
    lines = [f"{k + 1}. {cut(i['label'], 70)} · {i['desc'].split('• ')[-1]}"
             for k, i in enumerate(items[:10])]
    if len(items) > 10:
        lines.append(f"…and {len(items) - 10} more")
    text = (f"## {spec.emoji} {spec.title} — {len(items)} pending\n"
            "Pick one from the dropdown to review it. Only you will see it.\n\n"
            + "\n".join(lines))
    if len(items) > MAX_OPTIONS:
        text += f"\n\n-# Dropdown shows the oldest {MAX_OPTIONS}. Decide some to see the rest."
    return text[:3900]


class QueueBoxView(_LayoutBase):
    """The queue as one box: list, [Accept All] [Deny All], then the dropdown."""
    def __init__(self, spec: QueueSpec, items: list = None):
        super().__init__(timeout=None)
        ui = discord.ui
        items = items or []
        children = [ui.TextDisplay(queue_text(spec, items)), ui.Separator()]
        if spec.bulk_decide:
            row = ui.ActionRow()
            row.add_item(_BulkButton(spec, True, disabled=not items))
            row.add_item(_BulkButton(spec, False, disabled=not items))
            children.append(row)
        select_row = ui.ActionRow()
        select_row.add_item(_QueueSelect(spec, items))
        children.append(select_row)
        colour = constants.COLOR_WARNING if items else constants.COLOR_SUCCESS
        self.add_item(ui.Container(*children, accent_colour=discord.Colour(colour)))


def build_view(spec: QueueSpec, items: list):
    return QueueBoxView(spec, items) if box_supported() else QueueView(spec, items)


def persistent_view(spec: QueueSpec):
    """For bot.add_view() in cog_load: routes the dropdown + buttons by custom_id after a restart."""
    return build_view(spec, [{"id": "x", "label": "x", "desc": "x"}])


# ---------------------------------------------------------------------------
# Accept All / Deny All
# ---------------------------------------------------------------------------

class _BulkButton(discord.ui.Button):
    def __init__(self, spec: QueueSpec, accept: bool, disabled: bool = False):
        self.spec = spec
        self.accept = accept
        super().__init__(
            label="Accept All" if accept else "Deny All",
            style=discord.ButtonStyle.success if accept else discord.ButtonStyle.danger,
            custom_id=f"{spec.key}_queue:{'acceptall' if accept else 'denyall'}",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return
        spec = self.spec
        if spec.can_bulk is not None and not await spec.can_bulk(guild, interaction.user):
            return await interaction.response.send_message(
                embed=error_embed(f"You can't decide {spec.noun} here."), ephemeral=True)
        items = await spec.gather(guild)
        if not items:
            return await interaction.response.send_message(
                embed=info_embed(f"No {spec.noun} to review."), ephemeral=True)
        if (spec.key, guild.id) in _bulk_running:
            return await interaction.response.send_message(
                embed=info_embed("A bulk action is already running for this queue."), ephemeral=True)
        verb = "Accept" if self.accept else "Deny"
        text = f"{verb} **all {len(items)}** pending {spec.noun}?"
        if self.accept and spec.bulk_note:
            text += f"\n\n{spec.bulk_note}"
        text += "\n\nThis can't be undone."
        await interaction.response.send_message(
            embed=warning_embed(text, title=f"{verb} All"),
            view=_BulkConfirm(spec, self.accept, interaction.user.id),
            ephemeral=True,
        )


class _BulkConfirm(discord.ui.View):
    def __init__(self, spec: QueueSpec, accept: bool, user_id: int):
        super().__init__(timeout=60)
        self.spec = spec
        self.accept = accept
        self.user_id = user_id

    @discord.ui.button(label="Yes, do it", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if interaction.user.id != self.user_id or guild is None:
            return await interaction.response.send_message(
                embed=error_embed("This confirmation isn't yours."), ephemeral=True)
        spec = self.spec
        key = (spec.key, guild.id)
        if key in _bulk_running:
            return await interaction.response.edit_message(
                embed=info_embed("A bulk action is already running for this queue."), view=None)
        _bulk_running.add(key)
        verb = "Accepted" if self.accept else "Denied"
        counts = {"done": 0, "skipped": 0, "failed": 0}
        notes: list = []
        try:
            await interaction.response.edit_message(
                embed=info_embed(f"Working on it… {'accepting' if self.accept else 'denying'} "
                                 f"all pending {spec.noun}."), view=None)
            for item in await spec.gather(guild):
                try:
                    status, note = await spec.bulk_decide(
                        interaction.client, guild, interaction.user, item["id"], self.accept)
                except Exception as e:  # noqa: BLE001
                    print(f"[review_queue:{spec.key}] bulk item {item['id']} failed: {e}")
                    status, note = "failed", None
                counts[status if status in counts else "failed"] += 1
                if note and len(notes) < 6:
                    notes.append(f"• {cut(item['label'], 60)}: {cut(note, 120)}")
                await asyncio.sleep(0.4)    # stay well under Discord's rate limits
        finally:
            _bulk_running.discard(key)

        await refresh(interaction.client, guild.id, spec)

        summary = f"{verb} **{counts['done']}** {spec.noun}"
        if counts["skipped"]:
            summary += f" · skipped {counts['skipped']}"
        if counts["failed"]:
            summary += f" · failed {counts['failed']}"
        summary += "."
        detail = summary + ("\n\n" + "\n".join(notes) if notes else "")
        try:
            await interaction.edit_original_response(
                embed=success_embed(detail[:4000], title=f"{'Accept' if self.accept else 'Deny'} All done"))
        except discord.HTTPException:
            pass
        await send_log(guild, base_embed(
            title=f"{spec.emoji} {spec.title} — {'Accept' if self.accept else 'Deny'} All",
            description=f"{interaction.user.mention} " + summary[0].lower() + summary[1:],
            color=constants.COLOR_SUCCESS if self.accept else constants.COLOR_ERROR,
        ))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                embed=error_embed("This confirmation isn't yours."), ephemeral=True)
        await interaction.response.edit_message(embed=info_embed("Cancelled."), view=None)


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

async def refresh(bot, guild_id: int, spec: QueueSpec) -> None:
    """Edit (or re-post) the queue message. Never raises - callers are decision flows."""
    try:
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return
        async with _lock(spec, guild_id):
            cfg = await models.get_review_queue_config(guild.id, spec.key)
            if not cfg["channel_id"]:
                return
            channel = guild.get_channel(int(cfg["channel_id"]))
            if channel is None:
                return

            items = await spec.gather(guild)
            box = box_supported()
            if box:
                kwargs = {"view": QueueBoxView(spec, items)}
            else:
                kwargs = {"embed": build_embed(spec, items), "view": QueueView(spec, items)}

            if cfg["message_id"]:
                try:
                    msg = await channel.fetch_message(int(cfg["message_id"]))
                    if box and not msg.flags.components_v2:
                        # An old embed message can't be edited into a box: replace it once.
                        try:
                            await msg.delete()
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            pass
                    else:
                        await msg.edit(**kwargs)
                        return
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass  # deleted - post a new one below

            msg = await channel.send(**kwargs)
            await models.set_review_queue_config(guild.id, spec.key, channel.id, msg.id)
    except Exception as e:  # noqa: BLE001
        print(f"[review_queue:{spec.key}] refresh failed: {e}")


# ---------------------------------------------------------------------------
# Shared command bodies (each cog's command just calls these)
# ---------------------------------------------------------------------------

async def setup_command(ctx, spec: QueueSpec, channel: discord.TextChannel):
    perms = channel.permissions_for(ctx.guild.me)
    if not (perms.send_messages and perms.embed_links):
        return await ctx.send(embed=error_embed(
            f"I need Send Messages and Embed Links in {channel.mention}."
        ))
    old = await models.get_review_queue_config(ctx.guild.id, spec.key)
    if old["channel_id"] and old["message_id"]:
        old_ch = ctx.guild.get_channel(int(old["channel_id"]))
        if old_ch:
            try:
                await (await old_ch.fetch_message(int(old["message_id"]))).delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
    await models.set_review_queue_config(ctx.guild.id, spec.key, channel.id, None)
    await refresh(ctx.bot, ctx.guild.id, spec)
    await ctx.send(embed=success_embed(
        f"{spec.title} is on in {channel.mention}. New {spec.noun} go there instead of "
        "their old channel."
    ))


async def remove_command(ctx, spec: QueueSpec):
    cfg = await models.get_review_queue_config(ctx.guild.id, spec.key)
    if not cfg["channel_id"]:
        return await ctx.send(embed=info_embed(f"The {spec.title.lower()} is already off."))
    pending = await spec.gather(ctx.guild)
    if pending:
        return await ctx.send(embed=error_embed(
            f"There are still **{len(pending)}** pending {spec.noun} in the queue. "
            "Decide them first, otherwise they would have no buttons anywhere."
        ))
    if cfg["message_id"]:
        ch = ctx.guild.get_channel(int(cfg["channel_id"]))
        if ch:
            try:
                await (await ch.fetch_message(int(cfg["message_id"]))).delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
    await models.set_review_queue_config(ctx.guild.id, spec.key, None, None)
    await ctx.send(embed=success_embed(
        f"{spec.title} is off. New {spec.noun} use their old channel again."
    ))
