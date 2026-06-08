"""
core/data/campaign_repo.py
==========================
E.R.I.S. Bot — Campaign Database Operations

PURPOSE
-------
All reads and writes for campaigns, campaign_factions,
campaign_channels, campaign_staff, and commander_campaigns tables.
No Discord. No domain logic. Plain dicts in and out.
"""

from __future__ import annotations
import sqlite3
import logging

log = logging.getLogger(__name__)


# =============================================================================
# READ OPERATIONS
# =============================================================================

def get_active_campaigns(
    db: sqlite3.Connection, guild_id: int
) -> list[dict]:
    """Return all active campaigns for a guild."""
    rows = db.execute(
        "SELECT * FROM campaigns WHERE guild_id = ? AND status IN ('active', 'enrolling', 'battle') ORDER BY created_at DESC",
        (guild_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def find_campaign_by_name(
    db: sqlite3.Connection, guild_id: int, name: str
) -> dict | None:
    """
    Find a campaign by partial name match (case-insensitive).
    Returns the first match, or None.
    """
    row = db.execute(
        '''
        SELECT * FROM campaigns
        WHERE guild_id = ? AND LOWER(campaign_name) LIKE LOWER(?)
        ORDER BY status = 'active' DESC, created_at DESC
        LIMIT 1
        ''',
        (guild_id, f'%{name}%'),
    ).fetchone()
    return dict(row) if row else None


def get_campaign_by_id(
    db: sqlite3.Connection, campaign_id: int
) -> dict | None:
    """Return a campaign by primary key."""
    row = db.execute(
        'SELECT * FROM campaigns WHERE campaign_id = ?',
        (campaign_id,),
    ).fetchone()
    return dict(row) if row else None


def get_campaign_factions(
    db: sqlite3.Connection, campaign_id: int
) -> list[dict]:
    """Return all factions in a campaign with their win_progress."""
    rows = db.execute(
        'SELECT * FROM campaign_factions WHERE campaign_id = ? ORDER BY faction_name ASC',
        (campaign_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_enrolled_commanders(
    db: sqlite3.Connection, campaign_id: int
) -> list[dict]:
    """Return all actively enrolled commanders for a campaign."""
    rows = db.execute(
        '''
        SELECT c.*, cc.deployed_force, cc.enrollment_id, cc.side
        FROM commanders c
        JOIN commander_campaigns cc ON c.commander_id = cc.commander_id
        WHERE cc.campaign_id = ? AND cc.status = 'active'
        ORDER BY c.faction ASC, c.rank ASC
        ''',
        (campaign_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_enrolled_commander_count(
    db: sqlite3.Connection, campaign_id: int
) -> int:
    """Return the number of actively enrolled commanders in a campaign."""
    row = db.execute(
        "SELECT COUNT(*) AS cnt FROM commander_campaigns WHERE campaign_id = ? AND status = 'active'",
        (campaign_id,),
    ).fetchone()
    return row['cnt'] if row else 0


def get_channel_binding(
    db: sqlite3.Connection, channel_id: int
) -> dict | None:
    """Return the campaign binding for a channel, or None if not bound."""
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


def get_campaign_by_message(
    db: sqlite3.Connection, message_id: int
) -> dict | None:
    """Return the campaign associated with a campaign-board message (for ⚔️ enrollment)."""
    row = db.execute(
        '''
        SELECT c.*
        FROM campaigns c
        JOIN campaign_board_messages cbm ON c.campaign_id = cbm.campaign_id
        WHERE cbm.message_id = ?
        ''',
        (message_id,),
    ).fetchone()
    return dict(row) if row else None


def get_enrollment(
    db: sqlite3.Connection, commander_id: int, campaign_id: int
) -> dict | None:
    """Return an enrollment record, or None if not enrolled."""
    row = db.execute(
        "SELECT * FROM commander_campaigns WHERE commander_id = ? AND campaign_id = ? AND status = 'active'",
        (commander_id, campaign_id),
    ).fetchone()
    return dict(row) if row else None


def get_active_enrollments_for_commander(
    db: sqlite3.Connection, commander_id: int
) -> list[dict]:
    """Return all active campaign enrollments for a commander."""
    rows = db.execute(
        "SELECT * FROM commander_campaigns WHERE commander_id = ? AND status = 'active'",
        (commander_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_enrolled_commander(
    db: sqlite3.Connection, user_id: int, campaign_id: int
) -> dict | None:
    """Return a single enrolled commander for a user in a campaign."""
    row = db.execute(
        '''
        SELECT c.*, cc.deployed_force, cc.enrollment_id,
               cc.lp_max, cc.lp_current
        FROM commanders c
        JOIN commander_campaigns cc ON c.commander_id = cc.commander_id
        WHERE c.user_id = ? AND cc.campaign_id = ? AND cc.status = 'active'
        LIMIT 1
        ''',
        (user_id, campaign_id),
    ).fetchone()
    return dict(row) if row else None


def get_lp(
    db: sqlite3.Connection, commander_id: int, campaign_id: int
) -> int:
    """Return current LP for a commander in a campaign."""
    row = db.execute(
        'SELECT lp_current FROM commander_campaigns WHERE commander_id = ? AND campaign_id = ?',
        (commander_id, campaign_id),
    ).fetchone()
    return row['lp_current'] if row else 0

def get_side_ems_enrolled(db, campaign_id: int, side: str) -> int:
    """
    Sum the EMS currently enrolled on one side of a campaign.
    Accounts for deployed_force — Fleet, Army, or Both.
    """
    row = db.execute("""
        SELECT COALESCE(SUM(
            CASE cc.deployed_force
                WHEN 'Fleet' THEN c.fleet_total_ems
                WHEN 'Army'  THEN c.army_total_ems
                ELSE              c.fleet_total_ems + c.army_total_ems
            END
        ), 0) AS total
        FROM commander_campaigns cc
        JOIN commanders c ON cc.commander_id = c.commander_id
        WHERE cc.campaign_id = ? AND cc.side = ? AND cc.status = 'active'
    """, (campaign_id, side)).fetchone()
    return row['total'] if row else 0

def get_campaign_battle_count(db, campaign_id: int) -> int:
    """Return the number of completed battles in a campaign."""
    row = db.execute(
        "SELECT COUNT(*) AS cnt FROM battles WHERE campaign_id = ? AND status = 'complete'",
        (campaign_id,)
    ).fetchone()
    return row['cnt'] if row else 0

def get_campaign_leaderboard_data(
    db: sqlite3.Connection,
    campaign_id: int,
    theme: dict,
) -> dict:
    """
    Aggregate all stats needed for the end-of-campaign leaderboard.
    Returns a dict with side_a and side_b commander lists, each sorted
    by top damage descending.
    """
    from core.math.dice import get_space_matchup_bonus, get_ground_matchup_bonus

    rows = db.execute("""
        SELECT
            c.commander_name,
            0 AS is_npc,
            cc.side,
            b.battle_id,
            b.battle_type,
            CASE WHEN b.commander_one_id = c.commander_id
                 THEN b.wins_one        ELSE b.wins_two        END AS round_wins,
            CASE WHEN b.commander_one_id = c.commander_id
                 THEN b.wins_two        ELSE b.wins_one        END AS round_losses,
            CASE WHEN b.commander_one_id = c.commander_id
                 THEN b.p2_ems_lost     ELSE b.p1_ems_lost     END AS ems_dealt,
            CASE WHEN b.commander_one_id = c.commander_id
                 THEN b.p1_ems_lost     ELSE b.p2_ems_lost     END AS ems_lost,
            CASE WHEN b.commander_one_id = c.commander_id
                 THEN b.tenacious_wins_one ELSE b.tenacious_wins_two END AS tenacity_wins,
            CASE WHEN b.commander_one_id = c.commander_id
                 THEN b.p1_fleet_type   ELSE b.p2_fleet_type   END AS own_fleet_type,
            CASE WHEN b.commander_one_id = c.commander_id
                 THEN b.p2_fleet_type   ELSE b.p1_fleet_type   END AS opp_fleet_type
        FROM battles b
        JOIN commander_campaigns cc ON (
            cc.campaign_id = b.campaign_id AND (
                cc.commander_id = b.commander_one_id OR
                cc.commander_id = b.commander_two_id
            )
        )
        JOIN commanders c ON c.commander_id = cc.commander_id
        WHERE b.campaign_id = ? AND b.status = 'complete'
          AND c.commander_id IN (b.commander_one_id, b.commander_two_id)
    """, (campaign_id,)).fetchall()

    # Aggregate per commander
    commanders = {}
    for row in rows:
        name = row['commander_name']
        if name not in commanders:
            commanders[name] = {
                'name':           name,
                'is_npc':         row['is_npc'],
                'side':           row['side'],
                'battles':        0,
                'round_wins':     0,
                'ems_dealt':      0,
                'ems_lost':       0,
                'tenacity_wins':  0,
                'matchup_wins':   0,
            }
        c = commanders[name]
        c['battles']       += 1
        c['round_wins']    += row['round_wins']   or 0
        c['ems_dealt']     += row['ems_dealt']    or 0
        c['ems_lost']      += row['ems_lost']     or 0
        c['tenacity_wins'] += row['tenacity_wins'] or 0

        # Efficiency — did they have the matchup advantage?
        if row['own_fleet_type'] and row['opp_fleet_type']:
            fn = get_ground_matchup_bonus if row['battle_type'] == 'ground' \
                 else get_space_matchup_bonus
            bonus = fn(row['own_fleet_type'], row['opp_fleet_type'], theme)
            if bonus > 0:
                c['matchup_wins'] += 1

    # Build per-side lists
    side_a = [c for c in commanders.values() if c['side'] == 'a']
    side_b = [c for c in commanders.values() if c['side'] == 'b']

    return {'side_a': side_a, 'side_b': side_b}


# =============================================================================
# CAMPAIGN STAFF — READ
# =============================================================================

def get_campaign_staff(
    db: sqlite3.Connection, campaign_id: int
) -> list[dict]:
    """
    Return all staff rows for a campaign (owner + collaborators).
    Ordered: owner first, then collaborators alphabetically by assigned_at.
    """
    rows = db.execute(
        '''
        SELECT * FROM campaign_staff
        WHERE campaign_id = ?
        ORDER BY role = 'owner' DESC, assigned_at ASC
        ''',
        (campaign_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def is_campaign_owner(
    db: sqlite3.Connection, campaign_id: int, user_id: int
) -> bool:
    """Return True if user_id is the owner of this campaign."""
    row = db.execute(
        "SELECT 1 FROM campaign_staff WHERE campaign_id = ? AND user_id = ? AND role = 'owner'",
        (campaign_id, user_id),
    ).fetchone()
    return row is not None


def is_campaign_staff(
    db: sqlite3.Connection, campaign_id: int, user_id: int
) -> bool:
    """Return True if user_id is owner OR collaborator on this campaign."""
    row = db.execute(
        'SELECT 1 FROM campaign_staff WHERE campaign_id = ? AND user_id = ?',
        (campaign_id, user_id),
    ).fetchone()
    return row is not None


# =============================================================================
# CAMPAIGN STAFF — WRITE
# =============================================================================

def add_campaign_staff(
    db: sqlite3.Connection,
    campaign_id: int,
    user_id: int,
    guild_id: int,
    role: str,           # 'owner' or 'collaborator'
    assigned_by: int,    # Discord user_id of whoever is adding them
) -> None:
    """
    Add a staff row. Uses INSERT OR REPLACE so calling this on an existing
    user silently updates their role (e.g. promoting a collaborator to owner).
    """
    db.execute(
        '''
        INSERT OR REPLACE INTO campaign_staff
            (campaign_id, user_id, guild_id, role, assigned_by)
        VALUES (?, ?, ?, ?, ?)
        ''',
        (campaign_id, user_id, guild_id, role, assigned_by),
    )
    db.commit()


def remove_campaign_staff(
    db: sqlite3.Connection,
    campaign_id: int,
    user_id: int,
) -> bool:
    """
    Remove a staff row. Returns True if a row was deleted, False if not found.
    Will not remove the owner — callers must check role before calling.
    """
    cursor = db.execute(
        "DELETE FROM campaign_staff WHERE campaign_id = ? AND user_id = ? AND role = 'collaborator'",
        (campaign_id, user_id),
    )
    db.commit()
    return cursor.rowcount > 0


def transfer_campaign_ownership(
    db: sqlite3.Connection,
    campaign_id: int,
    new_owner_id: int,
    guild_id: int,
    assigned_by: int,
) -> None:
    """
    Transfer campaign ownership to a new user. ERIS Admin only.
    - Demotes the current owner to collaborator (keeps their audit history).
    - Inserts or promotes the new owner.
    - Updates owner_user_id on the campaign record.
    """
    # Demote current owner → collaborator
    db.execute(
        """
        UPDATE campaign_staff
        SET role = 'collaborator'
        WHERE campaign_id = ? AND role = 'owner'
        """,
        (campaign_id,),
    )
    # Insert or promote new owner
    db.execute(
        '''
        INSERT OR REPLACE INTO campaign_staff
            (campaign_id, user_id, guild_id, role, assigned_by)
        VALUES (?, ?, ?, 'owner', ?)
        ''',
        (campaign_id, new_owner_id, guild_id, assigned_by),
    )
    # Update fast-lookup column on campaigns
    db.execute(
        'UPDATE campaigns SET owner_user_id = ? WHERE campaign_id = ?',
        (new_owner_id, campaign_id),
    )
    db.commit()


# =============================================================================
# WRITE OPERATIONS
# =============================================================================

def create_campaign(
        db: sqlite3.Connection,
        name: str,
        campaign_type: str,
        factions: list[str],
        guild_id: int,
        owner_user_id: int,
        opposing_ems_total: int = 0,
        max_commanders: int = 0,
        enrollment_deadline: str | None = None,
        side_a_factions: str = '',
        side_b_factions: str = '',
) -> int:
    """
    Create a new campaign, its faction records, and the owner staff row.
    Returns the new campaign_id.

    Win progress starting values by type:
      Tug-of-War: split evenly (100 / number of factions)
      Invasion:   first faction = 0, last faction = 100
      Defense:    first faction = 100, last faction = 0

    opposing_ems_current starts equal to opposing_ems_total (full pool).
    """
    cursor = db.execute(
        '''
        INSERT INTO campaigns
        (guild_id, campaign_name, campaign_type, owner_user_id, status,
         opposing_ems_total, opposing_ems_current,
         max_commanders, enrollment_deadline,
         side_a_factions, side_b_factions)
        VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
        ''',
        (guild_id, name, campaign_type, owner_user_id,
         opposing_ems_total, opposing_ems_total,
         max_commanders, enrollment_deadline,
         side_a_factions, side_b_factions),
    )
    campaign_id = cursor.lastrowid

    # Insert the creator as owner in campaign_staff
    db.execute(
        '''
        INSERT INTO campaign_staff (campaign_id, user_id, guild_id, role, assigned_by)
        VALUES (?, ?, ?, 'owner', ?)
        ''',
        (campaign_id, owner_user_id, guild_id, owner_user_id),
    )

    # Set starting win_progress per type
    if campaign_type == 'Tug-of-War':
        start_progress = 100 // len(factions)
        starts = [start_progress] * len(factions)
    elif campaign_type == 'Invasion':
        starts = [0] + [100] * (len(factions) - 1)
    else:  # Defense
        starts = [100] + [0] * (len(factions) - 1)

    for faction, progress in zip(factions, starts):
        db.execute(
            'INSERT INTO campaign_factions (campaign_id, faction_name, win_progress) VALUES (?, ?, ?)',
            (campaign_id, faction, progress),
        )

    db.commit()
    return campaign_id


def update_campaign_opposing_ems(
    db: sqlite3.Connection,
    campaign_id: int,
    opposing_ems_total: int,
    opposing_ems_current: int,
) -> None:
    """
    Update the enemy EMS pool.
    Both total and current are updated together — caller is responsible
    for adjusting current by the same delta as total.
    """
    db.execute(
        '''
        UPDATE campaigns
        SET opposing_ems_total = ?, opposing_ems_current = ?
        WHERE campaign_id = ?
        ''',
        (opposing_ems_total, opposing_ems_current, campaign_id),
    )
    db.commit()


def update_campaign_max_commanders(
    db: sqlite3.Connection,
    campaign_id: int,
    max_commanders: int,
) -> None:
    """Update the enrollment cap. 0 = no limit."""
    db.execute(
        'UPDATE campaigns SET max_commanders = ? WHERE campaign_id = ?',
        (max_commanders, campaign_id),
    )
    db.commit()


def update_campaign_deadline(
    db: sqlite3.Connection,
    campaign_id: int,
    enrollment_deadline: str,
) -> None:
    """Update the enrollment deadline (ISO datetime string)."""
    db.execute(
        'UPDATE campaigns SET enrollment_deadline = ? WHERE campaign_id = ?',
        (enrollment_deadline, campaign_id),
    )
    db.commit()


def deduct_opposing_ems(
    db: sqlite3.Connection,
    campaign_id: int,
    amount: int,
) -> int:
    """
    Deduct EMS from the enemy pool after a player victory.
    Returns the new opposing_ems_current.
    Floors at 0 — never goes negative.
    """
    row = db.execute(
        'SELECT opposing_ems_current FROM campaigns WHERE campaign_id = ?',
        (campaign_id,),
    ).fetchone()
    current = row['opposing_ems_current'] if row else 0
    new_current = max(0, current - amount)
    db.execute(
        'UPDATE campaigns SET opposing_ems_current = ? WHERE campaign_id = ?',
        (new_current, campaign_id),
    )
    db.commit()
    return new_current


def snapshot_campaign_ems(
    db: sqlite3.Connection,
    campaign_id: int,
) -> tuple[int, int]:
    """
    Snapshot both sides' EMS at campaign start.
    Sums enrolled commanders' EMS per side and stores as starting values.
    Returns (side_a_total, side_b_total).
    """
    rows = db.execute(
        '''
        SELECT cc.side,
               SUM(c.fleet_total_ems + c.army_total_ems) AS total_ems
        FROM commander_campaigns cc
        JOIN commanders c ON cc.commander_id = c.commander_id
        WHERE cc.campaign_id = ? AND cc.status = 'active'
        GROUP BY cc.side
        ''',
        (campaign_id,),
    ).fetchall()

    side_a = 0
    side_b = 0
    for row in rows:
        if row['side'] == 'a':
            side_a = row['total_ems'] or 0
        elif row['side'] == 'b':
            side_b = row['total_ems'] or 0

    db.execute(
        '''
        UPDATE campaigns
        SET side_a_ems_start   = ?,
            side_b_ems_start   = ?,
            side_a_ems_current = ?,
            side_b_ems_current = ?,
            status             = 'battle'
        WHERE campaign_id = ?
        ''',
        (side_a, side_b, side_a, side_b, campaign_id),
    )
    db.commit()
    return side_a, side_b


def update_progress_thread_id(
    db: sqlite3.Connection,
    campaign_id: int,
    thread_id: int,
) -> None:
    """Store the Discord thread ID where battle progress is logged."""
    db.execute(
        'UPDATE campaigns SET progress_thread_id = ? WHERE campaign_id = ?',
        (thread_id, campaign_id),
    )
    db.commit()


def get_board_message_for_campaign(
    db: sqlite3.Connection,
    campaign_id: int,
) -> dict | None:
    """
    Return the campaign-board announcement message for a campaign.
    Used to cross-link the thread into the original enrollment embed.
    Returns {'message_id': ..., 'channel_id': ...} or None.
    """
    row = db.execute(
        '''
        SELECT message_id, channel_id
        FROM campaign_board_messages
        WHERE campaign_id = ?
        ORDER BY message_id DESC
        LIMIT 1
        ''',
        (campaign_id,),
    ).fetchone()
    return dict(row) if row else None


def get_enrolled_commanders_for_user(
    db: sqlite3.Connection, user_id: int, campaign_id: int
) -> list[dict]:
    """Return all enrolled commanders for a user in a campaign (one per side)."""
    rows = db.execute(
        '''
        SELECT c.*, cc.deployed_force, cc.enrollment_id,
               cc.lp_max, cc.lp_current, cc.side
        FROM commanders c
        JOIN commander_campaigns cc ON c.commander_id = cc.commander_id
        WHERE c.user_id = ? AND cc.campaign_id = ? AND cc.status = 'active'
        ''',
        (user_id, campaign_id),
    ).fetchall()
    return [dict(r) for r in rows]


def enroll_commander(
        db: sqlite3.Connection,
        commander_id: int,
        campaign_id: int,
        deployed_force: str,
        side: str = 'a',
) -> int:
    """
    Enroll a commander in a campaign.
    Returns the new enrollment_id.
    """
    # Pull LP from the commander record
    row = db.execute(
        'SELECT max_leadership_points FROM commanders WHERE commander_id = ?',
        (commander_id,)
    ).fetchone()
    lp = row['max_leadership_points'] if row else 5

    cursor = db.execute(
        '''
        INSERT INTO commander_campaigns
            (commander_id, campaign_id, deployed_force, status, lp_max, lp_current, side)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ''',
        (commander_id, campaign_id, deployed_force, 'active', lp, lp, side),
    )
    db.commit()
    return cursor.lastrowid


def bind_channel(
    db: sqlite3.Connection,
    channel_id: int,
    campaign_id: int,
    battle_type: str,    # 'space' or 'ground'
) -> None:
    """Bind a Discord channel to a campaign as a battle channel."""
    db.execute(
        '''
        INSERT OR REPLACE INTO campaign_channels (channel_id, campaign_id, battle_type)
        VALUES (?, ?, ?)
        ''',
        (channel_id, campaign_id, battle_type),
    )
    db.commit()


def unbind_channel(
    db: sqlite3.Connection,
    channel_id: int,
) -> bool:
    """
    Remove a channel binding entirely. ERIS Admin only.
    Returns True if a row was deleted, False if nothing was bound.
    """
    cursor = db.execute(
        'DELETE FROM campaign_channels WHERE channel_id = ?',
        (channel_id,),
    )
    db.commit()
    return cursor.rowcount > 0


def bind_results_channel(
    db: sqlite3.Connection,
    campaign_id: int,
    results_channel_id: int,
) -> None:
    """Store the results channel for a campaign."""
    db.execute(
        """
        UPDATE campaign_channels
        SET results_channel_id = ?
        WHERE campaign_id = ?
        """,
        (results_channel_id, campaign_id),
    )
    db.commit()


def close_campaign(
    db: sqlite3.Connection,
    campaign_id: int,
    result_note: str | None = None,
) -> None:
    """Mark a campaign as complete."""
    db.execute(
        '''
        UPDATE campaigns
        SET status       = 'complete',
            result_note  = ?,
            campaign_end_date = CURRENT_TIMESTAMP,
            completed_at = CURRENT_TIMESTAMP
        WHERE campaign_id = ?       
        ''',
        (result_note, campaign_id),
    )
    db.commit()


def delete_campaign(
    db: sqlite3.Connection,
    campaign_id: int,
) -> None:
    """
    Hard-delete a campaign record. ERIS Admin only.
    Also removes all related staff, faction, channel, and board message rows.
    Commander enrollments are left intact for history — their campaign_id
    will reference a missing campaign, which is acceptable for an admin nuke.
    """
    db.execute(
        '''
        UPDATE commanders SET status = 'active'
        WHERE commander_id IN (
            SELECT commander_id FROM commander_campaigns WHERE campaign_id = ?
        )
        ''',
        (campaign_id,)
    )
    db.execute('DELETE FROM commander_campaigns     WHERE campaign_id = ?', (campaign_id,))
    db.execute('DELETE FROM campaign_staff          WHERE campaign_id = ?', (campaign_id,))
    db.execute('DELETE FROM campaign_factions       WHERE campaign_id = ?', (campaign_id,))
    db.execute('DELETE FROM campaign_channels       WHERE campaign_id = ?', (campaign_id,))
    db.execute('DELETE FROM campaign_board_messages WHERE campaign_id = ?', (campaign_id,))
    db.execute('DELETE FROM campaigns               WHERE campaign_id = ?', (campaign_id,))


def register_board_message(
    db: sqlite3.Connection,
    campaign_id: int,
    message_id: int,
    channel_id: int,
) -> None:
    """Register a campaign-board message so the ⚔️ reaction listener can find it."""
    db.execute(
        '''
        INSERT OR REPLACE INTO campaign_board_messages (message_id, campaign_id, channel_id)
        VALUES (?, ?, ?)
        ''',
        (message_id, campaign_id, channel_id),
    )
    db.commit()


def spend_lp(
    db: sqlite3.Connection, commander_id: int, campaign_id: int, amount: int
) -> int:
    """
    Deduct LP from a commander's campaign pool.
    Returns the new lp_current.
    Raises ValueError if insufficient LP.
    """
    current = get_lp(db, commander_id, campaign_id)
    if current < amount:
        raise ValueError(f"Insufficient LP: {current} available, {amount} required.")
    new_total = current - amount
    db.execute(
        '''
        UPDATE commander_campaigns
        SET lp_current = ?
        WHERE commander_id = ? AND campaign_id = ?
        ''',
        (new_total, commander_id, campaign_id),
    )
    db.commit()
    return new_total

def update_campaign_progress_after_battle(
    db,
    campaign_id:     int,
    p1_commander_id: int,
    p2_commander_id: int,
    p1_ems_lost:     int,
    p2_ems_lost:     int,
) -> None:
    """
    After a battle completes:
    - Decrements side_X_ems_current for damage taken
    - Increments ems_dealt on campaign_factions for damage dealt
    """
    rows = db.execute("""
        SELECT cc.commander_id, cc.side, c.faction
        FROM commander_campaigns cc
        JOIN commanders c ON cc.commander_id = c.commander_id
        WHERE cc.campaign_id = ? AND cc.commander_id IN (?, ?)
          AND cc.status = 'active'
    """, (campaign_id, p1_commander_id, p2_commander_id)).fetchall()
    print(f"DEBUG sides lookup: {list(rows)} for commanders {p1_commander_id}, {p2_commander_id} in campaign {campaign_id}")

    sides = {
        row['commander_id']: {'side': row['side'], 'faction': row['faction']}
        for row in rows
    }

    p1_info = sides.get(p1_commander_id)
    p2_info = sides.get(p2_commander_id)

    if not p1_info or not p2_info:
        return  # Can't determine sides — skip

    # Decrement side EMS current (damage taken)
    p1_col = 'side_a_ems_current' if p1_info['side'] == 'a' else 'side_b_ems_current'
    p2_col = 'side_a_ems_current' if p2_info['side'] == 'a' else 'side_b_ems_current'

    db.execute(
        f"UPDATE campaigns SET {p1_col} = MAX(0, {p1_col} - ?) WHERE campaign_id = ?",
        (p1_ems_lost, campaign_id)
    )
    db.execute(
        f"UPDATE campaigns SET {p2_col} = MAX(0, {p2_col} - ?) WHERE campaign_id = ?",
        (p2_ems_lost, campaign_id)
    )

    # Increment faction ems_dealt (damage dealt = opponent's loss)
    if p1_info['faction']:
        db.execute("""
            UPDATE campaign_factions
            SET ems_dealt = ems_dealt + ?
            WHERE campaign_id = ? AND faction_name = ?
        """, (p2_ems_lost, campaign_id, p1_info['faction']))

    if p2_info['faction']:
        db.execute("""
            UPDATE campaign_factions
            SET ems_dealt = ems_dealt + ?
            WHERE campaign_id = ? AND faction_name = ?
        """, (p1_ems_lost, campaign_id, p2_info['faction']))