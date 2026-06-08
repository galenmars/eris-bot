"""
core/data/commander_repo.py
===========================
E.R.I.S. Bot — Commander & Submission Database Operations

All reads and writes for commanders and commander submissions.
No Discord objects. No domain logic. Plain dicts in, plain dicts out.

STATUS VALUES
-------------
Submission:  pending_block | pending_approval | approved | denied | cancelled
Commander:   active | deployed | inactive
"""

from __future__ import annotations
import sqlite3
import logging
import random
import string

log = logging.getLogger(__name__)


# =============================================================================
# SUBMISSION CODE
# =============================================================================

def _generate_code() -> str:
    """Generate a unique 6-char alphanumeric submission code."""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


# =============================================================================
# SUBMISSIONS — CREATE
# =============================================================================

def create_pending_submission(
    db:             sqlite3.Connection,
    user_id:        int,
    guild_id:       int,
    commander_name: str,
    rank:           str,
    faction:        str,
    force_type:     str,
) -> dict:
    """
    Create a new submission in pending_block state.
    Tier and EMS budget are set later by the approver.
    Returns the created submission as a dict including the submission_code.
    """
    code = _generate_code()
    while db.execute(
        'SELECT 1 FROM commander_submissions WHERE submission_code = ?', (code,)
    ).fetchone():
        code = _generate_code()

    db.execute(
        '''
        INSERT INTO commander_submissions
            (user_id, guild_id, commander_name, rank, faction,
             force_type, ems_budget, submission_code, status)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'pending_block')
        ''',
        (user_id, guild_id, commander_name, rank, faction, force_type, code),
    )
    db.commit()
    return get_submission_by_code(db, code)


# =============================================================================
# SUBMISSIONS — READ
# =============================================================================

def get_submission_by_id(
    db: sqlite3.Connection, submission_id: int
) -> dict | None:
    row = db.execute(
        'SELECT * FROM commander_submissions WHERE submission_id = ?',
        (submission_id,),
    ).fetchone()
    return dict(row) if row else None


def get_submission_by_code(
    db: sqlite3.Connection, code: str
) -> dict | None:
    row = db.execute(
        'SELECT * FROM commander_submissions WHERE submission_code = ?',
        (code,),
    ).fetchone()
    return dict(row) if row else None


def get_pending_submission(
    db: sqlite3.Connection, submission_id: int
) -> dict | None:
    """Alias for get_submission_by_id — kept for compatibility."""
    return get_submission_by_id(db, submission_id)


