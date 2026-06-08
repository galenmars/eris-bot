"""
core/domain/campaign.py
=======================
E.R.I.S. Bot — Campaign and Enrollment Rules

PURPOSE
-------
This file owns all rules about campaigns and commander enrollment.
It answers questions like:
  - Is this campaign type valid?
  - Does this campaign have enough factions to start?
  - Is this commander eligible to enroll?
  - Is this deployment choice compatible with the campaign?

WHAT THIS FILE IS
-----------------
Pure functions only. Every function here:
  - Takes plain data (strings, ints, dicts, lists)
  - Returns plain data OR raises DomainError
  - Never touches Discord
  - Never touches a database
  - Never does anything async

STRUCTURE — WHY SEPARATE FUNCTIONS + ONE ORCHESTRATOR
------------------------------------------------------
Each eligibility rule lives in its own `assert_*` function.
A single orchestrator, check_enrollment_eligible(), calls them all.

This means:
  - Changing one rule touches exactly one function
  - The sequences layer can call individual checks when needed
    (e.g. admin override skips assert_commander_available)
  - Each check has a specific, player-facing error message
  - The orchestrator is just a call list — easy to reorder or extend

WHAT DOES NOT LIVE HERE
------------------------
- Writing enrollment to the database     → data/campaign_repo.py
- The DM enrollment flow                 → sequences/enrollment_flow.py
- Campaign progress updates after battle → sequences/battle_flow.py
- Discord embeds and messages            → cogs/campaign_cog.py
- Commander force type validation        → domain/commander.py
  (assert_valid_deployment calls into it)
"""

from core.domain.commander import validate_deployment, DomainError as CommanderDomainError


# =============================================================================
# EXCEPTIONS
# =============================================================================

class DomainError(Exception):
    """
    Raised when a campaign or enrollment rule is violated.

    Messages are written to be shown directly to the player or admin
    in Discord. Keep them factual and action-oriented.
    """
    pass


# =============================================================================
# CONSTANTS
# =============================================================================

# The three valid campaign types, title-cased as stored in the database.
VALID_CAMPAIGN_TYPES = ('Tug-of-War', 'Invasion', 'Defense')

# Minimum number of factions required to start a campaign.
# Two factions is the floor — a campaign with one faction has no opponent.
MIN_FACTIONS = 2


# =============================================================================
# CAMPAIGN VALIDATION
# =============================================================================

def validate_campaign_type(campaign_type: str) -> None:
    """
    Assert that a campaign type string is valid.

    Args:
        campaign_type (str): The campaign type. Case-sensitive,
                             title-cased as stored in the database.

    Raises:
        DomainError: If campaign_type is not one of the three valid values.

    Examples:
        validate_campaign_type('Tug-of-War') → OK
        validate_campaign_type('Invasion')   → OK
        validate_campaign_type('invasion')   → raises (wrong case)
        validate_campaign_type('Conquest')   → raises
    """
    if campaign_type not in VALID_CAMPAIGN_TYPES:
        raise DomainError(
            f"'{campaign_type}' is not a valid campaign type. "
            f"Choose one of: {', '.join(VALID_CAMPAIGN_TYPES)}."
        )


def validate_faction_count(factions: list) -> None:
    """
    Assert that a campaign has at least the minimum number of factions.

    Args:
        factions (list): The list of faction name strings for the campaign.

    Raises:
        DomainError: If fewer than MIN_FACTIONS factions are provided.

    Examples:
        validate_faction_count(['Republic', 'Empire'])          → OK
        validate_faction_count(['Republic', 'Empire', 'Hutt']) → OK
        validate_faction_count(['Republic'])                    → raises
        validate_faction_count([])                              → raises
    """
    if len(factions) < MIN_FACTIONS:
        raise DomainError(
            f"A campaign requires at least {MIN_FACTIONS} factions. "
            f"Only {len(factions)} provided."
        )


