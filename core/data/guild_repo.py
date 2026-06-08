"""
core/data/guild_repo.py
=======================
E.R.I.S. Bot — Guild Configuration Database Operations

PURPOSE
-------
All reads and writes for per-guild settings: channel IDs, role IDs,
and other server-specific configuration values.
Setup_cog writes here. Every other cog reads from here.
No Discord objects. No domain logic. Plain dicts in and out.

SCHEMA NOTE
-----------
guild_config stores settings as a JSON blob in a 'settings' column
alongside fixed columns for commonly-accessed IDs.
This allows adding new config keys without schema migrations.
"""

from __future__ import annotations
import json
import sqlite3
import logging

log = logging.getLogger(__name__)


# =============================================================================
# READ
# =============================================================================

def get_guild_config(
    db: sqlite3.Connection, guild_id: int
) -> dict | None:
    """
    Return the full guild config as a dict, or None if not configured.

    The returned dict merges the fixed columns with the JSON settings blob,
    so callers can use config['channels']['battle-record'] without caring
    whether it's stored as a column or in the JSON.
    """
    row = db.execute(
        'SELECT * FROM guild_config WHERE guild_id = ?',
        (guild_id,),
    ).fetchone()
    if not row:
        return None

    config = dict(row)

    # Merge the JSON settings blob into the top-level dict
    settings_raw = config.pop('settings', '{}') or '{}'
    try:
        settings = json.loads(settings_raw)
    except json.JSONDecodeError:
        settings = {}

    config.update(settings)
    return config


def is_guild_configured(db: sqlite3.Connection, guild_id: int) -> bool:
    """Return True if this guild has been set up."""
    row = db.execute(
        'SELECT guild_id FROM guild_config WHERE guild_id = ?',
        (guild_id,),
    ).fetchone()
    return row is not None


# =============================================================================
# WRITE
# =============================================================================

