"""
core/domain/commander.py
========================
E.R.I.S. Bot — Commander Rules

PURPOSE
-------
This file owns all rules about commanders as game objects.
It answers questions like:
  - Is this rank valid?
  - How many LP does a Junior get?
  - Is this force type valid (Fleet or Army)?
  - Is this deployment choice legal?
  - Can this user create another Main?

WHAT THIS FILE IS
-----------------
Pure functions only. Every function here:
  - Takes plain data (strings, ints, dicts, lists)
  - Returns plain data OR raises a meaningful exception
  - Never touches Discord
  - Never touches a database
  - Never does anything async

WHY EXCEPTIONS INSTEAD OF BOOLEANS?
------------------------------------
Several functions below raise DomainError instead of returning False.
This is intentional. When a rule is violated, the caller (sequences layer)
needs to know WHY — not just that something failed — so it can send the
right message to the player. A bare False tells you nothing.

    # Bad — caller has no idea what went wrong
    if not can_deploy(rank, force_type, deployed):
        await ctx.send("Something is wrong")

    # Good — caller catches and uses the message
    try:
        validate_deployment(rank, force_type, deployed)
    except DomainError as e:
        await ctx.send(str(e))

Functions that are simple lookups (validate_rank, lp_for_rank) return
plain values because they have nothing useful to say on failure — the
caller already knows what it passed in.

WHAT DOES NOT LIVE HERE
------------------------
- Database reads/writes           → data/commander_repo.py
- Discord messages or embeds      → cogs/commander_cog.py
- The DM enrollment flow          → sequences/enrollment_flow.py
- EMS parsing and pool rules      → domain/ems.py
"""


# =============================================================================
# EXCEPTIONS
# =============================================================================

class DomainError(Exception):
    """
    Raised when a game rule is violated.

    The message is written to be shown directly to the player or admin
    via Discord. Keep messages factual and action-oriented — tell the
    person what they can do, not just what they can't.
    """
    pass


# =============================================================================
# CONSTANTS
# =============================================================================

# The three valid ranks, lowercase. Normalize all input before comparing.
# Main: guild leadership or senior officers — limited to one per user
# Senior: experienced officers
# Junior: new or secondary commanders
VALID_RANKS = ('main', 'senior', 'junior')

# How many Leadership Points each rank starts with and can hold.
# LP is spent to enroll in campaigns or use special abilities.
# These are hard caps — restoring past max is always an error.
RANK_LP = {
    'main':   10,
    'senior':  7,
    'junior':  5,
}

# Valid force type strings, title-cased as stored in the database.
# Fleet: space combat only
# Army:  ground combat only
# A commander is specialized into exactly one arm, fixed at submission.
# A player who wants both a fleet and an army submits two separate
# commanders (two submission codes, two EMS blocks).
VALID_FORCE_TYPES = ('Fleet', 'Army')

# Valid deployment choices when enrolling in a campaign.
# These are what gets stored in commander_campaigns.deployed_force_type.
# A commander deploys the single arm they were submitted as — there is
# no per-campaign force choice.
VALID_DEPLOYED_FORCES = ('Fleet', 'Army')


# =============================================================================
# RANK FUNCTIONS
# =============================================================================

def validate_rank(rank: str) -> bool:
    """
    Return True if rank is one of the three valid values, False otherwise.

    Comparison is case-insensitive. 'Main', 'main', 'MAIN' all pass.
    This is a soft check — it returns False rather than raising, because
    the caller often needs to produce its own error message.

    Args:
        rank (str): The rank string to validate.

    Returns:
        bool: True if valid, False if not.

    Examples:
        validate_rank('main')    → True
        validate_rank('Main')    → True
        validate_rank('admiral') → False
        validate_rank('')        → False
    """
    return rank.lower() in VALID_RANKS


def lp_for_rank(rank: str) -> int:
    """
    Return the Leadership Point maximum for a given rank.

    This is the starting LP and the hard ceiling — no commander can
    ever hold more LP than this value.

    Args:
        rank (str): The rank string. Case-insensitive.

    Returns:
        int: Max LP for the rank.

    Raises:
        DomainError: If rank is not recognized. This is a programming
                     error (should be validated before calling), so the
                     message is for developers, not players.

    Examples:
        lp_for_rank('main')   → 10
        lp_for_rank('Senior') → 7
        lp_for_rank('JUNIOR') → 5
    """
    lp = RANK_LP.get(rank.lower())
    if lp is None:
        raise DomainError(
            f"lp_for_rank() received unknown rank '{rank}'. "
            f"Valid ranks: {', '.join(VALID_RANKS)}. "
            f"This is a code bug — validate rank before calling this function."
        )
    return lp


# =============================================================================
# LP FUNCTIONS
# =============================================================================