def validate_campaign_factions(factions: list, theme: dict) -> None:
    """
    Assert that all faction names in the list exist in the theme.

    Checks every faction name against the theme's factions list.
    Collects all invalid names before raising so the error message
    shows everything wrong at once rather than one at a time.

    Args:
        factions (list): List of faction name strings.
        theme    (dict): The loaded theme dictionary.

    Raises:
        DomainError: If any faction name is not found in the theme.

    Examples:
        validate_campaign_factions(['Galactic Republic', 'Sith Empire'], theme) → OK
        validate_campaign_factions(['Galactic Republic', 'NATO'],        theme) → raises
    """
    valid_names = {f['name'] for f in theme.get('factions', [])}
    invalid = [name for name in factions if name not in valid_names]

    if invalid:
        raise DomainError(
            f"The following factions are not valid for this theme: "
            f"{', '.join(invalid)}. "
            f"Valid factions: {', '.join(sorted(valid_names))}."
        )


# =============================================================================
# ENROLLMENT ELIGIBILITY — INDIVIDUAL CHECKS
# =============================================================================
#
# Each function checks exactly one rule and raises DomainError if violated.
# Call them individually when you need a specific check, or use the
# orchestrator check_enrollment_eligible() to run all of them in order.
#
# The order in the orchestrator matters:
#   1. Campaign state first — no point checking the commander if the
#      campaign itself is not open for enrollment.
#   2. Duplicate check before availability — clearer error message for
#      the player ("already enrolled" vs "already deployed elsewhere").
#   3. Availability before compatibility — if a commander is locked to
#      another campaign, their force type is irrelevant.
#   4. Force compatibility before deployment — validate the broad
#      requirement (Fleet/Army/Both vs space/ground) before the
#      specific deployment choice within that.
#   5. Deployment last — most specific check, runs only if all others pass.

def assert_campaign_is_active(campaign: dict) -> None:
    """
    Assert that the campaign is currently open for enrollment.

    Args:
        campaign (dict): Campaign record from the database.
                         Must contain 'is_active' (int or bool)
                         and 'campaign_name' (str).

    Raises:
        DomainError: If the campaign is not active.

    Examples:
        assert_campaign_is_active({'is_active': 1, 'campaign_name': 'Operation Dawn'}) → OK
        assert_campaign_is_active({'is_active': 0, 'campaign_name': 'Operation Dawn'}) → raises
    """
    if campaign.get('status') != 'active':
        name = campaign.get('campaign_name', 'this campaign')
        raise DomainError(
            f"**{name}** is no longer active and is not accepting enrollment. "
            f"Contact an admin if you believe this is an error."
        )


def assert_not_already_enrolled(
    commander:            dict,
    campaign:             dict,
    existing_enrollments: list,
) -> None:
    """
    Assert that this commander is not already enrolled in this campaign.

    Prevents duplicate enrollment — same commander, same campaign.

    Args:
        commander            (dict): Commander record. Must have 'commander_id'.
        campaign             (dict): Campaign record. Must have 'campaign_id'
                                     and 'campaign_name'.
        existing_enrollments (list): List of active enrollment dicts for this
                                     commander. Each must have 'campaign_id'.

    Raises:
        DomainError: If the commander is already enrolled in this campaign.
    """
    commander_id = commander.get('commander_id')
    campaign_id  = campaign.get('campaign_id')

    already_enrolled = any(
        e.get('campaign_id') == campaign_id
        for e in existing_enrollments
    )

    if already_enrolled:
        commander_name = commander.get('commander_name', 'This commander')
        campaign_name  = campaign.get('campaign_name',  'this campaign')
        raise DomainError(
            f"**{commander_name}** is already enrolled in **{campaign_name}**."
        )


def assert_commander_available(commander: dict) -> None:
    """
    Assert that the commander is not currently deployed in another campaign.

    A commander can only be active in one campaign at a time. The
    'active_campaign' key is None when the commander is free and holds
    the campaign name when deployed.

    Args:
        commander (dict): Commander record from the database.
                          Must have 'commander_name' and 'active_campaign'
                          (None if free, campaign name string if deployed).

    Raises:
        DomainError: If the commander is already deployed elsewhere.

    Examples:
        assert_commander_available({'commander_name': 'Vexor', 'active_campaign': None})
            → OK
        assert_commander_available({'commander_name': 'Vexor', 'active_campaign': 'Korriban'})
            → raises
    """
    active = commander.get('active_campaign')
    if active is not None:
        name = commander.get('commander_name', 'This commander')
        raise DomainError(
            f"**{name}** is already deployed in **{active}** and cannot enroll "
            f"in another campaign. A commander may only serve in one active "
            f"campaign at a time."
        )


