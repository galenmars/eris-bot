"""
core/shared/embeds.py
=====================
E.R.I.S. Bot — Shared Embed Builders

PURPOSE
-------
One place to build Discord embeds that appear in multiple cogs or sequences.
If a cog or sequence needs an embed that is unique to its own flow,
it builds that embed inline. This file is for embeds that are reused
or that benefit from a consistent look across different contexts.

WHAT LIVES HERE
---------------
- error_embed()         — consistent red error box
- success_embed()       — consistent green success box
- info_embed()          — neutral blue information box
- warning_embed()       — orange caution box
- commander_card()      — commander summary embed (used in view + approvals)
- ems_request_embed()   — EMS request review card (used in approval flow)
- battle_result_embed() — end-of-battle result embed (used in battle_flow)
- round_result_embed()  — per-round result embed (used in battle_flow)

WHAT DOES NOT LIVE HERE
------------------------
- Embeds used only in one place (build them there)
- Any logic — these are pure formatters, no decisions made here
- Discord API calls — these return Embed objects, not send() calls
"""

import discord
from core.shared.config import (
    COLOR_SUCCESS, COLOR_ERROR, COLOR_INFO, COLOR_WARNING,
    COLOR_BATTLE, COLOR_CAMPAIGN, COLOR_EMS,
)


# =============================================================================
# GENERIC EMBEDS
# =============================================================================

def error_embed(title: str, description: str) -> discord.Embed:
    """Red embed for errors and rejections."""
    return discord.Embed(
        title=f"❌ {title}",
        description=description,
        color=COLOR_ERROR,
    )


def success_embed(title: str, description: str) -> discord.Embed:
    """Green embed for successful actions."""
    return discord.Embed(
        title=f"✅ {title}",
        description=description,
        color=COLOR_SUCCESS,
    )


def info_embed(title: str, description: str) -> discord.Embed:
    """Blue embed for neutral information."""
    return discord.Embed(
        title=title,
        description=description,
        color=COLOR_INFO,
    )


def warning_embed(title: str, description: str) -> discord.Embed:
    """Orange embed for warnings and pending states."""
    return discord.Embed(
        title=f"⚠️ {title}",
        description=description,
        color=COLOR_WARNING,
    )


# =============================================================================
# COMMANDER CARD
# =============================================================================

def commander_card(commander: dict, member: discord.Member | None = None) -> discord.Embed:
    """
    Summary embed for a single commander.
    Used in /commander view and the commander approval flow.

    Args:
        commander: Commander record from the database.
        member:    Discord member object (optional, for mention in footer).

    Returns:
        discord.Embed ready to send.
    """
    rank  = commander.get('rank', 'Unknown').capitalize()
    force = commander.get('force_type', 'Unknown')
    tier  = commander.get('tier', '?')

    embed = discord.Embed(
        title=f"🎖️ {commander.get('commander_name', 'Unknown Commander')}",
        color=COLOR_INFO,
    )
    embed.add_field(name="Rank",     value=rank,                              inline=True)
    embed.add_field(name="Faction",  value=commander.get('faction', '?'),     inline=True)
    embed.add_field(name="Force",    value=force,                             inline=True)
    embed.add_field(name="Tier",     value=str(tier),                         inline=True)
    embed.add_field(
        name="Leadership Points",
        value=(
            f"{commander.get('current_leadership_points', '?')} / "
            f"{commander.get('max_leadership_points', '?')}"
        ),
        inline=True,
    )

    # EMS pools — only show relevant ones based on force type
    if force in ('Fleet', 'Both'):
        embed.add_field(
            name="Fleet EMS",
            value=str(commander.get('fleet_total_ems', 0) or 0),
            inline=True,
        )
    if force in ('Army', 'Both'):
        embed.add_field(
            name="Army EMS",
            value=str(commander.get('army_total_ems', 0) or 0),
            inline=True,
        )

    # Campaign status
    active_campaign = commander.get('active_campaign')
    embed.add_field(
        name="Status",
        value=f"Deployed in {active_campaign}" if active_campaign else "Available",
        inline=False,
    )

    if member:
        embed.set_footer(text=f"Player: {member.display_name}")

    return embed


# =============================================================================
# EMS REQUEST EMBED
# =============================================================================

