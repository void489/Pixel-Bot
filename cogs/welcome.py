"""Welcome message + auto role.

Configured entirely from the dashboard (Welcome & Auto Role tab) - there are no
commands. When a member joins:
  - every auto role is added (humans only), and
  - the welcome message is posted in the chosen channel (if one is set).

A server uses ONE welcome type (cfg["type"], missing = normal):
  normal - plain text
  embed  - ping above an embed (joiner's avatar as the right-hand thumbnail)
  image  - ping + text line + a banner drawn with Pillow (utils/welcome_image.py)
Only the new member can ever be pinged.

The Leaves section (guild_settings["leave"]) works the same way when a member leaves,
except nobody is pinged and the image says GOODBYE.
"""
import asyncio
import io

import discord
from discord.ext import commands

from database import models
from utils.welcome import (
    DEFAULT_EMBED, DEFAULT_LEAVE_EMBED, DEFAULT_LEAVE_MESSAGE, DEFAULT_WELCOME_MESSAGE,
    EMBED_DESC_MAX, EMBED_FOOTER_MAX, EMBED_TITLE_MAX, IMAGE_TEXT_MAX, MAX_WELCOME_LEN,
    render_welcome, role_is_dangerous,
)


class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        try:
            cfg = await models.get_welcome_config(member.guild.id)
        except Exception as e:
            print(f"[welcome] couldn't load the config in {member.guild.id}: {e}")
            return
        try:
            await self._give_auto_roles(member, cfg["autorole_ids"])
        except Exception as e:
            print(f"[welcome] auto role failed in {member.guild.id}: {e}")
        try:
            await self._send_welcome(member, cfg)
        except Exception as e:
            print(f"[welcome] welcome message failed in {member.guild.id}: {e}")

    async def _give_auto_roles(self, member: discord.Member, role_ids: list):
        if not role_ids:
            return
        guild = member.guild
        me = guild.me
        roles = []
        for rid in role_ids:
            role = guild.get_role(int(rid))
            if role is None or role.is_default() or role.managed:
                continue
            if me is None or role >= me.top_role:
                print(f"[welcome] can't assign '{role.name}' in {guild.id}: it's not below my top role")
                continue
            if role_is_dangerous(role.permissions.value):
                print(f"[welcome] skipped '{role.name}' in {guild.id}: it has admin/moderation permissions")
                continue
            if role in member.roles:
                continue
            roles.append(role)
        if not roles:
            return
        try:
            await member.add_roles(*roles, reason="Auto role")
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[welcome] couldn't add auto roles in {guild.id}: {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot:
            return
        try:
            cfg = await models.get_leave_config(member.guild.id)
            await self._send_welcome(member, cfg, leave=True)
        except Exception as e:
            print(f"[welcome] leave message failed in {member.guild.id}: {e}")

    async def _send_welcome(self, member: discord.Member, cfg: dict, leave: bool = False):
        """leave=True: the Leaves section - same types, but nobody is pinged and the
        member's name is plain text."""
        if not cfg["channel_id"]:
            return
        guild = member.guild
        channel = guild.get_channel(int(cfg["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return
        # Only the new member can be pinged - never @everyone or roles. On leave: nobody.
        mentions = (discord.AllowedMentions.none() if leave else
                    discord.AllowedMentions(users=[member], roles=False, everyone=False))
        kind = cfg.get("type") or "normal"
        try:
            if kind == "embed":
                await self._send_embed(member, channel, cfg, mentions, leave)
            elif kind == "image":
                await self._send_image(member, channel, cfg, mentions, leave)
            else:
                await self._send_normal(member, channel, cfg, mentions, leave)
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[welcome] couldn't post {'leave' if leave else 'welcome'} message in {guild.id}: {e}")

    @staticmethod
    def _fill(template: str, member: discord.Member, *, markdown: bool, mention: bool) -> str:
        """Fill the placeholders. `markdown`: escape the username / server name so they
        can't change formatting. `mention`: {user} is a ping/mention, else the plain name
        (titles and footers can't show mentions)."""
        guild = member.guild
        name, server = member.name, guild.name
        if markdown:
            name, server = discord.utils.escape_markdown(name), discord.utils.escape_markdown(server)
        return render_welcome(
            template,
            user_mention=member.mention if mention else name,
            username=name,
            server=server,
            member_count=guild.member_count,
        )

    async def _send_normal(self, member, channel, cfg, mentions, leave=False):
        text = render_welcome(
            cfg["message"] or (DEFAULT_LEAVE_MESSAGE if leave else DEFAULT_WELCOME_MESSAGE),
            user_mention=member.name if leave else member.mention,
            username=member.name,
            server=member.guild.name,
            member_count=member.guild.member_count,
        )[:MAX_WELCOME_LEN]
        if not text.strip():
            return
        await channel.send(text, allowed_mentions=mentions)

    async def _send_embed(self, member, channel, cfg, mentions, leave=False):
        e = cfg["embed"]
        ping = not leave
        title = self._fill(e["title"], member, markdown=False, mention=False)[:EMBED_TITLE_MAX]
        desc = self._fill(e["description"], member, markdown=True, mention=ping)[:EMBED_DESC_MAX]
        footer = self._fill(e["footer"], member, markdown=False, mention=False)[:EMBED_FOOTER_MAX]
        default = DEFAULT_LEAVE_EMBED if leave else DEFAULT_EMBED
        if not (title.strip() or desc.strip() or footer.strip()):
            # an embed with nothing in it can't be sent: use the defaults
            title = self._fill(default["title"], member, markdown=False, mention=False)
            desc = self._fill(default["description"], member, markdown=True, mention=ping)
        try:
            colour = discord.Colour(int(e["color"].lstrip("#"), 16))
        except (ValueError, AttributeError):
            colour = discord.Colour(int(default["color"].lstrip("#"), 16))
        embed = discord.Embed(title=title.strip() or None, description=desc.strip() or None, colour=colour)
        embed.set_thumbnail(url=member.display_avatar.replace(size=256).url)   # shown on the right
        if footer.strip():
            embed.set_footer(text=footer.strip())
        # welcome: the ping lives in the message content, above the embed (embeds never ping)
        await channel.send(None if leave else member.mention, embed=embed, allowed_mentions=mentions)

    async def _send_image(self, member, channel, cfg, mentions, leave=False):
        text = self._fill(cfg["image_text"], member, markdown=True, mention=not leave)[:IMAGE_TEXT_MAX].strip()
        if not leave and member.mention not in text:     # the joiner is always pinged
            text = f"{member.mention}\n{text}".strip()
        png = await self._make_image(member, leave)
        if png is None:                          # image failed: still post the text line
            if text:
                await channel.send(text, allowed_mentions=mentions)
            return
        try:
            await channel.send(text or None, file=discord.File(io.BytesIO(png), filename="welcome.png"),
                               allowed_mentions=mentions)
        except discord.Forbidden:               # no "Attach Files" here: text only
            if text:
                await channel.send(text, allowed_mentions=mentions)

    async def _make_image(self, member: discord.Member, leave: bool = False):
        """PNG bytes, or None if anything goes wrong (never raises)."""
        try:
            from utils.welcome_image import render_welcome_image
            avatar = None
            try:
                avatar = await member.display_avatar.replace(size=256, format="png").read()
            except Exception as e:
                print(f"[welcome] avatar download failed in {member.guild.id}: {e}")
            try:
                background = await models.get_welcome_background(member.guild.id, "leave" if leave else "welcome")
            except Exception as e:
                print(f"[welcome] background load failed in {member.guild.id}: {e}")
                background = None
            return await asyncio.to_thread(
                render_welcome_image, member.display_name or member.name, avatar, background,
                "GOODBYE" if leave else "WELCOME")
        except Exception as e:
            print(f"[welcome] image failed in {member.guild.id}: {e}")
            return None


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