def apply_lp_cost(current_lp: int, cost: int) -> int:
    """
    Deduct a Leadership Point cost and return the new LP total.

    Called before enrolling a commander in a campaign or activating
    any ability that spends LP. Raises if the commander cannot afford it.

    Args:
        current_lp (int): The commander's current LP.
        cost       (int): How many LP this action costs.

    Returns:
        int: The new LP total after deduction.

    Raises:
        DomainError: If cost exceeds current_lp (insufficient LP).
        DomainError: If cost is negative (programming error).

    Examples:
        apply_lp_cost(7, 2)  → 5
        apply_lp_cost(5, 5)  → 0
        apply_lp_cost(3, 5)  → raises DomainError
    """
    if cost < 0:
        raise DomainError(
            f"LP cost cannot be negative (got {cost}). "
            f"This is a code bug — check the calling function."
        )
    if cost > current_lp:
        raise DomainError(
            f"Insufficient LP. This action costs {cost} LP "
            f"but the commander only has {current_lp} LP remaining."
        )
    return current_lp - cost


def restore_lp(current_lp: int, amount: int, max_lp: int) -> int:
    """
    Restore Leadership Points, clamping at the rank maximum.

    Used by admin restore commands and campaign-end LP resets.
    Never allows LP to exceed max_lp — restoring 999 on a Junior
    gives them 5, not 999.

    Args:
        current_lp (int): The commander's current LP.
        amount     (int): How many LP to restore.
        max_lp     (int): The commander's LP ceiling (from lp_for_rank).

    Returns:
        int: The new LP total, capped at max_lp.

    Raises:
        DomainError: If amount is negative.

    Examples:
        restore_lp(3, 2, 7)   → 5
        restore_lp(6, 10, 7)  → 7   (clamped)
        restore_lp(7, 3, 7)   → 7   (already at max)
    """
    if amount < 0:
        raise DomainError(
            f"LP restore amount cannot be negative (got {amount}). "
            f"This is a code bug — check the calling function."
        )
    return min(current_lp + amount, max_lp)


def validate_lp_in_range(current_lp: int, max_lp: int) -> None:
    """
    Assert that a commander's current LP is within valid bounds.

    Called as a sanity check when loading commander data — catches
    database corruption or migration bugs early, before they propagate
    into game logic.

    Args:
        current_lp (int): LP value to check.
        max_lp     (int): The commander's rank ceiling.

    Raises:
        DomainError: If current_lp is negative or exceeds max_lp.
    """
    if current_lp < 0:
        raise DomainError(
            f"Commander LP is negative ({current_lp}). "
            f"This indicates database corruption — contact an admin."
        )
    if current_lp > max_lp:
        raise DomainError(
            f"Commander LP ({current_lp}) exceeds rank maximum ({max_lp}). "
            f"This indicates database corruption — contact an admin."
        )


# =============================================================================
# MAIN COMMANDER LIMIT
# =============================================================================

def can_create_main(existing_commanders: list) -> bool:
    """
    Return True if the user is allowed to create a new Main commander.

    Each user may only have one Main commander. This function checks
    their existing commander list and returns False if one already exists.

    The decision to allow or block creation is left to the caller.
    This function only answers the yes/no question.

    Args:
        existing_commanders (list): A list of commander dicts for the user.
                                    Each dict must have a 'commander_rank' key.

    Returns:
        bool: True if no Main commander exists yet, False if one does.

    Examples:
        can_create_main([])                                    → True
        can_create_main([{'commander_rank': 'Junior'}])        → True
        can_create_main([{'commander_rank': 'Main'}])          → False
        can_create_main([{'commander_rank': 'Main'},
                         {'commander_rank': 'Senior'}])        → False
    """
    return not any(
        cmd.get('commander_rank', '').lower() == 'main'
        for cmd in existing_commanders
    )


def find_main_commander(existing_commanders: list) -> dict | None:
    """
    Return the user's Main commander dict, or None if they don't have one.

    Useful when showing players which commander is blocking them from
    creating a second Main.

    Args:
        existing_commanders (list): List of commander dicts with 'commander_rank'.

    Returns:
        dict | None: The Main commander dict, or None.
    """
    for cmd in existing_commanders:
        if cmd.get('commander_rank', '').lower() == 'main':
            return cmd
    return None


# =============================================================================
# FORCE TYPE FUNCTIONS
# =============================================================================

def can_have_force_type(rank: str, force_type: str) -> bool:
    """
    Return True if this force type is valid.

    Every commander is specialized into one arm (Fleet or Army), so this
    is simply a membership check. Rank no longer affects force type.

    Args:
        rank       (str): Commander rank. Accepted for signature stability;
                          no longer affects the result.
        force_type (str): One of Fleet, Army. Case-sensitive
                          (stored title-cased in the database).

    Returns:
        bool: True if the force type is recognized.

    Examples:
        can_have_force_type('main',   'Fleet') → True
        can_have_force_type('senior', 'Army')  → True
        can_have_force_type('junior', 'Tank')  → False
    """
    return force_type in VALID_FORCE_TYPES


