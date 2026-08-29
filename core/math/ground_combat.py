"""
core/math/ground_combat.py
==========================
E.R.I.S. Bot — Ground Battle Mathematics

PURPOSE
-------
This file handles everything specific to GROUND battles:
  - Reading a commander's army composition
  - Calculating army type ratios from EMS weights
  - Determining which battle options are available
  - Validating an army against tier and EMS rules
  - Orchestrating one complete ground battle round

Mirrors space_combat.py exactly in structure. The differences are:
  - Reads 'ground_forces' instead of 'space_ships' from theme
  - Reads 'ground_matchups' instead of 'space_matchups'
  - Army types: infantry / mechanized / heavy_armor / combined_arms
  - Force users are tier-gated attachments with special rules
  - EMS deducted from ground_ems pool, never space_ems pool

WHAT LIVES HERE
---------------
1. calculate_army_ratios()     — sums EMS by type, returns percentages
2. get_available_army_types()  — determines which 1-2 types player can choose
3. validate_army()             — checks tier gates, EMS, force user rules
4. calculate_ground_round()    — runs one full round using dice.py

IMPORT RULE
-----------
This file may import from:
  - core/math/dice.py       ← same layer, math only

This file must NEVER import from:
  - core/data/             ← would break the layer rule
  - core/domain/           ← would break the layer rule
  - core/sequences/        ← would break the layer rule
  - core/cogs/             ← would break the layer rule
"""

from core.math.dice import (
    calculate_round_total,
    get_ground_matchup_bonus,
)


# =============================================================================
# RATIO THRESHOLDS
# =============================================================================
#
# Identical to space_combat.py — same rules, different domain.
# Defined here separately so ground_combat.py is fully self-contained.
# If thresholds ever diverge between space and ground, change only here.

DOMINANT_THRESHOLD  = 40   # top type needs at least 40% of total army EMS
SECONDARY_THRESHOLD = 25   # second type needs at least 25% to qualify
MAX_GAP             = 30   # second type must be within 30 points of top
MAX_OPTIONS         = 2    # never more than 2 options, no exceptions


# =============================================================================
# ARMY RATIO CALCULATION
# =============================================================================