def create_guild_config(
    db: sqlite3.Connection,
    guild_id: int,
    channels: dict,     # {channel_name: channel_id}
    roles: dict,        # {role_name: role_id}
) -> None:
    """
    Write initial guild config after /setup completes.
    Called once per guild by setup_cog.py.
    """
    settings = {'channels': channels, 'roles': roles}
    db.execute(
        '''
        INSERT OR REPLACE INTO guild_config
            (guild_id, settings, max_mains, max_seniors, max_juniors,
             battle_record_channel_id,
             commander_submissions_channel_id,
             commander_approvals_channel_id,
             ems_requests_channel_id,
             notification_channel_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            guild_id, json.dumps(settings), 1, 2, 0,
            None,
            channels.get('commander-submissions'),
            channels.get('commander-approvals'),
            channels.get('ems-requests'),
            channels.get('eris-notifications'),
        ),
    )
    db.commit()

def set_config_value(
    db: sqlite3.Connection,
    guild_id: int,
    db_key: str,
    value: str,
) -> None:
    """
    Set a single config value.
    The db_key comes from the admin_cog's VALID_CONFIG_KEYS allowlist —
    never from raw user input.

    If the key maps to a known column, update that column directly.
    Otherwise, write to the JSON settings blob.
    """
    # Known top-level columns (fast path)
    COLUMN_KEYS = {
        'max_mains',
        'max_seniors',
        'max_juniors',
        'fleet_builder_url',
        'dice_string',
        'battle_record_channel_id',
        'commander_submissions_channel_id',
        'commander_approvals_channel_id',
        'ems_requests_channel_id',
        'notification_channel_id',
    }

    # Ensure the row exists
    db.execute(
        'INSERT OR IGNORE INTO guild_config (guild_id, settings) VALUES (?, ?)',
        (guild_id, '{}'),
    )

    if db_key in COLUMN_KEYS:
        db.execute(
            f'UPDATE guild_config SET {db_key} = ? WHERE guild_id = ?',
            (value, guild_id),
        )
    else:
        # Write into the JSON blob
        row = db.execute(
            'SELECT settings FROM guild_config WHERE guild_id = ?',
            (guild_id,),
        ).fetchone()
        settings = json.loads(row['settings'] or '{}') if row else {}
        settings[db_key] = value
        db.execute(
            'UPDATE guild_config SET settings = ? WHERE guild_id = ?',
            (json.dumps(settings), guild_id),
        )

    db.commit()


def update_channels(
    db: sqlite3.Connection,
    guild_id: int,
    channels: dict,   # {channel_name: channel_id}
) -> None:
    """Merge updated channel IDs into the settings blob."""
    row = db.execute(
        'SELECT settings FROM guild_config WHERE guild_id = ?',
        (guild_id,),
    ).fetchone()
    settings = json.loads(row['settings'] or '{}') if row else {}
    settings.setdefault('channels', {}).update(channels)
    db.execute(
        'UPDATE guild_config SET settings = ? WHERE guild_id = ?',
        (json.dumps(settings), guild_id),
    )
    db.commit()


def update_roles(
    db: sqlite3.Connection,
    guild_id: int,
    roles: dict,   # {role_name: role_id}
) -> None:
    """Merge updated role IDs into the settings blob."""
    row = db.execute(
        'SELECT settings FROM guild_config WHERE guild_id = ?',
        (guild_id,),
    ).fetchone()
    settings = json.loads(row['settings'] or '{}') if row else {}
    settings.setdefault('roles', {}).update(roles)
    db.execute(
        'UPDATE guild_config SET settings = ? WHERE guild_id = ?',
        (json.dumps(settings), guild_id),
    )
    db.commit()


def clear_guild_config(db: sqlite3.Connection, guild_id: int) -> None:
    """
    Remove guild config (used by /setup reset).
    Does NOT delete channels or roles in Discord — only clears the bot's record.
    """
    db.execute('DELETE FROM guild_config WHERE guild_id = ?', (guild_id,))
    db.commit()


def ping(db: sqlite3.Connection) -> None:
    """Verify the database connection is alive. Raises on failure."""
    db.execute('SELECT 1')


# =============================================================================
# ORGANIZATIONS
# =============================================================================

def get_organizations(db: sqlite3.Connection, guild_id: int) -> list[dict]:
    """
    Return the list of registered organizations for this guild.

    Each org dict has:
        name         (str)  — display name, e.g. "The Red Veil"
        faction_id   (str)  — theme faction id, e.g. "sith_empire"
        faction_name (str)  — theme faction display name, e.g. "Sith Empire"
        group        (str)  — alignment group key, e.g. "empire"

    Returns an empty list if none are registered.
    """
    row = db.execute(
        'SELECT settings FROM guild_config WHERE guild_id = ?',
        (guild_id,),
    ).fetchone()
    if not row:
        return []
    settings = json.loads(row['settings'] or '{}')
    return settings.get('organizations', [])


def add_organization(
    db: sqlite3.Connection,
    guild_id: int,
    name: str,
    faction_id: str,
    faction_name: str,
    group: str,
) -> bool:
    """
    Add an organization to the guild's list.

    Returns False (and does not write) if an org with the same name
    already exists (case-insensitive). Returns True on success.
    """
    row = db.execute(
        'SELECT settings FROM guild_config WHERE guild_id = ?',
        (guild_id,),
    ).fetchone()
    if not row:
        return False

    settings = json.loads(row['settings'] or '{}')
    orgs = settings.get('organizations', [])

    # Duplicate check
    if any(o['name'].lower() == name.lower() for o in orgs):
        return False

    orgs.append({
        'name':         name,
        'faction_id':   faction_id,
        'faction_name': faction_name,
        'group':        group,
    })
    settings['organizations'] = orgs
    db.execute(
        'UPDATE guild_config SET settings = ? WHERE guild_id = ?',
        (json.dumps(settings), guild_id),
    )
    db.commit()
    return True


def remove_organization(
    db: sqlite3.Connection,
    guild_id: int,
    name: str,
) -> bool:
    """
    Remove an organization by name (case-insensitive).

    Returns True if removed, False if not found.
    """
    row = db.execute(
        'SELECT settings FROM guild_config WHERE guild_id = ?',
        (guild_id,),
    ).fetchone()
    if not row:
        return False

    settings = json.loads(row['settings'] or '{}')
    orgs = settings.get('organizations', [])
    new_orgs = [o for o in orgs if o['name'].lower() != name.lower()]

    if len(new_orgs) == len(orgs):
        return False  # nothing removed

    settings['organizations'] = new_orgs
    db.execute(
        'UPDATE guild_config SET settings = ? WHERE guild_id = ?',
        (json.dumps(settings), guild_id),
    )
    db.commit()
    return True

