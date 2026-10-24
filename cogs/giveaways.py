"""
Giveaway system.

Every command is a hybrid command, so it works as a slash command AND with the
server's custom prefix (set on the dashboard's Command Permissions page):

  /giveaway create prize:<text> time:<1h> [claim_time:<10m>] [winners:<n>]
  !giveaway create prize: Nitro time: 1h claim_time: 10m winners: 2

Like every other command they're admin-only until enabled + given roles on the
dashboard (@command_check).

Buttons are discord.py DynamicItems: one registration in cog_load handles the
Enter / Claim buttons on EVERY giveaway message, before or after a restart,
and each click reads fresh state from Mongo - nothing is cached in memory.

Winner claims open a "claim ticket". These are stored in the normal `tickets`
collection (kind="claim"), so /ticket add, close, remove and rename all work on
them, while /ticket move refuses them. Category, who-to-ping and allow-roles
come from the dashboard's "Claim Tickets" tab (guild_settings.claim_tickets).

Giveaway blacklist: if guild_settings.blacklist.giveaway_role_id is set, any
member with that role is refused entry by GiveawayEnterButton.callback.

Game giveaways (same enter button, same claim flow):

  /giveaway rps         prize time [claim_time]  -> 2 winners play Rock Paper Scissors
  /giveaway splitsteal  prize time [claim_time]  -> 2 winners play Split or Steal

When the giveaway ends, 2 winners are picked and a game message is posted with
buttons only those two can press. Picks stay hidden until both are in (or the
timer runs out), then they're revealed together. The per-round timer is set on the
dashboard's Claim Tickets page. The winner(s) then get the normal Claim button.

More giveaway types (all are /giveaway subcommands, all can be ended with /giveaway end):

  /giveaway membercount  members prize [claim_time] [winners]
        No timer. Ends the moment the server reaches `members` members.
  /giveaway fastestfinger prize [claim_time] [mode]
        Starts instantly, no entries: the code is posted straight away and the first
        person to type it in ANY channel (or press the button) wins.
        mode = number (type only) | button (button only) | empty (both).
  /giveaway guessflag    prize
        Posts a random flag. First person to type the country (in ANY channel) wins.
  /giveaway guessthelanguage prize
        Posts a sentence in a random language. First person to type its name (ANY channel) wins.
  /giveaway doubleornothing prize time [claim_time]
        Normal entry, 1 winner. The winner can Double (risky, odds drop each time)
        or Take the current prize.

Reroll: create / membercount / doubleornothing can be rerolled;
rps / splitsteal / fastestfinger / guessflag / guessthelanguage can't (see /giveaway reroll for the exact rules).

  RPS          draw -> new round; one player doesn't pick -> the other wins;
               nobody picks -> nobody wins.
  Split/Steal  split+split -> both claim (prize split); steal+steal -> nobody;
               steal+split -> the stealer claims it all; a player who doesn't
               pick counts as Split; nobody picks -> nobody wins.
"""
import asyncio
import random
import re
import string
import time as _time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import models
from typing import Literal, Optional

from utils import flags_data, languages_data, ticket_core
from utils.embeds import success_embed, error_embed, info_embed
from utils.permissions import command_check
from cogs.tickets import TicketControlView, category_lock, find_category_by_name

EMBED_COLOR = 0x2B2D31
CLAIM_COLOR = 0x57F287


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ensure_aware(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


_DURATION_RE = re.compile(r"\s*(\d+)\s*([dhms])\s*", re.IGNORECASE)


def parse_duration(text: str):
    if not text or not text.strip():
        return None
    text = text.strip()
    unit_seconds = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    total, pos, matched = 0, 0, False
    for match in _DURATION_RE.finditer(text):
        if match.start() != pos:
            return None
        total += int(match.group(1)) * unit_seconds[match.group(2).lower()]
        pos = match.end()
        matched = True
    if not matched or pos != len(text):
        return None
    return total


def generate_display_id(length: int = 10) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choices(alphabet, k=length))


def pick_winners(entries: list, count: int) -> list:
    unique = list(set(entries))
    if len(unique) <= count:
        return unique
    return random.sample(unique, count)


GAME_NAMES = {"rps": "Rock Paper Scissors", "splitsteal": "Split or Steal"}

KIND_NAMES = {
    "membercount": "Member Count",
    "fastestfinger": "Fastest Finger",
    "doubleornothing": "Double or Nothing",
    "guessflag": "Guess the Flag",
    "guesslanguage": "Guess the Language",
}
KIND_HINTS = {
    "fastestfinger": "*When it ends, the entrants race - the first to type the code or "
                     "press the button wins.*\n",
    "doubleornothing": "*The winner can double their prize - but every double is riskier.*\n",
}

FF_MODE_HINTS = {
    "both": "*When it ends, the entrants race - the first to type the code or "
            "press the button wins.*\n",
    "number": "*When it ends, the entrants race - the first to type the code wins "
              "(in any channel).*\n",
    "button": "*When it ends, the entrants race - the first to press the button wins.*\n",
}

FAR_FUTURE = datetime(2100, 1, 1, tzinfo=timezone.utc)   # "no timer" giveaways
FF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"          # no 0/O/1/I confusion
FF_DELAY = (4.0, 10.0)        # random wait before the code shows (seconds)
FF_WINDOW_SECONDS = 30        # how long entrants have once the code is out
DON_ODDS = [0.5, 0.4, 0.3, 0.2, 0.1]   # chance of each successive Double succeeding
DON_DECISION_SECONDS = 60     # time to press Double / Take before auto-Take
GUESSFLAG_CLAIM_SECONDS = 600
GUESSLANGUAGE_CLAIM_SECONDS = 600


def mult_prize(prize, mult: int) -> str:
    return str(prize) if int(mult or 1) <= 1 else f"{int(mult)}x {prize}"


def display_prize(g: dict) -> str:
    """The prize text for announcements / claim tickets (Double or Nothing: 4x Nitro)."""
    if g.get("kind") == "doubleornothing":
        return mult_prize(g["prize"], g.get("don_mult") or 1)
    return str(g["prize"])


def build_running_embed(g: dict, member_count: int = None) -> discord.Embed:
    kind = g.get("kind")
    if kind == "membercount":
        goal = int(g["member_goal"])
        now_txt = f" (currently **{member_count:,}**)" if member_count is not None else ""
        timing = f"Ends at: **{goal:,}** members{now_txt}\n"
    else:
        end_ts = int(ensure_aware(g["end_time"]).timestamp())
        timing = f"Ends: <t:{end_ts}:R> (<t:{end_ts}:F>)\n"
    game_line = f"Game: **{GAME_NAMES[g['game']]}**\n" if g.get("game") in GAME_NAMES else ""
    kind_line = ""
    if kind in KIND_NAMES:
        hint = KIND_HINTS.get(kind, "")
        if kind == "fastestfinger":
            hint = FF_MODE_HINTS.get(g.get("ff_mode") or "both", hint)
        kind_line = f"Type: **{KIND_NAMES[kind]}**\n" + hint
    return discord.Embed(
        title=g["prize"],
        description=(
            f"{timing}"
            f"Hosted by: <@{g['host_id']}>\n"
            f"{game_line}"
            f"{kind_line}"
            f"Entries: **{len(g.get('entries', []))}**\n"
            f"Winners: **{g['winners_count']}**\n"
            f"ID: {g['display_id']}"
        ),
        color=EMBED_COLOR,
        timestamp=datetime.now(timezone.utc),
    )


def build_ended_embed(g: dict, winner_ids: list, status: str = None) -> discord.Embed:
    """`status` replaces the winners line (e.g. while a Fastest Finger race is on)."""
    end_ts = int(ensure_aware(g["end_time"]).timestamp())
    winner_text = ", ".join(f"<@{w}>" for w in winner_ids) if winner_ids else "No winners"
    is_game = g.get("game") in GAME_NAMES
    winners_label = "Players" if is_game and winner_ids else "Winners"
    game_line = f"Game: **{GAME_NAMES[g['game']]}**\n" if is_game else ""
    kind = g.get("kind")
    kind_line = f"Type: **{KIND_NAMES[kind]}**\n" if kind in KIND_NAMES else ""
    goal_line = f"Goal: **{int(g['member_goal']):,}** members\n" if kind == "membercount" else ""
    winners_line = status if status else f"{winners_label}: {winner_text}"
    embed = discord.Embed(
        title=g["prize"],
        description=(
            f"Ended: <t:{end_ts}:R> (<t:{end_ts}:F>)\n"
            f"Hosted by: <@{g['host_id']}>\n"
            f"Prize: **{display_prize(g)}**\n"
            f"{game_line}"
            f"{kind_line}"
            f"{goal_line}"
            f"Entries: **{len(g.get('entries', []))}**\n"
            f"{winners_line}\n"
            f"ID: {g['display_id']}"
        ),
        color=EMBED_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="🔴 Ended")
    return embed


def build_flag_embed(g: dict, winner_id=None, ended: bool = False) -> discord.Embed:
    """/giveaway guessflag message. The answer is only shown once it's over."""
    code = g["flag_code"]
    entry = flags_data.find_by_code(code)
    name = entry[1] if entry else code.upper()
    lines = [f"Prize: **{g['prize']}**", f"Hosted by: <@{g['host_id']}>", ""]
    if ended:
        lines.append(f"Answer: **{name}**")
        lines.append(f"Winner: <@{winner_id}>" if winner_id else "Winner: nobody - it was ended without a correct guess.")
    else:
        lines.append("Which country's flag is this?\n"
                     "Type the name in **any channel** - the first correct answer wins!")
    lines.append(f"ID: {g['display_id']}")
    embed = discord.Embed(
        title="🏳️ Guess the Flag", description="\n".join(lines),
        color=CLAIM_COLOR if winner_id else EMBED_COLOR, timestamp=datetime.now(timezone.utc),
    )
    embed.set_image(url=flags_data.flag_url(code))
    if ended:
        embed.set_footer(text="🔴 Ended")
    return embed


def build_language_embed(g: dict, winner_id=None, ended: bool = False) -> discord.Embed:
    """/giveaway guessthelanguage message. The answer is only shown once it's over."""
    entry = languages_data.find_by_key(g["language_code"])
    name = entry[1] if entry else str(g["language_code"]).upper()
    sample = entry[3] if entry else "?"
    lines = [f"Prize: **{g['prize']}**", f"Hosted by: <@{g['host_id']}>", "",
             f"> {sample}", ""]
    if ended:
        lines.append(f"Answer: **{name}**")
        lines.append(f"Winner: <@{winner_id}>" if winner_id
                     else "Winner: nobody - it was ended without a correct guess.")
    else:
        lines.append("Which language is this?\n"
                     "Type the name in **any channel** - the first correct answer wins!")
    lines.append(f"ID: {g['display_id']}")
    embed = discord.Embed(
        title="🗣️ Guess the Language", description="\n".join(lines),
        color=CLAIM_COLOR if winner_id else EMBED_COLOR, timestamp=datetime.now(timezone.utc),
    )
    if ended:
        embed.set_footer(text="🔴 Ended")
    return embed


