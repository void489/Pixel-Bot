"""
Auto-moderation - configured ONLY on the dashboard (Auto-Mod tab). There are no commands.

Everything is set on the dashboard: the master switch, each rule and its limit, the bad-word
list, allowed link domains, exempt roles / channels, the log channel and the strike ladder.
Changes take effect within ~30 seconds (the config is cached that long).

Rules
    spam      N messages inside 5 seconds (default 5); the burst is deleted
    mentions  more than N user + role mentions in one message (default 5)
    invites   Discord invites to OTHER servers (this server's own invites are allowed)
    links     any link whose domain isn't on the allow list (off by default)
    caps      a message that is N% or more capital letters (default 70, off by default)
    badwords  anything on the bad-word list (whole words; "word*" matches a prefix)

What happens on a violation: the message is deleted, the member gets a short notice that
vanishes after a few seconds, a mod case (action "automod") is saved so it shows in /history,
and it is logged. Each violation is also a STRIKE. Strikes expire after `window_minutes`
(default 10); when a member's strikes reach the ladder they are timed out / kicked / banned
(default: 3 strikes -> 10 minute timeout; kick and ban are off).

Strikes are stored in MongoDB (collection `automod_strikes`), so a restart or redeploy does
NOT give everyone a clean slate. They are visible - and can be cleared - on the dashboard.

Never checked: members with an exempt role (picked on the dashboard), exempt channels, bots and
webhooks. Administrators, Manage Messages holders and the owner get NO automatic bypass - only
the exempt roles you select. The bot needs Manage Messages (to delete) in the channel, plus Moderate Members /
Kick / Ban for the ladder, and Manage Server to tell its own invites from other servers'.
Edited messages are checked again (except for the spam rule).
"""
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from cogs.moderation import _dm_target, _log_case, format_duration
from config import constants
from database import models
from database.automod_config import clean_domain
from database.mongo import automod_strikes_col
from utils.embeds import base_embed

INVITE_RE = re.compile(
    r"(?:discord(?:app)?\.com/invite|discord\.gg|discord\.me|dsc\.gg|invite\.gg)/([A-Za-z0-9-]{2,32})",
    re.IGNORECASE)
URL_RE = re.compile(r"(?:https?://|www\.)([^\s/<>\"'?#]+)[^\s<>\"']*", re.IGNORECASE)
STRIP_RE = re.compile(r"https?://\S+|www\.\S+|<a?:\w+:\d+>|<[@#][!&]?\d+>")
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")

SPAM_WINDOW_SECONDS = 5

NOTICES = {
    "spam": "Slow down - you're sending messages too fast.",
    "mentions": "Too many mentions in one message.",
    "invites": "Invite links to other servers aren't allowed here.",
    "links": "Links aren't allowed here.",
    "caps": "Please don't shout (too many capital letters).",
    "badwords": "Watch your language.",
}
LABELS = {
    "spam": "Spam", "mentions": "Mass mentions", "invites": "Invite link",
    "links": "Link", "caps": "Excess caps", "badwords": "Bad word",
}


