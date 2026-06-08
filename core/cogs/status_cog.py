"""
core/cogs/status_cog.py
=======================
E.R.I.S. Bot — Rotating Presence

PURPOSE
-------
Background task only. No commands.
Rotates the bot's Discord presence every 20 minutes.
Status strings are read from the theme JSON if present,
otherwise falls back to the defaults defined here.

TO ADD CUSTOM STATUSES TO THE THEME
------------------------------------
Add a 'statuses' list to your theme JSON:

    {
        "theme_name": "Star Wars: The Old Republic",
        "statuses": [
            "Monitoring the battlefield",
            "Tracking fleet movements",
            "Processing campaign data"
        ]
    }

The cog will use those automatically on next restart.
"""

import asyncio
import itertools
import logging

import discord
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

# Fallback statuses used when the theme has none defined
DEFAULT_STATUSES = [
    "Monitoring the battlefield",
    "Tracking fleet movements",
    "Processing campaign data",
    "Coordinating strike teams",
    "Analyzing enemy formations",
    "Calculating hyperspace routes",
    "Scanning for hostiles",
    "Updating battle records",
    "Awaiting your command",
]

ROTATE_MINUTES = 20


class StatusCog(commands.Cog, name="Status"):
    """Rotating bot presence. No commands."""

    def __init__(self, bot):
        self.bot      = bot
        self._cycle   = None
        self.rotate.start()

    def cog_unload(self):
        self.rotate.cancel()

    def _get_statuses(self) -> list[str]:
        """Return status strings from theme, or fall back to defaults."""
        theme    = self.bot.get_theme()
        statuses = theme.get('statuses', [])
        return statuses if statuses else DEFAULT_STATUSES

    @tasks.loop(minutes=ROTATE_MINUTES)
    async def rotate(self):
        """Advance to the next status string."""
        if self._cycle is None:
            self._cycle = itertools.cycle(self._get_statuses())

        status = next(self._cycle)
        try:
            await self.bot.change_presence(
                activity=discord.Game(name=status),
                status=discord.Status.online,
            )
        except Exception as e:
            log.warning(f"Could not update presence: {e}")

    @rotate.before_loop
    async def before_rotate(self):
        """Wait until the bot is fully connected before touching presence."""
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(StatusCog(bot))
