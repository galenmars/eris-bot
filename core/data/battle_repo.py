"""
core/data/battle_repo.py
========================
E.R.I.S. Bot — Battle Database Operations

PURPOSE
-------
All reads and writes for the battles table.
No Discord. No domain logic. Plain dicts in and out.
"""

from __future__ import annotations
import sqlite3
import logging

log = logging.getLogger(__name__)


# =============================================================================
# READ OPERATIONS
# =============================================================================

def get_channel_binding(
    db: sqlite3.Connection, channel_id: int
) -> dict | None:
    """
    Return the campaign binding for a channel.
    Duplicated from campaign_repo for import convenience in battle_cog.
    """
    row = db.execute(
        '''
        SELECT cc.*, c.campaign_name, c.status AS campaign_status
        FROM campaign_channels cc
        JOIN campaigns c ON cc.campaign_id = c.campaign_id
        WHERE cc.channel_id = ?
        ''',
        (channel_id,),
    ).fetchone()
    return dict(row) if row else None


def get_active_battle_in_channel(
    db: sqlite3.Connection, channel_id: int
) -> dict | None:
    """Return the active battle in a channel, or None."""
    row = db.execute(
        "SELECT * FROM battles WHERE channel_id = ? AND status = 'active' LIMIT 1",
        (channel_id,),
    ).fetchone()
    return dict(row) if row else None


def get_active_battle_for_player(
    db: sqlite3.Connection, user_id: int
) -> dict | None:
    """Return any active battle involving this player."""
    row = db.execute(
        "SELECT * FROM battles WHERE (player_one_id = ? OR player_two_id = ?) AND status = 'active' LIMIT 1",
        (user_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def get_battle_by_id(
    db: sqlite3.Connection, battle_id: int
) -> dict | None:
    """Return a battle by primary key."""
    row = db.execute(
        'SELECT * FROM battles WHERE battle_id = ?',
        (battle_id,),
    ).fetchone()
    return dict(row) if row else None


def get_recent_battles_in_channel(
    db: sqlite3.Connection, channel_id: int, limit: int = 5
) -> list[dict]:
    """Return the most recently completed battles in a channel."""
    rows = db.execute(
        "SELECT * FROM battles WHERE channel_id = ? AND status = 'complete' ORDER BY completed_at DESC LIMIT ?",
        (channel_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# =============================================================================
# WRITE OPERATIONS
# =============================================================================

def create_battle(
    db: sqlite3.Connection,
    channel_id: int,
    campaign_id: int,
    player_one_id: int,
    player_two_id: int,
    commander_one_id: int,
    commander_two_id: int,
    battle_name: str,
    battle_type: str,
    battle_size: str,
    max_rounds: int,
    p1_faction: str,
    p2_faction: str,
    hero_name: str,
    hero_name2: str,
    p1_tactic: str,
    p2_tactic: str,
    p1_fleet_type: str,
    p2_fleet_type: str,
) -> int:
    """
    Create a new battle record with status 'active'.
    Returns the new battle_id.
    """
    cursor = db.execute(
        '''
        INSERT INTO battles
            (channel_id, campaign_id,
             player_one_id, player_two_id,
             commander_one_id, commander_two_id,
             battle_name, battle_type, battle_size, max_rounds,
             p1_faction, p2_faction,
             hero_name, hero_name2,
             p1_tactic, p2_tactic, p1_fleet_type, p2_fleet_type,
             current_round, wins_one, wins_two,
             tenacious_wins_one, tenacious_wins_two,
             status)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
             0, 0, 0, 0, 0, 'active')
        ''',
        (
            channel_id, campaign_id,
            player_one_id, player_two_id,
            commander_one_id, commander_two_id,
            battle_name, battle_type, battle_size, max_rounds,
            p1_faction, p2_faction,
            hero_name, hero_name2,
            p1_tactic, p2_tactic, p1_fleet_type, p2_fleet_type,
        ),
    )
    db.commit()
    return cursor.lastrowid


def update_round(
    db: sqlite3.Connection,
    battle_id: int,
    current_round: int,
    wins_one: int,
    wins_two: int,
) -> None:
    """Update round counter and win totals after a round resolves."""
    db.execute(
        '''
        UPDATE battles
        SET current_round = ?, wins_one = ?, wins_two = ?
        WHERE battle_id = ?
        ''',
        (current_round, wins_one, wins_two, battle_id),
    )
    db.commit()


def advance_to_combat(db: sqlite3.Connection, battle_id: int) -> None:
    """Move battle from initiative phase (round 0) to round 1."""
    db.execute(
        "UPDATE battles SET current_round = 1 WHERE battle_id = ?",
        (battle_id,),
    )
    db.commit()


def complete_battle(
    db: sqlite3.Connection,
    battle_id: int,
    wins_one: int,
    wins_two: int,
    p1_ems_lost: int,
    p2_ems_lost: int,
) -> None:
    """Mark a battle complete and record final EMS losses."""
    db.execute(
        '''
        UPDATE battles
        SET status = 'complete',
            wins_one = ?,
            wins_two = ?,
            p1_ems_lost = ?,
            p2_ems_lost = ?,
            completed_at = CURRENT_TIMESTAMP
        WHERE battle_id = ?
        ''',
        (wins_one, wins_two, p1_ems_lost, p2_ems_lost, battle_id),
    )
    db.commit()


def cancel_battle(db: sqlite3.Connection, battle_id: int) -> None:
    """Cancel a battle without recording a result."""
    db.execute(
        "UPDATE battles SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP WHERE battle_id = ?",
        (battle_id,),
    )
    db.commit()


def cancel_all_active_battles(
    db: sqlite3.Connection, guild_id: int
) -> int:
    """
    Cancel every active battle in a guild.
    Returns the number of battles cancelled.
    """
    cursor = db.execute(
        '''
        UPDATE battles
        SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP
        WHERE status = 'active'
          AND channel_id IN (
              SELECT cc.channel_id
              FROM campaign_channels cc
              JOIN campaigns c ON cc.campaign_id = c.campaign_id
              WHERE c.guild_id = ?
          )
        ''',
        (guild_id,),
    )
    db.commit()
    return cursor.rowcount
