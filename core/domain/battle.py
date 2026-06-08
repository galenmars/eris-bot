"""
core/domain/battle.py
=====================
E.R.I.S. Bot — Battle Rules

PURPOSE
-------
This file owns all rules about how a battle works as a game system.
It answers questions like:
  - Is this battle size valid?
  - Is 4 rounds a legal round count?
  - Did that nat 20 auto-resolve the defense roll?
  - Who wins this round?
  - Is the battle over?
  - Who won?

WHAT THIS FILE IS
-----------------
Pure functions only. Every function here:
  - Takes plain data (strings, ints, dicts, bools)
  - Returns plain data OR raises DomainError
  - Never touches Discord
  - Never touches a database
  - Never does anything async

WHAT THIS FILE CALLS
--------------------
Functions in math/dice.py — specifically get_space_matchup_bonus()
and get_ground_matchup_bonus(). Domain imports downward: domain → math
is a legal dependency direction.

    from core.math.dice import get_space_matchup_bonus, get_ground_matchup_bonus

HOW A ROUND WORKS
-----------------
Each round has exactly two exchanges:

    Exchange 1: P1 attacks. P2 defends.
                Whoever rolls higher wins the exchange.
                If P1 wins: P1 deals EMS damage to P2.
                If P2 wins: P2 blocked successfully. No damage.

    Exchange 2: Roles swap. P2 attacks. P1 defends.
                Same resolution: higher roll wins.
                If P2 wins: P2 deals EMS damage to P1.
                If P1 wins: P1 blocked successfully. No damage.

    Round result: compare total EMS lost by each player across both
                  exchanges. Whoever lost MORE EMS loses the round.

    Possible outcomes:
        Only P2 took damage                → P1 wins the round
        Only P1 took damage                → P2 wins the round
        Both took equal damage             → tie (no round win credited)
        Neither took damage (both blocked) → tie (no round win credited)

    Because base damage is fixed per battle size, the ONLY way the two
    players can take different EMS amounts in a round is crits.
    A critical hit (nat 20 attack or nat 1 defense) deals double damage.

    So within a single round:
        Normal hit + Normal hit  = equal EMS = tie round
        Crit       + Normal hit  = crit dealer loses less = crit dealer wins
        Crit       + No hit      = clear win for crit dealer
        No hit     + No hit      = zero EMS each = tie round

WHO ATTACKS FIRST
-----------------
Initiative (round 0) determines which player leads Exchange 1 in round 1.
Roles alternate every round:
    Odd rounds  (1, 3, 5...): initiative winner leads Exchange 1
    Even rounds (2, 4, 6...): the other player leads Exchange 1

WHAT DOES NOT LIVE HERE
------------------------
- Tenacity logic               → sequences/battle_flow.py calls this file twice
- EMS deduction                → sequences/battle_flow.py → ems_repo
- Damage allocation to ships   → math/damage.py
- Discord embeds               → cogs/battle_cog.py
- Battle state persistence     → data/battle_repo.py
- The coin flip on initiative  → caller owns randomness; this returns 'tie'
"""

from core.math.dice import get_space_matchup_bonus, get_ground_matchup_bonus


# =============================================================================
# EXCEPTIONS
# =============================================================================

class DomainError(Exception):
    """
    Raised when a game rule is violated.

    Messages are written to be shown directly to the player or admin
    in Discord. Keep them factual and action-oriented.
    """
    pass


# =============================================================================
# CONSTANTS
# =============================================================================

# The five battle sizes, lowercase as stored in the database.
VALID_BATTLE_SIZES = ('brawl', 'firefight', 'skirmish', 'engagement', 'battleground')

# The four legal round counts. Not a range — 4, 6, and 8 are invalid.
# Odd numbers mean EMS totals can always break a symmetry if at least
# one exchange produces a different crit outcome between players.
VALID_ROUND_COUNTS = (3, 5, 7, 9)

# The two battle types. Set by the campaign channel binding, not by player choice.
VALID_BATTLE_TYPES = ('space', 'ground')

# Base EMS damage dealt when an exchange lands, by battle size.
# Crits deal exactly double — see calculate_damage().
BASE_DAMAGE = {
    'brawl':        10,
    'firefight':    15,
    'skirmish':     20,
    'engagement':   25,
    'battleground': 30,
}


# =============================================================================
# VALIDATION
# =============================================================================

