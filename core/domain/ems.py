"""
core/domain/ems.py
==================
E.R.I.S. Bot — EMS Rules

PURPOSE
-------
This file owns all rules about EMS (Engagement Military Strength) as
a game resource. It answers questions like:
  - Which EMS pool does a space battle draw from?
  - What is the EMS after this loss is applied?
  - Is this EMS block string correctly formatted?
  - Does this unit's EMS exceed the tier cap?

WHAT THIS FILE IS
-----------------
Pure functions only. Every function here:
  - Takes plain data (strings, ints, dicts)
  - Returns plain data OR raises DomainError
  - Never touches Discord
  - Never touches a database
  - Never does anything async

EMS POOL STRUCTURE
------------------
Each commander holds two separate EMS pools:
    fleet_total_ems  — space forces, affected by space battles
    army_total_ems   — ground forces, affected by ground battles

Which pool a battle draws from is determined by the campaign's battle_type.
A commander with force_type 'Both' who deploys only 'Fleet' — their
army pool is completely untouched by that campaign's battles.

EMS BLOCK FORMAT — STRICT, NO FALLBACK
---------------------------------------
The EMS block is the text an admin enters when creating a commander.
It describes that commander's forces in a structured format.

ONE valid pattern per line:

    (qty) [current/max] Type "Name"

Examples:
    (1) [150/150] Battleship "The Terror"
    (3) [70/70] Heavy Cruiser "Relentless", "Iron Fist", "Sovereign"

Rules:
  - qty must be a positive integer
  - current and max must be positive integers and must be equal at creation
    (a new commander starts at full EMS — damaged forces aren't enrolled)
  - Type is the unit type string (any non-quoted text)
  - Each name must be in double quotes
  - Number of quoted names must match qty
  - Blank lines are ignored
  - Any line that doesn't match raises DomainError immediately

WHY STRICT?
-----------
The original cog had two fallback parsing patterns because the format
wasn't always consistent. Fallbacks mean:
  - Malformed input silently produces a wrong EMS total
  - The error surfaces mid-campaign when a battle result is wrong
  - By then it's too late to fix cleanly

Strict parsing means:
  - Malformed input raises immediately during commander creation
  - The admin sees exactly which line failed and what was expected
  - Thirty seconds of correction at creation vs a disputed battle later

WHAT DOES NOT LIVE HERE
------------------------
- Writing EMS changes to the database  → data/ems_repo.py
- EMS history logging                  → data/ems_repo.py
- Shop and inventory                   → data/shop_repo.py
- LP rules                             → domain/commander.py
- Battle loss table lookups            → math/ems_tables.py
- Deciding which commander pays EMS    → sequences/battle_flow.py
"""

import re


# =============================================================================
# EXCEPTIONS
# =============================================================================

class DomainError(Exception):
    """
    Raised when an EMS rule is violated.

    Messages are written to be shown directly to the admin in Discord.
    EMS errors during commander creation should be clear and actionable —
    show exactly what was wrong and what the correct format looks like.
    """
    pass


# =============================================================================
# CONSTANTS
# =============================================================================

# The compiled regex for one EMS block line.
#
# Pattern breakdown:
#   \((\d+)\)          — qty in parentheses, e.g. (3)
#   \s+                — whitespace
#   \[(\d+)/(\d+)\]    — current/max in brackets, e.g. [70/70]
#   \s+                — whitespace
#   ([^\["\n]+?)       — unit type: any chars except [, ", newline (non-greedy)
#   \s+                — whitespace
#   ((?:"[^"]+")       — first quoted name
#   (?:\s*,\s*         — optional: comma + whitespace
#   "[^"]+")*          — additional quoted names
#   )                  — end of names group
#   \s*$               — optional trailing whitespace, end of line
#
# Compiled once at module level — never re-compiled per call.
_EMS_LINE_PATTERN = re.compile(
    r'^\((\d+)\)\s+\[(\d+)/(\d+)\]\s+([^\["\n]+?)\s+((?:"[^"]+")'
    r'(?:\s*,\s*"[^"]+")*)\s*$'
)