class AutoMod(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cfg_cache = {}                  # guild_id -> (expires, cfg)
        self._recent = defaultdict(lambda: deque(maxlen=20))   # (guild, user) -> [(t, message)]
        self._invite_cache = {}               # guild_id -> (expires, {codes})
        self._word_cache = {}                 # guild_id -> (words tuple, compiled regex | None)

    async def cog_load(self):
        # Strikes live in Mongo; idle docs are removed by a TTL index.
        try:
            await automod_strikes_col().create_index(
                "updated_at", expireAfterSeconds=models.AUTOMOD_STRIKE_TTL_SECONDS)
            await automod_strikes_col().create_index("guild_id")
        except Exception as e:
            print(f"[automod] strike index creation failed: {e}")
        self._cleanup.start()

    async def cog_unload(self):
        self._cleanup.cancel()

    # ------------------------------------------------------------------
    # Config cache (30s) - so a busy server isn't one DB read per message
    # ------------------------------------------------------------------
    async def _get_cfg(self, guild_id: int) -> dict:
        now = time.monotonic()
        hit = self._cfg_cache.get(guild_id)
        if hit and hit[0] > now:
            return hit[1]
        cfg = await models.get_automod_config(guild_id)
        self._cfg_cache[guild_id] = (now + 30, cfg)
        return cfg

    def _drop_cache(self, guild_id: int):
        self._cfg_cache.pop(guild_id, None)
        self._word_cache.pop(guild_id, None)

    @tasks.loop(minutes=10)
    async def _cleanup(self):
        now = time.monotonic()
        for key in list(self._recent):
            dq = self._recent[key]
            while dq and now - dq[0][0] > 30:
                dq.popleft()
            if not dq:
                del self._recent[key]

    @_cleanup.before_loop
    async def _before_cleanup(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self._process(message, edited=False)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content != after.content:
            await self._process(after, edited=True)

    async def _process(self, message: discord.Message, edited: bool):
        if (message.guild is None or message.author.bot or message.webhook_id
                or not isinstance(message.author, discord.Member)):
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        cfg = await self._get_cfg(message.guild.id)
        if not cfg["enabled"]:
            return

        member = message.author
        # NO permission-based bypass: admins, Manage Messages holders and the server owner
        # are checked like everyone else. The ONLY way a member skips AutoMod is having one
        # of the exempt roles picked on the dashboard (Exemptions -> Exempt roles).
        chan = message.channel
        chan_ids = {str(chan.id)}
        for attr in ("parent_id", "category_id"):
            if getattr(chan, attr, None):
                chan_ids.add(str(getattr(chan, attr)))
        if chan_ids & set(cfg["exempt_channel_ids"]):
            return
        if {str(r.id) for r in member.roles} & set(cfg["exempt_role_ids"]):
            return
        if not chan.permissions_for(message.guild.me).manage_messages:
            return                                   # can't enforce anything here

        try:
            hit = await self._check(message, cfg, edited)
        except Exception as e:
            print(f"[automod] check failed: {type(e).__name__}: {e}")
            return
        if hit:
            await self._handle(message, cfg, *hit)

    async def _check(self, message: discord.Message, cfg: dict, edited: bool):
        """-> (rule, detail, [messages to delete]) or None."""
        rules = cfg["rules"]
        content = message.content or ""
        one = [message]

        link_text = content
        codes = INVITE_RE.findall(content) if content else []
        if codes:
            own = await self._own_invite_codes(message.guild)
            if rules["invites"]["enabled"] and any(c.lower() not in own for c in codes):
                return "invites", "posted an invite to another server", one
            # This server's own invites are fine for the links rule too.
            link_text = INVITE_RE.sub(
                lambda m: "" if m.group(1).lower() in own else m.group(0), content)

        if rules["links"]["enabled"] and link_text:
            allow = cfg["link_allow"]
            for host in URL_RE.findall(link_text):
                host = clean_domain(host)
                if host and not any(host == d or host.endswith("." + d) for d in allow):
                    return "links", f"posted a link ({host})", one

        if rules["badwords"]["enabled"] and content and cfg["badwords"]:
            rx = self._badword_regex(message.guild.id, cfg["badwords"])
            if rx and rx.search(ZERO_WIDTH_RE.sub("", content)):
                return "badwords", "used a word on the bad-word list", one

        limit = rules["mentions"]["limit"]
        if rules["mentions"]["enabled"] and limit > 0:
            count = len({m.id for m in message.mentions}) + len(message.role_mentions)
            if count > limit:
                return "mentions", f"{count} mentions in one message (max {limit})", one

        if rules["caps"]["enabled"] and content:
            letters = [c for c in STRIP_RE.sub("", content) if c.isalpha()]
            if len(letters) >= 10:
                pct = 100 * sum(1 for c in letters if c.isupper()) / len(letters)
                if pct >= rules["caps"]["limit"]:
                    return "caps", f"{pct:.0f}% capital letters", one

        if rules["spam"]["enabled"] and not edited:
            limit = max(3, rules["spam"]["limit"])
            key = (message.guild.id, message.author.id)
            now = time.monotonic()
            dq = self._recent[key]
            dq.append((now, message))
            burst = [m for t, m in dq if now - t <= SPAM_WINDOW_SECONDS]
            if len(burst) >= limit:
                dq.clear()
                return "spam", f"{len(burst)} messages in {SPAM_WINDOW_SECONDS}s", burst
        return None

    async def _own_invite_codes(self, guild: discord.Guild) -> set:
        now = time.monotonic()
        hit = self._invite_cache.get(guild.id)
        if hit and hit[0] > now:
            return hit[1]
        codes = set()
        if getattr(guild, "vanity_url_code", None):
            codes.add(guild.vanity_url_code.lower())
        try:
            for inv in await guild.invites():
                codes.add(inv.code.lower())
        except (discord.Forbidden, discord.HTTPException):
            pass
        self._invite_cache[guild.id] = (now + 60, codes)
        return codes

    def _badword_regex(self, guild_id: int, words: list):
        key = tuple(words)
        hit = self._word_cache.get(guild_id)
        if hit and hit[0] == key:
            return hit[1]
        parts = [re.escape(w.strip().lower()).replace(r"\*", r"\w*") for w in words if w.strip()]
        rx = re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)", re.IGNORECASE) if parts else None
        self._word_cache[guild_id] = (key, rx)
        return rx

    # ------------------------------------------------------------------
    # Reaction to a violation
    # ------------------------------------------------------------------
    async def _handle(self, message: discord.Message, cfg: dict, rule: str, detail: str, to_delete: list):
        guild, member = message.guild, message.author

        by_channel = defaultdict(list)
        for m in to_delete:
            by_channel[m.channel].append(m)
        for ch, msgs in by_channel.items():
            try:
                if len(msgs) == 1:
                    await msgs[0].delete()
                else:
                    await ch.delete_messages(msgs)
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                pass

        try:
            await message.channel.send(
                f"{member.mention} {NOTICES[rule]}", delete_after=6,
                allowed_mentions=discord.AllowedMentions(users=[member]))
        except (discord.Forbidden, discord.HTTPException):
            pass

        # Strike ladder - strikes are stored in Mongo so a restart doesn't forget them.
        st = cfg["strikes"]
        window = max(60, st["window_minutes"] * 60)
        try:
            count = await models.add_automod_strike(guild.id, member.id, window)
        except Exception as e:
            print(f"[automod] couldn't save strike: {type(e).__name__}: {e}")
            count = 1

        action = None
        if st["ban_at"] and count >= st["ban_at"]:
            action = "ban"
        elif st["kick_at"] and count >= st["kick_at"]:
            action = "kick"
        elif st["mute_at"] and count >= st["mute_at"]:
            action = "mute"

        reason = f"[AutoMod] {LABELS[rule]}: {detail}"
        case = await models.create_mod_case(
            guild.id, "automod", self.bot.user.id, member.id, reason=reason,
            channel_id=message.channel.id, extra={"rule": rule})

        outcome = await self._apply_action(guild, member, action, st, count, reason)
        await self._log_violation(guild, cfg, message, rule, detail, case, count, outcome)

    async def _apply_action(self, guild, member, action, st, count, reason):
        """Run the ladder step. Returns a short string for the log ('' if none was due)."""
        if not action:
            return ""
        if member.id == guild.owner_id or member.top_role >= guild.me.top_role:
            return f"⚠️ {action} skipped: their top role is at or above mine"
        try:
            if action == "mute":
                seconds = max(60, st["mute_minutes"] * 60)
                await _dm_target(member, guild, f"🔇 You were muted in {guild.name}",
                                 f"Duration: {format_duration(seconds)}\nReason: {reason}",
                                 constants.COLOR_WARNING)
                await member.timeout(datetime.now(timezone.utc) + timedelta(seconds=seconds),
                                     reason=reason)
                case = await models.create_mod_case(
                    guild.id, "mute", self.bot.user.id, member.id, reason=reason,
                    duration_seconds=seconds)
                await _log_case(self.bot, guild, case, constants.COLOR_WARNING, "🔇 Muted (AutoMod)")
                return f"🔇 Muted for {format_duration(seconds)} ({count} strikes)"
            if action == "kick":
                await _dm_target(member, guild, f"👢 You were kicked from {guild.name}",
                                 f"Reason: {reason}", constants.COLOR_WARNING)
                await member.kick(reason=reason)
                case = await models.create_mod_case(
                    guild.id, "kick", self.bot.user.id, member.id, reason=reason)
                await _log_case(self.bot, guild, case, constants.COLOR_WARNING, "👢 Kicked (AutoMod)")
                await models.clear_automod_strikes(guild.id, member.id)
                return f"👢 Kicked ({count} strikes)"
            if action == "ban":
                await _dm_target(member, guild, f"🔨 You were banned from {guild.name}",
                                 f"Reason: {reason}", constants.COLOR_ERROR)
                await member.ban(reason=reason, delete_message_days=0)
                case = await models.create_mod_case(
                    guild.id, "ban", self.bot.user.id, member.id, reason=reason)
                await _log_case(self.bot, guild, case, constants.COLOR_ERROR, "🔨 Banned (AutoMod)")
                await models.clear_automod_strikes(guild.id, member.id)
                return f"🔨 Banned ({count} strikes)"
        except discord.Forbidden:
            return f"⚠️ {action} failed: missing permission"
        except discord.HTTPException as e:
            return f"⚠️ {action} failed: {e}"
        return ""

    async def _log_violation(self, guild, cfg, message, rule, detail, case, count, outcome):
        log_id = cfg["log_channel_id"] or (await models.get_moderation_config(guild.id)).get("log_channel_id")
        channel = guild.get_channel(int(log_id)) if log_id else None
        if channel is None:
            return
        embed = base_embed(title=f"🤖 AutoMod · {LABELS[rule]} · Case #{case['case_num']}",
                           color=constants.COLOR_WARNING)
        embed.add_field(name="Member", value=f"{message.author.mention}\n`{message.author.id}`")
        embed.add_field(name="Channel", value=message.channel.mention)
        embed.add_field(name="Strikes", value=str(count))
        embed.add_field(name="Why", value=detail, inline=False)
        content = (message.content or "*(no text)*").replace("`", "'")
        embed.add_field(name="Message", value=f"```{content[:900]}```", inline=False)
        if outcome:
            embed.add_field(name="Action", value=outcome, inline=False)
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoMod(bot))