def ems_request_embed(
    request: dict,
    commander: dict,
    member: discord.Member | None,
) -> discord.Embed:
    """
    Review card posted to #ems-requests when a player submits an EMS request.
    Used by sequences/ems_flow.py after the guided DM conversation.

    Args:
        request:   EMS request record with keys: request_id, pool, amount, reason.
        commander: Commander record being credited.
        member:    The requesting Discord member.

    Returns:
        discord.Embed ready to send to the approvals channel.
    """
    pool   = request.get('pool', '?').capitalize()
    amount = request.get('amount', 0)
    reason = request.get('reason', 'No reason given.')

    embed = discord.Embed(
        title="📋 EMS Request — Pending Review",
        color=COLOR_EMS,
    )
    embed.add_field(name="Request ID",  value=str(request.get('request_id', '?')), inline=True)
    embed.add_field(name="Player",      value=member.mention if member else "Unknown", inline=True)
    embed.add_field(name="Commander",   value=commander.get('commander_name', '?'), inline=True)
    embed.add_field(name="Pool",        value=pool,           inline=True)
    embed.add_field(name="Amount",      value=f"{amount} EMS", inline=True)
    embed.add_field(name="\u200b",      value="\u200b",        inline=True)  # spacer
    embed.add_field(name="Reason",      value=reason,          inline=False)
    embed.add_field(
        name="Actions",
        value=(
            f"`/ems approve {request.get('request_id')}` — approve\n"
            f"`/ems deny {request.get('request_id')}` — deny (will ask for reason)"
        ),
        inline=False,
    )
    return embed


# =============================================================================
# COMMANDER BLOCK REVIEW EMBED
# =============================================================================

def commander_block_review_embed(
    pending: dict,
    parsed: dict,
    member: discord.Member | None,
) -> discord.Embed:
    """
    Review card posted to #commander-approvals when a player submits an EMS block.
    Used by sequences/commander_flow.py. >> it's currently unused/reserved for NPC flow

    Args:
        pending: Pending commander record (from DB — name, rank, tier, faction, etc.)
        parsed:  Parsed block dict (from domain/commander.parse_commander_block())
        member:  The submitting Discord member.

    Returns:
        discord.Embed ready to send to the approvals channel.
    """
    embed = discord.Embed(
        title="📋 Commander Block — Pending Approval",
        color=COLOR_CAMPAIGN,
    )
    embed.add_field(name="Submission ID", value=str(pending.get('submission_id', '?')), inline=True)
    embed.add_field(name="Player",        value=member.mention if member else "Unknown", inline=True)
    embed.add_field(name="\u200b",        value="\u200b", inline=True)

    embed.add_field(name="Name",       value=parsed.get('commander_name', '?'), inline=True)
    embed.add_field(name="Rank",       value=parsed.get('rank',   '?'),         inline=True)
    embed.add_field(name="Faction",    value=parsed.get('faction','?'),         inline=True)
    embed.add_field(name="Force Type", value=parsed.get('force_type', '?'),     inline=True)
    embed.add_field(name="Tier",       value=str(parsed.get('tier', '?')),      inline=True)
    embed.add_field(name="EMS Budget", value=str(pending.get('ems_budget', '?')), inline=True)
    embed.add_field(name="EMS Submitted", value=str(parsed.get('total_ems', '?')), inline=True)

    # Ship list preview (truncated if very long)
    ships = parsed.get('ships', {})
    if ships:
        ship_lines = [
            f"• {name} ({info.get('type', '?')}) — {info.get('ems', '?')} EMS"
            for name, info in list(ships.items())[:10]
        ]
        if len(ships) > 10:
            ship_lines.append(f"*... and {len(ships) - 10} more*")
        embed.add_field(name="Fleet Composition", value="\n".join(ship_lines), inline=False)

    embed.add_field(
        name="Actions",
        value=(
            f"`/commander approve {pending.get('submission_id')}` — approve and create commander\n"
            f"`/commander deny {pending.get('submission_id')}` — deny (will ask for reason)"
        ),
        inline=False,
    )
    return embed


# =============================================================================
# BATTLE RESULT EMBED
# =============================================================================

