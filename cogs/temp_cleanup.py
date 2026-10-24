"""TEMPORARY server-cleanup commands (bot owner only).

    /delete all channels   - deletes every channel + category in the server
    /delete all roles      - deletes every role the bot is allowed to delete
    /buildlayout           - creates the categories + channels (no permission overwrites)

!! THIS IS A TEMPORARY COG - REMOVE IT WHEN YOUR SERVER IS CLEANED !!
To remove it:
  1. delete  "cogs.temp_cleanup"  from the COGS list in main.py
  2. delete  cogs/temp_cleanup.py
  3. redeploy (the slash commands disappear on the next sync)

Safety:
  * Only the bot's Discord application owner (or a member of the owning Team) can
    run it - checked with bot.is_owner(), not with server permissions.
  * Each command asks for a button confirmation first (30s timeout).
  * Only affects the server the command is run in.
  * Not registered in the dashboard's Command Permissions page (no @command_check).
"""
import asyncio

import discord
from discord import app_commands
from discord.ext import commands

REASON = "Temporary owner cleanup command"
REMINDER = ("⚠️ **Temporary command.** Remove `cogs.temp_cleanup` from `main.py` and delete "
            "`cogs/temp_cleanup.py` once your server is cleaned.")

LAYOUT = """
.　　⁺　　ɪɴꜰᴏʀᴍᴀᴛɪᴠᴇ　　⁺　　﹒
【👋】・Welcome 【📢】・Announcements 【📜】・Rules 【🎭】・Role-Selection 【🤝】・Partnerships 【❔】・Frequently-Asked-Questions
.　　⁺　　ᴄᴏᴍᴍᴜɴɪᴛʏ　　⁺　　﹒
【☁️】・General 【😂】・Memes 【📸】・Media 【🤖】・Bot-Commands 【📊】・Level-Ups 【🎮】・Gaming 【💭】・Suggestions 【🏆】・Community-Events
.　　⁺　　ᴘᴜʙʟɪᴄ ᴍᴀʀᴋᴇᴛ　　⁺　　﹒
【🛒】・Public-Market 【⭐】・Vouches 【📏】・Market-Rules 【🔎】・Buyer-Requests 【📣】・Seller-Listings
.　　⁺　　ᴛʀᴜꜱᴛᴇᴅ ᴍᴀʀᴋᴇᴛ　　⁺　　﹒
【🛡️】・Trusted-Market 【💠】・Trusted-Listings 【📜】・Trusted-Market-Rules
.　　⁺　　ꜱᴇʀᴠɪᴄᴇꜱ　　⁺　　﹒
【🏗️】・Build-Services 【🧱】・Build-Showcase 【📐】・Schematic-Designs 【💎】・Spawner-Market 【💰】・Spawner-Prices 【📦】・Quickdrops 【🪄】・Custom-Orders 【🧾】・Service-Reviews
.　　⁺　　ᴛɪᴄᴋᴇᴛꜱ　　⁺　　﹒
【🎫】・Open-A-Ticket 【🌟】・Customer-Reviews
.　　⁺　　ɢɪᴠᴇᴀᴡᴀʏꜱ　　⁺　　﹒
【🎉】・Giveaways 【🎁】・Giveaway-Winners 【💎】・Sponsor-Information 【🎟️】・Giveaway-Rules
.　　⁺　　ᴀᴘᴘʟɪᴄᴀᴛɪᴏɴꜱ　　⁺　　﹒
【👮】・Staff-Applications 【🔨】・Builder-Applications 【📋】・Application-Information
.　　⁺　　ꜱᴛᴀꜰꜰ ʀᴏᴏᴍ　　⁺　　﹒
【🔒】・Staff-Chat 【📂】・Staff-Logs 【📨】・Ticket-Logs 【📈】・Staff-Activity 【⚖️】・Staff-Reports 【🚫】・Blacklist-Logs 【🗒️】・Staff-Announcements 【🧠】・Staff-Meeting
.　　⁺　　ʙᴜɪʟᴅᴇʀ ʀᴏᴏᴍ　　⁺　　﹒
【🛠️】・Builder-Chat 【📑】・Assigned-Orders 【💵】・Builder-Payouts 【🗃️】・Order-Logs 【🖼️】・Work-In-Progress 【🧮】・Build-Quotes
"""


