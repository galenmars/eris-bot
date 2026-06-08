"""
core/math/ems_tables.py
=======================
E.R.I.S. Bot — Pure EMS Loss Mathematics

PURPOSE
-------
This file contains ONLY pure functions — meaning every function here:
  - Takes in plain data (numbers, strings, dictionaries)
  - Returns plain data (numbers, strings, dictionaries)
  - Never touches Discord
  - Never touches a database
  - Never does anything async

WHY IS THIS SEPARATE?
---------------------
Because if the EMS formula ever needs to change, you open THIS file
and only this file. Nothing else in the bot needs to be touched.
You can also test every function here without running the bot at all.

WHAT LIVES HERE
---------------
1. The 5 EMS loss tables (one per battle size)
2. calculate_ems_losses()   — looks up how much EMS each player loses
3. determine_winner_text()  — builds the victory announcement string
4. determine_leading_faction() — tells you who is winning mid-battle

WHAT DOES NOT LIVE HERE
------------------------
format_battle_record() was in the original battleCommands.py but it
receives a Discord 'guild' object to look up player mentions. Anything
that needs a Discord object belongs in the cog layer, not math. So that
function will live in cogs/battle_cog.py instead.
"""


# =============================================================================
# THE LOSS TABLES
# =============================================================================
#
# HOW LOSS TABLES WORK
# --------------------
# Each table is a Python list with exactly 10 entries (index 0 through 9).
#
# The INDEX represents how many rounds a player LOST.
#   - Lost 0 rounds → index 0 → 0 EMS lost  (you won every round)
#   - Lost 1 round  → index 1 → small loss
#   - Lost 9 rounds → index 9 → maximum loss (you lost every round)
#
# Index 0 is always 0 because if you lost zero rounds, you take no EMS damage.
#
# Each battle size has its own table — bigger battle = steeper losses.
# This is the core of the risk/reward system: entering a Battleground
# when your forces are weak is a very different decision than a Brawl.
#
# The tables are defined at MODULE LEVEL (outside any function) because:
#   - They never change at runtime — they are constants
#   - Any function in this file can read them without re-creating them
#   - It is one source of truth — change a number here, it changes everywhere

# Brawl — smallest engagement, lowest stakes
# Maximum possible loss: 20 EMS (lost all 9 rounds)
BRAWL_TABLE = [0, 2, 4, 8, 10, 12, 14, 16, 18, 20]

# Firefight — meaningful clash, manageable consequences
# Maximum possible loss: 45 EMS
FIREFIGHT_TABLE = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45]

# Skirmish — significant engagement, you will feel a full defeat
# Maximum possible loss: 90 EMS
SKIRMISH_TABLE = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90]

# Engagement — major operation, can shift a campaign's outcome
# Maximum possible loss: 135 EMS
ENGAGEMENT_TABLE = [0, 15, 30, 45, 60, 75, 90, 105, 120, 135]

# Battleground — decisive, galaxy-defining warfare
# Maximum possible loss: 180 EMS
BATTLEGROUND_TABLE = [0, 20, 40, 60, 80, 100, 120, 140, 160, 180]

# This dictionary maps the battle size string (from the database) to its table.
# Using a dict here means we look up the right table with one line:
#   table = LOSS_TABLES["skirmish"]  →  gives us SKIRMISH_TABLE
#
# The string keys are lowercase because battle sizes are stored lowercase
# in the database. Always normalize to lowercase before looking up.
LOSS_TABLES = {
    "brawl":       BRAWL_TABLE,
    "firefight":   FIREFIGHT_TABLE,
    "skirmish":    SKIRMISH_TABLE,
    "engagement":  ENGAGEMENT_TABLE,
    "battleground": BATTLEGROUND_TABLE,
}


# =============================================================================
# FUNCTIONS
# =============================================================================

def calculate_ems_losses(battle_data: dict) -> dict:
    """
    Calculate how much EMS each player loses at the end of a battle.

    HOW IT WORKS
    ------------
    1. Look up the correct loss table using the battle size string.
    2. For each player, count how many rounds they LOST.
       (Rounds lost = the opponent's win count, plus any tenaciousWins)
    3. Use that count as the INDEX into the loss table.
    4. That index gives us the EMS loss number.

    WHAT IS min(..., len(table) - 1)?
    ----------------------------------
    The tables have 10 entries (index 0-9). If somehow a player lost
    more than 9 rounds (edge case with tenaciousWins stacking), we clamp
    the index to 9 so we never go out of bounds. This is defensive
    programming — it prevents an IndexError crash.

    Args:
        battle_data (dict): The battle record dictionary from the database.
                            Must contain these keys:
                              - 'contentionSize' (str): e.g. "brawl", "skirmish"
                              - 'winsOne'        (int): rounds won by player 1
                              - 'winsTwo'        (int): rounds won by player 2

    Returns:
        dict: {'p1': int, 'p2': int}
              p1 = EMS player 1 loses
              p2 = EMS player 2 loses
    """

    # Step 1: Get the right table.
    # .get(key, default) means: if 'contentionSize' is not in LOSS_TABLES,
    # use BRAWL_TABLE as a safe fallback instead of crashing.
    table = LOSS_TABLES.get(battle_data['contentionSize'], BRAWL_TABLE)

    # Step 2: Calculate how many rounds each player lost.
    #
    # Player 1 loses EMS based on how many rounds PLAYER 2 won.
    # Player 2 loses EMS based on how many rounds PLAYER 1 won.
    #
    # Note the key names — this is a gotcha from the original code:
    #   'winsOne' = rounds won by player 1
    #   'winsTwo' = rounds won by player 2
    #
    # TENACITY NOTE:
    # Tenacity has been deliberately removed from this function.
    # This file is pure math and has no knowledge that tenacity exists.
    # If tenacity activates, sequences/battle_flow.py calls this function
    # TWICE — once for normal rounds, once for the extension rounds —
    # and adds the results together. The math layer stays clean.
    p1_rounds_lost = battle_data['winsTwo']
    p2_rounds_lost = battle_data['winsOne']

    # Step 3: Clamp to the table's valid range (0 to 9) and look up the loss.
    # min() picks the smaller of the two values — so if rounds_lost is 12,
    # min(12, 9) gives us 9, which is the last valid index.
    p1_loss = table[min(p1_rounds_lost, len(table) - 1)]
    p2_loss = table[min(p2_rounds_lost, len(table) - 1)]

    return {'p1': p1_loss, 'p2': p2_loss}


