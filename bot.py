"""
bot.py
======
E.R.I.S. Bot — Entry Point

PURPOSE
-------
Creates the bot instance, attaches the database, loads the theme,
and registers all cogs. This file is intentionally thin — all logic
lives in cogs, sequences, domain, and math layers.

STARTUP SEQUENCE
----------------
1. Load .env (token, db path, theme path)
2. Create bot instance with required intents
3. Open database connection
4. Load theme JSON
5. Register all cogs
6. Sync slash commands on first ready
7. Start status rotation task

ENVIRONMENT VARIABLES (.env)
-----------------------------
DISCORD_TOKEN     — bot token (required)
DB_PATH           — path to commanders.db (default: commanders.db)
THEME_PATH        — path to theme JSON (default: themes/star_wars_old_republic.json)
GUILD_ID          — if set, syncs slash commands to this guild only (faster for dev)
"""

import json
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler('logs/eris.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger('eris')

# =============================================================================
# CONSTANTS
# =============================================================================

COGS = [
    'core.cogs.setup_cog',
    'core.cogs.owner_cog',
    'core.cogs.commander_cog',
    'core.cogs.campaign_cog',
    'core.cogs.battle_cog',
    'core.cogs.ems_cog',
    'core.cogs.admin_cog',
    'core.cogs.status_cog',
]


# =============================================================================
# BOT CLASS
# =============================================================================

class ErisBot(commands.Bot):
    """
    E.R.I.S. Bot — Engagement Resource & Intelligence System.

    Extends commands.Bot with:
    - self.db         — SQLite connection shared across all cogs
    - self.theme      — loaded theme dict (theme-agnostic; keyed by guild_id in future)
    - get_theme()     — returns the theme for a guild (currently one theme)
    """

    def __init__(self):
        intents                  = discord.Intents.default()
        intents.message_content  = True   # Required for wait_for message content
        intents.members          = True   # Required for member role management
        intents.reactions        = True   # Required for ⚔️ enrollment reaction

        super().__init__(
            command_prefix='!',   # Legacy prefix (slash commands are primary)
            intents=intents,
            description='E.R.I.S. — Engagement Resource & Intelligence System',
        )

        self.db           = None   # Set in setup_hook
        self._theme       = None   # Set in setup_hook

    async def setup_hook(self):
        """Called by discord.py before the bot connects. Sets up DB and loads cogs."""

        # --- Database ---
        db_path  = os.getenv('DB_PATH', 'commanders.db')
        from core.data.db import DatabaseManager
        self.db = DatabaseManager(db_path)

        # --- Theme ---
        theme_path = os.getenv('THEME_PATH', 'themes/star_wars_old_republic.json')
        self._theme = self._load_theme(theme_path)

        # --- Cogs ---
        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info(f"Loaded cog: {cog}")
            except Exception as e:
                log.error(f"Failed to load cog {cog}: {e}", exc_info=True)

        # --- Slash command sync ---
        guild_id = os.getenv('GUILD_ID')
        if guild_id:
            # Sync to a specific guild — immediate, good for development
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info(f"Slash commands synced to dev guild {guild_id}")
        else:
            # Global sync — takes up to 1 hour to propagate
            await self.tree.sync()
            log.info("Skipping global sync — commands sync per-guild after /setup")

    def _load_theme(self, path: str) -> dict:
        """Load a theme JSON file. Returns an empty dict on failure."""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                theme = json.load(f)
            log.info(f"Theme loaded: {theme.get('theme_name', path)}")
            return theme
        except FileNotFoundError:
            log.error(f"Theme file not found: {path}")
            return {}
        except json.JSONDecodeError as e:
            log.error(f"Theme JSON parse error in {path}: {e}")
            return {}

    def get_theme(self, guild_id: int | None = None) -> dict:
        """
        Return the theme dict for a guild.

        Currently all guilds share one theme — the one loaded at startup.
        In future, this can be extended to return guild-specific themes
        by looking up the theme path from guild_config.

        Args:
            guild_id: Discord guild ID (reserved for future per-guild themes).

        Returns:
            dict: The loaded theme, or an empty dict if loading failed.
        """
        return self._theme or {}

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")
        log.info(f"Serving {len(self.guilds)} guild(s)")
        log.info("E.R.I.S. is operational.")

    async def on_command_error(self, ctx, error):
        """Global error handler for prefix commands (legacy)."""
        if isinstance(error, commands.CommandNotFound):
            return   # Ignore unknown prefix commands silently
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You don't have permission to use that command.")
            return
        log.error(f"Command error in {ctx.command}: {error}", exc_info=True)

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ):
        """Global error handler for slash commands."""
        log.error(
            f"Slash command error ({interaction.command}): {error}",
            exc_info=True,
        )
        msg = "❌ An unexpected error occurred. Please try again or contact an admin."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass   # If we can't respond, log it and move on

    async def close(self):
        """Clean shutdown — close the database connection."""
        if self.db:
            self.db.close()
            log.info("Database connection closed.")
        await super().close()


# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        log.critical("DISCORD_TOKEN is not set in .env — cannot start.")
        raise SystemExit(1)

    bot = ErisBot()

    try:
        bot.run(token, log_handler=None)   # log_handler=None so our handler isn't overridden
    except KeyboardInterrupt:
        log.info("Shutdown requested.")
    finally:
        if bot.db:
            bot.db.close()


if __name__ == '__main__':
    main()