def validate_force_type(rank: str, force_type: str) -> None:
    """
    Assert that a commander's force type is valid.

    Raising version of can_have_force_type() — use this when you want
    the error to propagate up to the user automatically.

    Args:
        rank       (str): Commander rank. Accepted for signature stability;
                          no longer affects validation.
        force_type (str): Force type to validate.

    Raises:
        DomainError: If force_type is not recognized.
    """
    if force_type not in VALID_FORCE_TYPES:
        raise DomainError(
            f"'{force_type}' is not a valid force type. "
            f"Valid options: {', '.join(VALID_FORCE_TYPES)}."
        )


# =============================================================================
# DEPLOYMENT FORCE FUNCTIONS
# =============================================================================

def valid_deployment_choices(rank: str, force_type: str) -> tuple:
    """
    Return the set of valid deployed_force values for this commander.

    A commander deploys the single arm they were submitted as. There is
    no per-campaign force choice, so this always returns a one-element
    tuple matching the commander's force_type.

    Args:
        rank       (str): Commander rank. Accepted for signature stability;
                          no longer affects the result.
        force_type (str): The commander's force_type.

    Returns:
        tuple: A one-element tuple containing the commander's force type.

    Examples:
        valid_deployment_choices('main',   'Fleet') → ('Fleet',)
        valid_deployment_choices('senior', 'Army')  → ('Army',)
    """
    return (force_type,)


def validate_deployment(rank: str, force_type: str, deployed_force: str) -> None:
    """
    Assert that a deployment choice is legal for this commander.

    This is the main guard before writing deployed_force_type to the
    database. Since a commander deploys only their single arm, the
    deployed force must match their force_type exactly.

    Args:
        rank          (str): Commander rank. Accepted for signature stability.
        force_type    (str): The commander's force_type.
        deployed_force(str): The chosen deployment (must equal force_type).

    Raises:
        DomainError: If deployed_force does not match the commander's
                     force_type.

    Examples:
        validate_deployment('main',   'Fleet', 'Fleet') → OK
        validate_deployment('senior', 'Fleet', 'Army')  → raises DomainError
    """
    valid = valid_deployment_choices(rank, force_type)
    if deployed_force not in valid:
        raise DomainError(
            f"Invalid deployment choice '{deployed_force}' for a commander "
            f"with force type '{force_type}'. "
            f"Valid choices: {', '.join(valid)}."
        )


def deployment_matches_battle_type(deployed_force: str, battle_type: str) -> bool:
    """
    Return True if a commander's deployment is compatible with the battle type.

    Called when a player tries to /battle engage. A Fleet commander
    cannot fight in a ground campaign and vice versa.

    COMPATIBILITY
    -------------
    deployed 'Fleet'  → only 'space' battles
    deployed 'Army'   → only 'ground' battles

    Args:
        deployed_force (str): What the commander deployed in this campaign.
        battle_type    (str): The channel's battle type ('space' or 'ground').

    Returns:
        bool: True if the commander can fight in this channel.

    Examples:
        deployment_matches_battle_type('Fleet', 'space')  → True
        deployment_matches_battle_type('Fleet', 'ground') → False
        deployment_matches_battle_type('Army',  'ground') → True
    """
    if deployed_force == 'Fleet':
        return battle_type == 'space'
    if deployed_force == 'Army':
        return battle_type == 'ground'
    return False


# =============================================================================
# FACTION FUNCTIONS
# =============================================================================

def validate_faction(faction: str, theme: dict) -> bool:
    """
    Return True if the faction exists in the loaded theme.

    Faction names are theme-specific — the Star Wars theme has 'Galactic
    Republic', a different theme might have 'Terran Dominion'. This
    function reads from the theme dict rather than a hardcoded list.

    The theme dict must contain a 'factions' list where each entry
    has a 'name' key (matching the reference implementation in
    star_wars_old_republic.json).

    Args:
        faction (str):   The faction name to validate.
        theme   (dict):  The loaded theme dict from themes/*.json.

    Returns:
        bool: True if the faction is in the theme, False otherwise.

    Examples (Star Wars theme):
        validate_faction('Galactic Republic', theme)  → True
        validate_faction('Sith Empire', theme)        → True
        validate_faction('NATO', theme)               → False
    """
    valid_names = {f['name'] for f in theme.get('factions', [])}
    return faction in valid_names


def get_faction_names(theme: dict) -> list:
    """
    Return a sorted list of all valid faction names from the theme.

    Used to build the faction selection menu during commander creation.

    Args:
        theme (dict): The loaded theme dict.

    Returns:
        list: Sorted list of faction name strings.
    """
    return sorted(f['name'] for f in theme.get('factions', []))