def calculate_army_ratios(army_composition: list, theme: dict) -> dict:
    """
    Calculate what percentage of an army's total EMS belongs to each type.

    FORCE USER SPECIAL CASES
    ------------------------
    Force users (Apprentice, Lord/Knight, Darth) have type "choice" —
    the commander declares their type at battle time. Until that choice
    is made, we handle them differently by type_options:

    Apprentice/Padawan → choice between infantry / combined_arms
                         Split evenly: 50% infantry, 50% combined_arms

    Lord/Knight        → choice between infantry / combined_arms
                         Split evenly: 50% infantry, 50% combined_arms

    Darth              → choice between ANY type
                         Split evenly across all 4 types (25% each)
                         Because we genuinely don't know what they'll pick

    This matches the HTML builder behavior — the ratios show the
    uncertainty until the commander locks their choice at battle time.

    GROUND EMS IS SEPARATE FROM SPACE EMS
    ---------------------------------------
    A commander's 300 EMS ground pool is completely separate from their
    300 EMS space pool. This function only counts ground force EMS.
    The sequences layer ensures the right pool is checked.

    Args:
        army_composition (list): List of dicts, each containing:
                                   - 'unit_id'  (str): matches id in theme JSON
                                   - 'quantity' (int): how many of this unit
        theme            (dict): The loaded theme dictionary.

    Returns:
        dict: {
            'infantry':      float,  percentage of total EMS (0-100)
            'mechanized':    float,  percentage of total EMS (0-100)
            'heavy_armor':   float,  percentage of total EMS (0-100)
            'combined_arms': float,  percentage of total EMS (0-100)
            'total_ems':     int,    total EMS committed to army
            'by_type':       dict,   raw EMS per type before percentage
            'has_force_user':bool,   True if any force user is in the army
            'force_users':   list,   list of force user unit_ids in the army
        }
    """
    # Build a lookup dict from unit_id → unit data
    unit_lookup = {unit['id']: unit for unit in theme.get('ground_forces', [])}

    by_type      = {'infantry': 0, 'mechanized': 0, 'heavy_armor': 0, 'combined_arms': 0}
    total_ems    = 0
    force_users  = []

    for entry in army_composition:
        unit_id  = entry.get('unit_id')
        quantity = entry.get('quantity', 0)

        if not unit_id or quantity <= 0:
            continue

        unit = unit_lookup.get(unit_id)
        if not unit:
            print(f"[ground_combat.py] WARNING: Unit '{unit_id}' not found in theme")
            continue

        unit_ems    = unit.get('ems', 0)
        unit_type   = unit.get('type', 'infantry')
        ems_chunk   = unit_ems * quantity
        total_ems  += ems_chunk
        is_force_user = unit.get('force_user', False)

        if is_force_user:
            force_users.append(unit_id)

        # Handle choice units — split EMS across their possible types
        if unit_type == 'choice':
            type_options = unit.get('type_options', [])

            if not type_options:
                # No options defined — default to infantry
                by_type['infantry'] += ems_chunk

            else:
                # Split evenly across all declared options
                share = ems_chunk / len(type_options)
                for option in type_options:
                    if option in by_type:
                        by_type[option] += share

        else:
            if unit_type in by_type:
                by_type[unit_type] += ems_chunk

    # Convert raw EMS to percentages
    if total_ems == 0:
        return {
            'infantry':       0,
            'mechanized':     0,
            'heavy_armor':    0,
            'combined_arms':  0,
            'total_ems':      0,
            'by_type':        by_type,
            'has_force_user': False,
            'force_users':    [],
        }

    percentages = {
        army_type: round((ems / total_ems) * 100, 1)
        for army_type, ems in by_type.items()
    }

    return {
        **percentages,
        'total_ems':      total_ems,
        'by_type':        by_type,
        'has_force_user': len(force_users) > 0,
        'force_users':    force_users,
    }


# =============================================================================
# AVAILABLE ARMY TYPES
# =============================================================================

def get_available_army_types(ratios: dict) -> list:
    """
    Determine which 1-2 army types a commander can choose at battle time.

    Identical logic to get_available_fleet_types() in space_combat.py.
    Same thresholds, same rules, same maximum of 2 options.

    THE RULES
    ----------
    1. Sort all four types by EMS percentage, highest first.
    2. Top type qualifies if ≥ 40% of total army EMS.
    3. Second type qualifies if ≥ 25% AND within 30 points of top.
    4. Combined Arms is NOT a free fallback — must be earned.
    5. Never more than 2 options.
    6. Ties → both options available, player chooses at battle time.

    Args:
        ratios (dict): Output from calculate_army_ratios().

    Returns:
        list: 0, 1, or 2 army type strings the player may choose from.
              e.g. ['infantry', 'combined_arms'] or ['heavy_armor'] or []
    """
    if ratios.get('total_ems', 0) == 0:
        return []

    types        = ['infantry', 'mechanized', 'heavy_armor', 'combined_arms']
    sorted_types = sorted(types, key=lambda t: ratios.get(t, 0), reverse=True)
    available    = []

    top_type = sorted_types[0]
    top_pct  = ratios.get(top_type, 0)

    # Top type threshold check
    if top_pct >= DOMINANT_THRESHOLD:
        available.append(top_type)

    # Second type check
    if len(sorted_types) > 1:
        second_type = sorted_types[1]
        second_pct  = ratios.get(second_type, 0)
        gap         = top_pct - second_pct

        # Exact tie — both qualify
        if gap == 0 and second_pct >= DOMINANT_THRESHOLD:
            if second_type not in available:
                available.append(second_type)

        # Normal secondary qualification
        elif second_pct >= SECONDARY_THRESHOLD and gap <= MAX_GAP:
            if second_type not in available:
                available.append(second_type)

    return available[:MAX_OPTIONS]


