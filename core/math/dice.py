"""
core/math/dice.py
=================
E.R.I.S. Bot — Pure Dice Roll Mathematics

PURPOSE
-------
This file handles everything related to rolling dice and calculating
round totals. Like ems_tables.py, every function here is pure:
  - Takes plain data (strings, numbers, dicts)
  - Returns plain data
  - Never touches Discord
  - Never touches a database
  - Never does anything async

WHAT LIVES HERE
---------------
1. roll_dice()              — rolls XdY and returns the total
2. parse_dice_string()      — converts "2d6" into (2, 6)
3. get_tactic_bonus()       — looks up tactic bonus from the theme JSON
4. get_matchup_bonus()      — looks up space or ground matchup modifier
5. calculate_round_total()  — combines roll + tactic + matchup into final score

WHAT DOES NOT LIVE HERE
------------------------
- Reading the theme JSON from disk       → that's data/ layer
- Deciding which tactic a player picked  → that's sequences/ layer
- Announcing the roll result in Discord  → that's cogs/ layer
- Tenacity logic                         → that's sequences/battle_flow.py

THE FLOW (for context, not implemented here)
--------------------------------------------
sequences/battle_flow.py calls these functions like this:

    p1_total = calculate_round_total(
        dice_str      = "2d6",
        tactic_id     = "aggressive",
        matchup_bonus = 2,        ← already calculated by space_combat.py
        theme         = THEME,    ← already loaded by data/ layer
    )

dice.py just does the math. Everything else is someone else's job.
"""

import random


# =============================================================================
# DICE ROLLING
# =============================================================================

def parse_dice_string(dice_str: str) -> tuple:
    """
    Convert a die notation string into (num_dice, num_sides).

    WHY THIS FUNCTION EXISTS
    ------------------------
    Players and the system express rolls as strings like "2d6" or "1d20".
    Before we can roll anything, we need to split that string into two
    integers we can actually do math with.

    HOW IT WORKS
    ------------
    "2d6".split("d") gives us ["2", "6"].
    We convert each part to int and return them as a tuple.

    WHAT IF THE STRING IS MALFORMED?
    ---------------------------------
    If someone passes "banana" or "" we catch the error and return
    a safe default of (2, 6) — two six-sided dice. This prevents a crash
    from propagating up through the entire battle system over a bad string.
    We also print a warning so the issue is visible in logs.

    Args:
        dice_str (str): Dice notation string, e.g. "2d6", "1d20", "3d8".
                        Must be lowercase with exactly one "d" separator.

    Returns:
        tuple: (num_dice: int, num_sides: int)
               e.g. "2d6" → (2, 6)
               Returns (2, 6) as safe fallback on any parse error.

    Examples:
        parse_dice_string("2d6")  → (2, 6)
        parse_dice_string("1d20") → (1, 20)
        parse_dice_string("3d8")  → (3, 8)
    """
    try:
        # Split on "d" — "2d6" becomes ["2", "6"]
        parts = dice_str.lower().strip().split("d")

        # We expect exactly two parts: number of dice and number of sides
        if len(parts) != 2:
            raise ValueError(f"Expected format 'XdY', got: '{dice_str}'")

        num_dice  = int(parts[0])
        num_sides = int(parts[1])

        # Sanity checks — negative dice or zero-sided dice make no sense
        if num_dice < 1 or num_sides < 1:
            raise ValueError(f"Dice values must be positive, got: {num_dice}d{num_sides}")

        return num_dice, num_sides

    except (ValueError, AttributeError) as e:
        # Log the problem but don't crash the battle
        print(f"[dice.py] WARNING: Could not parse dice string '{dice_str}': {e}")
        print(f"[dice.py] Falling back to default: 2d6")
        return 2, 6