def build_ff_embed(g: dict, state: str, winner_id=None) -> discord.Embed:
    prize = g["prize"]
    n = len(g.get("ff_eligible") or [])
    if state == "countdown":
        embed = discord.Embed(
            title="⚡ Fastest Finger - get ready!",
            description=(
                f"Prize: **{prize}**\n"
                f"**{n}** entrant{'s' if n != 1 else ''} in the race. Only they can win.\n\n"
                "A code will appear in a moment - **don't jump the gun!**"
            ),
            color=EMBED_COLOR,
        )
    elif state == "live":
        deadline = int(ensure_aware(g["ff_deadline"]).timestamp())
        mode = g.get("ff_mode") or "both"
        if mode == "number":
            how = f"Type **`{g['ff_code']}`** in **any channel**!"
        elif mode == "button":
            how = "Press the button!"
        else:
            how = f"Type **`{g['ff_code']}`** in **any channel**, or press the button!"
        embed = discord.Embed(
            title="⚡ GO GO GO!",
            description=(
                f"Prize: **{prize}**\n\n"
                f"{how}\n"
                f"Closes <t:{deadline}:R>"
            ),
            color=0xFEE75C,
        )
    elif state == "won":
        embed = discord.Embed(
            title="⚡ Fastest Finger - Result",
            description=f"Prize: **{prize}**\n\n🏆 <@{winner_id}> was the fastest!",
            color=CLAIM_COLOR,
        )
    else:
        embed = discord.Embed(
            title="⚡ Fastest Finger - Result",
            description=f"Prize: **{prize}**\n\n{state}",
            color=EMBED_COLOR,
        )
    embed.set_footer(text=f"ID: {g['display_id']}")
    return embed


def build_don_embed(g: dict, result: str = None, won: bool = False) -> discord.Embed:
    prize = g["prize"]
    mult = int(g.get("don_mult") or 1)
    step = int(g.get("don_step") or 0)
    lines = [f"Winner: <@{g.get('don_winner')}>", f"Current prize: **{mult_prize(prize, mult)}**"]
    if result:
        lines += ["", result]
    elif step < len(DON_ODDS):
        odds = int(round(DON_ODDS[step] * 100))
        deadline = int(ensure_aware(g["don_deadline"]).timestamp())
        lines += [
            "",
            f"🎲 **Double** - **{odds}%** chance to win **{mult_prize(prize, mult * 2)}**, "
            "otherwise you lose everything.",
            f"💰 **Take** - keep **{mult_prize(prize, mult)}** now.",
            "",
            f"Decide <t:{deadline}:R> or you'll automatically take it.",
        ]
    embed = discord.Embed(
        title="🎲 Double or Nothing", description="\n".join(lines),
        color=CLAIM_COLOR if won else EMBED_COLOR, timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"ID: {g['display_id']}")
    return embed


# ---------------------------------------------------------------------------
# Claim ticket creation
# ---------------------------------------------------------------------------

_category_lock = asyncio.Lock()


async def _resolve_claim_category(guild: discord.Guild, cfg: dict):
    raw = cfg.get("category_id")
    if not raw:
        return None

    if str(raw).startswith("__new__:"):
        async with _category_lock:
            fresh = (await models.get_claim_ticket_config(guild.id)).get("category_id")
            if fresh and not str(fresh).startswith("__new__:"):
                raw = fresh
            else:
                name = str(raw)[len("__new__:"):].strip() or "Claim Tickets"
                category = find_category_by_name(guild, name)
                if category is None:
                    try:
                        category = await guild.create_category(name=name, reason="Claim ticket setup")
                    except discord.HTTPException:
                        return None
                await models.update_guild_settings(
                    guild.id, {"claim_tickets.category_id": str(category.id)}
                )
                return category

    try:
        category = guild.get_channel(int(raw))
    except (TypeError, ValueError):
        return None
    return category if isinstance(category, discord.CategoryChannel) else None


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


def _slug(text: str, fallback: str, limit: int) -> str:
    """Lowercase, dash-separated, channel-name-safe slice of `text`."""
    out = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:limit].strip("-")
    return out or fallback


async def _host_name(guild: discord.Guild, host_id) -> str:
    """Host's username for the channel name (falls back to the raw ID)."""
    try:
        member = guild.get_member(int(host_id))
        if member is None:
            member = await guild.fetch_member(int(host_id))
        return member.name
    except (discord.HTTPException, ValueError, TypeError):
        return str(host_id)


async def create_claim_ticket(interaction: discord.Interaction, message_id: str, qa: list):
    guild = interaction.guild
    user = interaction.user

    g = await models.get_giveaway(message_id)
    if not g:
        return await interaction.followup.send(
            embed=error_embed("This giveaway no longer exists."), ephemeral=True
        )

    if not await models.reserve_giveaway_claim(message_id, user.id):
        return await interaction.followup.send(
            embed=error_embed("You already claimed your prize!"), ephemeral=True
        )

    cfg = await models.get_claim_ticket_config(guild.id)
    try:
        allow_roles = _roles_from_ids(guild, cfg["allow_role_ids"])

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True
            ),
            user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
        }
        for role in allow_roles:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

        host_name = await _host_name(guild, g.get("host_id"))
        channel_name = (
            f"claim-{_slug(g['prize'], 'prize', 40)}-{_slug(host_name, 'host', 32)}"
        )[:100]

        async with category_lock(guild.id):
            category, overflow = await ticket_core.kind_category_plan(
                guild, await _resolve_claim_category(guild, cfg)
            )
            channel = await ticket_core.create_ticket_channel(
                guild, category, overflow,
                name=channel_name,
                overwrites=overwrites,
                topic=f"Prize claim | {user} | {g['prize']}"[:1024],
                reason=f"Giveaway prize claim by {user}",
            )
    except ticket_core.CategoriesFull as e:
        await models.release_giveaway_claim(message_id, user.id)
        await ticket_core.alert_staff_full(guild, e.names, "claim ticket")
        return await interaction.followup.send(
            embed=error_embed(ticket_core.FULL_MESSAGE), ephemeral=True
        )
    except discord.HTTPException as e:
        await models.release_giveaway_claim(message_id, user.id)
        return await interaction.followup.send(
            embed=error_embed(f"Couldn't create your claim ticket: {e}"), ephemeral=True
        )

    await models.create_ticket(
        guild.id, channel.id, user.id,
        panel_name="Prize Claim", ticket_type_id=None, kind="claim",
        extra={
            "answers": [{"question": q, "answer": a} for q, a in qa],
            "giveaway_message_id": str(message_id),
        },
    )
    await models.set_giveaway_claim_channel(message_id, user.id, channel.id)

    link = f"https://discord.com/channels/{guild.id}/{g['channel_id']}/{g['message_id']}"
    embed = discord.Embed(
        title="🎉 Prize Claim",
        description=(
            f"Ticket opened by {user.mention}.\n"
            "Staff will process your claim shortly. "
            "Use the button below to close it when resolved."
        ),
        color=CLAIM_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    prize_text = display_prize(g)
    if g.get("game_split"):
        prize_text += "\n*(Split Or Steal - both players chose Split, so this prize is split between the two winners.)*"
    embed.add_field(name="Prize", value=prize_text[:1000], inline=True)
    embed.add_field(name="Host", value=f"<@{g['host_id']}>", inline=True)
    embed.add_field(name="Original Giveaway", value=f"[Click here]({link})", inline=True)
    for i, (question, answer) in enumerate(qa, 1):
        embed.add_field(
            name=f"{i}. {question}"[:250],
            value=(answer or "*(no answer)*")[:1000],
            inline=False,
        )

    ping = user.mention
    host_mode = cfg.get("ping_mode") == "host"
    if host_mode:
        # Ping mode "host": opener + the giveaway host, no roles.
        if str(g.get("host_id")) != str(user.id):
            ping += f" <@{g['host_id']}>"
    else:
        for role in _roles_from_ids(guild, cfg["ping_role_ids"]):
            ping += f" {role.mention}"
    print(f"[giveaways] claim ticket in guild {guild.id}: ping_mode={cfg.get('ping_mode')!r} -> {ping}")

    # In host mode role pings are hard-blocked, so a role can never ring even by accident.
    mentions = discord.AllowedMentions(users=True, roles=not host_mode, everyone=False)
    welcome = await channel.send(
        content=ping, embed=embed, view=TicketControlView(), allowed_mentions=mentions
    )
    await models.update_ticket(channel.id, welcome_message_id=str(welcome.id))

    await interaction.followup.send(
        embed=success_embed(f"Your claim ticket has been created: {channel.mention}"),
        ephemeral=True,
    )


class ClaimQuestionsModal(discord.ui.Modal):
    def __init__(self, message_id: str, questions: list):
        super().__init__(title="Claim Your Prize")
        self.message_id = message_id
        self.questions = [q[:45] for q in questions[:5]] or ["What is your Minecraft IGN?"]
        for i, q in enumerate(self.questions, 1):
            self.add_item(discord.ui.TextInput(
                label=q,
                placeholder="Type your answer here...",
                style=discord.TextStyle.paragraph,
                required=(i == 1),
                max_length=800,
                custom_id=f"q{i}",
            ))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        qa = [(q, (child.value or "").strip())
              for q, child in zip(self.questions, self.children)]
        await create_claim_ticket(interaction, self.message_id, qa)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        import traceback
        traceback.print_exception(type(error), error, error.__traceback__)
        try:
            embed = error_embed("Something went wrong submitting your claim.")
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------------------
# Persistent buttons (DynamicItem)
# ---------------------------------------------------------------------------

class GiveawayEnterButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"giveaway_enter:(?P<mid>[0-9]+)"
):
    def __init__(self, message_id):
        super().__init__(discord.ui.Button(
            emoji="🚀",
            style=discord.ButtonStyle.blurple,
            custom_id=f"giveaway_enter:{message_id}",
        ))
        self.message_id = str(message_id)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match, /):
        return cls(match["mid"])

    async def callback(self, interaction: discord.Interaction):
        g = await models.get_giveaway(self.message_id)
        if (not g or g.get("ended")
                or datetime.now(timezone.utc) > ensure_aware(g["end_time"])):
            return await interaction.response.send_message(
                embed=error_embed("This giveaway has already ended!"), ephemeral=True
            )

        # Blacklist check
        blacklist = await models.get_blacklist_config(interaction.guild.id)
        bl_role_id = blacklist["giveaway_role_id"]
        if bl_role_id:
            member = interaction.guild.get_member(interaction.user.id)
            if member and any(str(r.id) == str(bl_role_id) for r in member.roles):
                return await interaction.response.send_message(
                    embed=error_embed("You're blacklisted from joining giveaways."),
                    ephemeral=True,
                )

        if not await models.add_giveaway_entry(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                embed=error_embed("You have already entered this giveaway!"), ephemeral=True
            )

        await interaction.response.send_message(
            embed=success_embed("You have entered the giveaway! Good luck! 🎉"), ephemeral=True
        )
        try:
            fresh = await models.get_giveaway(self.message_id)
            await interaction.message.edit(embed=build_running_embed(
                fresh, interaction.guild.member_count))
        except discord.HTTPException as e:
            print(f"Failed to update giveaway entry count: {e}")


class WinnerClaimButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"giveaway_claim:(?P<mid>[0-9]+)"
):
    def __init__(self, message_id):
        super().__init__(discord.ui.Button(
            label="Claim",
            style=discord.ButtonStyle.green,
            custom_id=f"giveaway_claim:{message_id}",
        ))
        self.message_id = str(message_id)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match, /):
        return cls(match["mid"])

    async def callback(self, interaction: discord.Interaction):
        g = await models.get_giveaway(self.message_id)
        if not g:
            return await interaction.response.send_message(
                embed=error_embed("This giveaway no longer exists."), ephemeral=True
            )

        claim_end = ensure_aware(g.get("claim_end_time"))
        if g.get("claim_expired") or (claim_end and datetime.now(timezone.utc) > claim_end):
            await interaction.response.send_message(
                embed=error_embed("The claim period has expired!"), ephemeral=True
            )
            try:
                await interaction.message.edit(view=None)
            except discord.HTTPException:
                pass
            return

        uid = str(interaction.user.id)
        if uid not in g.get("winners", []):
            return await interaction.response.send_message(
                embed=error_embed("You are not one of the giveaway winners!"), ephemeral=True
            )

        if uid in g.get("claimed_users", []):
            existing = (g.get("claim_channels") or {}).get(uid)
            where = ""
            if existing and interaction.guild.get_channel(int(existing)):
                where = f" It's here: <#{existing}>"
            return await interaction.response.send_message(
                embed=error_embed(f"You already claimed your prize!{where}"), ephemeral=True
            )

        cfg = await models.get_claim_ticket_config(interaction.guild.id)
        await interaction.response.send_modal(
            ClaimQuestionsModal(self.message_id, cfg["questions"])
        )


# ---------------------------------------------------------------------------
# Game giveaways: embeds + persistent pick buttons
# ---------------------------------------------------------------------------

RPS_CHOICES = {"rock": ("Rock", "🪨"), "paper": ("Paper", "📄"), "scissors": ("Scissors", "✂️")}
SS_CHOICES = {"split": ("Split", "🤝"), "steal": ("Steal", "💰")}
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
GAME_TITLES = {"rps": "🪨 Rock Paper Scissors", "splitsteal": "🤝 Split or Steal"}
GAME_RULES = {
    "rps": "Pick Rock, Paper or Scissors. Picks stay hidden until both players have "
           "picked. A draw means another round. If you don't pick in time, you forfeit.",
    "splitsteal": "Both **Split** - the prize is split. Both **Steal** - nobody wins. "
                  "One **Steal**, one **Split** - the stealer takes it all. "
                  "No pick counts as Split.",
}


def _choice_table(game: str) -> dict:
    return RPS_CHOICES if game == "rps" else SS_CHOICES


def _choice_label(game: str, choice) -> str:
    if not choice:
        return "*(no pick)*"
    name, emoji = _choice_table(game)[choice]
    return f"{emoji} **{name}**"


def build_game_embed(g: dict) -> discord.Embed:
    """The live game message: who has picked (never WHAT), the timer, past rounds."""
    game = g["game"]
    players = g.get("game_players") or []
    choices = g.get("game_choices") or {}
    deadline = int(ensure_aware(g["game_deadline"]).timestamp())

    lines = [f"Prize: **{g['prize']}**"]
    if game == "rps":
        lines.append(f"Round **{g.get('game_round', 1)}** - picks close <t:{deadline}:R>")
    else:
        lines.append(f"Picks close <t:{deadline}:R>")
    lines.append("")
    for p in players:
        lines.append(f"<@{p}> - " + ("✅ Picked" if str(p) in choices else "⏳ Waiting"))
    lines.append("")
    lines.append(GAME_RULES[game])
    log = g.get("game_log") or []
    if log:
        lines.append("")
        lines.extend(log[-5:])
    embed = discord.Embed(
        title=GAME_TITLES[game], description="\n".join(lines),
        color=EMBED_COLOR, timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"ID: {g['display_id']}")
    return embed


def build_game_result_embed(g: dict, choices: dict, outcome: str, won: bool) -> discord.Embed:
    game = g["game"]
    players = g.get("game_players") or []
    lines = [f"Prize: **{g['prize']}**", ""]
    for p in players:
        lines.append(f"<@{p}> picked {_choice_label(game, choices.get(str(p)))}")
    lines.append("")
    lines.append(outcome)
    log = g.get("game_log") or []
    if log:
        lines.append("")
        lines.extend(log[-5:])
    embed = discord.Embed(
        title=f"{GAME_TITLES[game]} - Result", description="\n".join(lines),
        color=CLAIM_COLOR if won else EMBED_COLOR, timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"ID: {g['display_id']}")
    return embed


class GameChoiceButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"giveaway_game:(?P<mid>[0-9]+):(?P<choice>rock|paper|scissors|split|steal)",
):
    STYLES = {
        "rock": discord.ButtonStyle.secondary,
        "paper": discord.ButtonStyle.secondary,
        "scissors": discord.ButtonStyle.secondary,
        "split": discord.ButtonStyle.success,
        "steal": discord.ButtonStyle.danger,
    }

    def __init__(self, message_id, choice: str):
        table = RPS_CHOICES if choice in RPS_CHOICES else SS_CHOICES
        name, emoji = table[choice]
        super().__init__(discord.ui.Button(
            label=name, emoji=emoji, style=self.STYLES[choice],
            custom_id=f"giveaway_game:{message_id}:{choice}",
        ))
        self.message_id = str(message_id)
        self.choice = choice

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match, /):
        return cls(match["mid"], match["choice"])

    async def callback(self, interaction: discord.Interaction):
        g = await models.get_giveaway(self.message_id)
        if not g or not g.get("game_active"):
            return await interaction.response.send_message(
                embed=error_embed("This game is already over."), ephemeral=True
            )

        uid = str(interaction.user.id)
        if uid not in (g.get("game_players") or []):
            return await interaction.response.send_message(
                embed=error_embed("You're not one of the two players in this game."),
                ephemeral=True,
            )
        game = g.get("game")
        if self.choice not in _choice_table(game):
            return await interaction.response.send_message(
                embed=error_embed("That button doesn't belong to this game."), ephemeral=True
            )
        if uid in (g.get("game_choices") or {}):
            return await interaction.response.send_message(
                embed=error_embed("You already picked - picks can't be changed."), ephemeral=True
            )

        updated = await models.game_record_choice(
            self.message_id, uid, g.get("game_round", 1), self.choice
        )
        if updated is None:
            return await interaction.response.send_message(
                embed=error_embed(
                    "Couldn't save your pick - the round may have just ended. "
                    "Check the game message."
                ),
                ephemeral=True,
            )

        await interaction.response.send_message(
            embed=success_embed(
                f"You picked {_choice_label(game, self.choice)}. "
                "It stays hidden until both players have picked."
            ),
            ephemeral=True,
        )
        cog = interaction.client.get_cog("Giveaways")
        if cog:
            await cog.after_pick(updated)


def build_game_view(message_id, game: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for choice in _choice_table(game):
        view.add_item(GameChoiceButton(message_id, choice))
    return view


# ---------------------------------------------------------------------------
# Fastest Finger + Double or Nothing buttons (persistent)
# ---------------------------------------------------------------------------

async def _is_giveaway_blacklisted(member) -> bool:
    try:
        cfg = await models.get_blacklist_config(member.guild.id)
        role_id = cfg["giveaway_role_id"]
        return bool(role_id and any(str(r.id) == str(role_id) for r in member.roles))
    except Exception:
        return False


class FastestFingerButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"giveaway_ff:(?P<mid>[0-9]+)"
):
    def __init__(self, message_id):
        super().__init__(discord.ui.Button(
            label="CLICK!", emoji="⚡", style=discord.ButtonStyle.success,
            custom_id=f"giveaway_ff:{message_id}",
        ))
        self.message_id = str(message_id)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match, /):
        return cls(match["mid"])

    async def callback(self, interaction: discord.Interaction):
        g = await models.get_giveaway(self.message_id)
        if not g or g.get("ff_state") != "live":
            return await interaction.response.send_message(
                embed=error_embed("Too late - this race is already over."), ephemeral=True
            )
        if (g.get("ff_mode") or "both") == "number":
            return await interaction.response.send_message(
                embed=error_embed("This race is type-the-code only - the button doesn't count."),
                ephemeral=True,
            )
        if g.get("ff_open") and await _is_giveaway_blacklisted(interaction.user):
            return await interaction.response.send_message(
                embed=error_embed("You're blacklisted from giveaways."), ephemeral=True
            )
        if not g.get("ff_open") and str(interaction.user.id) not in (g.get("ff_eligible") or []):
            return await interaction.response.send_message(
                embed=error_embed("Only people who entered this giveaway can win it."),
                ephemeral=True,
            )
        cog = interaction.client.get_cog("Giveaways")
        # Winning posts several messages; acknowledge first so Discord doesn't time out.
        await interaction.response.defer(ephemeral=True)
        won = await cog._ff_win(g, interaction.user.id) if cog else False
        if won:
            await interaction.followup.send(
                embed=success_embed("You were the fastest! 🏆"), ephemeral=True
            )
        else:
            await interaction.followup.send(
                embed=error_embed("Someone else was faster!"), ephemeral=True
            )


class DoubleOrNothingButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"giveaway_don:(?P<mid>[0-9]+):(?P<action>double|take)",
):
    def __init__(self, message_id, action: str):
        if action == "double":
            button = discord.ui.Button(
                label="Double", emoji="🎲", style=discord.ButtonStyle.danger,
                custom_id=f"giveaway_don:{message_id}:double",
            )
        else:
            button = discord.ui.Button(
                label="Take", emoji="💰", style=discord.ButtonStyle.success,
                custom_id=f"giveaway_don:{message_id}:take",
            )
        super().__init__(button)
        self.message_id = str(message_id)
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match, /):
        return cls(match["mid"], match["action"])

    async def callback(self, interaction: discord.Interaction):
        g = await models.get_giveaway(self.message_id)
        if not g or not g.get("don_active"):
            return await interaction.response.send_message(
                embed=error_embed("This Double or Nothing is already over."), ephemeral=True
            )
        if str(interaction.user.id) != str(g.get("don_winner")):
            return await interaction.response.send_message(
                embed=error_embed("Only the winner can make this choice."), ephemeral=True
            )
        cog = interaction.client.get_cog("Giveaways")
        if cog is None:
            return await interaction.response.send_message(
                embed=error_embed("Giveaways aren't available right now."), ephemeral=True
            )
        await interaction.response.defer()
        if self.action == "take":
            await cog._don_take(g)
        else:
            await cog._don_double(g)