# Pattern to extract individual quoted names from the names group.
_NAME_PATTERN = re.compile(r'"([^"]+)"')

# The format string shown in every parse error message.
# Single source of truth — change it here, every error message updates.
EXPECTED_FORMAT = '(qty) [current/max] Type "Name" or (qty) [current/max] Type "Name1", "Name2"'


# =============================================================================
# POOL SELECTION
# =============================================================================

def get_pool_key(battle_type: str) -> str:
    """
    Return the database column name for the EMS pool a battle affects.

    Space battles draw from fleet_total_ems.
    Ground battles draw from army_total_ems.

    Args:
        battle_type (str): 'space' or 'ground'.

    Returns:
        str: The column key for the affected pool.

    Raises:
        DomainError: If battle_type is not recognized.

    Examples:
        get_pool_key('space')  → 'fleet_total_ems'
        get_pool_key('ground') → 'army_total_ems'
    """
    if battle_type == 'space':
        return 'fleet_total_ems'
    elif battle_type == 'ground':
        return 'army_total_ems'
    else:
        raise DomainError(
            f"get_pool_key() received unknown battle_type '{battle_type}'. "
            f"Valid types: 'space', 'ground'."
        )


# =============================================================================
# EMS LOSS APPLICATION
# =============================================================================

def apply_ems_loss(current_ems: int, loss: int) -> int:
    """
    Apply an EMS loss and return the new total, floored at zero.

    EMS can never go negative. A commander at 0 EMS is effectively
    destroyed — the floor keeps the number clean and prevents downstream
    math from producing invalid states.

    Args:
        current_ems (int): The commander's current EMS in the affected pool.
        loss        (int): The EMS amount to deduct.

    Returns:
        int: The new EMS total. Minimum 0.

    Raises:
        DomainError: If loss is negative (programming error).

    Examples:
        apply_ems_loss(150, 20)  → 130
        apply_ems_loss(15,  20)  → 0    (clamped, not -5)
        apply_ems_loss(0,   20)  → 0    (already at floor)
        apply_ems_loss(150, 0)   → 150  (no loss)
    """
    if loss < 0:
        raise DomainError(
            f"EMS loss cannot be negative (got {loss}). "
            f"This is a code bug — check the calling function."
        )
    return max(0, current_ems - loss)


# =============================================================================
# EMS BLOCK PARSING
# =============================================================================

def _parse_ems_line(line: str, line_number: int) -> dict:
    """
    Parse one line of an EMS block and return its structured data.

    Internal function — called by parse_ems_block() for each line.
    Raises DomainError with a specific message if the line doesn't match.

    Args:
        line        (str): A single non-blank line from the EMS block.
        line_number (int): 1-indexed line number, used in error messages.

    Returns:
        dict: {
            'qty':       int,         number of units on this line
            'current':   int,         current EMS per unit
            'max':       int,         max EMS per unit
            'unit_type': str,         the unit type string
            'names':     list[str],   list of individual unit names
            'line_ems':  int,         total EMS for this line (qty * current)
        }

    Raises:
        DomainError: If the line does not match the expected format,
                     if current != max (damaged units at enrollment),
                     or if the number of names doesn't match qty.
    """
    match = _EMS_LINE_PATTERN.match(line.strip())

    if not match:
        raise DomainError(
            f"Line {line_number} could not be parsed:\n"
            f"  `{line.strip()}`\n\n"
            f"Expected format:\n"
            f"  `{EXPECTED_FORMAT}`\n\n"
            f"Check for missing parentheses, brackets, or quotes."
        )

    qty         = int(match.group(1))
    current_ems = int(match.group(2))
    max_ems     = int(match.group(3))
    unit_type   = match.group(4).strip()
    names_raw   = match.group(5)

    # Enforce qty > 0
    if qty < 1:
        raise DomainError(
            f"Line {line_number}: unit quantity must be at least 1 (got {qty}).\n"
            f"  `{line.strip()}`"
        )

    # Enforce current == max at enrollment
    # A new commander starts at full EMS — enrolling damaged forces is not allowed.
    if current_ems != max_ems:
        raise DomainError(
            f"Line {line_number}: current EMS ({current_ems}) does not equal "
            f"max EMS ({max_ems}).\n"
            f"  `{line.strip()}`\n\n"
            f"Commanders must enroll with full-strength forces. "
            f"Both values should be the same: [{max_ems}/{max_ems}]."
        )

    if current_ems < 1:
        raise DomainError(
            f"Line {line_number}: EMS value must be at least 1 (got {current_ems}).\n"
            f"  `{line.strip()}`"
        )

    # Extract individual unit names from the quoted names group
    names = _NAME_PATTERN.findall(names_raw)

    # Number of names must match quantity
    if len(names) != qty:
        raise DomainError(
            f"Line {line_number}: quantity is {qty} but {len(names)} "
            f"name{'s' if len(names) != 1 else ''} found.\n"
            f"  `{line.strip()}`\n\n"
            f"Each unit needs its own name in quotes. "
            f"For {qty} units, provide {qty} quoted names separated by commas."
        )

    return {
        'qty':       qty,
        'current':   current_ems,
        'max':       max_ems,
        'unit_type': unit_type,
        'names':     names,
        'line_ems':  qty * current_ems,
    }