def get_pending_submissions_for_guild(
    db: sqlite3.Connection, guild_id: int
) -> list[dict]:
    """Return all pending submissions for a guild."""
    rows = db.execute(
        '''
        SELECT * FROM commander_submissions
        WHERE guild_id = ? AND status IN ('pending_block', 'pending_approval')
        ORDER BY created_at ASC
        ''',
        (guild_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_active_submission_for_user(
    db: sqlite3.Connection, user_id: int, guild_id: int
) -> dict | None:
    """Return any active submission for a user."""
    row = db.execute(
        '''
        SELECT * FROM commander_submissions
        WHERE user_id = ? AND guild_id = ?
          AND status IN ('pending_block', 'pending_approval')
        ORDER BY created_at DESC LIMIT 1
        ''',
        (user_id, guild_id),
    ).fetchone()
    return dict(row) if row else None

def get_all_submissions_for_user(
    db: sqlite3.Connection, user_id: int
) -> list[dict]:
    """Return all active submissions for a user across all guilds."""
    rows = db.execute(
        '''
        SELECT * FROM commander_submissions
        WHERE user_id = ?
          AND status IN ('pending_block', 'pending_approval')
        ORDER BY created_at ASC
        ''',
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_commanders_for_user(
    db: sqlite3.Connection, user_id: int
) -> list[dict]:
    """Return all active/deployed commanders for a user across all guilds."""
    rows = db.execute(
        '''
        SELECT * FROM commanders
        WHERE user_id = ?
          AND status IN ('active', 'deployed')
        ORDER BY guild_id, rank, commander_name
        ''',
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]

# =============================================================================
# SUBMISSIONS — UPDATE
# =============================================================================

def claim_submission(
    db:            sqlite3.Connection,
    submission_id: int,
    claimed_by_id: int,
) -> None:
    db.execute(
        '''
        UPDATE commander_submissions
        SET claimed_by_id = ?, status = 'pending_approval'
        WHERE submission_id = ?
        ''',
        (claimed_by_id, submission_id),
    )
    db.commit()


def set_submission_budget(
    db:            sqlite3.Connection,
    submission_id: int,
    tier:          int,
    ems_budget:    int,
) -> None:
    db.execute(
        'UPDATE commander_submissions SET tier = ?, ems_budget = ? WHERE submission_id = ?',
        (tier, ems_budget, submission_id),
    )
    db.commit()


def approve_submission(
    db:             sqlite3.Connection,
    submission_id:  int,
    approved_by_id: int,
) -> None:
    db.execute(
        '''
        UPDATE commander_submissions
        SET status = 'pending_block', resolved_at = CURRENT_TIMESTAMP,
            approved_by_id = ?
        WHERE submission_id = ?
        ''',
        (approved_by_id, submission_id),
    )
    db.commit()


def deny_submission(
    db:            sqlite3.Connection,
    submission_id: int,
    denied_by_id:  int,
    denial_reason: str,
) -> None:
    db.execute(
        '''
        UPDATE commander_submissions
        SET status = 'denied', denial_reason = ?,
            resolved_at = CURRENT_TIMESTAMP, approved_by_id = ?
        WHERE submission_id = ?
        ''',
        (denial_reason, denied_by_id, submission_id),
    )
    db.commit()


def update_submission_status(
    db:            sqlite3.Connection,
    submission_id: int,
    status:        str,
    reason:        str | None = None,
) -> None:
    """Generic status update — kept for compatibility."""
    db.execute(
        '''
        UPDATE commander_submissions
        SET status = ?, denial_reason = ?, resolved_at = CURRENT_TIMESTAMP
        WHERE submission_id = ?
        ''',
        (status, reason, submission_id),
    )
    db.commit()


def set_submitted_block(
    db:              sqlite3.Connection,
    submission_code: str,
    block:           str,
) -> None:
    db.execute(
        'UPDATE commander_submissions SET submitted_block = ? WHERE submission_code = ?',
        (block, submission_code),
    )
    db.commit()


# =============================================================================
# COMMANDERS — CREATE
# =============================================================================

def create_commander_from_submission(
    db: sqlite3.Connection, submission: dict
) -> int:
    """Create a commander record from an approved submission. Returns commander_id."""
    lp = {'main': 10, 'senior': 7, 'junior': 5}.get(submission['rank'], 5)

    force_type      = submission.get('force_type', '').lower()
    submitted_block = submission.get('submitted_block')
    fleet_block     = submitted_block if 'fleet' in force_type or force_type == 'both' else None
    army_block      = submitted_block if 'army' in force_type or force_type == 'both' else None

    cursor = db.execute(
        '''
        INSERT INTO commanders
            (user_id, guild_id, commander_name, rank, faction,
             force_type, tier, fleet_total_ems, army_total_ems,
             fleet_ems_block, army_ems_block,
             max_leadership_points, current_leadership_points, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, 'active')
        ''',
        (
            submission['user_id'], submission['guild_id'],
            submission['commander_name'], submission['rank'],
            submission['faction'], submission['force_type'],
            submission['tier'], fleet_block, army_block, lp, lp,
        ),
    )
    db.execute(
        "UPDATE commander_submissions SET status = 'pending_block' WHERE submission_id = ?",
        (submission['submission_id'],)
    )
    db.commit()
    return cursor.lastrowid


def create_commander(
    db:                    sqlite3.Connection,
    user_id:               int,
    guild_id:              int,
    commander_name:        str,
    rank:                  str,
    faction:               str,
    force_type:            str,
    tier:                  int,
    fleet_total_ems:       int = 0,
    army_total_ems:        int = 0,
    fleet_ems_block:       str | None = None,
    army_ems_block:        str | None = None,
    max_leadership_points: int = 5,
) -> int:
    """Direct commander creation (admin use). Returns the new commander_id."""
    cursor = db.execute(
        '''
        INSERT INTO commanders
            (user_id, guild_id, commander_name, rank, faction, force_type,
             tier, fleet_total_ems, army_total_ems, fleet_ems_block,
             army_ems_block, max_leadership_points,
             current_leadership_points, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
        ''',
        (
            user_id, guild_id, commander_name, rank, faction, force_type,
            tier, fleet_total_ems, army_total_ems, fleet_ems_block,
            army_ems_block, max_leadership_points, max_leadership_points,
        ),
    )
    db.commit()
    return cursor.lastrowid


# =============================================================================
# COMMANDERS — READ
# =============================================================================

def get_by_id(db: sqlite3.Connection, commander_id: int) -> dict | None:
    row = db.execute(
        'SELECT * FROM commanders WHERE commander_id = ?', (commander_id,)
    ).fetchone()
    return dict(row) if row else None


def get_commander_by_id(db: sqlite3.Connection, commander_id: int) -> dict | None:
    """Alias for get_by_id."""
    return get_by_id(db, commander_id)


def get_by_user(
    db: sqlite3.Connection, user_id: int, guild_id: int
) -> list[dict]:
    """Return all commanders for a user in a guild, ordered by rank."""
    rows = db.execute(
        '''
        SELECT c.*, cc.campaign_id AS active_campaign,
               camp.campaign_name AS active_campaign_name
        FROM commanders c
        LEFT JOIN commander_campaigns cc
               ON c.commander_id = cc.commander_id AND cc.status = 'active'
        LEFT JOIN campaigns camp
               ON cc.campaign_id = camp.campaign_id
        WHERE c.user_id = ? AND c.guild_id = ?
        ORDER BY
            CASE c.rank WHEN 'main' THEN 1 WHEN 'senior' THEN 2
                        WHEN 'junior' THEN 3 ELSE 4 END,
            c.commander_name
        ''',
        (user_id, guild_id),
    ).fetchall()
    return [dict(r) for r in rows]


def get_commanders_for_user(
    db: sqlite3.Connection, user_id: int, guild_id: int
) -> list[dict]:
    """Alias for get_by_user."""
    return get_by_user(db, user_id, guild_id)


def get_total_ems_by_user(
    db: sqlite3.Connection, user_id: int, guild_id: int
) -> int:
    row = db.execute(
        '''
        SELECT COALESCE(SUM(fleet_total_ems + army_total_ems), 0) AS total
        FROM commanders
        WHERE user_id = ? AND guild_id = ? AND status != 'inactive'
        ''',
        (user_id, guild_id),
    ).fetchone()
    return row['total'] if row else 0


def get_by_name(
    db: sqlite3.Connection, guild_id: int, name: str
) -> list[dict]:
    """Case-insensitive partial match on commander name within a guild."""
    rows = db.execute(
        '''
        SELECT * FROM commanders
        WHERE guild_id = ? AND LOWER(commander_name) LIKE LOWER(?)
        ORDER BY commander_name
        ''',
        (guild_id, f'%{name}%'),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_for_guild(db: sqlite3.Connection, guild_id: int) -> list[dict]:
    rows = db.execute(
        '''
        SELECT c.*, cc.campaign_id AS active_campaign
        FROM commanders c
        LEFT JOIN commander_campaigns cc
               ON c.commander_id = cc.commander_id AND cc.status = 'active'
        WHERE c.guild_id = ? AND c.status != 'inactive'
        ORDER BY c.faction, c.rank, c.commander_name
        ''',
        (guild_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_commanders_for_guild(
    db: sqlite3.Connection, guild_id: int
) -> list[dict]:
    """Alias for get_all_for_guild."""
    return get_all_for_guild(db, guild_id)


def count_by_rank(
    db: sqlite3.Connection, user_id: int, guild_id: int, rank: str
) -> int:
    row = db.execute(
        '''
        SELECT COUNT(*) AS cnt FROM commanders
        WHERE user_id = ? AND guild_id = ? AND rank = ?
          AND status IN ('active', 'deployed')
        ''',
        (user_id, guild_id, rank),
    ).fetchone()
    return row['cnt'] if row else 0


def get_main_commander_count(
    db: sqlite3.Connection, user_id: int, guild_id: int
) -> int:
    """Alias for count_by_rank('main')."""
    return count_by_rank(db, user_id, guild_id, 'main')


# =============================================================================
# COMMANDERS — UPDATE
# =============================================================================

def update_field(
    db: sqlite3.Connection, commander_id: int, field: str, value
) -> None:
    """Update a single column. field must come from an allowlist — never user input."""
    db.execute(
        f'UPDATE commanders SET {field} = ? WHERE commander_id = ?',
        (value, commander_id),
    )
    db.commit()


def update_fields(
    db: sqlite3.Connection, commander_id: int, fields: dict
) -> None:
    if not fields:
        return
    set_clause = ', '.join(f'{col} = ?' for col in fields)
    values     = list(fields.values()) + [commander_id]
    db.execute(
        f'UPDATE commanders SET {set_clause} WHERE commander_id = ?', values
    )
    db.commit()


def set_status(
    db: sqlite3.Connection, commander_id: int, status: str
) -> None:
    db.execute(
        'UPDATE commanders SET status = ? WHERE commander_id = ?',
        (status, commander_id),
    )
    db.commit()


def update_ems(
    db: sqlite3.Connection, commander_id: int, pool: str, new_total: int
) -> None:
    col = 'fleet_total_ems' if pool == 'fleet' else 'army_total_ems'
    db.execute(
        f'UPDATE commanders SET {col} = ? WHERE commander_id = ?',
        (new_total, commander_id),
    )
    db.commit()


def set_commander_available(db: sqlite3.Connection, commander_id: int) -> None:
    """Mark a commander as no longer deployed."""
    db.execute(
        "UPDATE commander_campaigns SET status = 'unenrolled' "
        "WHERE commander_id = ? AND status = 'active'",
        (commander_id,),
    )
    db.commit()


def deduct_lp(db: sqlite3.Connection, commander_id: int, amount: int) -> None:
    db.execute(
        'UPDATE commanders SET current_leadership_points = '
        'current_leadership_points - ? WHERE commander_id = ?',
        (amount, commander_id),
    )
    db.commit()


def restore_lp(db: sqlite3.Connection, commander_id: int, amount: int) -> None:
    db.execute(
        '''
        UPDATE commanders
        SET current_leadership_points = MIN(
            current_leadership_points + ?,
            max_leadership_points
        )
        WHERE commander_id = ?
        ''',
        (amount, commander_id),
    )
    db.commit()

