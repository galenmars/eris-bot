"""
core/data/db.py
===============
E.R.I.S. Bot — Database Schema Initialization

PURPOSE
-------
Defines and creates all SQLite tables on first run.
Called once at startup from bot.py after the connection is opened.
Safe to call repeatedly — all CREATE TABLE statements use IF NOT EXISTS.

TABLES
------
commanders          — commander records
commander_submissions — pending blocks awaiting approval
campaigns           — campaign records
campaign_staff      — per-campaign ownership and collaborator assignments
campaign_factions   — per-campaign faction tracking with win_progress and ems_dealt
campaign_channels   — channel → campaign bindings
campaign_board_messages — message_id → campaign for ⚔️ enrollment
commander_campaigns — enrollment records (commander ↔ campaign)
battles             — battle records
ems_history         — log of every EMS change
ems_requests        — player EMS requests pending admin approval
battle_maneuvers    — per-battle maneuver log (type, LP cost, round)
refit_requests      — post-campaign block refit requests pending admin approval
guild_config        — per-guild bot configuration
guild_config        — per-guild bot configuration

CONVENTIONS
-----------
- All IDs are INTEGER PRIMARY KEY (SQLite auto-increment)
- Timestamps use CURRENT_TIMESTAMP default (UTC)
- Soft deletes via status columns — nothing is hard-deleted
- guild_id on every multi-tenant table for future multi-guild support
"""

import sqlite3
import logging

log = logging.getLogger(__name__)

# =============================================================================
# SCHEMA
# =============================================================================

