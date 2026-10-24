"""Startup events + persistent-view re-registration."""
import discord
from discord.ext import commands

from database import models
from cogs.tickets import TicketPanelView, TicketControlView, TicketPriorityView


class Events(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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
                        self.bot.add_view(TicketPanelView(
                            panel["name"], ticket_types,
                            "dropdown" if panel.get("display_style") == "dropdown" else "buttons",
                            panel.get("placeholder"), str(panel["_id"]),
                        ))
        except Exception as e:
            print(f"Failed to re-register ticket panel views: {e}")

        # 2) Re-attach the Close button view used inside every ticket channel.
        # The custom_id is global ("close_ticket_button"), so one add_view call
        # re-enables the button on every ticket message across every guild.
        try:
            self.bot.add_view(TicketControlView())
            self.bot.add_view(TicketPriorityView())
        except Exception as e:
            print(f"Failed to re-register ticket control view: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Events(bot))