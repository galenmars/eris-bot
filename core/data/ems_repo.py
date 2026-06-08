"""
core/data/ems_repo.py
=====================
E.R.I.S. Bot — EMS Database Operations

PURPOSE
-------
All reads and writes for EMS pool balances, EMS history,
and EMS request records.
No Discord. No domain logic. Plain dicts in and out.

TABLES OWNED
------------
ems_history      — log of every EMS change (add/subtract, reason, who did it)
ems_requests     — pending/approved/denied player EMS requests
refit_requests   — pending/approved/denied post-campaign block refit requests
commanders       — EMS pool columns (fleet_total_ems, army_total_ems)
                   written here so EMS changes stay in one module
"""

from __future__ import annotations
import sqlite3
import logging

log = logging.getLogger(__name__)


# =============================================================================
# EMS POOL — READ
# =============================================================================

def get_ems_pools(
    db: sqlite3.Connection, commander_id: int
) -> dict | None:
    """Return the EMS pool totals for a commander."""
    row = db.execute(
        'SELECT commander_id, fleet_total_ems, army_total_ems FROM commanders WHERE commander_id = ?',
        (commander_id,),
    ).fetchone()
    return dict(row) if row else None


# =============================================================================
# EMS POOL — WRITE
# =============================================================================

def apply_ems_change(
    db: sqlite3.Connection,
    commander_id: int,
    pool: str,            # 'fleet' or 'army'
    change_amount: int,   # positive = add, negative = subtract
    reason: str,
    changed_by_id: int,   # Discord user_id of who made the change
) -> int:
    """
    Apply an EMS change to a pool and log it to ems_history.
    Floors at 0 — cannot go negative.
    Returns the new EMS total for that pool.
    """
    column = 'fleet_total_ems' if pool == 'fleet' else 'army_total_ems'

    # Apply the change, floor at 0
    db.execute(
        f'''
        UPDATE commanders
        SET {column} = MAX(0, {column} + ?)
        WHERE commander_id = ?
        ''',
        (change_amount, commander_id),
    )

    # Fetch the new total after update
    row = db.execute(
        f'SELECT {column} AS new_total FROM commanders WHERE commander_id = ?',
        (commander_id,),
    ).fetchone()
    new_total = row['new_total'] if row else 0

    # Log to history
    db.execute(
        '''
        INSERT INTO ems_history
            (commander_id, pool, change_amount, new_total, reason, changed_by_id)
        VALUES (?, ?, ?, ?, ?, ?)
        ''',
        (commander_id, pool, change_amount, new_total, reason, changed_by_id),
    )

    db.commit()
    return new_total


# =============================================================================
# EMS HISTORY — READ
# =============================================================================

def get_ems_history(
    db: sqlite3.Connection,
    commander_id: int,
    limit: int = 10,
) -> list[dict]:
    """Return the most recent EMS history entries for a commander."""
    rows = db.execute(
        '''
        SELECT * FROM ems_history
        WHERE commander_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        ''',
        (commander_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# =============================================================================
# EMS REQUESTS — READ
# =============================================================================

def get_request(db: sqlite3.Connection, request_id: int) -> dict | None:
    """Return a single EMS request, or None."""
    row = db.execute(
        'SELECT * FROM ems_requests WHERE request_id = ?',
        (request_id,),
    ).fetchone()
    return dict(row) if row else None


def get_pending_requests(
    db: sqlite3.Connection, guild_id: int
) -> list[dict]:
    """Return all pending EMS requests for a guild."""
    rows = db.execute(
        "SELECT * FROM ems_requests WHERE guild_id = ? AND status = 'pending' ORDER BY created_at ASC",
        (guild_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# =============================================================================
# EMS REQUESTS — WRITE
# =============================================================================

def create_request(
    db: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    commander_id: int,
    pool: str,      # 'fleet', 'army', or 'both'
    amount: int,
    reason: str,
) -> int:
    """
    Write a new EMS request to the pending queue.
    Returns the new request_id.
    """
    cursor = db.execute(
        '''
        INSERT INTO ems_requests
            (guild_id, user_id, commander_id, pool, amount, reason, status)
        VALUES (?, ?, ?, ?, ?, ?, 'pending')
        ''',
        (guild_id, user_id, commander_id, pool, amount, reason),
    )
    db.commit()
    return cursor.lastrowid


def update_request_status(
    db: sqlite3.Connection,
    request_id: int,
    status: str,            # 'approved', 'denied', 'cancelled'
    denial_reason: str | None = None,
    resolved_by_id: int | None = None,
) -> None:
    """Update the status of an EMS request."""
    db.execute(
        '''
        UPDATE ems_requests
        SET status = ?,
            denial_reason = ?,
            resolved_by_id = ?,
            resolved_at = CURRENT_TIMESTAMP
        WHERE request_id = ?
        ''',
        (status, denial_reason, resolved_by_id, request_id),
    )
    db.commit()


# =============================================================================
# REFIT REQUESTS — READ / WRITE
# =============================================================================

def create_refit_request(
    db: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    commander_id: int,
    pool: str,              # 'fleet', 'army', or 'both'
    old_block: str,
    new_block: str,
) -> int:
    """
    Create a pending refit request.
    Stores the old (pre-campaign) block and the new proposed block
    so admins can diff them side by side.
    Returns the new refit_request_id.
    """
    cursor = db.execute(
        '''
        INSERT INTO refit_requests
            (guild_id, user_id, commander_id, pool, old_block, new_block, status)
        VALUES (?, ?, ?, ?, ?, ?, 'pending')
        ''',
        (guild_id, user_id, commander_id, pool, old_block, new_block),
    )
    db.commit()
    return cursor.lastrowid


def get_refit_request(
    db: sqlite3.Connection, refit_id: int
) -> dict | None:
    """Return a single refit request, or None."""
    row = db.execute(
        'SELECT * FROM refit_requests WHERE refit_id = ?',
        (refit_id,),
    ).fetchone()
    return dict(row) if row else None


def get_pending_refit_requests(
    db: sqlite3.Connection, guild_id: int
) -> list[dict]:
    """Return all pending refit requests for a guild."""
    rows = db.execute(
        "SELECT * FROM refit_requests WHERE guild_id = ? AND status = 'pending' ORDER BY created_at ASC",
        (guild_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_refit_requests_for_commander(
    db: sqlite3.Connection, commander_id: int
) -> list[dict]:
    """Return all refit requests for a specific commander."""
    rows = db.execute(
        "SELECT * FROM refit_requests WHERE commander_id = ? ORDER BY created_at DESC",
        (commander_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_refit_status(
    db: sqlite3.Connection,
    refit_id: int,
    status: str,                    # 'approved', 'denied'
    denial_reason: str | None = None,
    resolved_by_id: int | None = None,
) -> None:
    """Update the status of a refit request."""
    db.execute(
        '''
        UPDATE refit_requests
        SET status = ?,
            denial_reason = ?,
            resolved_by_id = ?,
            resolved_at = CURRENT_TIMESTAMP
        WHERE refit_id = ?
        ''',
        (status, denial_reason, resolved_by_id, refit_id),
    )
    db.commit()



