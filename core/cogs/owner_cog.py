"""
cogs/owner_cog.py
=================
E.R.I.S. Bot — Owner-only commands

PURPOSE
-------
Commands locked to the bot developer's Discord user ID.
Loaded at startup like any other cog but all commands
check OWNER_ID from .env before doing anything.

COMMANDS
--------
/eris broadcast <message>  — post to #eris-notifications on every guild
"""

import logging

import discord
from discord.ext import commands
from discord     import app_commands

from core.data   import guild_repo
from core.shared import config

log = logging.getLogger(__name__)


class OwnerCog(commands.Cog, name="Owner"):
    """Bot developer commands."""

    def __init__(self, bot):
        self.bot = bot
        self.db  = bot.db.conn

    def _is_owner(self, user: discord.User | discord.Member) -> bool:
        """True if user is the bot developer."""
        return user.id == config.OWNER_ID

    # =========================================================================
    # ERIS GROUP
    # =========================================================================

    eris_group = app_commands.Group(
        name="eris",
        description="Bot developer commands.",
    )

    # -------------------------------------------------------------------------
    # /eris broadcast
    # -------------------------------------------------------------------------

    @eris_group.command(
        name="broadcast",
        description="[Owner] Post a message to #eris-notifications on every server.",
    )
    @app_commands.describe(message="The message to broadcast")
    async def eris_broadcast(
        self,
        interaction: discord.Interaction,
        message: str,
    ):
        if not self._is_owner(interaction.user):
            await interaction.response.send_message(
                "❌ This command is restricted to the bot developer.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        success = 0
        failed  = 0

        for guild in self.bot.guilds:
            guild_config = guild_repo.get_guild_config(self.db, guild.id)
            if not guild_config:
                continue

            ch_id   = guild_config.get('channels', {}).get('eris-notifications')
            channel = guild.get_channel(ch_id) if ch_id else None

            if not channel:
                failed += 1
                continue

            try:
                await channel.send(
                    f"📡 **E.R.I.S. Broadcast**\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{message}"
                )
                success += 1
            except discord.HTTPException:
                log.warning("Failed to broadcast to guild %s", guild.id)
                failed += 1

        await interaction.followup.send(
            f"✅ Broadcast complete.\n"
            f"Delivered: **{success}** servers · Failed: **{failed}** servers",
            ephemeral=True,
        )


# =============================================================================
# SETUP
# =============================================================================

async def setup(bot):
    await bot.add_cog(OwnerCog(bot))