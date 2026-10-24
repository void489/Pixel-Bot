"""
Member tools - info, utility, fun and community commands.

Every command is a hybrid command (slash + prefix) and carries @command_check(), so each
one shows up on the dashboard's Command Permissions page and is blocked for non-admins
until it is enabled there. Which help list / permissions group a command sits in is set in
config/command_groups.py.

Info
    /serverinfo                       server overview
    /userinfo [member]                account/join dates, roles, level, vouches
    /avatar [member]                  full-size avatar with download links
    /banner [member]                  profile banner
    /ping                             bot latency
    /botinfo                          uptime, versions, counts, support link
Utility
    /remindme <time> <text>           DM reminder (e.g. 30m, 2h, 1d12h)
    /rolelist                         the server's reaction roles
    /translate <language> <text>      translate text (name or code, e.g. spanish / es)
    /profile [member]                 rank, vouches, dates, birthday (+ warns for staff/self)
Community
    /suggest <text>                   post a suggestion with voting reactions
    /suggestions setchannel <#chan>   (admin) where suggestions are posted
    /suggestions approve|deny <num>   (staff) decide a suggestion
    /giveaways                        active giveaways in this server
    /ticketstatus                     your open ticket and recent applications
    /birthday set|view|remove         save a birthday, the bot announces it
    /birthday setchannel <#chan>      (admin) where birthdays are announced
Fun
    /coinflip  /roll [dice]  /8ball <question>  /choose <a | b | c>
"""
import calendar
import html
import platform
import random
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import constants, settings
from database import models
from database.mongo import levels_col
from utils import leveling_core
from utils.embeds import base_embed, success_embed, error_embed, info_embed
from utils.permissions import command_check
from cogs.utilities import _reply

NO_MENTIONS = discord.AllowedMentions.none()

UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
MIN_REMINDER_SECONDS = 10
MAX_REMINDER_SECONDS = 365 * 86400
MAX_REMINDERS_PER_USER = 25

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

LANGUAGES = {
    "english": "en", "spanish": "es", "french": "fr", "german": "de", "italian": "it",
    "portuguese": "pt", "dutch": "nl", "russian": "ru", "polish": "pl", "turkish": "tr",
    "arabic": "ar", "hindi": "hi", "japanese": "ja", "korean": "ko", "chinese": "zh-CN",
    "swedish": "sv", "norwegian": "no", "danish": "da", "finnish": "fi", "greek": "el",
    "czech": "cs", "romanian": "ro", "hungarian": "hu", "ukrainian": "uk",
    "indonesian": "id", "vietnamese": "vi", "thai": "th", "hebrew": "he",
    "bulgarian": "bg", "croatian": "hr", "filipino": "tl", "persian": "fa",
}

