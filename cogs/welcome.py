"""Welcome message + auto role.

Configured entirely from the dashboard (Welcome & Auto Role tab) - there are no
commands. When a member joins:
  - every auto role is added (humans only), and
  - the welcome message is posted in the chosen channel (if one is set).
"""
import discord
from discord.ext import commands

from database import models
from utils.welcome import (
    DEFAULT_WELCOME_MESSAGE, MAX_WELCOME_LEN, render_welcome, role_is_dangerous,
)


class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        cfg = await models.get_welcome_config(member.guild.id)
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

    async def _send_welcome(self, member: discord.Member, cfg: dict):
        if not cfg["channel_id"]:
            return
        guild = member.guild
        channel = guild.get_channel(int(cfg["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return
        text = render_welcome(
            cfg["message"] or DEFAULT_WELCOME_MESSAGE,
            user_mention=member.mention,
            username=member.name,
            server=guild.name,
            member_count=guild.member_count,
        )[:MAX_WELCOME_LEN]
        if not text.strip():
            return
        try:
            # Only the new member can be pinged - never @everyone or roles.
            await channel.send(text, allowed_mentions=discord.AllowedMentions(
                users=[member], roles=False, everyone=False))
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[welcome] couldn't post welcome in {guild.id}: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