def battle_result_embed(
    battle: dict,
    p1_ems_lost: int,
    p2_ems_lost: int,
    guild: discord.Guild,
) -> discord.Embed:
    """
    Final result embed posted at the end of a battle.
    Used by sequences/battle_flow.py._complete_battle().

    Args:
        battle:      Battle record from DB.
        p1_ems_lost: Total EMS lost by player 1 across all rounds.
        p2_ems_lost: Total EMS lost by player 2 across all rounds.
        guild:       Discord guild (for member mentions).

    Returns:
        discord.Embed ready to send to the battle channel.
    """
    wins_one = battle.get('wins_one', 0)
    wins_two = battle.get('wins_two', 0)

    if p1_ems_lost < p2_ems_lost:
        winner_name   = battle.get('hero_name',  'Commander 1')
        winner_faction= battle.get('p1_faction', '?')
        result_title  = f"🎉 {winner_faction.upper()} VICTORY"
        color         = COLOR_SUCCESS
    elif p2_ems_lost < p1_ems_lost:
        winner_name   = battle.get('hero_name2', 'Commander 2')
        winner_faction= battle.get('p2_faction', '?')
        result_title  = f"🎉 {winner_faction.upper()} VICTORY"
        color         = COLOR_SUCCESS
    else:
        result_title  = "🤝 TIE BATTLE"
        color         = COLOR_WARNING

    embed = discord.Embed(
        title=result_title,
        description=f"**{battle.get('battle_name', 'Battle')}**",
        color=color,
    )

    embed.add_field(name="Type",  value=battle.get('battle_type',  '?').capitalize(), inline=True)
    embed.add_field(name="Size",  value=battle.get('battle_size',  '?').capitalize(), inline=True)
    embed.add_field(name="Rounds", value=str(battle.get('max_rounds', '?')),          inline=True)

    embed.add_field(
        name=f"{battle.get('hero_name','?')} ({battle.get('p1_faction','?')})",
        value=(
            f"Round wins: {wins_one}\n"
            f"EMS lost: {p1_ems_lost}"
        ),
        inline=True,
    )
    embed.add_field(name="VS", value="⚔️", inline=True)
    embed.add_field(
        name=f"{battle.get('hero_name2','?')} ({battle.get('p2_faction','?')})",
        value=(
            f"Round wins: {wins_two}\n"
            f"EMS lost: {p2_ems_lost}"
        ),
        inline=True,
    )

    p1 = guild.get_member(battle.get('player_one_id'))
    p2 = guild.get_member(battle.get('player_two_id'))
    p1_str = p1.mention if p1 else f"<@{battle.get('player_one_id')}>"
    p2_str = p2.mention if p2 else f"<@{battle.get('player_two_id')}>"
    embed.set_footer(text=f"{p1_str} vs {p2_str}")

    return embed


# =============================================================================
# ROUND RESULT EMBED
# =============================================================================

def round_result_embed(
    round_num:    int,
    max_rounds:   int,
    exchange_1:   dict,
    exchange_2:   dict,
    p1_ems_lost:  int,
    p2_ems_lost:  int,
    p1_name:      str,
    p2_name:      str,
    p1_total_lost: int,
    p2_total_lost: int,
) -> discord.Embed:
    """
    Per-round summary embed posted after each round resolves.
    Used by sequences/battle_flow.py._run_round().

    Args:
        round_num:     Current round number.
        max_rounds:    Total rounds in this battle.
        exchange_1:    Result dict from domain/battle.resolve_exchange() for exchange 1.
        exchange_2:    Result dict from domain/battle.resolve_exchange() for exchange 2.
        p1_ems_lost:   EMS P1 lost this round.
        p2_ems_lost:   EMS P2 lost this round.
        p1_name:       Hero name for player 1.
        p2_name:       Hero name for player 2.
        p1_total_lost: Cumulative EMS lost by P1 across all rounds so far.
        p2_total_lost: Cumulative EMS lost by P2 across all rounds so far.

    Returns:
        discord.Embed ready to send.
    """
    # Determine round winner from EMS lost this round
    if p1_ems_lost > p2_ems_lost:
        round_summary = f"🛡️ **{p2_name} wins the round** (less EMS lost)"
    elif p2_ems_lost > p1_ems_lost:
        round_summary = f"🛡️ **{p1_name} wins the round** (less EMS lost)"
    else:
        round_summary = "🤝 **Round tied** (equal EMS lost)"

    embed = discord.Embed(
        title=f"Round {round_num} of {max_rounds} — Complete",
        description=round_summary,
        color=COLOR_BATTLE,
    )

    def _exchange_line(ex: dict, attacker: str, defender: str) -> str:
        hit    = ex.get('hit',    False)
        double = ex.get('double', False)
        atk    = ex.get('attacker_total', '?')
        dfn    = ex.get('defender_total', '?')
        if double:
            return f"💥 {attacker} **CRITS** — {defender} takes double damage"
        elif hit:
            return f"⚔️ {attacker} hits ({atk} vs {dfn})"
        else:
            return f"🛡️ {defender} blocks ({dfn} vs {atk})"

    embed.add_field(
        name=f"Exchange 1 — {p1_name} attacks",
        value=_exchange_line(exchange_1, p1_name, p2_name),
        inline=False,
    )
    embed.add_field(
        name=f"Exchange 2 — {p2_name} attacks",
        value=_exchange_line(exchange_2, p2_name, p1_name),
        inline=False,
    )
    embed.add_field(
        name="EMS This Round",
        value=(
            f"{p1_name}: -{p1_ems_lost}\n"
            f"{p2_name}: -{p2_ems_lost}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Cumulative EMS Lost",
        value=(
            f"{p1_name}: {p1_total_lost}\n"
            f"{p2_name}: {p2_total_lost}"
        ),
        inline=True,
    )

    return embed