def assert_force_compatible(commander: dict, campaign: dict) -> None:
    """
    All campaigns accept both Fleet and Army commanders.
    Fleet commanders fight in space battles; Army commanders fight in ground battles.
    This check is a no-op — kept for compatibility with callers.
    """
    pass


def assert_valid_deployment(
    commander:      dict,
    deployed_force: str,
) -> None:
    """
    Assert that the chosen deployment force is valid for this commander.

    Delegates to domain/commander.py's validate_deployment() which owns
    the deployment rules. Wraps any CommanderDomainError as a
    DomainError so the sequences layer only catches one exception type
    from the campaign domain.

    Args:
        commander      (dict): Commander record. Must have 'commander_name',
                               'commander_rank', and 'force_type'.
        deployed_force (str):  The player's chosen deployment for this
                               campaign ('Fleet', 'Army', or 'Both').

    Raises:
        DomainError: If the deployment choice violates commander rules.
    """
    rank       = commander.get('commander_rank', '')
    force_type = commander.get('force_type', '')

    try:
        validate_deployment(rank, force_type, deployed_force)
    except CommanderDomainError as e:
        # Re-raise as campaign DomainError so sequences only needs
        # to catch one exception type from this module.
        raise DomainError(str(e)) from e


# =============================================================================
# ENROLLMENT ELIGIBILITY — ORCHESTRATOR
# =============================================================================

def check_enrollment_eligible(
    commander:            dict,
    campaign:             dict,
    existing_enrollments: list,
    deployed_force:       str,
    theme:                dict,
) -> None:
    """
    Run all enrollment eligibility checks in order.

    Calls each assert_* function sequentially. Raises DomainError at the
    first failed check with a specific, player-facing message. If all
    checks pass, returns None — the enrollment is clear to proceed.

    Call this from sequences/enrollment_flow.py before writing to the
    database. If it raises, do not enroll. If it returns, proceed.

    For admin overrides (e.g. force-enrolling an already-deployed
    commander), call the individual assert_* functions directly and
    skip whichever check the admin is overriding.

    Args:
        commander            (dict): Commander record from the database.
        campaign             (dict): Campaign record from the database.
        existing_enrollments (list): Active enrollments for this commander.
        deployed_force       (str):  Player's chosen deployment force.
        theme                (dict): The loaded theme dictionary.

    Raises:
        DomainError: At the first eligibility rule that fails.

    Returns:
        None: If all checks pass.
    """
    assert_campaign_is_active(campaign)
    assert_not_already_enrolled(commander, campaign, existing_enrollments)
    assert_commander_available(commander)
    assert_force_compatible(commander, campaign)
    assert_valid_deployment(commander, deployed_force)


# =============================================================================
# CAMPAIGN STATE HELPERS
# =============================================================================

def is_campaign_active(campaign: dict) -> bool:
    """
    Return True if the campaign is currently active.

    Soft check — returns bool rather than raising. Use this when you
    want to filter a list of campaigns rather than gate an action.

    Args:
        campaign (dict): Campaign record. Must have 'is_active'.

    Returns:
        bool: True if is_active is truthy.
    """
    return campaign.get('status') == 'active'


def get_campaign_type_label(campaign_type: str) -> str:
    """
    Return a display-ready label for a campaign type.

    Currently returns the type string as-is since they are already
    title-cased. Exists as a named function so display formatting
    is centralized — if labels ever need emoji or localization,
    this is the one place to change.

    Args:
        campaign_type (str): One of the valid campaign type strings.

    Returns:
        str: Display label for the campaign type.

    Examples:
        get_campaign_type_label('Tug-of-War') → 'Tug-of-War'
        get_campaign_type_label('Invasion')   → 'Invasion'
    """
    return campaign_type