EIGHT_BALL = [
    "It is certain.", "It is decidedly so.", "Without a doubt.", "Yes, definitely.",
    "You may rely on it.", "As I see it, yes.", "Most likely.", "Outlook good.", "Yes.",
    "Signs point to yes.", "Reply hazy, try again.", "Ask again later.",
    "Better not tell you now.", "Cannot predict now.", "Concentrate and ask again.",
    "Don't count on it.", "My reply is no.", "My sources say no.",
    "Outlook not so good.", "Very doubtful.",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_compound_duration(text: str):
    """'30m', '2h', '1d12h', '1h 30m' -> seconds, or None if it isn't a duration."""
    t = re.sub(r"\s+", "", (text or "").lower())
    if not t or not re.fullmatch(r"(\d+[smhdw])+", t):
        return None
    return sum(int(n) * UNIT_SECONDS[u] for n, u in re.findall(r"(\d+)([smhdw])", t))


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_uptime(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    days, secs = divmod(secs, 86400)
    hours, secs = divmod(secs, 3600)
    mins, secs = divmod(secs, 60)
    parts = [f"{days}d" if days else "", f"{hours}h" if hours else "", f"{mins}m" if mins else "",
             f"{secs}s" if not (days or hours or mins) else ""]
    return " ".join(p for p in parts if p)


def resolve_language(text: str):
    t = (text or "").strip().lower()
    if t in LANGUAGES:
        return LANGUAGES[t]
    codes = {c.lower(): c for c in LANGUAGES.values()}
    if t in codes:
        return codes[t]
    if re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", t):
        return t
    return None


def _suggestion_embed(doc: dict, guild: discord.Guild) -> discord.Embed:
    status = doc.get("status", "Pending")
    color = {"Approved": constants.COLOR_SUCCESS, "Denied": constants.COLOR_ERROR}.get(
        status, constants.COLOR_INFO)
    embed = base_embed(title=f"💡 Suggestion #{doc['number']}", description=doc["text"], color=color)
    author = guild.get_member(int(doc["author_id"]))
    if author:
        embed.set_author(name=author.display_name, icon_url=author.display_avatar.url)
    else:
        embed.set_author(name=f"User {doc['author_id']}")
    icon = {"Approved": "✅", "Denied": "❌"}.get(status, "⏳")
    embed.add_field(name="Status", value=f"{icon} {status}", inline=False)
    if doc.get("reason"):
        embed.add_field(name="Staff reason", value=doc["reason"][:1000], inline=False)
    return embed


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class MemberTools(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        if not hasattr(self.bot, "started_at"):
            self.bot.started_at = discord.utils.utcnow()
        self._reminder_loop.start()
        self._birthday_loop.start()

    async def cog_unload(self):
        self._reminder_loop.cancel()
        self._birthday_loop.cancel()

    # ======================================================================
    # Info
    # ======================================================================
    @commands.hybrid_command(name="serverinfo", description="Show information about this server")
    @command_check()
    async def serverinfo(self, ctx: commands.Context):
        g = ctx.guild
        total = g.member_count or len(g.members)
        bots = sum(1 for m in g.members if m.bot)
        owner = g.owner.mention if g.owner else f"<@{g.owner_id}>"
        embed = base_embed(title=f"🏠 {g.name}")
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        if g.banner:
            embed.set_image(url=g.banner.url)
        if g.description:
            embed.description = g.description
        embed.add_field(name="Owner", value=owner)
        embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "D")
                        + "\n" + discord.utils.format_dt(g.created_at, "R"))
        embed.add_field(name="ID", value=f"`{g.id}`")
        embed.add_field(name="Members", value=f"{total:,} total" + (f"\n{bots:,} bots (cached)" if bots else ""))
        embed.add_field(name="Channels",
                        value=f"{len(g.text_channels)} text\n{len(g.voice_channels)} voice\n"
                              f"{len(g.categories)} categories")
        embed.add_field(name="Roles / Emojis", value=f"{len(g.roles) - 1} roles\n{len(g.emojis)} emojis")
        embed.add_field(name="Boosts",
                        value=f"Level {g.premium_tier}\n{g.premium_subscription_count or 0} boosts")
        embed.add_field(name="Verification", value=str(g.verification_level).title())
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="userinfo", description="Show information about a member")
    @app_commands.describe(member="Who to look up (default: you)")
    @command_check()
    async def userinfo(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        m = member or ctx.author
        embed = base_embed(title=f"👤 {m}", color=m.color.value or constants.COLOR_DEFAULT)
        embed.set_thumbnail(url=m.display_avatar.url)
        embed.add_field(name="ID", value=f"`{m.id}`")
        embed.add_field(name="Nickname", value=m.nick or "*none*")
        embed.add_field(name="Bot", value="Yes" if m.bot else "No")
        embed.add_field(name="Account created", value=discord.utils.format_dt(m.created_at, "D")
                        + "\n" + discord.utils.format_dt(m.created_at, "R"))
        if m.joined_at:
            embed.add_field(name="Joined server", value=discord.utils.format_dt(m.joined_at, "D")
                            + "\n" + discord.utils.format_dt(m.joined_at, "R"))
        if m.guild_permissions.administrator:
            embed.add_field(name="Key permission", value="Administrator")

        roles = [r.mention for r in reversed(m.roles[1:])]
        shown = " ".join(roles[:15]) + (f" +{len(roles) - 15} more" if len(roles) > 15 else "")
        embed.add_field(name=f"Roles ({len(roles)})", value=shown or "*none*", inline=False)

        if not m.bot:
            doc = await levels_col().find_one({"guild_id": str(ctx.guild.id), "user_id": str(m.id)}) or {}
            xp = int(doc.get("xp", 0))
            if xp:
                embed.add_field(name="Level", value=f"{leveling_core.progress(xp)[0]} ({xp:,} XP)")
            normal = await models.count_vouches(ctx.guild.id, m.id, "normal")
            scam = await models.count_vouches(ctx.guild.id, m.id, "scam")
            if normal or scam:
                embed.add_field(name="Vouches", value=f"⭐ {normal}  🚨 {scam}")
            if await models.is_global_scammer(m.id):
                embed.add_field(name="⚠️ Scammer", value="Flagged as a global scammer", inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="avatar", description="Show someone's avatar in full size")
    @app_commands.describe(member="Whose avatar (default: you)")
    @command_check()
    async def avatar(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        m = member or ctx.author
        av = m.display_avatar
        links = " | ".join(
            f"[{fmt.upper()}]({av.replace(size=1024, format=fmt).url})"
            for fmt in ("png", "jpg", "webp"))
        if av.is_animated():
            links += f" | [GIF]({av.replace(size=1024, format='gif').url})"
        embed = base_embed(title=f"🖼️ {m.display_name}'s avatar", description=links)
        embed.set_image(url=av.replace(size=1024).url)
        if m.guild_avatar and m.avatar:
            embed.set_footer(text="Showing their server avatar")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="banner", description="Show someone's profile banner")
    @app_commands.describe(member="Whose banner (default: you)")
    @command_check()
    async def banner(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        m = member or ctx.author
        try:
            user = await self.bot.fetch_user(m.id)   # banners only come with a full fetch
        except discord.HTTPException:
            return await _reply(ctx, error_embed("Couldn't load that profile."))
        if not user.banner:
            return await _reply(ctx, info_embed(f"**{m.display_name}** doesn't have a banner."))
        embed = base_embed(title=f"🎨 {m.display_name}'s banner")
        embed.set_image(url=user.banner.replace(size=1024).url)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="ping", description="Show the bot's latency")
    @command_check()
    async def ping(self, ctx: commands.Context):
        ws = round(self.bot.latency * 1000)
        start = time.perf_counter()
        msg = await ctx.send(embed=info_embed("Pinging…"))
        api = round((time.perf_counter() - start) * 1000)
        await msg.edit(embed=base_embed(
            title="🏓 Pong!", description=f"**Gateway:** {ws} ms\n**API:** {api} ms"))

    @commands.hybrid_command(name="botinfo", description="Bot uptime, versions and stats")
    @command_check()
    async def botinfo(self, ctx: commands.Context):
        up = discord.utils.utcnow() - getattr(self.bot, "started_at", discord.utils.utcnow())
        cmds = sum(1 for c in self.bot.walk_commands() if not isinstance(c, commands.Group))
        users = sum(g.member_count or 0 for g in self.bot.guilds)
        prefix = await models.get_prefix(ctx.guild.id)
        embed = base_embed(title=f"🤖 {self.bot.user.name}")
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.add_field(name="Uptime", value=_fmt_uptime(up))
        embed.add_field(name="Latency", value=f"{round(self.bot.latency * 1000)} ms")
        embed.add_field(name="Servers", value=f"{len(self.bot.guilds):,}")
        embed.add_field(name="Users", value=f"{users:,}")
        embed.add_field(name="Commands", value=str(cmds))
        embed.add_field(name="Prefix", value=f"`{prefix}` or /")
        embed.add_field(name="Versions",
                        value=f"Python {platform.python_version()}\ndiscord.py {discord.__version__}")
        view = None
        if settings.SUPPORT_URL.startswith(("http://", "https://")):
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Support", url=settings.SUPPORT_URL))
        await ctx.send(embed=embed, view=view)

    # ======================================================================
    # Utility
    # ======================================================================
    @commands.hybrid_command(name="remindme", description="Get a reminder by DM after a delay")
    @app_commands.describe(time="When, e.g. 30m, 2h, 1d12h (10s to 365d)", text="What to remind you of")
    @command_check()
    async def remindme(self, ctx: commands.Context, time: str, *, text: str):
        seconds = parse_compound_duration(time)
        if seconds is None:
            return await _reply(ctx, error_embed(
                "I couldn't read that time. Use units like `30m`, `2h`, `1d12h` (no spaces in prefix form)."))
        if seconds < MIN_REMINDER_SECONDS or seconds > MAX_REMINDER_SECONDS:
            return await _reply(ctx, error_embed("Reminders can be between 10 seconds and 365 days."))
        text = text.strip()[:500]
        if not text:
            return await _reply(ctx, error_embed("Tell me what to remind you of."))
        if await models.count_user_reminders(ctx.author.id) >= MAX_REMINDERS_PER_USER:
            return await _reply(ctx, error_embed(
                f"You already have {MAX_REMINDERS_PER_USER} pending reminders."))
        due = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        await models.add_reminder(ctx.guild.id, ctx.author.id, ctx.channel.id, text, due)
        ts = int(due.timestamp())
        await _reply(ctx, success_embed(
            f"I'll remind you <t:{ts}:R> (<t:{ts}:f>):\n{discord.utils.escape_markdown(text)}"),
            ephemeral=True, delete_after=10)

    @tasks.loop(seconds=15)
    async def _reminder_loop(self):
        try:
            now = datetime.now(timezone.utc)
            for doc in await models.get_due_reminders(now):
                try:
                    await self._deliver_reminder(doc, now)
                except Exception as e:
                    print(f"[reminders] failed to deliver {doc.get('_id')}: {e}")
                finally:
                    await models.delete_reminder(doc["_id"])
        except Exception as e:
            print(f"[reminders] loop error: {e}")

    @_reminder_loop.before_loop
    async def _before_reminder_loop(self):
        await self.bot.wait_until_ready()

    async def _deliver_reminder(self, doc: dict, now: datetime):
        due = _aware(doc["due_at"])
        late = (now - due).total_seconds() > 120
        embed = base_embed(title="⏰ Reminder", description=doc["text"], color=constants.COLOR_INFO)
        if late:
            embed.set_footer(text="Delivered late - the bot was offline when this was due.")
        user = self.bot.get_user(int(doc["user_id"])) or await self.bot.fetch_user(int(doc["user_id"]))
        try:
            await user.send(embed=embed)
            return
        except (discord.Forbidden, discord.HTTPException):
            pass
        channel = self.bot.get_channel(int(doc["channel_id"]))   # DMs closed: fall back to the channel
        if channel is not None:
            await channel.send(
                content=user.mention, embed=embed,
                allowed_mentions=discord.AllowedMentions(users=[user]))

    @commands.hybrid_command(name="rolelist", description="List this server's reaction roles")
    @command_check()
    async def rolelist(self, ctx: commands.Context):
        rows = await models.get_guild_reaction_roles(ctx.guild.id)
        if not rows:
            return await _reply(ctx, info_embed("This server has no reaction roles set up."))
        lines = []
        for r in rows:
            role = ctx.guild.get_role(int(r["role_id"]))
            if role is None:
                continue
            link = f"https://discord.com/channels/{ctx.guild.id}/{r['channel_id']}/{r['message_id']}"
            lines.append(f"{r['emoji_display']} → {role.mention} · [get it here]({link})")
        if not lines:
            return await _reply(ctx, info_embed("This server has no reaction roles set up."))
        desc = "\n".join(lines)
        if len(desc) > 4000:
            desc = desc[:3990].rsplit("\n", 1)[0] + "\n…"
        await ctx.send(embed=base_embed(title="🎭 Self-assign roles", description=desc))

    @commands.hybrid_command(name="translate", description="Translate text into another language")
    @app_commands.describe(language="Target language, e.g. spanish or es", text="Text to translate")
    @command_check()
    async def translate(self, ctx: commands.Context, language: str, *, text: str):
        code = resolve_language(language)
        if not code:
            return await _reply(ctx, error_embed(
                "I don't know that language. Use a name like `spanish` or a code like `es`."))
        text = text.strip()
        if not text:
            return await _reply(ctx, error_embed("Give me some text to translate."))
        if len(text) > 400:
            return await _reply(ctx, error_embed("Keep it under 400 characters."))
        await ctx.defer()
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get("https://api.mymemory.translated.net/get",
                                 params={"q": text, "langpair": f"Autodetect|{code}"}) as resp:
                    data = await resp.json(content_type=None)
            if int(data.get("responseStatus", 0)) != 200:
                raise ValueError(data.get("responseDetails") or "service error")
            out = html.unescape(data["responseData"]["translatedText"])
        except Exception as e:
            print(f"[translate] failed: {e}")
            return await ctx.send(embed=error_embed(
                "The translation service didn't answer. Try again in a moment."))
        embed = base_embed(title=f"🌐 Translation → {code}")
        embed.add_field(name="Original", value=text[:1000], inline=False)
        embed.add_field(name="Translated", value=out[:1000], inline=False)
        embed.set_footer(text="Machine translation via MyMemory")
        await ctx.send(embed=embed, allowed_mentions=NO_MENTIONS)

    @commands.hybrid_command(name="profile", aliases=["mystats"],
                             description="Your (or someone's) rank, vouches and dates in one card")
    @app_commands.describe(member="Whose profile (default: you)")
    @command_check()
    async def profile(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        m = member or ctx.author
        embed = base_embed(title=f"📇 {m.display_name}", color=m.color.value or constants.COLOR_DEFAULT)
        embed.set_thumbnail(url=m.display_avatar.url)

        doc = await levels_col().find_one({"guild_id": str(ctx.guild.id), "user_id": str(m.id)}) or {}
        xp = int(doc.get("xp", 0))
        if xp and not m.bot:
            level, into, need = leveling_core.progress(xp)
            pos = await levels_col().count_documents(
                {"guild_id": str(ctx.guild.id), "xp": {"$gt": xp}}) + 1
            embed.add_field(name="Level", value=f"{level}  ({into:,}/{need:,} XP)\nRank #{pos}")
        normal = await models.count_vouches(ctx.guild.id, m.id, "normal")
        scam = await models.count_vouches(ctx.guild.id, m.id, "scam")
        embed.add_field(name="Vouches", value=f"⭐ {normal}  🚨 {scam}")

        # Warns are only shown to the member themselves and to staff who can timeout.
        if m.id == ctx.author.id or ctx.author.guild_permissions.moderate_members:
            embed.add_field(name="Active warns", value=str(await models.count_user_warns(ctx.guild.id, m.id)))
        if m.joined_at:
            embed.add_field(name="Joined", value=discord.utils.format_dt(m.joined_at, "D"))
        embed.add_field(name="Account created", value=discord.utils.format_dt(m.created_at, "D"))
        bday = await models.get_birthday(ctx.guild.id, m.id)
        if bday:
            embed.add_field(name="Birthday", value=f"{bday['day']} {MONTHS[bday['month'] - 1]}")
        if await models.is_global_scammer(m.id):
            embed.add_field(name="⚠️ Scammer", value="Flagged as a global scammer", inline=False)
        await ctx.send(embed=embed)

    # ======================================================================
    # Community: suggestions
    # ======================================================================
    @commands.hybrid_command(name="suggest", description="Post a suggestion for the server")
    @app_commands.describe(text="Your suggestion")
    @command_check()
    async def suggest(self, ctx: commands.Context, *, text: str):
        text = text.strip()
        if not text:
            return await _reply(ctx, error_embed("Write your suggestion after the command."))
        if len(text) > 1500:
            return await _reply(ctx, error_embed("Keep suggestions under 1500 characters."))
        chan_id = await models.get_suggestion_channel_id(ctx.guild.id)
        channel = ctx.guild.get_channel(int(chan_id)) if chan_id else None
        if channel is None:
            return await _reply(ctx, error_embed(
                "Suggestions aren't set up yet. An admin can set the channel with `/suggestions setchannel`."))
        number = await models.next_suggestion_num(ctx.guild.id)
        doc = {"number": number, "author_id": str(ctx.author.id), "text": text, "status": "Pending"}
        try:
            msg = await channel.send(embed=_suggestion_embed(doc, ctx.guild), allowed_mentions=NO_MENTIONS)
            for emoji in ("👍", "👎"):
                await msg.add_reaction(emoji)
        except (discord.Forbidden, discord.HTTPException):
            return await _reply(ctx, error_embed(
                f"I can't post in {channel.mention}. Check my permissions there."))
        await models.create_suggestion(ctx.guild.id, ctx.author.id, channel.id, msg.id, text, number)
        await _reply(ctx, success_embed(f"Suggestion **#{number}** posted in [{channel.name}]({msg.jump_url})."),
                     ephemeral=True, delete_after=8)

    @commands.hybrid_group(name="suggestions", description="Suggestion settings and decisions",
                           invoke_without_command=True)
    async def suggestions(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await _reply(ctx, info_embed(
                "Use `/suggestions setchannel`, `/suggestions approve <number>` or "
                "`/suggestions deny <number>`."))

    @suggestions.command(name="setchannel", description="Set the channel suggestions are posted in")
    @command_check()
    async def suggestions_setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        await models.update_guild_settings(ctx.guild.id, {"suggestions.channel_id": str(channel.id)})
        await _reply(ctx, success_embed(f"Suggestions will be posted in {channel.mention}."), ephemeral=False)

    async def _decide(self, ctx: commands.Context, number: int, status: str, reason: Optional[str]):
        doc = await models.get_suggestion(ctx.guild.id, number)
        if not doc:
            return await _reply(ctx, error_embed(f"No suggestion #{number} in this server."))
        if doc["status"] != "Pending":
            return await _reply(ctx, error_embed(f"Suggestion #{number} was already **{doc['status'].lower()}**."))
        doc = await models.decide_suggestion(ctx.guild.id, number, status, ctx.author.id,
                                             (reason or "").strip()[:1000] or None)
        channel = ctx.guild.get_channel(int(doc["channel_id"]))
        try:
            msg = await channel.fetch_message(int(doc["message_id"]))
            await msg.edit(embed=_suggestion_embed(doc, ctx.guild), allowed_mentions=NO_MENTIONS)
        except (discord.HTTPException, AttributeError):
            pass    # message deleted - the decision is still saved
        author = ctx.guild.get_member(int(doc["author_id"]))
        if author:
            try:
                await author.send(embed=base_embed(
                    title=f"Your suggestion #{number} was {status.lower()}",
                    description=doc["text"][:1500] + (f"\n\n**Reason:** {doc['reason']}" if doc.get("reason") else ""),
                    color=constants.COLOR_SUCCESS if status == "Approved" else constants.COLOR_ERROR))
            except (discord.Forbidden, discord.HTTPException):
                pass
        await _reply(ctx, success_embed(f"Suggestion **#{number}** {status.lower()}."), ephemeral=False)

    @suggestions.command(name="approve", description="Approve a suggestion by its number")
    @command_check()
    async def suggestions_approve(self, ctx: commands.Context, number: int, *, reason: Optional[str] = None):
        await self._decide(ctx, number, "Approved", reason)

    @suggestions.command(name="deny", description="Deny a suggestion by its number")
    @command_check()
    async def suggestions_deny(self, ctx: commands.Context, number: int, *, reason: Optional[str] = None):
        await self._decide(ctx, number, "Denied", reason)

    # ======================================================================
    # Community: giveaways / tickets
    # ======================================================================
    @commands.hybrid_command(name="giveaways", description="List the active giveaways in this server")
    @command_check()
    async def giveaways(self, ctx: commands.Context):
        rows = [g for g in await models.get_guild_giveaways(ctx.guild.id) if not g.get("ended")]
        if not rows:
            return await _reply(ctx, info_embed("There are no active giveaways right now."))
        rows.sort(key=lambda g: _aware(g.get("end_time")) or datetime.max.replace(tzinfo=timezone.utc))
        lines = []
        for g in rows[:15]:
            end = _aware(g.get("end_time"))
            when = (f"ends {discord.utils.format_dt(end, 'R')}"
                    if end and g.get("kind") != "membercount" else "no timer")
            link = f"https://discord.com/channels/{ctx.guild.id}/{g['channel_id']}/{g['message_id']}"
            lines.append(f"🎉 **[{discord.utils.escape_markdown(str(g['prize']))[:80]}]({link})**\n"
                         f"-# {when} · hosted by <@{g['host_id']}> · {len(g.get('entries') or [])} entries")
        embed = base_embed(title="🎉 Active giveaways", description="\n\n".join(lines))
        if len(rows) > 15:
            embed.set_footer(text=f"Showing 15 of {len(rows)}")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="ticketstatus", description="See your open ticket and recent applications")
    @command_check()
    async def ticketstatus(self, ctx: commands.Context):
        embed = base_embed(title="🎫 Your status")
        ticket = await models.get_open_ticket_for_user(ctx.guild.id, ctx.author.id)
        if ticket:
            chan = ctx.guild.get_channel(int(ticket["channel_id"]))
            since = _aware(ticket.get("created_at"))
            text = chan.mention if chan else "*(channel missing)*"
            if since:
                text += f"\nOpened {discord.utils.format_dt(since, 'R')}"
            embed.add_field(name="Open ticket", value=text, inline=False)
        else:
            embed.add_field(name="Open ticket", value="You don't have an open ticket.", inline=False)
        subs = await models.get_submissions_for_user(ctx.guild.id, ctx.author.id, limit=5)
        if subs:
            lines = []
            for s in subs:
                icon = {"Accepted": "✅", "Denied": "❌", "Pending": "⏳"}.get(s.get("status"), "•")
                when = _aware(s.get("created_at"))
                lines.append(f"{icon} **{s.get('application_name') or 'Application'}** - {s.get('status', 'Pending')}"
                             + (f" ({discord.utils.format_dt(when, 'R')})" if when else ""))
            embed.add_field(name="Recent applications", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Recent applications", value="You haven't submitted any.", inline=False)
        await _reply(ctx, embed, ephemeral=True, delete_after=30)

    # ======================================================================
    # Community: birthdays
    # ======================================================================
    @commands.hybrid_group(name="birthday", description="Birthdays",
                           invoke_without_command=True)
    async def birthday(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await _reply(ctx, info_embed(
                "Use `/birthday set <day> <month>`, `/birthday view [member]` or `/birthday remove`."))

    @birthday.command(name="set", description="Save your birthday (day and month)")
    @app_commands.describe(day="Day of the month (1-31)", month="Month number (1-12)")
    @command_check()
    async def birthday_set(self, ctx: commands.Context, day: int, month: int):
        if not 1 <= month <= 12 or not 1 <= day <= calendar.monthrange(2024, month)[1]:   # 2024 allows Feb 29
            return await _reply(ctx, error_embed("That isn't a real date. Use a day and a month number, e.g. `14 2`."))
        await models.set_birthday(ctx.guild.id, ctx.author.id, day, month)
        await _reply(ctx, success_embed(f"Saved your birthday as **{day} {MONTHS[month - 1]}**."),
                     ephemeral=True, delete_after=8)

    @birthday.command(name="view", description="Show someone's saved birthday")
    @app_commands.describe(member="Whose birthday (default: you)")
    @command_check()
    async def birthday_view(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        m = member or ctx.author
        doc = await models.get_birthday(ctx.guild.id, m.id)
        if not doc:
            return await _reply(ctx, info_embed(f"{m.display_name} hasn't set a birthday."))
        await ctx.send(embed=base_embed(
            title="🎂 Birthday", description=f"**{m.display_name}**: {doc['day']} {MONTHS[doc['month'] - 1]}"))

    @birthday.command(name="remove", description="Delete your saved birthday")
    @command_check()
    async def birthday_remove(self, ctx: commands.Context):
        gone = await models.delete_birthday(ctx.guild.id, ctx.author.id)
        await _reply(ctx, success_embed("Your birthday was removed.") if gone
                     else info_embed("You didn't have a birthday saved."))

    @birthday.command(name="setchannel", description="Set the channel birthdays are announced in")
    @command_check()
    async def birthday_setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        await models.update_guild_settings(ctx.guild.id, {"birthdays.channel_id": str(channel.id)})
        await _reply(ctx, success_embed(f"Birthdays will be announced in {channel.mention}."), ephemeral=False)

    @tasks.loop(minutes=10)
    async def _birthday_loop(self):
        try:
            now = datetime.now(timezone.utc)
            rows = await models.get_birthdays_on(now.month, [now.day])
            if now.month == 3 and now.day == 1 and not calendar.isleap(now.year):
                rows += await models.get_birthdays_on(2, [29])    # Feb 29 people celebrate on Mar 1
            for doc in rows:
                if doc.get("last_announced_year") == now.year:
                    continue
                guild = self.bot.get_guild(int(doc["guild_id"]))
                chan_id = await models.get_birthday_channel_id(int(doc["guild_id"])) if guild else None
                channel = guild.get_channel(int(chan_id)) if chan_id else None
                member = guild.get_member(int(doc["user_id"])) if guild else None
                if channel is None or member is None:
                    continue
                try:
                    await channel.send(
                        content=member.mention,
                        embed=base_embed(title="🎂 Happy birthday!",
                                         description=f"Everyone wish {member.mention} a happy birthday! 🎉"),
                        allowed_mentions=discord.AllowedMentions(users=[member]))
                except (discord.Forbidden, discord.HTTPException):
                    continue
                await models.mark_birthday_announced(doc["_id"], now.year)
        except Exception as e:
            print(f"[birthday] loop error: {e}")

    @_birthday_loop.before_loop
    async def _before_birthday_loop(self):
        await self.bot.wait_until_ready()

    # ======================================================================
    # Fun
    # ======================================================================
    @commands.hybrid_command(name="coinflip", description="Flip a coin")
    @command_check()
    async def coinflip(self, ctx: commands.Context):
        side = random.choice(["Heads", "Tails"])
        await ctx.send(embed=base_embed(title="🪙 Coin flip", description=f"It's **{side}**!"))

    @commands.hybrid_command(name="roll", description="Roll dice, e.g. 2d6 or 1d20 (default 1d6)")
    @app_commands.describe(dice="NdM like 2d6, or just a number of sides like 20")
    @command_check()
    async def roll(self, ctx: commands.Context, dice: str = "1d6"):
        m = re.fullmatch(r"(?:(\d{1,2})d)?(\d{1,4})", dice.strip().lower())
        if not m:
            return await _reply(ctx, error_embed("Use `NdM` like `2d6`, or a number like `20`."))
        count, sides = int(m.group(1) or 1), int(m.group(2))
        if not 1 <= count <= 20 or not 2 <= sides <= 1000:
            return await _reply(ctx, error_embed("Roll 1-20 dice with 2-1000 sides."))
        rolls = [random.randint(1, sides) for _ in range(count)]
        body = f"**{sum(rolls)}**" + (f"\n-# {' + '.join(map(str, rolls))}" if count > 1 else "")
        await ctx.send(embed=base_embed(title=f"🎲 {count}d{sides}", description=body))

    @commands.hybrid_command(name="8ball", description="Ask the magic 8-ball a question")
    @app_commands.describe(question="Your yes/no question")
    @command_check()
    async def eight_ball(self, ctx: commands.Context, *, question: str):
        embed = base_embed(title="🎱 Magic 8-ball")
        embed.add_field(name="Question", value=question[:500], inline=False)
        embed.add_field(name="Answer", value=random.choice(EIGHT_BALL), inline=False)
        await ctx.send(embed=embed, allowed_mentions=NO_MENTIONS)

    @commands.hybrid_command(name="choose", description="Pick one of several options (separate with | or commas)")
    @app_commands.describe(options="e.g. pizza | burger | sushi")
    @command_check()
    async def choose(self, ctx: commands.Context, *, options: str):
        parts = [p.strip() for p in re.split(r"[|,]", options) if p.strip()]
        if len(parts) < 2:
            return await _reply(ctx, error_embed("Give me at least two options, separated by `|` or commas."))
        if len(parts) > 25:
            return await _reply(ctx, error_embed("That's too many options (max 25)."))
        await ctx.send(embed=base_embed(title="🤔 I choose…", description=f"**{random.choice(parts)[:500]}**"),
                       allowed_mentions=NO_MENTIONS)


async def setup(bot: commands.Bot):
    await bot.add_cog(MemberTools(bot))
