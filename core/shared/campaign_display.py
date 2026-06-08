"""
core/shared/campaign_display.py
================================
E.R.I.S. Bot — Campaign Progress Display Formatter

Generates the text-based progress display posted to #campaign-board
after every battle. All visual formatting lives here — change this
file to update the look without touching any logic.

CAMPAIGN TYPES
--------------
Tug-of-War  — symmetric, both factions push from opposite ends
Invasion    — guild pushes from 0% toward 80% victory threshold
Defense     — enemy pushes from right, guild holds above 20%

SPECS (locked in with guild vote)
----------------------------------
Bars:        24 characters, clean edges
Border lines: 30 characters (━)
Tug rope:    27 characters (24 dashes + ❮●❯)
Filled char: ▓
Empty char:  ░
"""

from __future__ import annotations


# =============================================================================
# CONSTANTS
# =============================================================================

BAR_WIDTH     = 24
BORDER_WIDTH  = 30
ROPE_WIDTH    = 29   # 24 dashes + 3 for ❮●❯
FILLED        = '▓'
EMPTY         = '░'
BORDER        = '━' * BORDER_WIDTH
VICTORY_PCT   = 0.80   # 80% of enemy EMS = win condition

# Spaces to right-align '◄ advancing' under the right edge of the bar
# Calibrated for desktop Discord monospace rendering
ADVANCING_INDENT = ' ' * 50


# =============================================================================
# FACTION EMOJI LOOKUP
# =============================================================================

def get_faction_emoji(faction_name: str, theme: dict) -> str:
    """Look up emoji for a faction by name. Falls back to ⚪ if not found.
    Tries exact match first, then partial match for lazy typers."""
    groups = theme.get('alignment_groups', {})
    name_lower = faction_name.lower()
    # Exact match on name, id, or any alias
    for f in theme.get('factions', []):
        aliases = [a.lower() for a in f.get('aliases', [])]
        if (f['name'].lower() == name_lower
                or f['id'].lower() == name_lower
                or name_lower in aliases):
            return groups.get(f['group'], '⚪')
    # Partial match fallback on name and id
    for f in theme.get('factions', []):
        if (name_lower in f['name'].lower() or f['name'].lower() in name_lower
                or name_lower in f['id'].lower() or f['id'].lower() in name_lower):
            return groups.get(f['group'], '⚪')
    return '⚪'


# =============================================================================
# BAR BUILDERS
# =============================================================================

def _bar_ltr(pct: float) -> str:
    """Left-to-right bar. Fills from left. pct is 0.0–1.0."""
    pct     = max(0.0, min(1.0, pct))
    filled  = round(pct * BAR_WIDTH)
    empty   = BAR_WIDTH - filled
    return FILLED * filled + EMPTY * empty


def _bar_rtl(pct: float) -> str:
    """Right-to-left bar. Fills from right. pct is 0.0–1.0."""
    pct     = max(0.0, min(1.0, pct))
    filled  = round(pct * BAR_WIDTH)
    empty   = BAR_WIDTH - filled
    return EMPTY * empty + FILLED * filled


def _rope(guild_pct: float) -> str:
    """
    Tug-of-war rope. Dot moves right as guild_pct increases.
    Total width = ROPE_WIDTH (27).
    """
    guild_pct  = max(0.0, min(1.0, guild_pct))
    inner      = ROPE_WIDTH - 2   # subtract ◄ and ►
    dot_pos    = round(guild_pct * (inner - 3))  # -3 for ❮●❯
    dot_pos    = max(0, min(inner - 3, dot_pos))

    left_dashes  = '─' * dot_pos
    right_dashes = '─' * (inner - 3 - dot_pos)
    return f"◄{left_dashes}❮●❯{right_dashes}►"


# =============================================================================
# FORMAT FUNCTIONS
# =============================================================================

def format_tug_of_war(
    campaign_name:  str,
    guild_name:     str,
    opposing_name:     str,
    guild_ems:      int,
    opposing_ems_dealt: int,
    total_ems:      int,
    battles_fought: int,
    threshold:      int,
) -> str:
    """
    Format the Tug-of-War progress display.

    Args:
        campaign_name:   Name of the campaign.
        guild_name:      Guild faction name.
        opposing_name:      Enemy faction name.
        guild_ems:       EMS dealt by guild so far.
        opposing_ems_dealt: EMS dealt by enemy so far.
        total_ems:       Total EMS pool (guild + enemy starting EMS).
        battles_fought:  Number of battles completed.
        threshold:       EMS required to win (80% of enemy starting EMS).
    """
    guild_pct  = guild_ems / total_ems if total_ems else 0.0
    enemy_pct  = opposing_ems_dealt / total_ems if total_ems else 0.0

    guild_pct_display = round(guild_pct * 100)
    enemy_pct_display = round(enemy_pct * 100)

    return (
        f"## ⚔️ {campaign_name.upper()}\n"
        f"**Tug of War**\n"
        f"{BORDER}\n\n"
        f"🔵 {guild_name}\n"
        f"**{guild_pct_display}%** · {guild_ems} EMS\n"
        f"{_bar_ltr(guild_pct)}\n\n"
        f"{_rope(guild_pct)}\n\n"
        f"{_bar_rtl(enemy_pct)}\n"
        f"🔴 {opposing_name}\n"
        f"**{enemy_pct_display}%** · {opposing_ems_dealt} EMS\n\n"
        f"{BORDER}\n"
        f"Battles: **{battles_fought}** · Total EMS in play: **{total_ems}**"
    )