def validate_battle_size(size: str) -> None:
    """
    Assert that a battle size string is valid.

    Args:
        size (str): The battle size to validate. Must be lowercase.

    Raises:
        DomainError: If the size is not one of the five valid values.

    Examples:
        validate_battle_size('skirmish')    → OK
        validate_battle_size('mega-battle') → raises
    """
    if size not in VALID_BATTLE_SIZES:
        raise DomainError(
            f"'{size}' is not a valid battle size. "
            f"Choose one of: {', '.join(VALID_BATTLE_SIZES)}."
        )


def validate_round_count(count: int) -> None:
    """
    Assert that a round count is one of the four legal values.

    Args:
        count (int): The number of rounds. Must be 3, 5, 7, or 9.

    Raises:
        DomainError: If count is not in the valid set.

    Examples:
        validate_round_count(5) → OK
        validate_round_count(4) → raises
    """
    if count not in VALID_ROUND_COUNTS:
        raise DomainError(
            f"{count} is not a valid round count. "
            f"Choose one of: {', '.join(str(n) for n in VALID_ROUND_COUNTS)}."
        )


def validate_battle_type(battle_type: str) -> None:
    """
    Assert that a battle type is 'space' or 'ground'.

    Args:
        battle_type (str): The battle type string.

    Raises:
        DomainError: If battle_type is not recognized.
    """
    if battle_type not in VALID_BATTLE_TYPES:
        raise DomainError(
            f"'{battle_type}' is not a valid battle type. "
            f"Valid types: {', '.join(VALID_BATTLE_TYPES)}."
        )


# =============================================================================
# CRITICAL HIT DETECTION
# =============================================================================
#
# The battle system uses 1d20. A natural 20 (the die face before bonuses)
# auto-resolves the exchange in the roller's favor. A natural 1 auto-fails.
#
# "Raw roll" = the die result BEFORE tactic and matchup bonuses are added.
# A roll of 18 + 2 tactic bonus is NOT a crit.
# A raw 20 with a -3 matchup penalty IS still a crit.
#
# On ATTACK:
#   Nat 20 → auto-hit, defense roll skipped, double damage
#   Nat 1  → auto-miss, defense roll skipped, no damage
#
# On DEFENSE:
#   Nat 20 → auto-block, attack fails regardless of attacker total
#   Nat 1  → auto-fail, attack hits for double damage

def is_nat20(raw_roll: int) -> bool:
    """
    Return True if this roll is a natural 20.

    Args:
        raw_roll (int): The raw die result before bonuses.

    Returns:
        bool: True if raw_roll == 20.
    """
    return raw_roll == 20


def is_nat1(raw_roll: int) -> bool:
    """
    Return True if this roll is a natural 1.

    Args:
        raw_roll (int): The raw die result before bonuses.

    Returns:
        bool: True if raw_roll == 1.
    """
    return raw_roll == 1


def roll_auto_resolves(raw_roll: int) -> bool:
    """
    Return True if this roll skips the opponent's response roll.

    A nat 20 or nat 1 both auto-resolve — the defense prompt is skipped.
    sequences/battle_flow.py calls this after each roll to decide
    whether to prompt the opponent or move straight to resolution.

    Args:
        raw_roll (int): The raw die result.

    Returns:
        bool: True if raw_roll is 1 or 20.
    """
    return raw_roll == 1 or raw_roll == 20


# =============================================================================
# EXCHANGE RESOLUTION
# =============================================================================
#
# An "exchange" is one attack-and-defense pair.
# Each round has two exchanges with roles swapped between them.
#
# resolve_exchange() is called ONLY when the attacker's roll did not
# auto-resolve (raw was 2-19). By the time this runs, both players
# have rolled and both totals are known.

def resolve_exchange(
    attacker_raw:   int,
    attacker_total: int,
    defender_raw:   int,
    defender_total: int,
) -> dict:
    """
    Determine the outcome of one attack-and-defense exchange.

    Higher total wins. Defender nat 20/1 override the total comparison.
    Called only when the attacker's raw roll was normal (2-19).

    RESOLUTION RULES
    ----------------
    Defender nat 20 → auto-block, no damage regardless of totals
    Defender nat 1  → auto-fail, double damage
    Otherwise       → higher total wins; equal totals = block (tie goes to defender)

    Args:
        attacker_raw   (int): Attacker's raw die result (before bonuses).
        attacker_total (int): Attacker's total (raw + tactic + matchup).
        defender_raw   (int): Defender's raw die result.
        defender_total (int): Defender's total.

    Returns:
        dict: {
            'hit':    bool,  True if the attack lands
            'double': bool,  True if it deals double damage
        }

    Examples:
        resolve_exchange(15, 18, 20, 25) → {'hit': False, 'double': False}  # defender nat 20
        resolve_exchange(15, 18,  1,  4) → {'hit': True,  'double': True}   # defender nat 1
        resolve_exchange(15, 18, 12, 14) → {'hit': True,  'double': False}  # attacker higher
        resolve_exchange(15, 14, 12, 18) → {'hit': False, 'double': False}  # defender higher
        resolve_exchange(15, 17, 12, 17) → {'hit': False, 'double': False}  # equal → defender wins
    """
    if is_nat20(defender_raw):
        return {'hit': False, 'double': False}

    if is_nat1(defender_raw):
        return {'hit': True, 'double': True}

    # Strict greater-than: ties go to the defender (successful block)
    hit = attacker_total > defender_total
    return {'hit': hit, 'double': False}