def parse_ems_block(ems_block: str) -> dict:
    """
    Parse a complete EMS block string into structured data.

    Processes the block line by line. Blank lines are skipped.
    Any line that doesn't match the strict format raises immediately
    with a message showing exactly which line failed.

    Args:
        ems_block (str): The full EMS block text from the admin's input.

    Returns:
        dict: {
            'total_ems': int,   total EMS across all lines
            'units':     list,  list of per-line parsed dicts (from _parse_ems_line)
            'ships':     dict,  {name: {'ems': int, 'type': str, 'current_ems': int}}
                                flat lookup by individual unit name
        }

    Raises:
        DomainError: If the block is empty, or if any line fails to parse.

    Examples:
        Input:
            (1) [150/150] Battleship "The Terror"
            (2) [70/70] Heavy Cruiser "Iron Fist", "Sovereign"

        Output:
            {
                'total_ems': 290,
                'units': [
                    {'qty': 1, 'current': 150, 'max': 150,
                     'unit_type': 'Battleship', 'names': ['The Terror'],
                     'line_ems': 150},
                    {'qty': 2, 'current': 70, 'max': 70,
                     'unit_type': 'Heavy Cruiser',
                     'names': ['Iron Fist', 'Sovereign'],
                     'line_ems': 140},
                ],
                'ships': {
                    'The Terror':  {'ems': 150, 'type': 'Battleship',    'current_ems': 150},
                    'Iron Fist':   {'ems': 70,  'type': 'Heavy Cruiser', 'current_ems': 70},
                    'Sovereign':   {'ems': 70,  'type': 'Heavy Cruiser', 'current_ems': 70},
                }
            }
    """
    if not ems_block or not ems_block.strip():
        raise DomainError(
            f"EMS block is empty. Provide at least one unit line.\n\n"
            f"Expected format:\n"
            f"  `{EXPECTED_FORMAT}`"
        )

    lines      = ems_block.strip().splitlines()
    total_ems  = 0
    units      = []
    ships      = {}
    line_number = 0

    for raw_line in lines:
        # Skip blank lines silently
        if not raw_line.strip():
            continue

        line_number += 1
        parsed = _parse_ems_line(raw_line, line_number)

        total_ems += parsed['line_ems']
        units.append(parsed)

        # Build the flat ships lookup — one entry per named unit
        for name in parsed['names']:
            ships[name] = {
                'ems':         parsed['current'],
                'type':        parsed['unit_type'],
                'current_ems': parsed['current'],
            }

    if not units:
        raise DomainError(
            f"EMS block contained no valid unit lines. "
            f"Blank lines are ignored — make sure at least one line "
            f"matches the expected format:\n"
            f"  `{EXPECTED_FORMAT}`"
        )

    return {
        'total_ems': total_ems,
        'units':     units,
        'ships':     ships,
    }