def format_invasion(
    campaign_name:  str,
    guild_name:     str,
    opposing_name:     str,
    guild_ems:      int,
    opposing_total_ems: int,
    battles_fought: int,
    threshold:      int,
) -> str:
    """
    Format the Attack progress display.

    Args:
        campaign_name:    Name of the campaign.
        guild_name:       Guild faction name.
        opposing_name:       Enemy faction name (defender).
        guild_ems:        Total EMS dealt by guild so far.
        opposing_total_ems:  Enemy's starting EMS pool.
        battles_fought:   Number of battles completed.
        threshold:        EMS required to win (80% of enemy EMS).
    """
    guild_pct         = guild_ems / opposing_total_ems if opposing_total_ems else 0.0
    enemy_holding_pct = round((1.0 - guild_pct) * 100)
    guild_pct_display = round(guild_pct * 100)

    return (
        f"## ⚔️ {campaign_name.upper()}\n"
        f"**Attack**\n"
        f"{BORDER}\n\n"
        f"🔵 {guild_name}\n"
        f"**{guild_pct_display}%** · {guild_ems} EMS dealt\n"
        f"{_bar_ltr(guild_pct)}\n"
        f"► pushing\n\n"
        f"🔴 {opposing_name}\n"
        f"Holding at **{enemy_holding_pct}%**\n\n"
        f"{BORDER}\n"
        f"Battles: **{battles_fought}** · Victory at: **{threshold} EMS**"
    )


def format_defense(
    campaign_name:   str,
    guild_name:      str,
    opposing_name:      str,
    opposing_ems_dealt: int,
    guild_total_ems: int,
    battles_fought:  int,
    threshold:       int,
) -> str:
    """
    Format the Defense progress display.

    Args:
        campaign_name:   Name of the campaign.
        guild_name:      Guild faction name (defender).
        opposing_name:      Enemy faction name (attacker).
        opposing_ems_dealt: Total EMS dealt by enemy so far.
        guild_total_ems: Guild's starting EMS pool.
        battles_fought:  Number of battles completed.
        threshold:       EMS enemy needs to win (80% of guild EMS).
    """
    enemy_pct         = opposing_ems_dealt / guild_total_ems if guild_total_ems else 0.0
    guild_holding_pct = round((1.0 - enemy_pct) * 100)
    enemy_pct_display = round(enemy_pct * 100)

    return (
        f"## 🛡️ {campaign_name.upper()}\n"
        f"**Defense**\n"
        f"{BORDER}\n\n"
        f"🔴 {opposing_name}\n"
        f"**{enemy_pct_display}%** · {opposing_ems_dealt} EMS dealt\n"
        f"{_bar_rtl(enemy_pct)}\n"
        f"{ADVANCING_INDENT}◄ advancing\n\n"
        f"🔵 {guild_name}\n"
        f"Holding at **{guild_holding_pct}%**\n\n"
        f"{BORDER}\n"
        f"Battles: **{battles_fought}** · Defeat at: **{threshold} EMS**"
    )


# =============================================================================
# UNIFIED ENTRY POINT
# =============================================================================

def format_progress(campaign: dict, stats: dict) -> str:
    """
    Main entry point. Routes to the correct formatter based on campaign type.

    Args:
        campaign: Campaign record from DB. Must have:
                  campaign_type, campaign_name, enemy_ems,
                  guild_faction, enemy_faction.
        stats:    Computed battle stats. Must have:
                  guild_ems_dealt, opposing_ems_dealt, battles_fought.

    Returns:
        Formatted string ready to post to Discord.
    """
    campaign_type   = campaign.get('campaign_type', '').lower()
    campaign_name   = campaign.get('campaign_name', 'Unknown Campaign')
    guild_name      = campaign.get('guild_faction', 'Guild')
    opposing_name      = campaign.get('enemy_faction', 'Enemy')
    enemy_ems       = campaign.get('enemy_ems', 1000)
    threshold       = round(enemy_ems * VICTORY_PCT)

    guild_ems_dealt  = stats.get('guild_ems_dealt', 0)
    opposing_ems_dealt  = stats.get('opposing_ems_dealt', 0)
    battles_fought   = stats.get('battles_fought', 0)

    if campaign_type == 'tug-of-war':
        total_ems = enemy_ems * 2   # equal starting pool
        return format_tug_of_war(
            campaign_name   = campaign_name,
            guild_name      = guild_name,
            opposing_name      = opposing_name,
            guild_ems       = guild_ems_dealt,
            opposing_ems_dealt = opposing_ems_dealt,
            total_ems       = total_ems,
            battles_fought  = battles_fought,
            threshold       = threshold,
        )

    elif campaign_type == 'invasion':
        return format_invasion(
            campaign_name    = campaign_name,
            guild_name       = guild_name,
            opposing_name       = opposing_name,
            guild_ems        = guild_ems_dealt,
            opposing_total_ems  = enemy_ems,
            battles_fought   = battles_fought,
            threshold        = threshold,
        )

    elif campaign_type == 'defense':
        guild_ems = campaign.get('guild_ems', enemy_ems)   # guild fields same as enemy
        return format_defense(
            campaign_name    = campaign_name,
            guild_name       = guild_name,
            opposing_name       = opposing_name,
            opposing_ems_dealt  = opposing_ems_dealt,
            guild_total_ems  = guild_ems,
            battles_fought   = battles_fought,
            threshold        = round(guild_ems * VICTORY_PCT),
        )

    else:
        return f"## ⚔️ {campaign_name.upper()}\nProgress tracking not available for this campaign type."