# =============================================================================
# ROUND EMS AND WINNER
# =============================================================================

def calculate_round_ems(
    exchange_1:  dict,
    exchange_2:  dict,
    battle_size: str,
) -> dict:
    """
    Calculate EMS lost by each player across both exchanges in a round.

    Exchange 1 is P1 attacking P2, so a hit in exchange 1 costs P2 EMS.
    Exchange 2 is P2 attacking P1, so a hit in exchange 2 costs P1 EMS.

    Args:
        exchange_1  (dict): Result from P1's attack on P2. {'hit', 'double'}
        exchange_2  (dict): Result from P2's attack on P1. {'hit', 'double'}
        battle_size (str):  The battle size string, e.g. 'skirmish'.

    Returns:
        dict: {
            'p1_lost': int,  EMS player 1 lost this round
            'p2_lost': int,  EMS player 2 lost this round
        }

    Examples (skirmish, base = 20):
        Both land normal hits  → {'p1_lost': 20, 'p2_lost': 20}  → tie
        P1 crits, P2 misses    → {'p1_lost':  0, 'p2_lost': 40}  → P1 wins round
        P1 misses, P2 crits    → {'p1_lost': 40, 'p2_lost':  0}  → P2 wins round
        Both miss              → {'p1_lost':  0, 'p2_lost':  0}  → tie
    """
    p2_lost = calculate_damage(battle_size, exchange_1['double']) if exchange_1['hit'] else 0
    p1_lost = calculate_damage(battle_size, exchange_2['double']) if exchange_2['hit'] else 0

    return {'p1_lost': p1_lost, 'p2_lost': p2_lost}


def determine_round_winner(p1_ems_lost: int, p2_ems_lost: int) -> str:
    """
    Determine who wins the round from EMS lost by each player.

    The player who lost MORE EMS loses the round.
    If both lost the same amount (including zero), it is a tie.

    Args:
        p1_ems_lost (int): EMS player 1 lost this round.
        p2_ems_lost (int): EMS player 2 lost this round.

    Returns:
        str: 'p1' if player 1 won the round (p2 lost more EMS),
             'p2' if player 2 won, 'tie' if equal.

    Examples:
        determine_round_winner(0,  20) → 'p1'
        determine_round_winner(20,  0) → 'p2'
        determine_round_winner(20, 20) → 'tie'
        determine_round_winner(0,   0) → 'tie'
        determine_round_winner(20, 40) → 'p1'  # P2 critted, P1 hit normally
    """
    if p2_ems_lost > p1_ems_lost:
        return 'p1'
    elif p1_ems_lost > p2_ems_lost:
        return 'p2'
    else:
        return 'tie'


# =============================================================================
# DAMAGE
# =============================================================================

def calculate_damage(battle_size: str, is_double: bool) -> int:
    """
    Calculate the EMS damage dealt by one landed exchange.

    Args:
        battle_size (str):  The battle size string, lowercase.
        is_double   (bool): True if this is a critical hit (double damage).

    Returns:
        int: The damage amount.

    Raises:
        DomainError: If battle_size is not recognized.

    Examples:
        calculate_damage('brawl',    False) → 10
        calculate_damage('brawl',    True)  → 20
        calculate_damage('skirmish', False) → 20
        calculate_damage('skirmish', True)  → 40
    """
    base = BASE_DAMAGE.get(battle_size)
    if base is None:
        raise DomainError(
            f"calculate_damage() received unknown battle size '{battle_size}'. "
            f"Valid sizes: {', '.join(VALID_BATTLE_SIZES)}."
        )
    return base * 2 if is_double else base


# =============================================================================
# INITIATIVE
# =============================================================================

