"""Startup events, persistent-view re-registration, and global error handlers."""
import discord
from discord import app_commands
from discord.ext import commands

from database import models
from utils.embeds import error_embed
from utils.log import get_logger
from cogs.tickets import (
    TicketPanelView, TicketPanelBoxView, panel_image_ref, TicketControlView, TicketPriorityView, box_supported,
)

logger = get_logger(__name__)

_UNEXPECTED_ERROR_TEXT = ("Something went wrong while running that. The error has been "
                          "logged for the bot's admins - please try again in a moment.")


class Events(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._original_tree_error = None

    async def cog_load(self):
        # Slash commands: take over the CommandTree's error handler (discord.py's
        # default only logs; ours also tells the user something went wrong).
        # Restored in cog_unload, same pattern as the interaction_check swap in
        # cogs/admin_tools.py.
        self._original_tree_error = self.bot.tree.on_error
        self.bot.tree.on_error = self._on_app_command_error

    async def cog_unload(self):
        self.bot.tree.on_error = self._original_tree_error

    # ------------------------------------------------------------------
    # Global error handlers
    # ------------------------------------------------------------------
    # Both handlers log at ERROR level, which is what the admin page's Errors
    # tab collects (_ErrorCapture in cogs/admin_tools.py), and answer the user
    # so a failed command doesn't just hang on Discord's generic response.
    # Check failures are skipped: those checks (maintenance mode, switched-off
    # features, command permissions) already send their own message.

    async def _on_app_command_error(self, interaction: discord.Interaction,
                                    error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return
        cmd = interaction.command.qualified_name if interaction.command else "?"
        logger.error("Unhandled error in app command %s: %s", cmd, error, exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=error_embed(_UNEXPECTED_ERROR_TEXT), ephemeral=True)
            else:
                await interaction.response.send_message(embed=error_embed(_UNEXPECTED_ERROR_TEXT), ephemeral=True)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CheckFailure):
            return
        cmd = ctx.command.qualified_name if ctx.command else "?"
        logger.error("Unhandled error in command %s: %s", cmd, error, exc_info=error)
        try:
            await ctx.send(embed=error_embed(_UNEXPECTED_ERROR_TEXT))
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------
    # Startup: re-attach persisted views
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self):
        # 1) Re-attach every persisted ticket panel view (per-type buttons).
        try:
            for guild in self.bot.guilds:
                panels = await models.get_ticket_panels(guild.id)
                for panel in panels:
                    ticket_types = []
                    for tid in panel.get("ticket_type_ids", []):
                        tt = await models.get_ticket_type(tid)
                        if tt:
                            ticket_types.append(tt)
                    if ticket_types:
                        display = "dropdown" if panel.get("display_style") == "dropdown" else "buttons"
                        if panel.get("posted_style") == "box" and box_supported():
                            # the posted message is a one-box panel: re-attach that kind of view
                            # keep the picture: the dropdown edits the message after a pick,
                            # and a view built without it would wipe the picture
                            self.bot.add_view(TicketPanelBoxView(
                                panel, ticket_types, await panel_image_ref(panel), display,
                                panel.get("placeholder"), str(panel["_id"]),
                            ))
                        else:
                            self.bot.add_view(TicketPanelView(
                                panel["name"], ticket_types, display,
                                panel.get("placeholder"), str(panel["_id"]),
                            ))
        except Exception as e:
            logger.error(f"Failed to re-register ticket panel views: {e}")

        # 2) Re-attach the Close button view used inside every ticket channel.
        # The custom_id is global ("close_ticket_button"), so one add_view call
        # re-enables the button on every ticket message across every guild.
        try:
            self.bot.add_view(TicketControlView())
            self.bot.add_view(TicketPriorityView())
        except Exception as e:
            logger.error(f"Failed to re-register ticket control view: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Events(bot))