def determine_winner_text(battle_data: dict) -> tuple:
    """
    Generate the victory announcement text at the end of a battle.

    Returns two strings — one short (for the battle record log),
    one formatted (for the Discord embed announcement).

    WHY A TUPLE?
    ------------
    We need two slightly different versions of the same information:
      - 'win_faction' goes into the battle record channel (plain text)
      - 'winner_text' goes into the embed title (bold, uppercase)
    Returning both at once avoids calling this function twice.

    Args:
        battle_data (dict): Must contain:
                              - 'winsOne'   (int): rounds won by player 1
                              - 'winsTwo'   (int): rounds won by player 2
                              - 'p1Faction' (str): faction name for player 1
                              - 'p2Faction' (str): faction name for player 2

    Returns:
        tuple: (win_faction: str, winner_text: str)
               Example: ("Galactic Republic Victory",
                          "**GALACTIC REPUBLIC VICTORY**")
               Or on a tie: ("Tie", "**TIE**")
    """
    wins_one = battle_data['winsOne']
    wins_two = battle_data['winsTwo']

    # Three possible outcomes: tie, player 2 wins, player 1 wins.
    # We check tie first because it is the most specific condition.
    if wins_one == wins_two:
        return "Tie", "**TIE**"

    elif wins_two > wins_one:
        # Player 2 won more rounds — player 2's faction is the victor.
        # .capitalize() ensures consistent casing regardless of how
        # the faction name was stored (e.g. "sith empire" → "Sith empire")
        side = battle_data.get('p2Side') or battle_data.get('p2Faction', 'Side B')
        win_faction = f"{side.capitalize()} Victory"
        winner_text = f"**{side.upper()} VICTORY**"
        return win_faction, winner_text

    else:
        # Player 1 won more rounds.
        side = battle_data.get('p1Side') or battle_data.get('p1Faction', 'Side A')
        win_faction = f"{side.capitalize()} Victory"
        winner_text = f"**{side.upper()} VICTORY**"
        return win_faction, winner_text


def determine_leading_faction(battle_data: dict) -> str:
    """
    Return the name of the faction currently winning mid-battle.

    Called after each round to update the embed showing the live score.
    Unlike determine_winner_text(), this returns a plain faction name
    (not formatted text) because the cog formats it into the embed itself.

    Args:
        battle_data (dict): Must contain:
                              - 'winsOne'   (int): rounds won by player 1
                              - 'winsTwo'   (int): rounds won by player 2
                              - 'p1Faction' (str): faction name for player 1
                              - 'p2Faction' (str): faction name for player 2

    Returns:
        str: The faction name that is currently ahead, or "Tie".
    """
    if battle_data['winsOne'] == battle_data['winsTwo']:
        return "Tie"
    elif battle_data['winsOne'] > battle_data['winsTwo']:
        return battle_data.get('p1Side') or battle_data.get('p1Faction', 'Side A')
    else:
        return battle_data.get('p2Side') or battle_data.get('p2Faction', 'Side B')


def get_max_loss(battle_size: str) -> int:
    """
    Return the maximum possible EMS loss for a given battle size.

    This is a convenience function — useful for displaying to players
    what they are risking before they commit to a battle size.

    Example:
        get_max_loss("brawl")       → 20
        get_max_loss("battleground") → 180

    Args:
        battle_size (str): The battle size string, lowercase.

    Returns:
        int: The maximum EMS loss (last entry in the table).
             Returns 0 if battle_size is not recognized.
    """
    table = LOSS_TABLES.get(battle_size)

    # If the battle_size string wasn't recognized, return 0 rather than
    # crashing. The caller can decide what to do with an unknown size.
    if table is None:
        return 0

    # The last entry in the table is always the maximum loss
    # (all 9 rounds lost). [-1] is Python shorthand for "last element".
    return table[-1]


def get_loss_preview(battle_size: str) -> dict:
    """
    Return a full preview of the loss table for a given battle size.

    Useful for the /battle info command or a future admin command that
    shows players exactly what is at stake before they commit.

    Example output for "firefight":
        {
            0: 0,
            1: 5,
            2: 10,
            ...
            9: 45
        }

    Args:
        battle_size (str): The battle size string, lowercase.

    Returns:
        dict: {rounds_lost: ems_loss} mapping, or empty dict if not found.
    """
    table = LOSS_TABLES.get(battle_size)

    if table is None:
        return {}

    # enumerate() gives us both the index (rounds lost) and the value (EMS)
    # at the same time — cleaner than using range(len(table))
    return {rounds_lost: ems for rounds_lost, ems in enumerate(table)}
