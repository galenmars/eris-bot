"""
core/math/space_combat.py
=========================
E.R.I.S. Bot — Space Battle Mathematics

PURPOSE
-------
This file handles everything specific to SPACE battles:
  - Reading a commander's fleet composition
  - Calculating fleet type ratios from EMS weights
  - Determining which battle options are available
  - Validating a fleet against tier and EMS rules
  - Orchestrating one complete space battle round

Like all files in core/math/, every function here is pure:
  - Takes plain data (dicts, lists, strings, numbers)
  - Returns plain data
  - Never touches Discord
  - Never touches a database
  - Never does anything async

WHAT LIVES HERE
---------------
1. calculate_fleet_ratios()    — sums EMS by type, returns percentages
2. get_available_fleet_types() — determines which 1-2 types player can choose
3. validate_fleet()            — checks tier gates and EMS budget
4. calculate_space_round()     — runs one full round using dice.py

WHAT DOES NOT LIVE HERE
------------------------
- Loading the theme JSON          → data/ layer
- Storing fleet composition in DB → data/ layer  
- Asking player to choose type    → sequences/ layer
- Announcing results in Discord   → cogs/ layer
- Ground battle logic             → ground_combat.py

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
    get_space_matchup_bonus,
)


# =============================================================================
# RATIO THRESHOLDS
# =============================================================================
#
# These constants define when a fleet type qualifies as an available option.
# They mirror the thresholds used in the HTML fleet builder tool exactly —
# what the player sees in the builder is what the bot will offer in battle.
#
# DOMINANT_THRESHOLD  → a type must represent at least this % to be top type
# SECONDARY_THRESHOLD → a type must represent at least this % to be second option
# MAX_GAP             → second type must be within this many points of the top
# MAX_OPTIONS         → never offer more than this many options, ever

DOMINANT_THRESHOLD  = 40   # top type needs at least 40% of total fleet EMS
SECONDARY_THRESHOLD = 25   # second type needs at least 25% to qualify
MAX_GAP             = 30   # second type must be within 30 points of top type
MAX_OPTIONS         = 2    # never more than 2 options, no exceptions


# =============================================================================
# FLEET RATIO CALCULATION
# =============================================================================

def calculate_fleet_ratios(fleet_composition: list, theme: dict) -> dict:
    """
    Calculate what percentage of a fleet's total EMS belongs to each type.

    HOW IT WORKS
    ------------
    Fleet composition is a list of ships with quantities. We look up each
    ship's type from the theme JSON, multiply EMS × quantity, and sum by type.
    Then we convert each type's total into a percentage of the grand total.

    WHY EMS WEIGHT NOT SHIP COUNT
    ------------------------------
    A commander with one Dreadnought (200 EMS, Combat) and ten Corvettes
    (200 EMS total, Picket) has an even 50/50 split by EMS — not 1/10 by
    count. The big ship's cost IS its weight. This is the design decision
    that makes fleet building meaningful: you cannot hide a Dreadnought
    behind a swarm of Corvettes and pretend you're a Picket fleet.

    SUPER DREADNOUGHT SPECIAL CASE
    --------------------------------
    The Super Dreadnought has type "choice" — the commander declares
    Combat or Carrier at battle time. Until that choice is made, we
    split its EMS evenly between Combat and Carrier for ratio display
    purposes. This matches the HTML builder behavior.

    Args:
        fleet_composition (list): List of dicts, each containing:
                                    - 'ship_id'  (str): matches id in theme JSON
                                    - 'quantity' (int): how many of this ship
        theme             (dict): The loaded theme dictionary.

    Returns:
        dict: {
            'combat':    float,  percentage of total EMS (0-100)
            'carrier':   float,  percentage of total EMS (0-100)
            'picket':    float,  percentage of total EMS (0-100)
            'balanced':  float,  percentage of total EMS (0-100)
            'total_ems': int,    total EMS committed to fleet
            'by_type':   dict,   raw EMS per type before percentage conversion
        }
    """
    # Build a lookup dict from ship_id → ship data for fast access
    ship_lookup = {ship['id']: ship for ship in theme.get('space_ships', [])}

    # Track raw EMS totals per type
    by_type    = {'combat': 0, 'carrier': 0, 'picket': 0, 'balanced': 0}
    total_ems  = 0

    for entry in fleet_composition:
        ship_id  = entry.get('ship_id')
        quantity = entry.get('quantity', 0)

        if not ship_id or quantity <= 0:
            continue

        ship = ship_lookup.get(ship_id)
        if not ship:
            print(f"[space_combat.py] WARNING: Ship '{ship_id}' not found in theme")
            continue

        ship_ems  = ship.get('ems', 0)
        ship_type = ship.get('type', 'balanced')
        ems_chunk = ship_ems * quantity
        total_ems += ems_chunk

        # Super Dreadnought special case — split evenly until choice is declared
        if ship_type == 'choice':
            half = ems_chunk / 2
            by_type['combat']  += half
            by_type['carrier'] += half
        else:
            by_type[ship_type] = by_type.get(ship_type, 0) + ems_chunk

    # Convert raw EMS totals to percentages
    if total_ems == 0:
        # Empty fleet — all zeros
        return {
            'combat':    0,
            'carrier':   0,
            'picket':    0,
            'balanced':  0,
            'total_ems': 0,
            'by_type':   by_type,
        }

    percentages = {
        fleet_type: round((ems / total_ems) * 100, 1)
        for fleet_type, ems in by_type.items()
    }

    return {
        **percentages,
        'total_ems': total_ems,
        'by_type':   by_type,
    }


# =============================================================================
# AVAILABLE FLEET TYPES
# =============================================================================

def get_available_fleet_types(ratios: dict) -> list:
    """
    Determine which 1-2 fleet types a commander can choose at battle time.

    THE RULES (mirrors the HTML builder exactly)
    ---------------------------------------------
    1. Sort all four types by their EMS percentage, highest first.
    2. Top type qualifies if it has ≥ DOMINANT_THRESHOLD (40%) of total EMS.
    3. Second type qualifies if:
         - It has ≥ SECONDARY_THRESHOLD (25%) of total EMS, AND
         - It is within MAX_GAP (30 percentage points) of the top type.
    4. Balanced is not special — it qualifies by the same rules as any type.
       It is NOT a free fallback. You must have earned it through composition.
    5. Never return more than MAX_OPTIONS (2) options, no exceptions.
       Even if three types qualify by percentage, only the top two by EMS
       weight are offered.
    6. Tied percentages → both qualify. Player chooses freely at battle time.

    WHY THIS DESIGN?
    ----------------
    Your fleet composition IS your commitment. A player cannot build a
    Combat-heavy fleet and then declare Picket at battle time. The ratios
    lock the options. The spy intel value comes from knowing someone's fleet
    composition — it tells you their available options before battle starts.

    Args:
        ratios (dict): Output from calculate_fleet_ratios().
                       Must contain 'combat', 'carrier', 'picket', 'balanced'.

    Returns:
        list: 0, 1, or 2 fleet type strings the player may choose from.
              e.g. ['picket', 'balanced'] or ['combat'] or []

    Examples:
        Combat 70%, Picket 20%, Balanced 10%, Carrier 0%
        → ['combat']  (only combat ≥ 40%, picket < 25%)

        Picket 40%, Balanced 60%, Combat 0%, Carrier 0%
        → ['balanced', 'picket']  (balanced is top, picket qualifies second)

        Picket 50%, Balanced 50%, Combat 0%, Carrier 0%
        → ['picket', 'balanced']  (tied — both qualify, player chooses)

        Combat 80%, Carrier 15%, Picket 5%, Balanced 0%
        → ['combat']  (only combat qualifies)
    """
    if ratios.get('total_ems', 0) == 0:
        return []

    # The four types we evaluate
    types = ['combat', 'carrier', 'picket', 'balanced']

    # Sort by percentage descending — highest ratio first
    # For ties, order doesn't matter since both qualify equally
    sorted_types = sorted(types, key=lambda t: ratios.get(t, 0), reverse=True)

    available = []

    top_type = sorted_types[0]
    top_pct  = ratios.get(top_type, 0)

    # Rule 1: Top type must meet the dominant threshold
    if top_pct >= DOMINANT_THRESHOLD:
        available.append(top_type)

    # Rule 2: Check the second type
    if len(sorted_types) > 1:
        second_type = sorted_types[1]
        second_pct  = ratios.get(second_type, 0)

        gap = top_pct - second_pct

        # Special case: exact tie — both qualify regardless of thresholds
        if gap == 0 and second_pct >= DOMINANT_THRESHOLD:
            if second_type not in available:
                available.append(second_type)

        # Normal case: second type qualifies if above threshold and close enough
        elif (second_pct >= SECONDARY_THRESHOLD and gap <= MAX_GAP):
            if second_type not in available:
                available.append(second_type)

    # Hard cap — never more than MAX_OPTIONS
    return available[:MAX_OPTIONS]


# =============================================================================
# FLEET VALIDATION
# =============================================================================

def validate_fleet(fleet_composition: list, commander_tier: int, ems_pool: int, theme: dict) -> dict:
    """
    Validate a fleet composition against tier gates and EMS budget.

    TWO GATES — BOTH MUST PASS
    ---------------------------
    Gate 1: EMS budget — total fleet EMS must not exceed the commander's pool.
    Gate 2: Tier — every ship in the fleet must be allowed at this tier.

    A commander cannot field a ship above their tier no matter how much
    EMS they have. EMS is budget. Tier is clearance. Both required.

    Args:
        fleet_composition (list): List of {'ship_id': str, 'quantity': int}
        commander_tier    (int):  The commander's tier (1, 2, or 3)
        ems_pool          (int):  Total EMS available to this commander
        theme             (dict): The loaded theme dictionary

    Returns:
        dict: {
            'valid':        bool,        True only if ALL checks pass
            'errors':       list[str],   human-readable error messages
            'warnings':     list[str],   non-blocking warnings
            'total_ems':    int,         total EMS committed
            'ems_remaining':int,         EMS pool minus committed
        }
    """
    ship_lookup = {ship['id']: ship for ship in theme.get('space_ships', [])}
    errors      = []
    warnings    = []
    total_ems   = 0

    for entry in fleet_composition:
        ship_id  = entry.get('ship_id')
        quantity = entry.get('quantity', 0)

        # Unknown ship
        if ship_id not in ship_lookup:
            errors.append(f"Unknown ship: '{ship_id}' — not found in theme '{theme.get('theme_name')}'")
            continue

        ship = ship_lookup[ship_id]

        # Tier gate check
        ship_tier = ship.get('tier', 1)
        if ship_tier > commander_tier:
            errors.append(
                f"{ship['name']} requires Tier {ship_tier} — "
                f"commander is Tier {commander_tier}"
            )

        # EMS accumulation
        total_ems += ship.get('ems', 0) * quantity

    # EMS budget check
    ems_remaining = ems_pool - total_ems
    if ems_remaining < 0:
        errors.append(
            f"Fleet exceeds EMS budget by {abs(ems_remaining)} EMS "
            f"({total_ems} committed, {ems_pool} available)"
        )

    # Empty fleet warning
    if total_ems == 0:
        warnings.append("Fleet is empty — no ships committed")

    return {
        'valid':         len(errors) == 0,
        'errors':        errors,
        'warnings':      warnings,
        'total_ems':     total_ems,
        'ems_remaining': ems_remaining,
    }


# =============================================================================
# SPACE BATTLE ROUND
# =============================================================================

def calculate_space_round(
    p1_fleet_type: str,
    p2_fleet_type: str,
    p1_tactic:     str,
    p2_tactic:     str,
    dice_str:      str,
    theme:         dict,
) -> dict:
    """
    Calculate the complete result of one round of space battle.

    HOW A ROUND WORKS
    -----------------
    1. Look up matchup bonuses from the theme JSON.
       (p1's bonus against p2's type, and vice versa)
    2. Roll dice + add tactic bonus + add matchup bonus for each player.
    3. Compare totals — higher total wins the round.
    4. On a tie — neither player wins the round.

    WHY TIES DON'T AUTO-REROLL
    ---------------------------
    The original bot rerolled ties. We are not doing that here.
    This function returns the tie result cleanly. If the sequences
    layer wants to reroll on ties, it can call this function again.
    The math layer does not make that policy decision.

    Args:
        p1_fleet_type (str): Player 1's declared fleet type, e.g. "combat"
        p2_fleet_type (str): Player 2's declared fleet type, e.g. "picket"
        p1_tactic     (str): Player 1's tactic choice, e.g. "aggressive"
        p2_tactic     (str): Player 2's tactic choice, e.g. "defensive"
        dice_str      (str): Dice notation for this battle, e.g. "2d6"
        theme         (dict): The loaded theme dictionary

    Returns:
        dict: {
            'p1': {
                'fleet_type':    str,
                'tactic':        str,
                'matchup_bonus': int,
                'round_result':  dict,  full output from calculate_round_total()
                'total':         int,
            },
            'p2': {
                'fleet_type':    str,
                'tactic':        str,
                'matchup_bonus': int,
                'round_result':  dict,
                'total':         int,
            },
            'winner':  str,   "p1", "p2", or "tie"
            'flavor':  str,   matchup flavor text from theme JSON (optional)
        }
    """
    # Step 1: Look up matchup bonuses
    # p1 attacks p2's type → get p1's modifier
    # p2 attacks p1's type → get p2's modifier
    p1_matchup = get_space_matchup_bonus(p1_fleet_type, p2_fleet_type, theme)
    p2_matchup = get_space_matchup_bonus(p2_fleet_type, p1_fleet_type, theme)

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

    # Step 4: Pull flavor text from theme if available
    flavor = ""
    for matchup in theme.get('space_matchups', []):
        if (matchup.get('attacker') == p1_fleet_type and
                matchup.get('defender') == p2_fleet_type):
            flavor = matchup.get('flavor', "")
            break

    return {
        'p1': {
            'fleet_type':    p1_fleet_type,
            'tactic':        p1_tactic,
            'matchup_bonus': p1_matchup,
            'round_result':  p1_result,
            'total':         p1_total,
        },
        'p2': {
            'fleet_type':    p2_fleet_type,
            'tactic':        p2_tactic,
            'matchup_bonus': p2_matchup,
            'round_result':  p2_result,
            'total':         p2_total,
        },
        'winner': winner,
        'flavor': flavor,
    }