def determine_initiative_winner(p1_score: int, p2_score: int) -> str:
    """
    Determine who wins initiative from the two scores.

    Initiative uses no tactic or matchup bonuses — flat die roll only.
    Returns 'tie' rather than resolving it. The caller
    (sequences/battle_flow.py) owns the coin flip.

    Args:
        p1_score (int): Player 1's initiative roll.
        p2_score (int): Player 2's initiative roll.

    Returns:
        str: 'p1', 'p2', or 'tie'.
    """
    if p1_score > p2_score:
        return 'p1'
    elif p2_score > p1_score:
        return 'p2'
    else:
        return 'tie'


def initiative_attacker(round_number: int, initiative_winner: str) -> str:
    """
    Determine which player leads Exchange 1 in a given round.

    Initiative winner leads in odd rounds. Roles alternate every round.

    Args:
        round_number      (int): Current round, 1-indexed.
        initiative_winner (str): 'p1' or 'p2'.

    Returns:
        str: 'p1' or 'p2' — whoever attacks first this round.

    Raises:
        DomainError: If initiative_winner is not 'p1' or 'p2'.

    Examples:
        initiative_attacker(1, 'p1') → 'p1'
        initiative_attacker(2, 'p1') → 'p2'
        initiative_attacker(3, 'p1') → 'p1'
    """
    if initiative_winner not in ('p1', 'p2'):
        raise DomainError(
            f"initiative_attacker() received invalid winner '{initiative_winner}'. "
            f"Must be 'p1' or 'p2'. This is a code bug."
        )
    winner_leads = (round_number % 2 == 1)
    if winner_leads:
        return initiative_winner
    return 'p2' if initiative_winner == 'p1' else 'p1'


# =============================================================================
# WIN CONDITIONS
# =============================================================================

def is_battle_over(current_round: int, max_rounds: int) -> bool:
    """
    Return True if the battle has reached its final round.

    Args:
        current_round (int): The round that just completed (1-indexed).
        max_rounds    (int): Total rounds the battle runs for.

    Returns:
        bool: True if current_round >= max_rounds.
    """
    return current_round >= max_rounds


def determine_battle_result(p1_round_wins: int, p2_round_wins: int) -> str:
    """
    Determine the battle outcome from round wins.

    The player who won MORE rounds wins the battle. Equal round wins = tie.

    Args:
        p1_round_wins (int): Rounds won by player 1.
        p2_round_wins (int): Rounds won by player 2.

    Returns:
        str: 'p1', 'p2', or 'tie'.

    Examples:
        determine_battle_result(2, 1)  → 'p1'
        determine_battle_result(1, 2)  → 'p2'
        determine_battle_result(1, 1)  → 'tie'
    """
    if p1_round_wins > p2_round_wins:
        return 'p1'
    elif p2_round_wins > p1_round_wins:
        return 'p2'
    else:
        return 'tie'


# =============================================================================
# MATCHUP BONUS ROUTING
# =============================================================================

def get_matchup_bonus(
    attacker_type: str,
    defender_type: str,
    battle_type:   str,
    theme:         dict,
) -> int:
    """
    Look up the attacker's matchup modifier for one exchange.

    Routes to the correct math/dice.py function based on battle_type.
    Called separately for each player per exchange — arguments must
    reflect who is currently rolling (attacker) vs who they face (defender).

    Sequences can cache both players' matchup values once per round since
    the matchup between the same two fleet types is stable:

        p1_matchup = get_matchup_bonus(p1_fleet, p2_fleet, 'space', theme)
        p2_matchup = get_matchup_bonus(p2_fleet, p1_fleet, 'space', theme)

    Both values then apply to whichever exchange that player is the
    attacker in.

    Args:
        attacker_type (str): Fleet/army type of the player rolling.
        defender_type (str): Fleet/army type of the opponent.
        battle_type   (str): 'space' or 'ground'.
        theme         (dict): The loaded theme dictionary.

    Returns:
        int: The attacker's matchup modifier (positive, negative, or zero).

    Raises:
        DomainError: If battle_type is not recognized.
    """
    if battle_type == 'space':
        return get_space_matchup_bonus(attacker_type, defender_type, theme)
    elif battle_type == 'ground':
        return get_ground_matchup_bonus(attacker_type, defender_type, theme)
    else:
        raise DomainError(
            f"get_matchup_bonus() received unknown battle_type '{battle_type}'. "
            f"Valid types: {', '.join(VALID_BATTLE_TYPES)}."
        )