# =============================================================================
# ARMY VALIDATION
# =============================================================================

def validate_army(
    army_composition: list,
    commander_tier:   int,
    ems_pool:         int,
    theme:            dict,
) -> dict:
    """
    Validate an army composition against tier gates, EMS budget,
    and force user rules.

    THREE GATES FOR GROUND (vs two for space)
    -----------------------------------------
    Gate 1: EMS budget      — total army EMS must not exceed pool
    Gate 2: Tier            — every unit must be allowed at this tier
    Gate 3: Force user rules:
              - Cannot field a force user equal to or above your own rank tier
              - Maximum ONE Darth per army
              - Force user tier is checked against commander tier

    FORCE USER TIER RULES
    ----------------------
    Tier 1 commanders → can bring Apprentice/Padawan only
    Tier 2 commanders → can bring Apprentice or Lord/Knight
    Tier 3 commanders → can bring Apprentice, Lord/Knight, or Darth
                        (but never more than one Darth)

    Args:
        army_composition (list): List of {'unit_id': str, 'quantity': int}
        commander_tier   (int):  The commander's tier (1, 2, or 3)
        ems_pool         (int):  Total ground EMS available
        theme            (dict): The loaded theme dictionary

    Returns:
        dict: {
            'valid':         bool,
            'errors':        list[str],
            'warnings':      list[str],
            'total_ems':     int,
            'ems_remaining': int,
            'force_users':   list[str],  unit_ids of force users in army
        }
    """
    unit_lookup  = {unit['id']: unit for unit in theme.get('ground_forces', [])}
    errors       = []
    warnings     = []
    total_ems    = 0
    force_users  = []
    darth_count  = 0

    # Get force user tier access rules from theme
    force_user_rules = theme.get('force_user_rules', {})
    tier_access      = force_user_rules.get('tier_access', {})
    allowed_force_users = tier_access.get(str(commander_tier), [])

    for entry in army_composition:
        unit_id  = entry.get('unit_id')
        quantity = entry.get('quantity', 0)

        # Unknown unit
        if unit_id not in unit_lookup:
            errors.append(f"Unknown unit: '{unit_id}' — not found in theme '{theme.get('theme_name')}'")
            continue

        unit      = unit_lookup[unit_id]
        unit_tier = unit.get('tier', 1)
        is_force_user = unit.get('force_user', False)

        # Tier gate check
        if unit_tier > commander_tier:
            errors.append(
                f"{unit['name']} requires Tier {unit_tier} — "
                f"commander is Tier {commander_tier}"
            )

        # Force user specific checks
        if is_force_user:
            force_users.append(unit_id)

            # Check if this force user is allowed at commander's tier
            if unit_id not in allowed_force_users:
                errors.append(
                    f"{unit['name']} is not available at Tier {commander_tier} — "
                    f"force user tier too high"
                )

            # Darth limit — maximum one per army
            if unit_id == 'darth':
                darth_count += quantity
                if darth_count > 1:
                    errors.append(
                        f"Maximum one Darth per army — "
                        f"{darth_count} Darths detected"
                    )

            # Force users are single units — quantity > 1 makes no sense
            if quantity > 1 and is_force_user:
                errors.append(
                    f"{unit['name']} is a Force user — "
                    f"only one individual can be fielded (quantity: {quantity})"
                )

        # EMS accumulation
        total_ems += unit.get('ems', 0) * quantity

    # EMS budget check
    ems_remaining = ems_pool - total_ems
    if ems_remaining < 0:
        errors.append(
            f"Army exceeds EMS budget by {abs(ems_remaining)} EMS "
            f"({total_ems} committed, {ems_pool} available)"
        )

    # Empty army warning
    if total_ems == 0:
        warnings.append("Army is empty — no units committed")

    # Force user without army warning
    if force_users and len(army_composition) == len(force_users):
        warnings.append(
            "Army consists only of Force users — "
            "consider adding troops for area control"
        )

    return {
        'valid':         len(errors) == 0,
        'errors':        errors,
        'warnings':      warnings,
        'total_ems':     total_ems,
        'ems_remaining': ems_remaining,
        'force_users':   force_users,
    }