def build_don_view(message_id, step: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if step < len(DON_ODDS):
        view.add_item(DoubleOrNothingButton(message_id, "double"))
    view.add_item(DoubleOrNothingButton(message_id, "take"))
    return view


# ---------------------------------------------------------------------------
# /giveaway create flags
# ---------------------------------------------------------------------------

class CreateFlags(commands.FlagConverter):
    prize: str = commands.flag(
        description="What the winner(s) get (also used as the giveaway title)"
    )
    time: str = commands.flag(
        description="How long it runs, e.g. 1h, 30m, 2d, 45s (combos like 1d12h work)"
    )
    claim_time: str = commands.flag(
        default="10m", description="How long winners have to claim, e.g. 10m, 1h (default 10m)"
    )
    winners: int = commands.flag(default=1, description="Number of winners (default 1)")


class GameFlags(commands.FlagConverter):
    """Same flags as /giveaway create. Games always have exactly 2 winners."""
    prize: str = commands.flag(
        description="What the winner(s) get (also used as the giveaway title)"
    )
    time: str = commands.flag(
        description="How long entries stay open, e.g. 1h, 30m, 2d (combos like 1d12h work)"
    )
    claim_time: str = commands.flag(
        default="10m", description="How long winners have to claim, e.g. 10m, 1h (default 10m)"
    )
    winners: int = commands.flag(
        default=2, description="Ignored - games always have exactly 2 winners"
    )


class MemberCountFlags(commands.FlagConverter):
    members: int = commands.flag(
        description="Member count that ends the giveaway (must be higher than the current count)"
    )
    prize: str = commands.flag(
        description="What the winner(s) get (also used as the giveaway title)"
    )
    claim_time: str = commands.flag(
        default="10m", description="How long winners have to claim, e.g. 10m, 1h (default 10m)"
    )
    winners: int = commands.flag(default=1, description="Number of winners (default 1)")


class FastestFingerFlags(commands.FlagConverter):
    prize: str = commands.flag(
        description="What the winner gets (also used as the title)"
    )
    mode: Optional[Literal["number", "button"]] = commands.flag(
        default=None,
        description="number = type the code, button = press the button (leave empty for both)",
    )
    claim_time: str = commands.flag(
        default="10m", description="How long the winner has to claim, e.g. 10m, 1h (default 10m)"
    )


class DoubleOrNothingFlags(commands.FlagConverter):
    prize: str = commands.flag(
        description="What the winner gets (also used as the giveaway title)"
    )
    time: str = commands.flag(
        description="How long entries stay open, e.g. 1h, 30m, 2d (combos like 1d12h work)"
    )
    claim_time: str = commands.flag(
        default="10m", description="How long the winner has to claim, e.g. 10m, 1h (default 10m)"
    )


class GuessFlagFlags(commands.FlagConverter):
    prize: str = commands.flag(description="What the winner gets (also used as the title)")


class GuessLanguageFlags(commands.FlagConverter):
    prize: str = commands.flag(description="What the winner gets (also used as the title)")


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Giveaways(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ff_live = {}        # guild_id -> {giveaway message_id: (code lowercase, mode)}
        self.flag_live = {}      # guild_id   -> {"mid": ..., "answers": {...}}
        self.lang_live = {}      # guild_id   -> {"mid": ..., "answers": {...}}
        self._count_edit = {}    # giveaway message_id -> last member-count embed edit
        self._restored = False

    async def cog_load(self):
        self.bot.add_dynamic_items(
            GiveawayEnterButton, WinnerClaimButton, GameChoiceButton,
            FastestFingerButton, DoubleOrNothingButton,
        )
        self.check_giveaways.start()
        self.check_games.start()
        if self.bot.is_ready():
            asyncio.create_task(self._restore_caches())

    # ------------------------------------------------------------------
    # In-memory lookups for the on_message listeners (rebuilt from Mongo on start)
    # ------------------------------------------------------------------
    def _cache_ff(self, g: dict):
        self.ff_live.setdefault(int(g["guild_id"]), {})[str(g["message_id"])] = (
            str(g["ff_code"]).lower(), g.get("ff_mode") or "both",
        )

    def _uncache_ff(self, g: dict):
        live = self.ff_live.get(int(g["guild_id"]))
        if live is not None:
            live.pop(str(g["message_id"]), None)
            if not live:
                self.ff_live.pop(int(g["guild_id"]), None)

    def _cache_language(self, g: dict):
        entry = languages_data.find_by_key(g.get("language_code"))
        if entry:
            self.lang_live[int(g["guild_id"])] = {
                "mid": g["message_id"], "answers": languages_data.answers_for(entry),
            }

    def _cache_flag(self, g: dict):
        entry = flags_data.find_by_code(g.get("flag_code"))
        if entry:
            self.flag_live[int(g["guild_id"])] = {
                "mid": g["message_id"], "answers": flags_data.answers_for(entry),
            }

    async def _restore_caches(self):
        if self._restored:
            return
        self._restored = True
        try:
            for g in await models.get_active_ff_live():
                self._cache_ff(g)
            for g in await models.get_active_flag_game():
                self._cache_flag(g)
            for g in await models.get_active_language_game():
                self._cache_language(g)
        except Exception as e:
            self._restored = False
            print(f"[giveaways] cache restore failed: {e}")

    @commands.Cog.listener()
    async def on_ready(self):
        await self._restore_caches()

    async def cog_unload(self):
        self.check_giveaways.cancel()
        self.check_games.cancel()

    # ------------------------------------------------------------------
    # Game timeouts (5s loop so a 60s round closes on time)
    # ------------------------------------------------------------------
    @tasks.loop(seconds=5)
    async def check_games(self):
        try:
            now = datetime.now(timezone.utc)
            for g in await models.get_due_games(now):
                try:
                    await self._resolve_game(g)
                except Exception as e:
                    print(f"[giveaways] failed to resolve game {g.get('message_id')}: {e}")
            for g in await models.get_due_fastest(now):
                try:
                    if g.get("ff_state") == "countdown":
                        await self._ff_arm(g["message_id"])
                    else:
                        await self._ff_timeout(g)
                except Exception as e:
                    print(f"[giveaways] fastest finger {g.get('message_id')}: {e}")
            for g in await models.get_due_don(now):
                try:
                    await self._don_take(g, note="⏰ Time's up - the prize was taken automatically.")
                except Exception as e:
                    print(f"[giveaways] double or nothing {g.get('message_id')}: {e}")
        except Exception as e:
            print(f"[giveaways] game loop error: {e}")

    @check_games.before_loop
    async def _before_games(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=30)
    async def check_giveaways(self):
        try:
            now = datetime.now(timezone.utc)
            for g in await models.get_due_giveaways(now):
                try:
                    await self._end_giveaway(g)
                except Exception as e:
                    print(f"[giveaways] failed to end {g.get('message_id')}: {e}")
            # Backup for the member-join listener (member-count giveaways)
            for g in await models.get_active_membercount():
                try:
                    guild = self.bot.get_guild(int(g["guild_id"]))
                    if guild and guild.member_count >= int(g["member_goal"]):
                        await self._end_giveaway(g)
                except Exception as e:
                    print(f"[giveaways] member count check {g.get('message_id')}: {e}")
            for g in await models.get_expired_claims(now):
                try:
                    await self._expire_claim(g)
                except Exception as e:
                    print(f"[giveaways] failed to expire claim {g.get('message_id')}: {e}")
        except Exception as e:
            print(f"[giveaways] loop error: {e}")

    @check_giveaways.before_loop
    async def _before_check(self):
        await self.bot.wait_until_ready()

    async def _post_winners(self, channel, prize, message_id, winners, claim_seconds,
                            rerolled: bool = False, intro: str = None):
        claim_end = datetime.now(timezone.utc) + timedelta(seconds=claim_seconds)
        mentions = ", ".join(f"<@{w}>" for w in winners)
        suffix = " (Rerolled)" if rerolled else ""
        view = discord.ui.View(timeout=None)
        view.add_item(WinnerClaimButton(message_id))
        msg = await channel.send(
            content=(
                f"{intro or f'🎉 Congratulations {mentions}, you won **{prize}**!{suffix}'}\n"
                f"Press **Claim** below - the claim window closes "
                f"<t:{int(claim_end.timestamp())}:R>."
            ),
            view=view,
        )
        return msg, claim_end

    async def _end_giveaway(self, g: dict) -> bool:
        message_id = g["message_id"]
        snap = await models.mark_giveaway_ended(message_id)
        if snap is None:
            return False
        kind = snap.get("kind")

        # No-timer giveaways end "now" - show that instead of the placeholder date.
        if kind in ("membercount", "guessflag", "guesslanguage"):
            snap["end_time"] = datetime.now(timezone.utc)
            await models.update_giveaway(message_id, end_time=snap["end_time"])
        if kind == "guessflag":
            self.flag_live.pop(int(snap["guild_id"]), None)
        if kind == "guesslanguage":
            self.lang_live.pop(int(snap["guild_id"]), None)

        channel = self.bot.get_channel(int(snap["channel_id"]))
        if channel is None:
            await models.delete_giveaway(message_id)
            return True
        try:
            msg = await channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden):
            await models.delete_giveaway(message_id)
            return True

        if kind == "guesslanguage":
            try:
                await msg.edit(embed=build_language_embed(snap, None, ended=True), view=None)
            except discord.HTTPException as e:
                print(f"[giveaways] couldn't edit ended language embed: {e}")
            await models.update_giveaway(message_id, winners=[], claim_expired=True)
            return True

        if kind == "guessflag":
            # Ended by staff with nobody guessing it.
            try:
                await msg.edit(embed=build_flag_embed(snap, None, ended=True), view=None)
            except discord.HTTPException as e:
                print(f"[giveaways] couldn't edit ended flag embed: {e}")
            await models.update_giveaway(message_id, winners=[], claim_expired=True)
            return True

        if kind == "fastestfinger" and snap.get("ff_open"):
            # Instant race ended by staff while still live.
            before = await models.ff_set_state(
                message_id, ["countdown", "live"], "timeout", winners=[], claim_expired=True,
                end_time=datetime.now(timezone.utc),
            )
            self._uncache_ff(snap)
            if before is not None:
                fresh = await models.get_giveaway(message_id) or snap
                try:
                    await msg.edit(
                        embed=build_ff_embed(fresh, "🛑 Ended early by staff - no winner."),
                        view=None,
                    )
                except discord.HTTPException:
                    pass
            return True

        if kind == "fastestfinger":
            entrants = sorted(set(snap.get("entries", [])))
            if not entrants:
                try:
                    await msg.edit(embed=build_ended_embed(snap, []), view=None)
                except discord.HTTPException as e:
                    print(f"[giveaways] couldn't edit ended embed: {e}")
                await models.update_giveaway(message_id, winners=[], claim_expired=True)
                return True
            try:
                await msg.edit(embed=build_ended_embed(
                    snap, [], status="Winner: ⚡ Fastest Finger race in progress..."
                ), view=None)
            except discord.HTTPException as e:
                print(f"[giveaways] couldn't edit ended embed: {e}")
            await self._start_fastest(snap, channel, entrants)
            return True

        winners = pick_winners(snap.get("entries", []), snap["winners_count"])
        is_game = snap.get("game") in GAME_NAMES
        if is_game and len(winners) < 2:
            winners = []      # a game needs two players
        try:
            await msg.edit(embed=build_ended_embed(snap, winners), view=None)
        except discord.HTTPException as e:
            print(f"[giveaways] couldn't edit ended embed: {e}")

        if not winners:
            await models.update_giveaway(message_id, winners=[], claim_expired=True)
            return True

        if kind == "doubleornothing":
            await self._start_don(snap, channel, winners[0])
            return True

        if is_game:
            await self._start_game(snap, channel, winners)
            return True

        ann, claim_end = await self._post_winners(
            channel, snap["prize"], message_id, winners, snap["claim_time_seconds"]
        )
        await models.update_giveaway(
            message_id, winners=winners,
            announcement_message_id=str(ann.id), claim_end_time=claim_end,
        )
        return True

    async def _refresh_ended(self, g: dict, winners: list, status: str = None):
        """Re-draw the original giveaway embed (after a race / double-or-nothing)."""
        if g.get("ff_open"):
            return      # instant race: the race message already shows the result
        channel = self.bot.get_channel(int(g["channel_id"]))
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(int(g["message_id"]))
            await msg.edit(embed=build_ended_embed(g, winners, status=status), view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    # ------------------------------------------------------------------
    # Game engine
    # ------------------------------------------------------------------
    async def _game_timer(self, guild_id, game: str) -> int:
        cfg = await models.get_claim_ticket_config(int(guild_id))
        return cfg["rps_timer"] if game == "rps" else cfg["splitsteal_timer"]

    async def _start_game(self, snap: dict, channel, players: list):
        game = snap["game"]
        mid = snap["message_id"]
        timer = await self._game_timer(snap["guild_id"], game)
        deadline = datetime.now(timezone.utc) + timedelta(seconds=timer)
        await models.update_giveaway(
            mid, winners=[], game_players=[str(p) for p in players], game_round=1,
            game_choices={}, game_deadline=deadline, game_active=True,
            game_log=[], game_message_id=None,
        )
        g = await models.get_giveaway(mid)
        try:
            msg = await channel.send(
                content=" ".join(f"<@{p}>" for p in players) + " - you're up!",
                embed=build_game_embed(g),
                view=build_game_view(mid, game),
            )
        except discord.HTTPException as e:
            print(f"[giveaways] couldn't post game message: {e}")
            await models.update_giveaway(mid, game_active=False, claim_expired=True)
            return
        await models.update_giveaway(mid, game_message_id=str(msg.id))

    async def _game_message(self, g: dict):
        channel = self.bot.get_channel(int(g["channel_id"]))
        mid = g.get("game_message_id")
        if channel is None or not mid:
            return None
        try:
            return await channel.fetch_message(int(mid))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def after_pick(self, g: dict):
        """Called by a pick button once the pick is saved."""
        players = g.get("game_players") or []
        choices = g.get("game_choices") or {}
        if players and all(str(p) in choices for p in players):
            await self._resolve_game(g)
            return
        msg = await self._game_message(g)
        if msg:
            try:
                await msg.edit(embed=build_game_embed(g))
            except discord.HTTPException:
                pass

    async def _resolve_game(self, g: dict):
        """Decide the round (everyone picked, or the timer ran out). Every write is
        a compare-and-set on the round number, so calling this twice is harmless."""
        game = g.get("game")
        players = [str(p) for p in (g.get("game_players") or [])]
        if game not in GAME_NAMES or len(players) != 2:
            return
        mid = g["message_id"]
        rnd = g.get("game_round", 1)
        a, b = players
        choices = {str(k): v for k, v in (g.get("game_choices") or {}).items()}
        ca, cb = choices.get(a), choices.get(b)

        winners, split, draw, note = [], False, False, ""
        if game == "rps":
            if ca and cb:
                if ca == cb:
                    draw = True
                elif RPS_BEATS[ca] == cb:
                    winners = [a]
                else:
                    winners = [b]
            elif ca or cb:
                winner, loser = (a, b) if ca else (b, a)
                winners = [winner]
                note = f"<@{loser}> didn't pick in time and forfeits. "
            else:
                note = "Nobody picked in time. "
        else:
            if not ca and not cb:
                note = "Nobody picked in time. "
            else:
                ea, eb = ca or "split", cb or "split"
                missing = [p for p, c in ((a, ca), (b, cb)) if not c]
                if missing:
                    note = f"<@{missing[0]}> didn't pick in time and counts as Split. "
                if ea == "split" and eb == "split":
                    winners, split = [a, b], True
                elif ea == "steal" and eb == "steal":
                    winners = []
                else:
                    winners = [a] if ea == "steal" else [b]

        # --- draw: next round -------------------------------------------------
        if draw:
            timer = await self._game_timer(g["guild_id"], game)
            deadline = datetime.now(timezone.utc) + timedelta(seconds=timer)
            line = f"Round {rnd}: both picked {_choice_label(game, ca)} - draw!"
            if await models.game_next_round(mid, rnd, deadline, line) is None:
                return
            fresh = await models.get_giveaway(mid)
            msg = await self._game_message(fresh)
            if msg:
                try:
                    await msg.edit(embed=build_game_embed(fresh))
                except discord.HTTPException:
                    pass
            return

        # --- final result -----------------------------------------------------
        if await models.game_close(mid, rnd) is None:
            return
        prize = g["prize"]
        if game == "rps":
            if winners:
                outcome = f"{note}🏆 <@{winners[0]}> wins **{prize}**!"
            else:
                outcome = f"{note}Nobody wins this one."
        else:
            if split:
                outcome = f"{note}🤝 Both chose **Split** - <@{a}> and <@{b}> share **{prize}**!"
            elif winners:
                outcome = f"{note}💰 <@{winners[0]}> chose **Steal** and takes **{prize}**!"
            else:
                outcome = f"{note}💥 Both chose **Steal** - nobody wins and the prize is gone."

        result_g = dict(g)
        result_g["game_log"] = g.get("game_log") or []
        embed = build_game_result_embed(result_g, choices, outcome, bool(winners))
        msg = await self._game_message(g)
        channel = self.bot.get_channel(int(g["channel_id"]))
        try:
            if msg:
                await msg.edit(content=None, embed=embed, view=None)
            elif channel:
                await channel.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[giveaways] couldn't post game result: {e}")

        if not winners or channel is None:
            await models.update_giveaway(mid, winners=[], claim_expired=True)
            return

        if game == "rps":
            loser = b if winners[0] == a else a
            intro = (f"🎉 Congratulations <@{winners[0]}>, you beat <@{loser}> at "
                     f"Rock Paper Scissors and won **{prize}**!")
        elif split:
            intro = f"🎉 <@{a}> and <@{b}> chose Split - you each share **{prize}**!"
        else:
            intro = f"🎉 <@{winners[0]}> chose Steal and takes **{prize}**!"

        ann, claim_end = await self._post_winners(
            channel, prize, mid, winners, g["claim_time_seconds"], intro=intro
        )
        await models.update_giveaway(
            mid, winners=winners, game_split=split,
            announcement_message_id=str(ann.id), claim_end_time=claim_end,
        )

    # ------------------------------------------------------------------
    # Fastest Finger
    # ------------------------------------------------------------------
    async def _start_fastest(self, snap: dict, channel, eligible: list,
                             rerolled: bool = False):
        """Post the "get ready" message and schedule the code reveal."""
        mid = snap["message_id"]
        code = "".join(random.choices(FF_ALPHABET, k=4))
        delay = random.uniform(*FF_DELAY)
        arm_time = datetime.now(timezone.utc) + timedelta(seconds=delay)
        await models.update_giveaway(
            mid, ff_state="countdown", ff_code=code, ff_eligible=[str(u) for u in eligible],
            ff_arm_time=arm_time, ff_deadline=None, ff_rerolled=rerolled,
            winners=[], claim_end_time=None, claim_expired=False,
            claimed_users=[], claim_channels={}, announcement_message_id=None,
        )
        g = await models.get_giveaway(mid)
        try:
            msg = await channel.send(embed=build_ff_embed(g, "countdown"))
        except discord.HTTPException as e:
            print(f"[giveaways] couldn't post fastest finger message: {e}")
            await models.ff_set_state(mid, "countdown", "timeout", claim_expired=True)
            return
        await models.update_giveaway(mid, game_message_id=str(msg.id))
        asyncio.create_task(self._ff_arm_later(mid, delay))

    async def _ff_arm_later(self, mid, delay: float):
        try:
            await asyncio.sleep(delay)
            await self._ff_arm(mid)
        except Exception as e:
            print(f"[giveaways] fastest finger arm failed ({mid}): {e}")

    async def _ff_arm(self, mid):
        """countdown -> live: show the code. Safe to call twice (compare-and-set)."""
        deadline = datetime.now(timezone.utc) + timedelta(seconds=FF_WINDOW_SECONDS)
        if await models.ff_set_state(mid, "countdown", "live", ff_deadline=deadline) is None:
            return
        g = await models.get_giveaway(mid)
        if not g:
            return
        self._cache_ff(g)
        msg = await self._game_message(g)
        if msg:
            view = discord.ui.View(timeout=None)
            if (g.get("ff_mode") or "both") != "number":
                view.add_item(FastestFingerButton(mid))
            try:
                await msg.edit(embed=build_ff_embed(g, "live"), view=view)
            except discord.HTTPException as e:
                print(f"[giveaways] couldn't show fastest finger code: {e}")

    async def _ff_win(self, g: dict, uid) -> bool:
        mid = g["message_id"]
        uid = str(uid)
        if not g.get("ff_open") and uid not in (g.get("ff_eligible") or []):
            return False
        fields = {"winners": [uid]}
        if g.get("ff_open"):
            fields.update(ended=True, end_time=datetime.now(timezone.utc))
        if await models.ff_set_state(mid, "live", "done", **fields) is None:
            return False
        self._uncache_ff(g)
        fresh = await models.get_giveaway(mid) or g
        channel = self.bot.get_channel(int(g["channel_id"]))
        msg = await self._game_message(fresh)
        if msg:
            try:
                await msg.edit(embed=build_ff_embed(fresh, "won", uid), view=None)
            except discord.HTTPException:
                pass
        if channel is not None:
            suffix = " (Rerolled)" if g.get("ff_rerolled") else ""
            ann, claim_end = await self._post_winners(
                channel, g["prize"], mid, [uid], g["claim_time_seconds"],
                intro=f"⚡ <@{uid}> was the fastest finger and won **{g['prize']}**!{suffix}",
            )
            await models.update_giveaway(
                mid, announcement_message_id=str(ann.id), claim_end_time=claim_end,
            )
        await self._refresh_ended(fresh, [uid])
        return True

    async def _ff_timeout(self, g: dict):
        mid = g["message_id"]
        extra_fields = ({"ended": True, "end_time": datetime.now(timezone.utc)}
                        if g.get("ff_open") else {})
        if await models.ff_set_state(mid, "live", "timeout", winners=[], claim_expired=True,
                                     **extra_fields) is None:
            return
        self._uncache_ff(g)
        fresh = await models.get_giveaway(mid) or g
        msg = await self._game_message(fresh)
        if msg:
            try:
                await msg.edit(
                    embed=build_ff_embed(fresh, "⏰ Nobody reacted in time - no winner."),
                    view=None,
                )
            except discord.HTTPException:
                pass
        await self._refresh_ended(fresh, [])

    async def _ff_try_win(self, mid, uid, message: discord.Message):
        g = await models.get_giveaway(mid)
        if not g or g.get("ff_state") != "live":
            live = self.ff_live.get(message.guild.id)
            if live is not None:
                live.pop(str(mid), None)
            return
        if g.get("ff_open") and await _is_giveaway_blacklisted(message.author):
            return
        if await self._ff_win(g, uid):
            try:
                await message.add_reaction("⚡")
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------
    # Double or Nothing
    # ------------------------------------------------------------------
    async def _start_don(self, snap: dict, channel, winner, rerolled: bool = False):
        mid = snap["message_id"]
        deadline = datetime.now(timezone.utc) + timedelta(seconds=DON_DECISION_SECONDS)
        await models.update_giveaway(
            mid, don_active=True, don_winner=str(winner), don_mult=1, don_step=0,
            don_deadline=deadline, don_bust=False, winners=[],
            claim_end_time=None, claim_expired=False, claimed_users=[],
            claim_channels={}, announcement_message_id=None,
        )
        g = await models.get_giveaway(mid)
        try:
            msg = await channel.send(
                content=f"<@{winner}> - you won!" + (" (Rerolled)" if rerolled else ""),
                embed=build_don_embed(g), view=build_don_view(mid, 0),
            )
        except discord.HTTPException as e:
            print(f"[giveaways] couldn't post double or nothing panel: {e}")
            await models.don_finish(mid, 0, claim_expired=True)
            return
        await models.update_giveaway(mid, game_message_id=str(msg.id))

    async def _don_double(self, g: dict):
        mid = g["message_id"]
        step = int(g.get("don_step") or 0)
        if step >= len(DON_ODDS):
            return await self._don_take(g, note="Maximum doubles reached!")
        mult = int(g.get("don_mult") or 1)
        success = random.random() < DON_ODDS[step]
        winner = g.get("don_winner")
        if not success:
            before = await models.don_finish(
                mid, step, don_bust=True, winners=[], claim_expired=True
            )
            if before is None:
                return
            fresh = await models.get_giveaway(mid) or g
            result = (f"💥 <@{winner}> went for **{mult_prize(g['prize'], mult * 2)}** "
                      f"({int(DON_ODDS[step] * 100)}% chance) and lost everything!")
            msg = await self._game_message(fresh)
            if msg:
                try:
                    await msg.edit(content=None, embed=build_don_embed(fresh, result), view=None)
                except discord.HTTPException:
                    pass
            await self._refresh_ended(fresh, [], status="Winner: 💥 lost it all going for a double")
            return

        deadline = datetime.now(timezone.utc) + timedelta(seconds=DON_DECISION_SECONDS)
        if await models.don_advance(mid, step, mult * 2, deadline) is None:
            return
        fresh = await models.get_giveaway(mid) or g
        if step + 1 >= len(DON_ODDS):
            return await self._don_take(fresh, note="🔥 Maximum doubles reached!")
        msg = await self._game_message(fresh)
        if msg:
            try:
                await msg.edit(
                    embed=build_don_embed(fresh), view=build_don_view(mid, step + 1)
                )
            except discord.HTTPException:
                pass

    async def _don_take(self, g: dict, note: str = ""):
        mid = g["message_id"]
        step = int(g.get("don_step") or 0)
        winner = g.get("don_winner")
        if not winner:
            return
        before = await models.don_finish(mid, step, winners=[str(winner)], claim_expired=False)
        if before is None:
            return
        fresh = await models.get_giveaway(mid) or g
        prize = display_prize(fresh)
        result = f"{note + chr(10) if note else ''}🎉 <@{winner}> takes **{prize}**!"
        msg = await self._game_message(fresh)
        if msg:
            try:
                await msg.edit(content=None, embed=build_don_embed(fresh, result, won=True), view=None)
            except discord.HTTPException:
                pass
        channel = self.bot.get_channel(int(g["channel_id"]))
        if channel is not None:
            ann, claim_end = await self._post_winners(
                channel, prize, mid, [str(winner)], g["claim_time_seconds"],
                intro=f"🎉 <@{winner}> played Double or Nothing and took **{prize}**!",
            )
            await models.update_giveaway(
                mid, announcement_message_id=str(ann.id), claim_end_time=claim_end,
            )
        await self._refresh_ended(fresh, [str(winner)])

    # ------------------------------------------------------------------
    # Guess the Flag
    # ------------------------------------------------------------------
    async def _flag_guess(self, mid, message: discord.Message):
        g = await models.get_giveaway(mid)
        if not g or g.get("ended"):
            self.flag_live.pop(message.guild.id, None)
            return
        blacklist = await models.get_blacklist_config(message.guild.id)
        bl_role_id = blacklist["giveaway_role_id"]
        if bl_role_id and any(str(r.id) == str(bl_role_id) for r in message.author.roles):
            return      # blacklisted members can't win giveaways

        uid = str(message.author.id)
        if await models.flag_win(mid, uid, datetime.now(timezone.utc)) is None:
            self.flag_live.pop(message.guild.id, None)
            return
        self.flag_live.pop(message.guild.id, None)
        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass

        fresh = await models.get_giveaway(mid) or g
        channel = self.bot.get_channel(int(g["channel_id"]))
        entry = flags_data.find_by_code(g["flag_code"])
        name = entry[1] if entry else g["flag_code"].upper()
        if channel is None:
            return
        try:
            orig = await channel.fetch_message(int(mid))
            await orig.edit(embed=build_flag_embed(fresh, uid, ended=True), view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        ann, claim_end = await self._post_winners(
            channel, g["prize"], mid, [uid], g["claim_time_seconds"],
            intro=f"🏳️ <@{uid}> guessed it - that's **{name}**! You won **{g['prize']}**!",
        )
        await models.update_giveaway(
            mid, announcement_message_id=str(ann.id), claim_end_time=claim_end,
        )
        if message.channel.id != channel.id:
            try:
                await message.reply(
                    f"🎉 Correct - it's **{name}**! Head to {channel.mention} to claim your prize.",
                    mention_author=False,
                )
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------
    # Guess the Language
    # ------------------------------------------------------------------
    async def _language_guess(self, mid, message: discord.Message):
        g = await models.get_giveaway(mid)
        if not g or g.get("ended"):
            self.lang_live.pop(message.guild.id, None)
            return
        blacklist = await models.get_blacklist_config(message.guild.id)
        bl_role_id = blacklist["giveaway_role_id"]
        if bl_role_id and any(str(r.id) == str(bl_role_id) for r in message.author.roles):
            return      # blacklisted members can't win giveaways

        uid = str(message.author.id)
        if await models.language_win(mid, uid, datetime.now(timezone.utc)) is None:
            self.lang_live.pop(message.guild.id, None)
            return
        self.lang_live.pop(message.guild.id, None)
        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass

        fresh = await models.get_giveaway(mid) or g
        channel = self.bot.get_channel(int(g["channel_id"]))
        entry = languages_data.find_by_key(g["language_code"])
        name = entry[1] if entry else str(g["language_code"]).upper()
        if channel is None:
            return
        try:
            orig = await channel.fetch_message(int(mid))
            await orig.edit(embed=build_language_embed(fresh, uid, ended=True), view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        ann, claim_end = await self._post_winners(
            channel, g["prize"], mid, [uid], g["claim_time_seconds"],
            intro=f"🗣️ <@{uid}> guessed it - that's **{name}**! You won **{g['prize']}**!",
        )
        await models.update_giveaway(
            mid, announcement_message_id=str(ann.id), claim_end_time=claim_end,
        )
        if message.channel.id != channel.id:
            try:
                await message.reply(
                    f"🎉 Correct - it's **{name}**! Head to {channel.mention} to claim your prize.",
                    mention_author=False,
                )
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------
    # Listeners: typed code / typed flag / typed language name / member joins
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        content = (message.content or "").strip()
        if not content or len(content) > 80:
            return
        for ff_mid, (ff_code, ff_mode) in list((self.ff_live.get(message.guild.id) or {}).items()):
            if ff_mode != "button" and content.lower() == ff_code:
                try:
                    await self._ff_try_win(ff_mid, message.author.id, message)
                except Exception as e:
                    print(f"[giveaways] fastest finger message handler: {e}")
                break
        fl = self.flag_live.get(message.guild.id)
        if fl and flags_data.normalize(content) in fl["answers"]:
            try:
                await self._flag_guess(fl["mid"], message)
            except Exception as e:
                print(f"[giveaways] guess flag handler: {e}")
        ll = self.lang_live.get(message.guild.id)
        if ll and languages_data.matches(ll["answers"], content):
            try:
                await self._language_guess(ll["mid"], message)
            except Exception as e:
                print(f"[giveaways] guess language handler: {e}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            for g in await models.get_active_membercount(member.guild.id):
                if member.guild.member_count >= int(g["member_goal"]):
                    await self._end_giveaway(g)
                    continue
                # Keep the "currently N members" line fresh, at most once a minute.
                mid = g["message_id"]
                if _time.monotonic() - self._count_edit.get(mid, 0) < 60:
                    continue
                self._count_edit[mid] = _time.monotonic()
                channel = self.bot.get_channel(int(g["channel_id"]))
                if channel is None:
                    continue
                try:
                    msg = await channel.fetch_message(int(mid))
                    await msg.edit(embed=build_running_embed(g, member.guild.member_count))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        except Exception as e:
            print(f"[giveaways] member join handler: {e}")

    async def _expire_claim(self, g: dict):
        channel = self.bot.get_channel(int(g["channel_id"]))
        ann_id = g.get("announcement_message_id")
        if channel and ann_id:
            try:
                msg = await channel.fetch_message(int(ann_id))
                if "expired" not in (msg.content or "").lower():
                    await msg.edit(
                        content=(msg.content or "") + "\n\n⏰ Claim period has expired.",
                        view=None,
                    )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        await models.update_giveaway(g["message_id"], claim_expired=True)

    async def _lookup(self, ctx: commands.Context, message_id: str):
        try:
            mid = int(message_id)
        except ValueError:
            await ctx.send(embed=error_embed("Invalid message ID!"), ephemeral=True)
            return None
        g = await models.get_giveaway(mid)
        if not g or g.get("guild_id") != str(ctx.guild.id):
            await ctx.send(embed=error_embed("Giveaway not found!"), ephemeral=True)
            return None
        return g

    async def _autocomplete(self, interaction: discord.Interaction, current: str):
        current = (current or "").lower()
        choices = []
        for g in await models.get_guild_giveaways(interaction.guild.id):
            status = "Ended" if g.get("ended") else "Active"
            label = f"{g['prize']} - {status} ({g['display_id']})"[:100]
            if current in g["prize"].lower() or current in g["message_id"]:
                choices.append(app_commands.Choice(name=label, value=g["message_id"]))
        return choices[:25]

    @commands.hybrid_group(name="giveaway", description="Giveaway management commands")
    async def giveaway(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=info_embed("Use `giveaway create / membercount / fastestfinger / guessflag / guessthelanguage / doubleornothing / "
                "rps / splitsteal / end / reroll / delete`."))

    @giveaway.command(name="create", description="Create a new giveaway")
    @command_check()
    async def giveaway_create(self, ctx: commands.Context, *, flags: CreateFlags):
        await self._create_giveaway(ctx, flags)

    @giveaway.command(
        name="rps",
        description="Giveaway where the 2 winners play Rock Paper Scissors for the prize",
    )
    @command_check()
    async def giveaway_rps(self, ctx: commands.Context, *, flags: GameFlags):
        await self._create_giveaway(ctx, flags, game="rps")

    @giveaway.command(
        name="splitsteal",
        description="Giveaway where the 2 winners play Split or Steal for the prize",
    )
    @command_check()
    async def giveaway_splitsteal(self, ctx: commands.Context, *, flags: GameFlags):
        await self._create_giveaway(ctx, flags, game="splitsteal")

    @giveaway.command(
        name="membercount",
        description="Giveaway with no timer - it ends when the server reaches a member count",
    )
    @command_check()
    async def giveaway_membercount(self, ctx: commands.Context, *, flags: MemberCountFlags):
        await self._create_special(
            ctx, "membercount", flags.prize, claim_time=flags.claim_time,
            winners=flags.winners, members=flags.members,
        )

    @giveaway.command(
        name="fastestfinger",
        description="Giveaway where the entrants race - first to type the code wins",
    )
    @command_check()
    async def giveaway_fastestfinger(self, ctx: commands.Context, *, flags: FastestFingerFlags):
        """Instant race: no entry phase. The code is posted straight away and the
        first person to type it (any channel) or press the button wins."""
        await ctx.defer(ephemeral=True)
        prize = flags.prize.strip()
        if not prize or len(prize) > 200:
            return await ctx.send(
                embed=error_embed("Prize must be 1-200 characters."), ephemeral=True
            )
        claim_seconds = parse_duration(flags.claim_time)
        if claim_seconds is None:
            return await ctx.send(embed=error_embed(
                "Invalid claim time! Use a number plus d/h/m/s, e.g. `10m`, `1h`, `30s`."
            ), ephemeral=True)
        if claim_seconds < 30:
            return await ctx.send(
                embed=error_embed("Claim time must be at least 30 seconds!"), ephemeral=True
            )
        for g0 in await models.get_active_ff_live():
            if str(g0.get("guild_id")) == str(ctx.guild.id) and g0.get("ff_open"):
                link = f"https://discord.com/channels/{ctx.guild.id}/{g0['channel_id']}/{g0['message_id']}"
                return await ctx.send(embed=error_embed(
                    f"A Fastest Finger race is already running ([jump to it]({link})). "
                    "Wait for someone to win it or end it with `/giveaway end`."
                ), ephemeral=True)

        mode = flags.mode or "both"
        code = "".join(random.choices(FF_ALPHABET, k=4))
        now = datetime.now(timezone.utc)
        draft = {
            "prize": prize, "host_id": str(ctx.author.id), "display_id": generate_display_id(),
            "ff_code": code, "ff_mode": mode, "ff_eligible": [],
            "ff_deadline": now + timedelta(seconds=FF_WINDOW_SECONDS),
        }
        view = discord.ui.View(timeout=None)
        message = await ctx.channel.send(embed=build_ff_embed(draft, "live"))
        await models.create_giveaway(
            ctx.guild.id, ctx.channel.id, message.id, ctx.author.id,
            prize, 1, FAR_FUTURE, claim_seconds, draft["display_id"],
            kind="fastestfinger",
            extra={
                "ff_open": True, "ff_mode": mode, "ff_code": code, "ff_state": "live",
                "ff_eligible": [], "ff_deadline": draft["ff_deadline"],
                "ff_rerolled": False, "game_message_id": str(message.id),
            },
        )
        if mode != "number":
            view.add_item(FastestFingerButton(message.id))
            await message.edit(view=view)
        g = await models.get_giveaway(message.id)
        if g:
            self._cache_ff(g)
        await ctx.send(
            embed=success_embed(f"Fastest Finger started in {ctx.channel.mention}!"),
            ephemeral=True,
        )

    @giveaway.command(
        name="doubleornothing",
        description="Giveaway where the winner can double the prize - or lose it all",
    )
    @command_check()
    async def giveaway_doubleornothing(self, ctx: commands.Context, *, flags: DoubleOrNothingFlags):
        await self._create_special(
            ctx, "doubleornothing", flags.prize, time=flags.time, claim_time=flags.claim_time,
        )

    @giveaway.command(
        name="guessflag",
        description="Post a random flag - the first person to name the country wins",
    )
    @command_check()
    async def giveaway_guessflag(self, ctx: commands.Context, *, flags: GuessFlagFlags):
        await ctx.defer(ephemeral=True)
        prize = flags.prize.strip()
        if not prize or len(prize) > 200:
            return await ctx.send(
                embed=error_embed("Prize must be 1-200 characters."), ephemeral=True
            )
        running = await models.get_active_flag_game(ctx.guild.id)
        if running:
            g0 = running[0]
            link = f"https://discord.com/channels/{ctx.guild.id}/{g0['channel_id']}/{g0['message_id']}"
            return await ctx.send(embed=error_embed(
                f"A Guess the Flag game is already running ([jump to it]({link})). "
                "Wait for someone to guess it or end it with `/giveaway end`."
            ), ephemeral=True)

        code = flags_data.random_flag()[0]
        draft = {
            "prize": prize, "host_id": str(ctx.author.id), "flag_code": code,
            "display_id": generate_display_id(),
        }
        message = await ctx.channel.send(embed=build_flag_embed(draft))
        await models.create_giveaway(
            ctx.guild.id, ctx.channel.id, message.id, ctx.author.id,
            prize, 1, FAR_FUTURE, GUESSFLAG_CLAIM_SECONDS, draft["display_id"],
            kind="guessflag", extra={"flag_code": code},
        )
        self._cache_flag({
            "guild_id": ctx.guild.id, "message_id": str(message.id), "flag_code": code,
        })
        await ctx.send(
            embed=success_embed(f"Guess the Flag started in {ctx.channel.mention}!"),
            ephemeral=True,
        )

    @giveaway.command(
        name="guessthelanguage",
        description="Post a sentence in a random language - the first person to name it wins",
    )
    @command_check()
    async def giveaway_guessthelanguage(self, ctx: commands.Context, *, flags: GuessLanguageFlags):
        await ctx.defer(ephemeral=True)
        prize = flags.prize.strip()
        if not prize or len(prize) > 200:
            return await ctx.send(
                embed=error_embed("Prize must be 1-200 characters."), ephemeral=True
            )
        running = await models.get_active_language_game(ctx.guild.id)
        if running:
            g0 = running[0]
            link = f"https://discord.com/channels/{ctx.guild.id}/{g0['channel_id']}/{g0['message_id']}"
            return await ctx.send(embed=error_embed(
                f"A Guess the Language game is already running ([jump to it]({link})). "
                "Wait for someone to guess it or end it with `/giveaway end`."
            ), ephemeral=True)

        code = languages_data.random_language()[0]
        draft = {
            "prize": prize, "host_id": str(ctx.author.id), "language_code": code,
            "display_id": generate_display_id(),
        }
        message = await ctx.channel.send(embed=build_language_embed(draft))
        await models.create_giveaway(
            ctx.guild.id, ctx.channel.id, message.id, ctx.author.id,
            prize, 1, FAR_FUTURE, GUESSLANGUAGE_CLAIM_SECONDS, draft["display_id"],
            kind="guesslanguage", extra={"language_code": code},
        )
        self._cache_language({
            "guild_id": ctx.guild.id, "message_id": str(message.id), "language_code": code,
        })
        await ctx.send(
            embed=success_embed(f"Guess the Language started in {ctx.channel.mention}!"),
            ephemeral=True,
        )

    async def _create_special(self, ctx: commands.Context, kind: str, prize: str,
                              time: str = None, claim_time: str = "10m",
                              winners: int = 1, members: int = None, ff_mode: str = None):
        """membercount / fastestfinger / doubleornothing (all use the Enter button)."""
        await ctx.defer(ephemeral=True)
        extra = {}
        if kind == "membercount":
            current = ctx.guild.member_count or 0
            if members is None or members <= current:
                return await ctx.send(embed=error_embed(
                    f"The member goal must be higher than the current member count "
                    f"(**{current:,}**)."
                ), ephemeral=True)
            end_time = FAR_FUTURE
            extra["member_goal"] = int(members)
            winners_count = winners
        else:
            total = parse_duration(time)
            if total is None or total <= 0:
                return await ctx.send(embed=error_embed(
                    "Invalid duration! Use a number plus d/h/m/s, e.g. `1h`, `30m`, `2d`, "
                    "`45s` (or a combo like `1d12h`)."
                ), ephemeral=True)
            end_time = datetime.now(timezone.utc) + timedelta(seconds=total)
            winners_count = 1       # fastest finger / double or nothing: always one winner
            if kind == "fastestfinger":
                extra["ff_mode"] = ff_mode or "both"

        if not 1 <= winners_count <= 25:
            return await ctx.send(
                embed=error_embed("Winners must be between 1 and 25!"), ephemeral=True
            )
        claim_seconds = parse_duration(claim_time)
        if claim_seconds is None:
            return await ctx.send(embed=error_embed(
                "Invalid claim time! Use a number plus d/h/m/s, e.g. `10m`, `1h`, `30s`."
            ), ephemeral=True)
        if claim_seconds < 30:
            return await ctx.send(
                embed=error_embed("Claim time must be at least 30 seconds!"), ephemeral=True
            )
        prize = prize.strip()
        if not prize or len(prize) > 200:
            return await ctx.send(
                embed=error_embed("Prize must be 1-200 characters."), ephemeral=True
            )

        display_id = generate_display_id()
        draft = {
            "prize": prize, "host_id": str(ctx.author.id), "winners_count": winners_count,
            "end_time": end_time, "display_id": display_id, "entries": [], "game": None,
            "kind": kind, **extra,
        }
        message = await ctx.channel.send(
            embed=build_running_embed(draft, ctx.guild.member_count)
        )
        await models.create_giveaway(
            ctx.guild.id, ctx.channel.id, message.id, ctx.author.id,
            prize, winners_count, end_time, claim_seconds, display_id, None,
            kind=kind, extra=extra,
        )
        view = discord.ui.View(timeout=None)
        view.add_item(GiveawayEnterButton(message.id))
        await message.edit(view=view)
        await ctx.send(
            embed=success_embed(f"{KIND_NAMES[kind]} giveaway created in {ctx.channel.mention}!"),
            ephemeral=True,
        )

    async def _create_giveaway(self, ctx: commands.Context, flags, game: str = None):
        await ctx.defer(ephemeral=True)
        winners_count = 2 if game else flags.winners

        total = parse_duration(flags.time)
        if total is None or total <= 0:
            return await ctx.send(embed=error_embed(
                "Invalid duration! Use a number plus d/h/m/s, e.g. `1h`, `30m`, `2d`, `45s` "
                "(or a combo like `1d12h`)."
            ), ephemeral=True)

        if not 1 <= winners_count <= 25:
            return await ctx.send(
                embed=error_embed("Winners must be between 1 and 25!"), ephemeral=True
            )

        claim_seconds = parse_duration(flags.claim_time)
        if claim_seconds is None:
            return await ctx.send(embed=error_embed(
                "Invalid claim time! Use a number plus d/h/m/s, e.g. `10m`, `1h`, `30s`."
            ), ephemeral=True)
        if claim_seconds < 30:
            return await ctx.send(
                embed=error_embed("Claim time must be at least 30 seconds!"), ephemeral=True
            )

        prize = flags.prize.strip()
        if not prize or len(prize) > 200:
            return await ctx.send(
                embed=error_embed("Prize must be 1-200 characters."), ephemeral=True
            )

        end_time = datetime.now(timezone.utc) + timedelta(seconds=total)
        display_id = generate_display_id()

        draft = {
            "prize": prize, "host_id": str(ctx.author.id), "winners_count": winners_count,
            "end_time": end_time, "display_id": display_id, "entries": [], "game": game,
        }
        message = await ctx.channel.send(embed=build_running_embed(draft))
        await models.create_giveaway(
            ctx.guild.id, ctx.channel.id, message.id, ctx.author.id,
            prize, winners_count, end_time, claim_seconds, display_id, game,
        )
        view = discord.ui.View(timeout=None)
        view.add_item(GiveawayEnterButton(message.id))
        await message.edit(view=view)

        label = f"{GAME_NAMES[game]} giveaway" if game else "Giveaway"
        await ctx.send(
            embed=success_embed(f"{label} created in {ctx.channel.mention}!"), ephemeral=True
        )

    async def _force_end_active(self, g: dict):
        """/giveaway end on a giveaway whose entry phase is over but whose second
        phase (race / double-or-nothing panel) is still running. Returns the message
        to show staff, or None if there's nothing running."""
        mid = g["message_id"]
        kind = g.get("kind")
        if kind == "fastestfinger" and g.get("ff_state") in ("countdown", "live"):
            before = await models.ff_set_state(
                mid, ["countdown", "live"], "timeout", winners=[], claim_expired=True,
                **({"ended": True, "end_time": datetime.now(timezone.utc)}
                   if g.get("ff_open") else {})
            )
            if before is None:
                return None
            self._uncache_ff(g)
            fresh = await models.get_giveaway(mid) or g
            msg = await self._game_message(fresh)
            if msg:
                try:
                    await msg.edit(
                        embed=build_ff_embed(fresh, "🛑 Ended early by staff - no winner."),
                        view=None,
                    )
                except discord.HTTPException:
                    pass
            await self._refresh_ended(fresh, [])
            return "Fastest Finger race ended - no winner."
        if kind == "doubleornothing" and g.get("don_active"):
            await self._don_take(g, note="🛑 Ended early by staff - the current prize was taken.")
            return "Double or Nothing ended - the winner keeps their current prize."
        return None

    @giveaway.command(name="end", description="Force end a giveaway early")
    @command_check()
    async def giveaway_end(self, ctx: commands.Context, message_id: str):
        await ctx.defer(ephemeral=True)
        g = await self._lookup(ctx, message_id)
        if not g:
            return
        if g.get("ended"):
            done = await self._force_end_active(g)
            if done:
                return await ctx.send(embed=success_embed(done), ephemeral=True)
            return await ctx.send(
                embed=error_embed("This giveaway has already ended!"), ephemeral=True
            )
        if await self._end_giveaway(g):
            await ctx.send(embed=success_embed("Giveaway ended!"), ephemeral=True)
        else:
            await ctx.send(embed=error_embed("This giveaway has already ended!"), ephemeral=True)

    @giveaway_end.autocomplete("message_id")
    async def giveaway_end_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._autocomplete(interaction, current)

    async def _reroll_special(self, ctx: commands.Context, g: dict, channel, eligible: list):
        """Reroll for Fastest Finger (re-run the race among everyone who hasn't won)
        and Double or Nothing (pick a fresh winner, fresh panel)."""
        mid = g["message_id"]
        kind = g.get("kind")
        if kind == "doubleornothing" and g.get("don_active"):
            await models.don_finish(mid, int(g.get("don_step") or 0), winners=[])

        for key in ("announcement_message_id", "game_message_id"):
            old_id = g.get(key)
            if not old_id:
                continue
            try:
                old = await channel.fetch_message(int(old_id))
                await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        await models.update_giveaway(
            mid, winners=[], claimed_users=[], claim_channels={}, claim_expired=False,
            claim_end_time=None, announcement_message_id=None, game_message_id=None,
        )

        if kind == "fastestfinger":
            await self._refresh_ended(
                g, [], status="Winner: ⚡ Fastest Finger race in progress... (Rerolled)"
            )
            await self._start_fastest(g, channel, eligible, rerolled=True)
            note = (f"Fastest Finger re-run among the **{len(eligible)}** entrant(s) "
                    "who haven't won.")
        else:
            new_winner = random.choice(eligible)
            await self._refresh_ended(g, [new_winner])
            await self._start_don(g, channel, new_winner, rerolled=True)
            note = f"Rerolled! New winner: <@{new_winner}>"
        await ctx.send(embed=success_embed(note), ephemeral=True)

    @giveaway.command(name="reroll", description="Reroll a giveaway's winner(s)")
    @command_check()
    async def giveaway_reroll(self, ctx: commands.Context, message_id: str):
        await ctx.defer(ephemeral=True)
        g = await self._lookup(ctx, message_id)
        if not g:
            return

        if g.get("game"):
            return await ctx.send(embed=error_embed(
                "Game giveaways can't be rerolled - the two players already played for the prize."
            ), ephemeral=True)

        kind = g.get("kind")
        if kind == "guessflag":
            return await ctx.send(embed=error_embed(
                "Guess the Flag can't be rerolled - there are no entries, the first correct "
                "guess wins. Start a new one with `/giveaway guessflag`."
            ), ephemeral=True)
        if kind == "guesslanguage":
            return await ctx.send(embed=error_embed(
                "Guess the Language can't be rerolled - there are no entries, the first correct "
                "guess wins. Start a new one with `/giveaway guessthelanguage`."
            ), ephemeral=True)
        if kind == "fastestfinger" and g.get("ff_open"):
            return await ctx.send(embed=error_embed(
                "Fastest Finger can't be rerolled - there are no entries, the first to type the "
                "code or press the button wins. Start a new one with `/giveaway fastestfinger`."
            ), ephemeral=True)
        if kind == "fastestfinger" and g.get("ff_state") in ("countdown", "live"):
            return await ctx.send(embed=error_embed(
                "The Fastest Finger race is still running! Let it finish or end it first."
            ), ephemeral=True)
        if kind == "doubleornothing":
            if g.get("don_bust"):
                return await ctx.send(embed=error_embed(
                    "Can't reroll - the winner went for a double and lost the prize."
                ), ephemeral=True)
            if int(g.get("don_step") or 0) > 0:
                return await ctx.send(embed=error_embed(
                    "Can't reroll - the winner already doubled their prize."
                ), ephemeral=True)

        if not g.get("ended"):
            return await ctx.send(embed=error_embed(
                "This giveaway hasn't ended yet! End it first."
            ), ephemeral=True)

        claimed = g.get("claimed_users", [])
        if claimed:
            return await ctx.send(embed=error_embed(
                "Cannot reroll! These users already opened a claim ticket for this prize: "
                + ", ".join(f"<@{u}>" for u in claimed)
            ), ephemeral=True)

        old_winners = set(g.get("winners", []))
        if g.get("don_winner"):
            old_winners.add(str(g["don_winner"]))
        eligible = [u for u in set(g.get("entries", [])) if u not in old_winners]
        if not eligible:
            return await ctx.send(embed=error_embed(
                "There are no other entries to reroll from (everyone already won)!"
            ), ephemeral=True)

        channel = self.bot.get_channel(int(g["channel_id"]))
        if channel is None:
            return await ctx.send(
                embed=error_embed("Giveaway channel not found."), ephemeral=True
            )

        if kind in ("fastestfinger", "doubleornothing"):
            return await self._reroll_special(ctx, g, channel, eligible)

        new_winners = random.sample(eligible, min(g["winners_count"], len(eligible)))

        if g.get("announcement_message_id"):
            try:
                old_ann = await channel.fetch_message(int(g["announcement_message_id"]))
                await old_ann.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        try:
            original = await channel.fetch_message(int(g["message_id"]))
            await original.edit(embed=build_ended_embed(g, new_winners), view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            print(f"[giveaways] couldn't update original message on reroll: {e}")

        ann, claim_end = await self._post_winners(
            channel, g["prize"], g["message_id"], new_winners,
            g["claim_time_seconds"], rerolled=True,
        )
        await models.update_giveaway(
            g["message_id"], winners=new_winners, claimed_users=[], claim_channels={},
            claim_expired=False, announcement_message_id=str(ann.id), claim_end_time=claim_end,
        )

        await ctx.send(embed=success_embed(
            "Giveaway rerolled! New winners: " + ", ".join(f"<@{w}>" for w in new_winners)
        ), ephemeral=True)

    @giveaway_reroll.autocomplete("message_id")
    async def giveaway_reroll_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._autocomplete(interaction, current)

    @giveaway.command(name="delete", description="Permanently delete a giveaway and its messages")
    @command_check()
    async def giveaway_delete(self, ctx: commands.Context, message_id: str):
        await ctx.defer(ephemeral=True)
        g = await self._lookup(ctx, message_id)
        if not g:
            return

        channel = self.bot.get_channel(int(g["channel_id"]))
        if channel:
            for mid in (g["message_id"], g.get("announcement_message_id"),
                        g.get("game_message_id")):
                if not mid:
                    continue
                try:
                    msg = await channel.fetch_message(int(mid))
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        self._uncache_ff(g)
        if (self.flag_live.get(int(g["guild_id"])) or {}).get("mid") == g["message_id"]:
            self.flag_live.pop(int(g["guild_id"]), None)
        if (self.lang_live.get(int(g["guild_id"])) or {}).get("mid") == g["message_id"]:
            self.lang_live.pop(int(g["guild_id"]), None)
        await models.delete_giveaway(g["message_id"])
        await ctx.send(embed=success_embed(
            f"Giveaway **{g['prize']}** has been permanently deleted."
        ), ephemeral=True)

    @giveaway_delete.autocomplete("message_id")
    async def giveaway_delete_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._autocomplete(interaction, current)


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaways(bot))