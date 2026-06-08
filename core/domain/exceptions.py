"""
core/domain/exceptions.py
=========================
E.R.I.S. Bot — Shared Domain Exceptions

PURPOSE
-------
Single definition of DomainError used across all domain files.

WHY THIS EXISTS
---------------
Each domain file (commander.py, battle.py, campaign.py, ems.py)
originally defined its own DomainError class. That works, but the
sequences layer would need to import and catch four separate classes:

    from core.domain.commander import DomainError as CommanderError
    from core.domain.battle    import DomainError as BattleError
    ...

With this file, sequences imports once:

    from core.domain.exceptions import DomainError

And catches everything the domain layer raises with a single except block.

DOMAIN FILES
------------
Each domain file should import from here:

    from core.domain.exceptions import DomainError

The local DomainError definitions in commander.py, battle.py,
campaign.py, and ems.py should be removed and replaced with this import.
"""


class DomainError(Exception):
    """
    Raised when a game rule is violated.

    Messages are written to be shown directly to the player or admin
    in Discord. Always factual and action-oriented — tell the person
    what they can do, not just what they can't.

    Examples of good messages:
        "Main commanders cannot deploy both Fleet and Army in the same
         campaign. Please choose Fleet or Army for this campaign."

        "'{size}' is not a valid battle size.
         Choose one of: brawl, firefight, skirmish, engagement, battleground."

    Examples of bad messages:
        "Invalid input."
        "Error."
    """
    pass