# =============================================================================
# GROUND BATTLE ROUND
# =============================================================================

def calculate_ground_round(
    p1_army_type: str,
    p2_army_type: str,
    p1_tactic:    str,
    p2_tactic:    str,
    dice_str:     str,
    theme:        dict,
) -> dict:
    """
    Calculate the complete result of one round of ground battle.

    Identical structure to calculate_space_round() in space_combat.py.
    The only differences are:
      - Uses get_ground_matchup_bonus() instead of get_space_matchup_bonus()
      - Reads flavor from 'ground_matchups' instead of 'space_matchups'
      - Army types instead of fleet types

    FORCE USER TYPE CHOICE
    -----------------------
    By the time this function is called, the force user's type choice
    must already be resolved. The sequences layer handles the DM asking
    "What type does your Darth fight as today?" and passes the resolved
    type string here. This function only sees a clean type string —
    it has no knowledge that a Darth was involved.

    Args:
        p1_army_type (str): Player 1's declared army type, e.g. "infantry"
        p2_army_type (str): Player 2's declared army type, e.g. "heavy_armor"
        p1_tactic    (str): Player 1's tactic choice, e.g. "aggressive"
        p2_tactic    (str): Player 2's tactic choice, e.g. "defensive"
        dice_str     (str): Dice notation for this battle, e.g. "2d6"
        theme        (dict): The loaded theme dictionary

    Returns:
        dict: {
            'p1': {
                'army_type':     str,
                'tactic':        str,
                'matchup_bonus': int,
                'round_result':  dict,
                'total':         int,
            },
            'p2': {
                'army_type':     str,
                'tactic':        str,
                'matchup_bonus': int,
                'round_result':  dict,
                'total':         int,
            },
            'winner': str,   "p1", "p2", or "tie"
            'flavor': str,   matchup flavor text from theme JSON
        }
    """
    # Step 1: Look up matchup bonuses from theme ground_matchups
    p1_matchup = get_ground_matchup_bonus(p1_army_type, p2_army_type, theme)
    p2_matchup = get_ground_matchup_bonus(p2_army_type, p1_army_type, theme)

    # Step 2: Calculate each player's round total
    # Each player's tactic bonus depends on BOTH tactics — the matrix lookup
    # needs to know what the opponent picked, not just your own choice.
    p1_result = calculate_round_total(dice_str, p1_tactic, p2_tactic, p1_matchup, theme)
    p2_result = calculate_round_total(dice_str, p2_tactic, p1_tactic, p2_matchup, theme)

    p1_total = p1_result['total']
    p2_total = p2_result['total']

    # Step 3: Determine round winner
    if p1_total > p2_total:
        winner = "p1"
    elif p2_total > p1_total:
        winner = "p2"
    else:
        winner = "tie"

    # Step 4: Pull flavor text from theme ground_matchups
    flavor = ""
    for matchup in theme.get('ground_matchups', []):
        if (matchup.get('attacker') == p1_army_type and
                matchup.get('defender') == p2_army_type):
            flavor = matchup.get('flavor', "")
            break

    return {
        'p1': {
            'army_type':     p1_army_type,
            'tactic':        p1_tactic,
            'matchup_bonus': p1_matchup,
            'round_result':  p1_result,
            'total':         p1_total,
        },
        'p2': {
            'army_type':     p2_army_type,
            'tactic':        p2_tactic,
            'matchup_bonus': p2_matchup,
            'round_result':  p2_result,
            'total':         p2_total,
        },
        'winner': winner,
        'flavor': flavor,
    }