def calculate_ems_total(ems_block: str) -> int:
    """
    Parse an EMS block and return only the total EMS value.

    Convenience wrapper around parse_ems_block() for callers that only
    need the number, not the full ship breakdown.

    Args:
        ems_block (str): The full EMS block text.

    Returns:
        int: Total EMS across all lines.

    Raises:
        DomainError: If parsing fails for any reason.

    Examples:
        calculate_ems_total('(1) [150/150] Battleship "The Terror"') → 150
    """
    return parse_ems_block(ems_block)['total_ems']


# =============================================================================
# TIER CAP VALIDATION
# =============================================================================

def get_tier_ems_cap(tier: int, battle_type: str, theme: dict) -> int:
    """
    Return the maximum EMS a single unit may have for a given tier.

    Reads from the theme JSON rather than hardcoding. The Star Wars
    theme defines:
        Tier 1: space 50,  ground 30
        Tier 2: space 150, ground 50
        Tier 3: space 750, ground 180

    Args:
        tier        (int):  The tier number (1, 2, or 3).
        battle_type (str):  'space' or 'ground'.
        theme       (dict): The loaded theme dictionary.

    Returns:
        int: The EMS cap for this tier and battle type.

    Raises:
        DomainError: If the tier is not found in the theme.
        DomainError: If battle_type is not 'space' or 'ground'.

    Examples (Star Wars theme):
        get_tier_ems_cap(1, 'space',  theme) → 50
        get_tier_ems_cap(2, 'ground', theme) → 50
        get_tier_ems_cap(3, 'space',  theme) → 750
    """
    tiers = theme.get('tiers', {})
    tier_data = tiers.get(str(tier))

    if tier_data is None:
        valid_tiers = ', '.join(sorted(tiers.keys()))
        raise DomainError(
            f"Tier {tier} is not defined in the theme. "
            f"Valid tiers: {valid_tiers}."
        )

    if battle_type == 'space':
        cap_key = 'max_ship_ems'
    elif battle_type == 'ground':
        cap_key = 'max_ground_ems'
    else:
        raise DomainError(
            f"get_tier_ems_cap() received unknown battle_type '{battle_type}'. "
            f"Valid types: 'space', 'ground'."
        )

    cap = tier_data.get(cap_key)
    if cap is None:
        raise DomainError(
            f"Theme tier {tier} is missing the '{cap_key}' value. "
            f"This is a theme configuration error."
        )

    return cap


def validate_unit_ems(
    unit_ems:    int,
    unit_type:   str,
    tier:        int,
    battle_type: str,
    theme:       dict,
) -> None:
    """
    Assert that a single unit's EMS does not exceed its tier cap.

    Called during commander creation when validating each line of the
    EMS block. Raises immediately if any unit is over-capped so the
    admin can correct it before it reaches the database.

    Args:
        unit_ems    (int):  The EMS value of the unit (per-unit, not total).
        unit_type   (str):  The unit type string, used in the error message.
        tier        (int):  The commander's tier.
        battle_type (str):  'space' or 'ground'.
        theme       (dict): The loaded theme dictionary.

    Raises:
        DomainError: If unit_ems exceeds the tier cap.

    Examples (Star Wars theme, Tier 2, space — cap is 150):
        validate_unit_ems(150, 'Battleship',    2, 'space', theme) → OK
        validate_unit_ems(200, 'Heavy Cruiser', 2, 'space', theme) → raises
        validate_unit_ems(50,  'Frigate',       2, 'space', theme) → OK
    """
    cap = get_tier_ems_cap(tier, battle_type, theme)

    if unit_ems > cap:
        raise DomainError(
            f"**{unit_type}** has {unit_ems} EMS but Tier {tier} "
            f"{'space' if battle_type == 'space' else 'ground'} units "
            f"are capped at {cap} EMS each.\n\n"
            f"Either reduce the unit's EMS or use a Tier {tier + 1} commander."
        )
