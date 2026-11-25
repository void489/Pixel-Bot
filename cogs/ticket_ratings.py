"""
Ticket ratings - 1 to 5 buttons in the "Ticket Closed" DM.

When a ticket closes and it was CLAIMED, the opener's close DM gets five buttons (1-5).
The rating is credited to the staff member who claimed the ticket (the same person the
taskbook's "Tickets handled" counts). Unclaimed tickets, and tickets the opener claimed
themselves, are not rated. One rating per ticket; after rating, an optional
"Add a comment" button opens a small form.

Where it shows up:
    - the staff member's taskbook embed and /staffprofile (average + count)
    - the dashboard's "Ticket Ratings" page (averages, spread, latest comments)

Storage: "ticket_ratings", one document per rated-or-pending close DM:
    guild_id, channel_name, opener_id, staff_id, staff_name, rating (None until rated),
    comment, created_at, rated_at.
The buttons carry the document id in their custom_id and are registered as dynamic items,
so they keep working after a bot restart. An unrated DM stops working after RATING_WINDOW_DAYS (30);
the record stays so the response rate stays honest. A taskbook reset only zeroes the taskbook's
own rating figures; this history stays.
"""
from datetime import datetime, timedelta, timezone

import discord
from bson import ObjectId
from bson.errors import InvalidId
from discord.ext import commands

from database.mongo import ticket_ratings_col, transcripts_col
from utils.embeds import error_embed

RATING_COLOR = 0xFEE75C
COMMENT_MAX = 500
RATING_WINDOW_DAYS = 30   # after this, an unanswered close-DM can no longer be rated


def _expired(doc: dict) -> bool:
    """True for a still-unrated record older than RATING_WINDOW_DAYS. The record is kept
    (not deleted) so the dashboard's response rate (rated / DMs sent) stays accurate."""
    created = doc.get("created_at")
    if doc.get("rating") is not None or not created:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created > timedelta(days=RATING_WINDOW_DAYS)


async def build_rating_view(guild: discord.Guild, opener: discord.abc.User, ticket_doc: dict,
                            channel_name: str, transcript_id: str = None):
    """Create the pending rating record and return the View to attach to the close DM,
    or None if this ticket shouldn't be rated. Call right before sending the DM; if the
    DM then fails, call discard_rating(view) so no orphan record stays."""
    staff_id = ticket_doc.get("claimed_by")
    if not staff_id or str(staff_id) == str(opener.id):
        return None
    member = guild.get_member(int(staff_id))
    doc = {
        "guild_id": str(guild.id),
        "channel_id": str(ticket_doc.get("channel_id")) if ticket_doc.get("channel_id") else None,
        "channel_name": channel_name,
        "transcript_id": str(transcript_id) if transcript_id else None,
        "opener_id": str(opener.id),
        "staff_id": str(staff_id),
        "staff_name": member.display_name if member else str(staff_id),
        "rating": None,
        "comment": None,
        "created_at": datetime.now(timezone.utc),
        "rated_at": None,
    }
    res = await ticket_ratings_col().insert_one(doc)
    view = discord.ui.View(timeout=None)
    for n in range(1, 6):
        view.add_item(RateButton(str(res.inserted_id), n))
    view.rating_id = res.inserted_id
    return view


async def discard_rating(view) -> None:
    try:
        await ticket_ratings_col().delete_one({"_id": view.rating_id, "rating": None})
    except Exception:
        pass


async def _mirror_to_transcript(doc: dict, **fields) -> None:
    """Copy the rating / comment onto the ticket's transcript so the dashboard's
    Transcripts page can show and filter by it. Never raises."""
    try:
        tid = _oid(doc.get("transcript_id"))
        if tid:
            await transcripts_col().update_one({"_id": tid}, {"$set": fields})
    except Exception as e:
        print(f"[ticket_ratings] couldn't update the transcript: {e}")


def _oid(raw):
    try:
        return ObjectId(raw)
    except (InvalidId, TypeError):
        return None


class RateButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=r"tkrate:(?P<rid>[0-9a-f]{24}):(?P<n>[1-5])"):
    def __init__(self, rating_id: str, n: int):
        super().__init__(discord.ui.Button(
            label=str(n), emoji="⭐", style=discord.ButtonStyle.secondary,
            custom_id=f"tkrate:{rating_id}:{n}"))
        self.rating_id = str(rating_id)
        self.n = int(n)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match, /):
        return cls(match["rid"], int(match["n"]))

    async def callback(self, interaction: discord.Interaction):
        oid = _oid(self.rating_id)
        doc = await ticket_ratings_col().find_one({"_id": oid}) if oid else None
        if not doc:
            return await interaction.response.send_message(
                embed=error_embed("This rating is no longer available."), ephemeral=True)
        if str(interaction.user.id) != doc["opener_id"]:
            return await interaction.response.send_message(
                embed=error_embed("Only the person who opened the ticket can rate it."),
                ephemeral=True)
        if _expired(doc):
            return await interaction.response.send_message(
                embed=error_embed(f"This rating link has expired - tickets can be rated for "
                                  f"{RATING_WINDOW_DAYS} days after they close."), ephemeral=True)
        won = await ticket_ratings_col().find_one_and_update(
            {"_id": oid, "rating": None},
            {"$set": {"rating": self.n, "rated_at": datetime.now(timezone.utc)}})
        if won is None:
            return await interaction.response.send_message(
                embed=error_embed("You already rated this ticket."), ephemeral=True)

        await _mirror_to_transcript(doc, rating=self.n)
        try:
            from utils import ticket_stats
            await ticket_stats.record_rating(doc.get("channel_id"), self.n, doc.get("staff_id"))
        except Exception as e:
            print(f"[ticket_ratings] analytics update failed: {e}")
        try:
            from cogs.taskbook import add_rating
            guild = interaction.client.get_guild(int(doc["guild_id"]))
            if guild is not None:
                await add_rating(guild, int(doc["staff_id"]), self.n)
        except Exception as e:
            print(f"[ticket_ratings] taskbook update failed: {e}")

        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed.add_field(name="⭐ Your rating", value=f"**{self.n}** / 5 - thank you!", inline=False)
        view = discord.ui.View(timeout=None)
        view.add_item(CommentButton(self.rating_id))
        await interaction.response.edit_message(embed=embed, view=view)


class CommentButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"tkcomment:(?P<rid>[0-9a-f]{24})"):
    def __init__(self, rating_id: str):
        super().__init__(discord.ui.Button(
            label="Add a comment (optional)", emoji="💬", style=discord.ButtonStyle.primary,
            custom_id=f"tkcomment:{rating_id}"))
        self.rating_id = str(rating_id)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match, /):
        return cls(match["rid"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CommentModal(self.rating_id))


class CommentModal(discord.ui.Modal, title="Tell us more"):
    comment = discord.ui.TextInput(
        label="Your comment", style=discord.TextStyle.paragraph,
        max_length=COMMENT_MAX, required=True,
        placeholder="What went well, or what could be better?")

    def __init__(self, rating_id: str):
        super().__init__()
        self.rating_id = rating_id

    async def on_submit(self, interaction: discord.Interaction):
        oid = _oid(self.rating_id)
        saved = None
        if oid:
            saved = await ticket_ratings_col().find_one_and_update(
                {"_id": oid, "opener_id": str(interaction.user.id), "comment": None,
                 "rating": {"$ne": None}},
                {"$set": {"comment": str(self.comment.value).strip()[:COMMENT_MAX]}})
        if saved is None:
            return await interaction.response.send_message(
                embed=error_embed("Couldn't save that comment."), ephemeral=True)
        await _mirror_to_transcript(saved, rating_comment=str(self.comment.value).strip()[:COMMENT_MAX])
        embed = (interaction.message.embeds[0]
                 if interaction.message and interaction.message.embeds else discord.Embed())
        embed.add_field(name="💬 Comment", value="Sent to the staff team. Thank you!", inline=False)
        try:
            await interaction.response.edit_message(embed=embed, view=None)
        except discord.HTTPException:
            await interaction.response.send_message("Thanks, your comment was sent.", ephemeral=True)


class TicketRatings(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_dynamic_items(RateButton, CommentButton)
        try:
            await ticket_ratings_col().create_index([("guild_id", 1), ("staff_id", 1)])
            await ticket_ratings_col().create_index([("guild_id", 1), ("rated_at", -1)])
            await ticket_ratings_col().create_index([("guild_id", 1), ("created_at", -1)])
        except Exception as e:
            print(f"[ticket_ratings] index creation failed: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketRatings(bot))