def roll_dice(dice_str: str) -> dict:
    """
    Roll a set of dice and return the individual results and total.

    WHY RETURN INDIVIDUAL ROLLS?
    ----------------------------
    Returning each die's result separately lets the Discord layer display
    something like "🎲 Rolled 4 + 3 = 7" instead of just "7".
    That transparency builds player trust in the system.

    Args:
        dice_str (str): Dice notation string, e.g. "2d6".

    Returns:
        dict: {
            'rolls':     list[int],  individual die results
            'total':     int,        sum of all rolls
            'dice_str':  str,        the original string (for display)
            'num_dice':  int,        how many dice were rolled
            'num_sides': int,        how many sides each die had
        }

    Examples:
        roll_dice("2d6") might return:
        {
            'rolls':     [4, 3],
            'total':     7,
            'dice_str':  '2d6',
            'num_dice':  2,
            'num_sides': 6,
        }
    """
    num_dice, num_sides = parse_dice_string(dice_str)

    # Roll each die individually using randint (inclusive on both ends)
    # randint(1, 6) gives a result from 1 to 6, never 0
    rolls = [random.randint(1, num_sides) for _ in range(num_dice)]

    return {
        'rolls':     rolls,
        'total':     sum(rolls),
        'dice_str':  dice_str,
        'num_dice':  num_dice,
        'num_sides': num_sides,
    }


# =============================================================================
# TACTIC BONUS
# =============================================================================

def get_tactic_bonus(tactic_id: str, opponent_tactic_id: str, theme: dict) -> int:
    """
    Look up the bonus for a tactic choice AGAINST a specific opponent tactic.

    Tactics form a rock-paper-scissors matrix, not a flat bonus list:
        Defensive beats Aggressive (+2 / -2)
        Aggressive beats Risky (+2 / -2)
        Risky beats Defensive (+2 / -2)
        Cautious is weak into everything except mirrors (-1) but +1 elsewhere
        Same tactic vs itself = No Bonus (0)

    Reads from theme['tactic_matchups'], structured identically to
    space_matchups / ground_matchups:
        {"attacker": "defensive", "defender": "aggressive", "attacker_mod": 2}

    Args:
        tactic_id          (str): This player's tactic, e.g. "defensive"
        opponent_tactic_id (str): The opponent's tactic, e.g. "aggressive"
        theme               (dict): The loaded theme dictionary.

    Returns:
        int: This player's bonus for this tactic matchup. 0 if not found
             (correctly covers mirror matchups where tactic == opponent_tactic).
    """
    matchups = theme.get('tactic_matchups', [])

    for matchup in matchups:
        if (matchup.get('attacker') == tactic_id and
                matchup.get('defender') == opponent_tactic_id):
            return matchup.get('attacker_mod', 0)

    # Not found — either a mirror matchup (correctly 0) or missing entry
    return 0


# =============================================================================
# MATCHUP BONUS
# =============================================================================

def get_space_matchup_bonus(attacker_type: str, defender_type: str, theme: dict) -> int:
    """
    Look up the attacker's modifier for a space fleet type matchup.

    HOW MATCHUPS WORK
    -----------------
    The theme JSON defines a list of matchup objects:
        {
            "attacker": "combat",
            "defender": "picket",
            "attacker_mod": 2,
            "defender_mod": -2
        }

    We search for the entry where attacker and defender match,
    then return the attacker's modifier. The defender's modifier
    is returned by calling this same function with the arguments swapped.

    WHY ONLY RETURN ONE MODIFIER?
    ------------------------------
    Each player calculates their own total independently.
    Player 1 calls get_space_matchup_bonus(p1_type, p2_type, theme) → their mod
    Player 2 calls get_space_matchup_bonus(p2_type, p1_type, theme) → their mod
    Clean separation — no side effects.

    Args:
        attacker_type (str): The fleet type of the player rolling, e.g. "combat".
        defender_type (str): The fleet type of the opponent, e.g. "picket".
        theme         (dict): The loaded theme dictionary.

    Returns:
        int: The attacker's matchup modifier. Returns 0 if not found.

    Examples:
        get_space_matchup_bonus("combat",  "picket",  theme) →  2
        get_space_matchup_bonus("picket",  "combat",  theme) → -2
        get_space_matchup_bonus("combat",  "combat",  theme) →  0
        get_space_matchup_bonus("balanced","balanced",theme) →  0
    """
    matchups = theme.get('space_matchups', [])

    for matchup in matchups:
        if matchup.get('attacker') == attacker_type and matchup.get('defender') == defender_type:
            return matchup.get('attacker_mod', 0)

    # No matchup entry found — same type vs same type or missing entry
    # Returns 0 which is correct for mirror matchups (combat vs combat)
    return 0