SCHEMA = '''

-- -----------------------------------------------------------------------
-- Guild configuration
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id                         INTEGER PRIMARY KEY,
    fleet_builder_url                TEXT,
    army_builder_url                 TEXT, 
    dice_string                      TEXT    DEFAULT '2d6',
    battle_record_channel_id         INTEGER,
    commander_submissions_channel_id INTEGER,
    commander_approvals_channel_id   INTEGER,
    ems_requests_channel_id          INTEGER,
    notification_channel_id          INTEGER,
    settings                         TEXT    DEFAULT '{}',  -- JSON blob for overflow
    max_mains                        INTEGER DEFAULT 1,     -- max main commanders per player
    max_seniors                      INTEGER DEFAULT 2,     -- max senior commanders per player
    max_juniors                      INTEGER DEFAULT 0,     -- 0 = unlimited
    max_total_ems                    INTEGER DEFAULT 0,     -- 0 = unlimited
    created_at                       TEXT    DEFAULT CURRENT_TIMESTAMP,
    updated_at                       TEXT    DEFAULT CURRENT_TIMESTAMP
);

-- -----------------------------------------------------------------------
-- Commanders
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS commanders (
    commander_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                   INTEGER NOT NULL,
    guild_id                  INTEGER NOT NULL,
    commander_name            TEXT    NOT NULL,
    rank                      TEXT    NOT NULL,  -- 'main', 'senior', 'junior'
    faction                   TEXT    NOT NULL,
    force_type                TEXT    NOT NULL,  -- 'Fleet', 'Army'
    tier                      INTEGER NOT NULL,  -- 1, 2, 3
    fleet_total_ems           INTEGER DEFAULT 0,
    army_total_ems            INTEGER DEFAULT 0,
    fleet_name                TEXT,              -- fleet/force name (e.g. "The Storm's Fury")
    army_name                 TEXT,              -- army/force name
    fleet_ems_block           TEXT,              -- raw EMS block string
    army_ems_block            TEXT,
    max_leadership_points     INTEGER DEFAULT 5,
    current_leadership_points INTEGER DEFAULT 5,
    status                    TEXT    DEFAULT 'active',  -- 'active', 'deployed', 'inactive'
    created_at                TEXT    DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_commanders_user_guild
    ON commanders (user_id, guild_id);

-- -----------------------------------------------------------------------
-- Commander submissions (pending blocks)
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS commander_submissions (
    submission_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    guild_id        INTEGER NOT NULL,
    commander_name  TEXT    NOT NULL,
    rank            TEXT    NOT NULL,
    faction         TEXT    NOT NULL,
    force_type      TEXT    NOT NULL,
    tier            INTEGER DEFAULT 0,
    ems_budget      INTEGER DEFAULT 0,
    fleet_name      TEXT,               -- fleet name from builder block
    army_name       TEXT,               -- army name from builder block
    submission_code TEXT    UNIQUE,     -- e.g. "A3X9KP" — used in fleet builder
    submitted_block TEXT,               -- pasted EMS block, populated by /ems submit
    claimed_by_id   INTEGER,            -- Discord user_id of approver who claimed it
    approved_by_id  INTEGER,            -- Discord user_id of approver who decided
    denial_reason   TEXT,
    status          TEXT    DEFAULT 'pending_block',  -- 'pending_block', 'pending_approval', 'approved', 'denied', 'cancelled'
    created_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
    resolved_at     TEXT
);

-- -----------------------------------------------------------------------
-- Campaigns
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id           INTEGER NOT NULL,
    campaign_name      TEXT    NOT NULL,
    campaign_type      TEXT    NOT NULL,
    owner_user_id      INTEGER,            -- Discord user_id of the Main Creator
    guild_faction      TEXT,
    enemy_faction      TEXT,
    opposing_ems          INTEGER DEFAULT 0,
    opposing_ems_total    INTEGER DEFAULT 0,
    opposing_ems_current  INTEGER DEFAULT 0,
    guild_ems_dealt    INTEGER DEFAULT 0,
    opposing_ems_dealt    INTEGER DEFAULT 0,
    side_a_ems_start   INTEGER DEFAULT 0,
    side_b_ems_start   INTEGER DEFAULT 0,
    side_a_ems_current INTEGER DEFAULT 0,
    side_b_ems_current INTEGER DEFAULT 0,
    max_commanders     INTEGER DEFAULT 0,
    enrollment_deadline TEXT,
    side_a_factions    TEXT    DEFAULT '',
    side_b_factions    TEXT    DEFAULT '',
    status             TEXT    DEFAULT 'active',
    result_note        TEXT,
    side_a_full        INTEGER DEFAULT 0,
    side_b_full        INTEGER DEFAULT 0,
    progress_thread_id INTEGER,
    campaign_end_date  TEXT,
    created_at         TEXT    DEFAULT CURRENT_TIMESTAMP,
    completed_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_campaigns_guild
    ON campaigns (guild_id, status);

-- -----------------------------------------------------------------------
-- Campaign staff (ownership and collaborators)
--
-- role = 'owner'        — the Creator who created this campaign.
--                         Can complete, delete, invite collaborators,
--                         bind channels, start, host battles, create NPCs.
-- role = 'collaborator' — another Creator invited to assist.
--                         Can start, host battles, create NPCs, bind channels.
--                         Cannot complete, delete, or invite others.
--
-- ERIS Admins bypass this table entirely — they are checked separately.
-- Collaborators must hold the @ERIS Creator Discord role; this is
-- enforced at the command layer when the row is inserted.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaign_staff (
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(campaign_id),
    user_id      INTEGER NOT NULL,
    guild_id     INTEGER NOT NULL,
    role         TEXT    NOT NULL CHECK(role IN ('owner', 'collaborator')),
    assigned_by  INTEGER NOT NULL,   -- Discord user_id of whoever added them
    assigned_at  TEXT    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (campaign_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_campaign_staff_user
    ON campaign_staff (user_id, guild_id);

-- -----------------------------------------------------------------------
-- Campaign factions
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaign_factions (
    faction_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id   INTEGER NOT NULL REFERENCES campaigns(campaign_id),
    faction_name  TEXT    NOT NULL,
    win_progress  INTEGER DEFAULT 0,   -- percentage 0–100
    ems_dealt     INTEGER DEFAULT 0   -- running EMS dealt by this faction
);

-- -----------------------------------------------------------------------
-- Campaign channels (channel → campaign binding)
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaign_channels (
    channel_id          INTEGER PRIMARY KEY,
    campaign_id         INTEGER NOT NULL REFERENCES campaigns(campaign_id),
    battle_type         TEXT    NOT NULL CHECK(battle_type IN ('space','ground')),
    results_channel_id  INTEGER
);

-- -----------------------------------------------------------------------
-- Campaign board messages (message_id → campaign, for ⚔️ reaction)
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaign_board_messages (
    message_id  INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(campaign_id),
    channel_id  INTEGER NOT NULL
);

-- -----------------------------------------------------------------------
-- Commander enrollments
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS commander_campaigns (
    enrollment_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    commander_id    INTEGER NOT NULL REFERENCES commanders(commander_id),
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(campaign_id),
    deployed_force  TEXT    NOT NULL,  -- 'Fleet', 'Army', or 'Both'
    status          TEXT    DEFAULT 'active',  -- 'active', 'unenrolled'
    enrolled_at     TEXT    DEFAULT CURRENT_TIMESTAMP,
    side            TEXT    DEFAULT 'a',       -- 'a' or 'b'
    lp_max          INTEGER DEFAULT 2,
    lp_current      INTEGER DEFAULT 2,

    UNIQUE (commander_id, campaign_id)
);

-- -----------------------------------------------------------------------
-- Battle maneuvers
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS battle_maneuvers (
    maneuver_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    battle_id     INTEGER NOT NULL REFERENCES battles(battle_id),
    commander_id  INTEGER NOT NULL REFERENCES commanders(commander_id),
    maneuver_type TEXT    NOT NULL,
    lp_cost       INTEGER NOT NULL,
    round_number  INTEGER,
    created_at    TEXT    DEFAULT CURRENT_TIMESTAMP
);

-- -----------------------------------------------------------------------
-- Battles
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS battles (
    battle_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id        INTEGER NOT NULL,
    campaign_id       INTEGER NOT NULL REFERENCES campaigns(campaign_id),
    player_one_id     INTEGER NOT NULL,
    player_two_id     INTEGER NOT NULL,
    commander_one_id  INTEGER NOT NULL REFERENCES commanders(commander_id),
    commander_two_id  INTEGER NOT NULL REFERENCES commanders(commander_id),
    battle_name       TEXT    NOT NULL,
    battle_type       TEXT    NOT NULL,   -- 'space' or 'ground'
    battle_size       TEXT    NOT NULL,   -- 'brawl', 'firefight', etc.
    max_rounds        INTEGER NOT NULL,
    current_round     INTEGER DEFAULT 0,  -- 0 = initiative, 1+ = combat
    p1_faction        TEXT    NOT NULL,
    p2_faction        TEXT    NOT NULL,
    hero_name         TEXT    NOT NULL,
    hero_name2        TEXT    NOT NULL,
    p1_tactic         TEXT,
    p2_tactic         TEXT,
    p1_fleet_type     TEXT,
    p2_fleet_type     TEXT,
    wins_one          INTEGER DEFAULT 0,
    wins_two          INTEGER DEFAULT 0,
    p1_ems_lost       INTEGER DEFAULT 0,
    p2_ems_lost       INTEGER DEFAULT 0,
    tenacious_wins_one INTEGER DEFAULT 0,
    tenacious_wins_two INTEGER DEFAULT 0,
    status            TEXT    DEFAULT 'active',  -- 'active', 'complete', 'cancelled'
    created_at        TEXT    DEFAULT CURRENT_TIMESTAMP,
    completed_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_battles_channel_status
    ON battles (channel_id, status);

CREATE INDEX IF NOT EXISTS idx_battles_players
    ON battles (player_one_id, player_two_id, status);

-- -----------------------------------------------------------------------
-- EMS history
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ems_history (
    history_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    commander_id  INTEGER NOT NULL REFERENCES commanders(commander_id),
    pool          TEXT    NOT NULL,   -- 'fleet' or 'army'
    change_amount INTEGER NOT NULL,   -- positive = add, negative = subtract
    new_total     INTEGER NOT NULL,
    reason        TEXT    NOT NULL,
    changed_by_id INTEGER NOT NULL,   -- Discord user_id of admin/system
    battle_id     INTEGER,            -- FK if this change came from a battle
    created_at    TEXT    DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ems_history_commander
    ON ems_history (commander_id, created_at DESC);

-- -----------------------------------------------------------------------
-- EMS requests
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ems_requests (
    request_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    commander_id    INTEGER NOT NULL REFERENCES commanders(commander_id),
    pool            TEXT    NOT NULL,   -- 'fleet', 'army', or 'both'
    amount          INTEGER NOT NULL,
    reason          TEXT    NOT NULL,
    status          TEXT    DEFAULT 'pending',  -- 'pending', 'approved', 'denied', 'cancelled'
    denial_reason   TEXT,
    resolved_by_id  INTEGER,
    created_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
    resolved_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_ems_requests_guild_status
    ON ems_requests (guild_id, status);

CREATE TABLE IF NOT EXISTS refit_requests (
    refit_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    commander_id    INTEGER NOT NULL REFERENCES commanders(commander_id),
    pool            TEXT    NOT NULL,   -- 'fleet', 'army', or 'both'
    old_block       TEXT    NOT NULL,   -- block before the campaign
    new_block       TEXT    NOT NULL DEFAULT '',  -- player's proposed new block
    status          TEXT    DEFAULT 'awaiting_block',  -- 'awaiting_block', 'pending_approval', 'approved', 'denied'
    denial_reason   TEXT,
    resolved_by_id  INTEGER,
    created_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
    resolved_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_refit_requests_guild_status
    ON refit_requests (guild_id, status);

'''