def parse_layout():
    """-> [(category_name, [channel_name, ...]), ...]. A line starting with '.' is a
    category; any other line is a row of channels, each starting with '【'."""
    result = []
    for line in LAYOUT.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("."):
            result.append((line, []))
        elif result:
            for chunk in line.split("【"):
                chunk = chunk.strip()
                if chunk:
                    result[-1][1].append("【" + chunk)
    return result


class ConfirmView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=30)
        self.owner_id = owner_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, delete", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


class TempCleanup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        print("[temp_cleanup] TEMP owner-only /delete all channels|roles loaded - "
              "remove cogs.temp_cleanup from main.py when done.")

    # ----------------------------------------------------------------- helpers
    async def _guard(self, ctx: commands.Context, perm: str, perm_label: str) -> bool:
        """Owner-only + guild-only + bot-has-permission. Replies and returns False if not OK."""
        if ctx.guild is None:
            await ctx.send("This only works in a server.", ephemeral=True)
            return False
        if not await self.bot.is_owner(ctx.author):
            await ctx.send("Only the bot owner can use this command.", ephemeral=True)
            return False
        if not getattr(ctx.guild.me.guild_permissions, perm):
            await ctx.send(f"I need the **{perm_label}** permission to do that.", ephemeral=True)
            return False
        return True

    async def _confirm(self, ctx: commands.Context, text: str):
        """Returns (confirmed, message)."""
        view = ConfirmView(ctx.author.id)
        msg = await ctx.send(f"{text}\n\n{REMINDER}", view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            try:
                await msg.edit(content="Cancelled. Nothing was deleted.", view=None)
            except discord.HTTPException:
                pass
        return view.confirmed, msg

    async def _report(self, ctx: commands.Context, msg, text: str):
        """Edit the confirmation message; if that fails (e.g. channel gone), DM the owner."""
        text = text[:1900]
        try:
            await msg.edit(content=text, view=None)
            return
        except discord.HTTPException:
            pass
        try:
            await ctx.author.send(text)
        except discord.HTTPException:
            pass

    @staticmethod
    def _summary(what: str, ok: int, failed: list, skipped: int = 0) -> str:
        lines = [f"✅ Deleted **{ok}** {what}."]
        if skipped:
            lines.append(f"⏭️ Skipped **{skipped}** (managed/integration roles, @everyone, "
                         f"or roles above my top role).")
        if failed:
            lines.append(f"❌ Failed to delete **{len(failed)}**:")
            lines += [f"• {name}: {err}" for name, err in failed[:10]]
            if len(failed) > 10:
                lines.append(f"…and {len(failed) - 10} more.")
        lines.append("")
        lines.append(REMINDER)
        return "\n".join(lines)

    # ---------------------------------------------------------------- commands
    @commands.hybrid_group(name="delete", description="TEMP owner-only cleanup commands",
                           invoke_without_command=True)
    @commands.guild_only()
    async def delete(self, ctx: commands.Context):
        await ctx.send("Use `/delete all channels` or `/delete all roles`.", ephemeral=True)

    @delete.group(name="all", description="TEMP owner-only: delete everything of one kind",
                  invoke_without_command=True)
    @commands.guild_only()
    async def delete_all(self, ctx: commands.Context):
        await ctx.send("Use `/delete all channels` or `/delete all roles`.", ephemeral=True)

    @delete_all.command(name="channels", description="TEMP owner-only: delete EVERY channel and category")
    @commands.guild_only()
    async def delete_all_channels(self, ctx: commands.Context):
        if not await self._guard(ctx, "manage_channels", "Manage Channels"):
            return
        guild = ctx.guild

        confirmed, msg = await self._confirm(
            ctx,
            f"🚨 This will delete **ALL {len(guild.channels)} channels and categories** in "
            f"**{guild.name}**. This cannot be undone.",
        )
        if not confirmed:
            return

        try:
            await msg.edit(content="🗑️ Deleting channels… I'll DM you the result if this channel "
                                   "gets deleted before I can post it.", view=None)
        except discord.HTTPException:
            pass

        # If the command was run in a thread, its parent channel is what must go last.
        current = ctx.channel.parent if isinstance(ctx.channel, discord.Thread) else ctx.channel
        current_id = current.id if current else None

        others = [c for c in guild.channels
                  if not isinstance(c, discord.CategoryChannel) and c.id != current_id]
        cats = [c for c in guild.channels
                if isinstance(c, discord.CategoryChannel) and c.id != current_id]
        order = others + cats
        if current is not None and current in guild.channels:
            order.append(current)  # last, so the confirmation stays visible as long as possible

        ok, failed = 0, []
        for channel in order:
            try:
                await channel.delete(reason=f"{REASON} ({ctx.author})")
                ok += 1
            except (discord.Forbidden, discord.HTTPException) as e:
                failed.append((f"#{channel.name}", str(e)[:80]))
            await asyncio.sleep(0.4)

        text = self._summary("channels", ok, failed)
        if failed:
            text += ("\nTip: Community servers can't delete the Rules/Updates channels - "
                     "turn off Community in Server Settings first.")
        await self._report(ctx, msg, text)

    @delete_all.command(name="roles", description="TEMP owner-only: delete EVERY role I'm able to delete")
    @commands.guild_only()
    async def delete_all_roles(self, ctx: commands.Context):
        if not await self._guard(ctx, "manage_roles", "Manage Roles"):
            return
        guild = ctx.guild
        top = guild.me.top_role

        deletable = [r for r in guild.roles
                     if not r.is_default() and not r.managed and r < top]
        skipped = len(guild.roles) - 1 - len(deletable)  # -1 for @everyone

        confirmed, msg = await self._confirm(
            ctx,
            f"🚨 This will delete **{len(deletable)} roles** in **{guild.name}** "
            f"(skipping {skipped} I can't delete). This cannot be undone.",
        )
        if not confirmed:
            return

        try:
            await msg.edit(content="🗑️ Deleting roles…", view=None)
        except discord.HTTPException:
            pass

        ok, failed = 0, []
        for role in deletable:
            try:
                await role.delete(reason=f"{REASON} ({ctx.author})")
                ok += 1
            except (discord.Forbidden, discord.HTTPException) as e:
                failed.append((f"@{role.name}", str(e)[:80]))
            await asyncio.sleep(0.4)

        await self._report(ctx, msg, self._summary("roles", ok, failed, skipped))

    @commands.hybrid_command(name="buildlayout",
                             description="TEMP owner-only: create the server's categories + channels (no permissions)")
    @commands.guild_only()
    async def buildlayout(self, ctx: commands.Context):
        if not await self._guard(ctx, "manage_channels", "Manage Channels"):
            return
        guild = ctx.guild
        layout = parse_layout()
        total = sum(len(chs) for _, chs in layout)

        confirmed, msg = await self._confirm(
            ctx,
            f"This will create **{len(layout)} categories** and **{total} text channels** in "
            f"**{guild.name}**, with **no permission overwrites** (default permissions only).",
        )
        if not confirmed:
            return
        try:
            await msg.edit(content="Building…", view=None)
        except discord.HTTPException:
            pass

        made_cats, made_chs, failed = 0, 0, []
        for cat_name, channels in layout:
            try:
                category = await guild.create_category(cat_name, reason=REASON)
                made_cats += 1
            except (discord.Forbidden, discord.HTTPException) as e:
                failed.append((cat_name, str(e)[:80]))
                continue
            await asyncio.sleep(0.4)
            for ch_name in channels:
                try:
                    await guild.create_text_channel(ch_name, category=category, reason=REASON)
                    made_chs += 1
                except (discord.Forbidden, discord.HTTPException) as e:
                    failed.append((ch_name, str(e)[:80]))
                await asyncio.sleep(0.4)

        lines = [f"✅ Created **{made_cats}** categories and **{made_chs}** channels."]
        if failed:
            lines.append(f"❌ Failed **{len(failed)}**:")
            lines += [f"• {n}: {e}" for n, e in failed[:10]]
        lines += ["", REMINDER]
        await self._report(ctx, msg, "\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(TempCleanup(bot))