def get_ground_matchup_bonus(attacker_type: str, defender_type: str, theme: dict) -> int:
    """
    Look up the attacker's modifier for a ground force type matchup.

    Identical logic to get_space_matchup_bonus() but reads from
    'ground_matchups' instead of 'space_matchups'.

    Args:
        attacker_type (str): The army type of the player rolling, e.g. "infantry".
        defender_type (str): The army type of the opponent, e.g. "heavy_armor".
        theme         (dict): The loaded theme dictionary.

    Returns:
        int: The attacker's matchup modifier. Returns 0 if not found.

    Examples:
        get_ground_matchup_bonus("infantry",    "heavy_armor", theme) →  2
        get_ground_matchup_bonus("heavy_armor", "infantry",    theme) → -2
        get_ground_matchup_bonus("mechanized",  "infantry",    theme) →  2
    """
    matchups = theme.get('ground_matchups', [])

    for matchup in matchups:
        if matchup.get('attacker') == attacker_type and matchup.get('defender') == defender_type:
            return matchup.get('attacker_mod', 0)

    return 0


# =============================================================================
# ROUND TOTAL
# =============================================================================

def calculate_round_total(
    dice_str:           str,
    tactic_id:          str,
    opponent_tactic_id: str,
    matchup_bonus:       int,
    theme:               dict,
) -> dict:
    """
    Calculate a player's complete total for one round of battle.

    THIS IS THE MAIN FUNCTION
    -------------------------
    Everything else in this file exists to support this one function.
    It combines the three sources of a round total:

        Final Total = Dice Roll + Tactic Bonus + Matchup Bonus

    WHY RETURN A DICT INSTEAD OF JUST AN INT?
    ------------------------------------------
    The cog layer needs to display a breakdown to players:
        "🎲 Roll: 4+3=7  |  ⚔️ Tactic: +2  |  🛡️ Matchup: +2  |  Total: 11"

    Returning the full breakdown dict means the cog can format it
    however Discord needs without re-calculating anything.

    Args:
        dice_str      (str): Dice notation, e.g. "2d6".
        tactic_id     (str): Player's chosen tactic, e.g. "aggressive".
        matchup_bonus (int): Pre-calculated matchup modifier.
                             Positive = attacker advantage.
                             Negative = attacker disadvantage.
                             0 = neutral (same type vs same type).
        theme         (dict): The loaded theme dictionary.

    Returns:
        dict: {
            'dice_result':    dict,  full output from roll_dice()
            'tactic_bonus':   int,   bonus from tactic choice
            'matchup_bonus':  int,   bonus from fleet/army type matchup
            'total':          int,   final combined score for this round
        }

    Example:
        calculate_round_total("2d6", "aggressive", 2, theme)
        might return:
        {
            'dice_result':   {'rolls': [4, 3], 'total': 7, ...},
            'tactic_bonus':  2,
            'matchup_bonus': 2,
            'total':         11,
        }
    """
    # Roll the dice
    dice_result   = roll_dice(dice_str)

    # Get the tactic bonus from the theme — needs both tactics for the matrix lookup
    tactic_bonus  = get_tactic_bonus(tactic_id, opponent_tactic_id, theme)

    # Combine everything into the final round total
    total = dice_result['total'] + tactic_bonus + matchup_bonus

    return {
        'dice_result':   dice_result,
        'tactic_bonus':  tactic_bonus,
        'matchup_bonus': matchup_bonus,
        'total':         total,
    }


# =============================================================================
# ACTION DETECTION
# =============================================================================

def detect_battle_domain(commander_data: dict) -> str:
    """
    Determine whether a battle is a space battle or ground battle.

    HOW IT WORKS
    ------------
    A commander's force type is stored in the database. If they have a
    fleet, it's space. If they have an army, it's ground.

    The returned string tells sequences/battle_flow.py which combat
    module to activate:
        "space"  → space_combat.py handles matchup calculation
        "ground" → ground_combat.py handles matchup calculation

    Args:
        commander_data (dict): Commander record from the database.
                               Must contain:
                                 - 'has_fleet'  (bool): True if space forces committed
                                 - 'has_army'   (bool): True if ground forces committed

    Returns:
        str: "space", or "ground".
             Returns "space" as default if neither flag is set.

    Examples:
        detect_battle_domain({'has_fleet': True,  'has_army': False}) → "space"
        detect_battle_domain({'has_fleet': False, 'has_army': True})  → "ground"
    """
    has_fleet = commander_data.get('has_fleet', False)
    has_army  = commander_data.get('has_army',  False)

    if has_army:
        return "ground"
    else:
        return "space"