# =============================================================================
# INITIALIZATION FUNCTION
# =============================================================================

def initialize(db: sqlite3.Connection) -> None:
    """
    Execute the full schema against an open database connection.
    Safe to call on an existing database — IF NOT EXISTS prevents re-creation.
    """
    try:
        db.executescript(SCHEMA)
        db.commit()
        log.info("Database schema initialized.")
    except sqlite3.Error as e:
        log.error(f"Schema initialization failed: {e}", exc_info=True)
        raise


# =============================================================================
# DATABASE MANAGER
# =============================================================================

class DatabaseManager:
    """
    Wraps a SQLite connection with WAL mode, row factory, and schema init.

    Usage:
        db = DatabaseManager('commanders.db')
        # Pass db.conn to repo functions
        # Call db.close() on shutdown
    """

    def __init__(self, db_path: str = 'commanders.db'):
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row

        self.conn.execute('PRAGMA foreign_keys = ON')
        self.conn.execute('PRAGMA journal_mode = WAL')
        self.conn.execute('PRAGMA synchronous = NORMAL')

        log.info(f"Database connection opened: {db_path}")
        log.info("WAL mode active — concurrent reads will not block on writes")

        self._initialize_schema()

    def _initialize_schema(self) -> None:
        """Create all tables if they don't exist yet."""
        initialize(self.conn)

    def get_cursor(self):
        """Return a cursor from the shared connection."""
        return self.conn.cursor()

    def close(self) -> None:
        """Close the database connection cleanly."""
        try:
            self.conn.close()
            log.info("Database connection closed.")
        except sqlite3.Error as e:
            log.error(f"Error closing database: {e}", exc_info=True)
