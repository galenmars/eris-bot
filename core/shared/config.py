"""
core/shared/config.py
=====================
E.R.I.S. Bot — Shared Configuration Constants

PURPOSE
-------
Central home for values that are referenced across multiple layers
(cogs, sequences, domain) but don't belong in any single layer.

WHAT LIVES HERE
---------------
- Default fallback values used when guild config is missing
- Timeout constants for wait_for calls
- Embed color palette (consistent across the whole bot)
- Channel and role name constants (so typos don't silently break things)

WHAT DOES NOT LIVE HERE
------------------------
- Guild-specific config (stored in DB, retrieved via data/guild_repo.py)
- Theme data (stored in themes/*.json, loaded by bot.get_theme())
- Secrets or tokens (live in .env, never in code)
"""

import os

# =============================================================================
# TIMEOUTS
# =============================================================================
# All wait_for() calls should use one of these constants so timeout behavior
# is consistent and easy to tune in one place.

TIMEOUT_SHORT     = 60    # Simple yes/no confirmations
TIMEOUT_STANDARD  = 300   # Normal player prompts (5 minutes)
TIMEOUT_LONG      = 1200  # Complex setups like battle engage (20 minutes)
TIMEOUT_DM        = 300   # DM conversations for tactical choices, etc.

# =============================================================================
# EMBED COLORS
# =============================================================================
# Consistent color coding across all bot embeds.
# Colors are raw int values (discord.py accepts these directly).

COLOR_SUCCESS  = 0x2ECC71   # Green  — action completed
COLOR_ERROR    = 0xE74C3C   # Red    — something went wrong
COLOR_INFO     = 0x3498DB   # Blue   — neutral information
COLOR_WARNING  = 0xE67E22   # Orange — caution, pending, or irreversible
COLOR_BATTLE   = 0xC0392B   # Dark red — active battle embeds
COLOR_CAMPAIGN = 0x8E44AD   # Purple — campaign embeds
COLOR_EMS      = 0x1ABC9C   # Teal   — EMS and resource embeds
COLOR_SETUP    = 0x95A5A6   # Grey   — setup and config embeds

# =============================================================================
# CHANNEL NAMES
# =============================================================================
# Defined here so nothing is hardcoded as a bare string elsewhere.
# If a channel gets renamed, change it here and it updates everywhere.

CHANNEL_SETUP           = 'eris-setup'
CHANNEL_NOTIFICATIONS   = 'eris-notifications'
CHANNEL_ADMIN_LOG       = 'eris-admin-log'
CHANNEL_CAMPAIGN_BOARD  = 'campaign-board'
CHANNEL_HOLO_BROADCAST  = 'holo-broadcast'
CHANNEL_SUBMISSIONS     = 'commander-submissions'
CHANNEL_APPROVALS       = 'commander-approvals'
CHANNEL_EMS_REQUESTS    = 'ems-requests'


# Role names match what setup_cog.py creates
ROLE_ERIS_ADMIN         = 'ERIS Admin'
ROLE_ERIS_CREATOR       = 'ERIS Creator'
ROLE_COMMANDER          = 'Commander'
ROLE_FLEET_COMMANDER    = 'Fleet Commander'
ROLE_ARMY_COMMANDER     = 'Army Commander'

# All channel names the bot expects to exist after setup
REQUIRED_CHANNELS = (
    CHANNEL_SETUP,
    CHANNEL_NOTIFICATIONS,
    CHANNEL_ADMIN_LOG,
    CHANNEL_CAMPAIGN_BOARD,
    CHANNEL_HOLO_BROADCAST,
    CHANNEL_SUBMISSIONS,
    CHANNEL_APPROVALS,
    CHANNEL_EMS_REQUESTS,
)

# All role names the bot expects to exist after setup
REQUIRED_ROLES = (
    ROLE_ERIS_ADMIN,
    ROLE_ERIS_CREATOR,
    ROLE_COMMANDER,
    ROLE_FLEET_COMMANDER,
    ROLE_ARMY_COMMANDER,
)

# =============================================================================
# BATTLE DEFAULTS
# =============================================================================

DEFAULT_DICE_STRING = '2d6'         # Used if no guild dice config is set
VALID_ROUND_COUNTS  = (3, 5, 7, 9)  # Only these values accepted at battle setup

VALID_BATTLE_SIZES  = (
    'brawl',
    'firefight',
    'skirmish',
    'engagement',
    'battleground',
)

# =============================================================================
# COMMANDER DEFAULTS
# =============================================================================

VALID_RANKS          = ('main', 'senior', 'junior')
VALID_FORCE_TYPES    = ('Fleet', 'Army')
VALID_TIERS          = (1, 2, 3)

LP_BY_RANK = {
    'main':   10,
    'senior':  7,
    'junior':  5,
}

# =============================================================================
# EMS DEFAULTS
# =============================================================================

VALID_POOLS = ('fleet', 'army', 'both') # TODO: remove 'both' when ems_flow.py refactor is done

# =============================================================================
# REACTION TRIGGERS
# =============================================================================

REACTION_ENROLL = '⚔️'   # The reaction players add to campaign board to enroll

# =============================================================================
# SUBMISSION BLOCK FORMAT
# =============================================================================
# The exact delimiter strings the parser looks for in commander blocks.
# If these ever change, update both this file and the fleet builder HTML.

BLOCK_HEADER = '=== ERIS COMMANDER BLOCK ==='
BLOCK_FOOTER = '=== END BLOCK ==='

# =============================================================================
# OWNER
# =============================================================================

OWNER_ID = int(os.getenv('OWNER_ID', 0))