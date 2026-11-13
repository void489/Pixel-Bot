"""Keeps ticket-transcript images for only 7 days (saves MongoDB space).

Runs once on startup (wipes everything already older than 7 days) and then
every 6 hours. Transcript text is kept; only the images are removed."""
import discord
from discord.ext import commands, tasks

from database import models

IMAGE_RETENTION_DAYS = 7


class TranscriptCleanup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cleanup.start()

    def cog_unload(self):
        self._cleanup.cancel()

    @tasks.loop(hours=6)
    async def _cleanup(self):
        try:
            r = await models.prune_transcript_images(IMAGE_RETENTION_DAYS)
            print(f"[transcript_cleanup] removed {r['files']} image(s), "
                  f"{r['orphan_chunks']} orphan chunk(s)")
        except Exception as e:
            print(f"[transcript_cleanup] failed: {type(e).__name__}: {e}")

    @_cleanup.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(TranscriptCleanup(bot))
