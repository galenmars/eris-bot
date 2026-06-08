"""
sequences/battle_flow.py
========================
E.R.I.S. Bot — Battle Flow

PURPOSE
-------
Owns the full lifecycle of a battle from engagement to completion.
Called by battle_cog.py; delegates math to core/math/ and DB ops to
core/data/battle_repo.py.

ARCHITECTURE
------------
- engage() is the single entry point — called by battle_cog.py
- db is raw sqlite3.Connection throughout (no ORM)
- Initiative: 1d100 flat, no bonuses
- Roll display: "🎲 CommanderName — 17" only
- Turn prompts via DM before each roll
- Fleet/army type auto-detected from EMS block BATTLE_OPTIONS
- Tenacity: 2 extension rounds, loser declares (5 LP cost), winner accepts
- Results posted to results_channel via campaign_channels binding
- Roll trigger messages sent by the bot; checks match on content not author

FUNCTIONS
---------
engage()                   — entry point; validates, sets up, runs the battle loop
_collect_tactical_choices()— DMs both players for tactic and fleet/army type
_run_initiative()          — 1d100 roll-off to determine who attacks first
_run_round()               — orchestrates one full round (two exchanges)
_resolve_exchange()        — resolves a single attack/defense exchange
_check_tenacity()          — handles post-battle tenacity declaration and extension
_complete_battle()         — writes result, applies EMS losses, posts to results channel
_try_dm()                  — helper; sends a DM silently ignoring Forbidden errors

WHAT THIS FILE DOES NOT OWN
----------------------------
Dice math and matchup bonuses  → core/math/dice.py
Space round resolution         → core/math/space_combat.py
Ground round resolution        → core/math/ground_combat.py
EMS loss tables                → core/math/ems_tables.py
Battle DB reads/writes         → core/data/battle_repo.py
EMS DB reads/writes            → core/data/ems_repo.py
Slash command definitions      → cogs/battle_cog.py
"""

import asyncio
import logging
import random
import re
import sqlite3


import discord

from core.data   import battle_repo, campaign_repo, ems_repo
from core.domain import battle as battle_domain
from core.domain.exceptions import DomainError
from core.math   import dice, ems_tables

log = logging.getLogger(__name__)

# =============================================================================
# CONSTANTS
# =============================================================================

ROLL_TIMEOUT     = 300
TACTIC_TIMEOUT   = 300
TENACITY_TIMEOUT = 120
TENACITY_ROUNDS  = 2
TENACITY_LP_COST = 5
DICE_COMBAT      = '1d20'

TACTIC_MAP = {
    '1': 'cautious',
    '2': 'defensive',
    '3': 'aggressive',
    '4': 'risky',
}

FLEET_MAP = {
    '1': 'balanced',
    '2': 'carrier',
    '3': 'combat',
    '4': 'picket',
}

ARMY_MAP = {
    '1': 'combined_arms',
    '2': 'infantry',
    '3': 'mechanized',
    '4': 'heavy_armor',
}


# =============================================================================
# ENTRY POINT
# =============================================================================

async def engage(
    bot:         discord.Client,
    interaction: discord.Interaction,
    p1:          discord.Member,
    p2:          discord.Member,
    channel:     discord.TextChannel,
    guild:       discord.Guild,
    db:          sqlite3.Connection,
    theme:       dict,
    binding:     dict,
) -> None:
    campaign_id   = binding['campaign_id']
    campaign_name = binding['campaign_name']
    battle_type   = binding['battle_type']

    # Validate enrolled commanders
    # The channel binding defines battle_type (space/ground).
    # Auto-select the commander matching that force type.
    # A/B side is inferred from whoever the opponent is on the other side.

    p1_enrolled = campaign_repo.get_enrolled_commanders_for_user(db, p1.id, campaign_id)
    p2_enrolled = campaign_repo.get_enrolled_commanders_for_user(db, p2.id, campaign_id)

    if not p1_enrolled:
        await channel.send(f"❌ {p1.mention} has no commander enrolled in **{campaign_name}**.")
        return
    if not p2_enrolled:
        await channel.send(f"❌ {p2.mention} has no commander enrolled in **{campaign_name}**.")
        return

    # Filter to commanders matching this channel's battle type
    force_filter = 'Fleet' if battle_type == 'space' else 'Army'
    p1_filtered  = [c for c in p1_enrolled if c.get('deployed_force') == force_filter]
    p2_filtered  = [c for c in p2_enrolled if c.get('deployed_force') == force_filter]

    # Fallback to all enrolled if no match (shouldn't happen in normal play)
    if not p1_filtered:
        await channel.send(
            f"❌ {p1.mention} has no **{force_filter}** commander enrolled in **{campaign_name}**.\n"
            f"This is a **{battle_type}** battle channel."
        )
        return
    if not p2_filtered:
        await channel.send(
            f"❌ {p2.mention} has no **{force_filter}** commander enrolled in **{campaign_name}**.\n"
            f"This is a **{battle_type}** battle channel."
        )
        return

    p1_commander = p1_filtered[0]
    p2_commander = None

    # Auto-resolve p2 to the opposite side
    opposite = 'b' if p1_commander['side'] == 'a' else 'a'
    p2_sides = {e['side']: e for e in p2_filtered}
    p2_commander = p2_sides.get(opposite)

    if not p2_commander:
        await channel.send(
            f"❌ {p2.mention} has no **{force_filter}** commander on the opposing side "
            f"to **{p1_commander['commander_name']}**."
        )
        return

    if p1_commander.get('side') == p2_commander.get('side'):
        await channel.send(
            f"❌ Both commanders are on the same side (**Side {p1_commander.get('side', '?').upper()}**). "
            f"You can only battle commanders on the opposing side."
        )
        return

    # Battle size
    def channel_check(m):
        return m.author == p1 and m.channel == channel

    await channel.send(
        "**Battle Size:**\n"
        "```\n"
        "1. Brawl\n"
        "2. Firefight\n"
        "3. Skirmish\n"
        "4. Engagement\n"
        "5. Battleground\n"
        "```"
    )

    size_map = {
        '1': 'brawl',
        '2': 'firefight',
        '3': 'skirmish',
        '4': 'engagement',
        '5': 'battleground',
    }

    try:
        size_msg = await bot.wait_for('message', check=channel_check, timeout=120)
    except asyncio.TimeoutError:
        await channel.send("❌ Battle setup timed out.")
        return

    battle_size = size_map.get(size_msg.content.strip(), size_msg.content.strip().lower())

    try:
        battle_domain.validate_battle_size(battle_size)
    except DomainError as e:
        await channel.send(f"❌ {e}")
        return

    # Round count
    await channel.send("**Number of rounds:**\n```\nChoose: 3, 5, 7, or 9\n```")

    try:
        rounds_msg = await bot.wait_for('message', check=channel_check, timeout=120)
    except asyncio.TimeoutError:
        await channel.send("❌ Battle setup timed out.")
        return

    try:
        max_rounds = int(rounds_msg.content.strip())
        battle_domain.validate_round_count(max_rounds)
    except (ValueError, DomainError) as e:
        await channel.send(f"❌ {e}")
        return

    # Tactical choices via parallel DMs
    await channel.send(
        f"🔒 **Tactical Setup**\n"
        f"{p1.mention} and {p2.mention} — check your DMs!\n"
        f"⏳ Waiting for both commanders..."
    )

    p1_choices, p2_choices = await _collect_tactical_choices(
        bot, channel, p1, p2, battle_type, p1_commander, p2_commander
    )

    if p1_choices is None or p2_choices is None:
        await channel.send("❌ Tactical setup timed out. Battle cancelled.")
        return

    # Write battle record
    battle_id = battle_repo.create_battle(
        db,
        channel_id       = channel.id,
        campaign_id      = campaign_id,
        player_one_id    = p1.id,
        player_two_id    = p2.id,
        commander_one_id = p1_commander['commander_id'],
        commander_two_id = p2_commander['commander_id'],
        battle_name      = binding.get('territory_name', channel.name),
        battle_type      = battle_type,
        battle_size      = battle_size,
        max_rounds       = max_rounds,
        p1_faction       = p1_commander['faction'],
        p2_faction       = p2_commander['faction'],
        hero_name        = p1_commander['commander_name'],
        hero_name2       = p2_commander['commander_name'],
        p1_tactic        = p1_choices['tactic'],
        p2_tactic        = p2_choices['tactic'],
        p1_fleet_type    = p1_choices['fleet_type'],
        p2_fleet_type    = p2_choices['fleet_type'],
    )

    await channel.send(
        f"✅ **Battle Engaged!**\n\n"
        f"⚔️ **{p1_commander['commander_name']}** ({p1_commander['faction']}) "
        f"vs **{p2_commander['commander_name']}** ({p2_commander['faction']})\n"
        f"📋 {battle_size.capitalize()} · {max_rounds} rounds · {battle_type.upper()}\n\n"
        f"Roll for initiative!"
    )

    # Initiative
    initiative_winner = await _run_initiative(
        bot, channel, p1, p2, p1_commander, p2_commander
    )

    if initiative_winner is None:
        await channel.send("❌ Initiative timed out. Battle cancelled.")
        battle_repo.cancel_battle(db, battle_id)
        return

    # Pre-calculate matchup bonuses
    p1_matchup = battle_domain.get_matchup_bonus(
        p1_choices['fleet_type'], p2_choices['fleet_type'], battle_type, theme
    )
    p2_matchup = battle_domain.get_matchup_bonus(
        p2_choices['fleet_type'], p1_choices['fleet_type'], battle_type, theme
    )

    # Combat rounds
    p1_total_ems_lost    = 0
    p2_total_ems_lost    = 0
    p1_round_wins        = 0
    p2_round_wins        = 0
    current_round        = 1
    tenacity_penalty_for = None

    while not battle_domain.is_battle_over(current_round - 1, max_rounds):
        round_result = await _run_round(
            bot                  = bot,
            channel              = channel,
            p1                   = p1,
            p2                   = p2,
            p1_commander         = p1_commander,
            p2_commander         = p2_commander,
            p1_tactic            = p1_choices['tactic'],
            p2_tactic            = p2_choices['tactic'],
            p1_matchup           = p1_matchup,
            p2_matchup           = p2_matchup,
            initiative_winner    = initiative_winner,
            round_number         = current_round,
            battle_size          = battle_size,
            theme                = theme,
            tenacity_penalty_for = tenacity_penalty_for,
        )

        tenacity_penalty_for = None

        if round_result is None:
            await channel.send("❌ Battle timed out. Battle cancelled.")
            battle_repo.cancel_battle(db, battle_id)
            return

        p1_total_ems_lost += round_result['p1_ems_lost']
        p2_total_ems_lost += round_result['p2_ems_lost']

        round_winner = battle_domain.determine_round_winner(
            round_result['p1_ems_lost'],
            round_result['p2_ems_lost'],
        )

        if round_winner == 'p1':
            p1_round_wins += 1
        elif round_winner == 'p2':
            p2_round_wins += 1

        winner_text = (
            f"🏆 **{p1_commander['commander_name']} wins round {current_round}!**"
            if round_winner == 'p1' else
            f"🏆 **{p2_commander['commander_name']} wins round {current_round}!**"
            if round_winner == 'p2' else
            f"🤝 **Round {current_round}: No decisive winner.**"
        )

        await channel.send(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{winner_text}\n"
            f"📊 **Round EMS:** "
            f"{p1_commander['commander_name']} -{round_result['p1_ems_lost']} · "
            f"{p2_commander['commander_name']} -{round_result['p2_ems_lost']}\n"
            f"📊 **Running total:** "
            f"{p1_commander['commander_name']} -{p1_total_ems_lost} · "
            f"{p2_commander['commander_name']} -{p2_total_ems_lost}\n"
            f"🏅 **Round wins:** {p1_round_wins} – {p2_round_wins}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        battle_repo.update_round(
            db,
            battle_id     = battle_id,
            current_round = current_round,
            wins_one      = p1_round_wins,
            wins_two      = p2_round_wins,
        )

        current_round += 1

    # Tenaciousness check
    result = battle_domain.determine_battle_result(p1_round_wins, p2_round_wins)
    tenacity_fired = False

    if result != 'tie':
        loser       = p1 if result == 'p2' else p2
        winner      = p2 if result == 'p2' else p1
        loser_cmd   = p1_commander if result == 'p2' else p2_commander
        loser_is_p1 = (result == 'p2')

        tenacity_accepted = await _check_tenacity(
            bot, loser, winner, loser_cmd, db, campaign_id
        )

        if tenacity_accepted:
            tenacity_fired       = True
            tenacity_penalty_for = 'p1' if loser_is_p1 else 'p2'

            for extra in range(TENACITY_ROUNDS):
                round_result = await _run_round(
                    bot                  = bot,
                    channel              = channel,
                    p1                   = p1,
                    p2                   = p2,
                    p1_commander         = p1_commander,
                    p2_commander         = p2_commander,
                    p1_tactic            = p1_choices['tactic'],
                    p2_tactic            = p2_choices['tactic'],
                    p1_matchup           = p1_matchup,
                    p2_matchup           = p2_matchup,
                    initiative_winner    = initiative_winner,
                    round_number         = current_round,
                    battle_size          = battle_size,
                    theme                = theme,
                    tenacity_penalty_for = tenacity_penalty_for,
                )
                tenacity_penalty_for = None

                if round_result is None:
                    break

                p1_total_ems_lost += round_result['p1_ems_lost']
                p2_total_ems_lost += round_result['p2_ems_lost']

                rw = battle_domain.determine_round_winner(
                    round_result['p1_ems_lost'], round_result['p2_ems_lost']
                )
                if rw == 'p1':
                    p1_round_wins += 1
                elif rw == 'p2':
                    p2_round_wins += 1

                await channel.send(
                    f"⚔️ **Tenacity Round {extra + 1}**\n"
                    f"📊 {p1_commander['commander_name']} -{round_result['p1_ems_lost']} · "
                    f"{p2_commander['commander_name']} -{round_result['p2_ems_lost']}\n"
                    f"🏅 **Round wins:** {p1_round_wins} – {p2_round_wins}"
                )
                current_round += 1

            result = battle_domain.determine_battle_result(p1_round_wins, p2_round_wins)

    # Complete the battle
    await _complete_battle(
        bot               = bot,
        channel           = channel,
        guild             = guild,
        db                = db,
        battle_id         = battle_id,
        campaign_id       = campaign_id,
        p1                = p1,
        p2                = p2,
        p1_commander      = p1_commander,
        p2_commander      = p2_commander,
        result            = result,
        p1_total_ems_lost = p1_total_ems_lost,
        p2_total_ems_lost = p2_total_ems_lost,
        p1_round_wins     = p1_round_wins,
        p2_round_wins     = p2_round_wins,
        battle_size       = battle_size,
        battle_type       = battle_type,
        tenacity_fired    = tenacity_fired,
        theme             = theme,
    )


# =============================================================================
# TACTICAL CHOICE COLLECTION
# =============================================================================

async def _collect_tactical_choices(
    bot:          discord.Client,
    channel:      discord.TextChannel,
    p1:           discord.Member,
    p2:           discord.Member,
    battle_type:  str,
    p1_commander: dict,
    p2_commander: dict,
) -> tuple:
    fleet_map      = FLEET_MAP if battle_type == 'space' else ARMY_MAP
    tactic_options = '\n'.join(f"{k}. {v.capitalize()}" for k, v in TACTIC_MAP.items())

    async def collect_for_player(player: discord.Member, commander: dict) -> dict | None:
        def dm_check(m):
            return m.author.id == player.id and isinstance(m.channel, discord.DMChannel)

        lp_current = commander.get('lp_current', 0)
        lp_max     = commander.get('lp_max', 0)

        try:
            await player.send(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚔️ **NEW BATTLE — Tactical Setup**\n"
                f"**{commander['commander_name']}**\n\n"
                f"LP: {lp_current}/{lp_max}\n\n"
                f"**Choose your tactic:**\n"
                f"```\n{tactic_options}\n```"
            )
        except discord.Forbidden:
            await channel.send(f"❌ {player.mention} — I can't DM you. Enable DMs and try again.")
            return None

        tactic_id = None
        while tactic_id is None:
            try:
                msg = await bot.wait_for('message', check=dm_check, timeout=TACTIC_TIMEOUT)
            except asyncio.TimeoutError:
                await _try_dm(player, "⏲️ Timed out. Battle cancelled.")
                return None

            choice = msg.content.strip()
            if choice in TACTIC_MAP:
                tactic_id = TACTIC_MAP[choice]
                await _try_dm(player, "✅ Tactic set.")
            else:
                await _try_dm(player, "❌ Invalid choice. Reply with 1, 2, 3, or 4.")

        # Auto-detect fleet/army type from EMS block
        block_key      = 'fleet_ems_block' if battle_type == 'space' else 'army_ems_block'
        raw_block      = commander.get(block_key, '') or ''
        battle_options = []

        # Try BATTLE_OPTIONS first — stops at next ALL_CAPS_KEY: token
        match = re.search(
            r'BATTLE_OPTIONS:\s*([\w,\s]+?)(?=\s+[A-Z_]{3,}:|$)',
            raw_block, re.IGNORECASE
        )
        log.debug(f"EMS block for {commander.get('commander_name')}: {raw_block[:100]}")
        log.debug(f"BATTLE_OPTIONS match: {match.group(1) if match else 'None'}")
        if match:
            opts = match.group(1).strip().split(',')
            battle_options = [o.strip().split()[0].lower() for o in opts]

        # Fallback: use ARMY_TYPE or FLEET_TYPE if BATTLE_OPTIONS parsing yielded nothing
        if not battle_options:
            type_key = 'FLEET_TYPE' if battle_type == 'space' else 'ARMY_TYPE'
            type_match = re.search(rf'{type_key}:\s*(\w[\w\s]*?)(?=\s+[A-Z_]{{3,}}:|$)', raw_block, re.IGNORECASE)
            if type_match:
                battle_options = [type_match.group(1).strip().lower().replace(' ', '_')]
                log.debug(f"Fell back to {type_key}: {battle_options}")

        available = {k: v for k, v in fleet_map.items() if v in battle_options}

        # Auto-select by dominant COMPOSITION percentage
        fleet_type = None
        comp_match = re.search(
            r'COMPOSITION:\s*(.*?)(?=UNITS:|SHIPS:|===\s*END)',
            raw_block, re.IGNORECASE | re.DOTALL
        )
        if comp_match:
            percentages = re.findall(r'(\w[\w\s]*?):\s*(\d+)%', comp_match.group(1))
            if percentages:
                # Filter to only types that are valid options for this commander
                valid_types = set(available.values()) if available else set(fleet_map.values())
                valid_pcts  = [(name.strip().lower().replace(' ', '_'), int(pct))
                               for name, pct in percentages
                               if name.strip().lower().replace(' ', '_') in valid_types]
                if valid_pcts:
                    fleet_type = max(valid_pcts, key=lambda x: x[1])[0]
                    log.debug(f"Auto-selected force type by composition: {fleet_type}")

        # Final fallback: single available option or first in map
        if not fleet_type:
            fleet_type = list(available.values())[0] if available else list(fleet_map.values())[0]

        await _try_dm(
            player,
            f"✅ Force type auto-selected: **{fleet_type.replace('_', ' ').capitalize()}**\n\n"
            f"🎯 Setup complete! Return to the battle channel.\n"
            f"🎲 Roll for initiative! Use `/battle roll` in the battle channel."
        )

        await channel.send(f"✅ **{player.display_name}** has submitted their tactical choices.")
        return {'tactic': tactic_id, 'fleet_type': fleet_type}

    p1_choices, p2_choices = await asyncio.gather(
        collect_for_player(p1, p1_commander),
        collect_for_player(p2, p2_commander),
    )

    return p1_choices, p2_choices


# =============================================================================
# INITIATIVE
# =============================================================================

async def _run_initiative(
    bot:          discord.Client,
    channel:      discord.TextChannel,
    p1:           discord.Member,
    p2:           discord.Member,
    p1_commander: dict,
    p2_commander: dict,
) -> str | None:
    await channel.send(
        "⚔️ **INITIATIVE PHASE**\n"
        "Both commanders roll! Flat 1d100 — no bonuses.\n"
        "Use `/battle roll`"
    )

    await _try_dm(p1, "🎲 Roll for initiative! Use `/battle roll` in the battle channel.")
    await _try_dm(p2, "🎲 Roll for initiative! Use `/battle roll` in the battle channel.")

    p1_score = None
    p2_score = None

    def roll_check(m):
        return (
            m.channel == channel and
            m.content.strip().startswith('__ERIS_ROLL__') and
            any(str(uid) in m.content for uid in (p1.id, p2.id))
        )

    while p1_score is None or p2_score is None:
        try:
            msg = await bot.wait_for('message', check=roll_check, timeout=ROLL_TIMEOUT)
        except asyncio.TimeoutError:
            return None

        roll        = random.randint(1, 100)
        msg_user_id = int(msg.content.strip().replace('__ERIS_ROLL__', ''))

        if msg_user_id == p1.id and p1_score is None:
            p1_score = roll
            await channel.send(f"🎲 {p1_commander['commander_name']} — **{roll}**")
        elif msg_user_id == p2.id and p2_score is None:
            p2_score = roll
            await channel.send(f"🎲 {p2_commander['commander_name']} — **{roll}**")

    result = battle_domain.determine_initiative_winner(p1_score, p2_score)

    if result == 'tie':
        await channel.send("🪙 **Tie! The fates decide...**")
        result = random.choice(('p1', 'p2'))

    winner_cmd = p1_commander if result == 'p1' else p2_commander
    winner_m   = p1 if result == 'p1' else p2

    await channel.send(
        f"✅ **{winner_cmd['commander_name']} wins initiative!**\n"
        f"{winner_m.mention} leads Exchange 1 in Round 1."
    )

    return result


# =============================================================================
# COMBAT ROUND
# =============================================================================

async def _run_round(
    bot:                  discord.Client,
    channel:              discord.TextChannel,
    p1:                   discord.Member,
    p2:                   discord.Member,
    p1_commander:         dict,
    p2_commander:         dict,
    p1_tactic:            str,
    p2_tactic:            str,
    p1_matchup:           int,
    p2_matchup:           int,
    initiative_winner:    str,
    round_number:         int,
    battle_size:          str,
    theme:                dict,
    tenacity_penalty_for: str | None = None,
) -> dict | None:
    first_attacker = battle_domain.initiative_attacker(round_number, initiative_winner)

    attacker    = p1 if first_attacker == 'p1' else p2
    defender    = p2 if first_attacker == 'p1' else p1
    att_cmd     = p1_commander if first_attacker == 'p1' else p2_commander
    def_cmd     = p2_commander if first_attacker == 'p1' else p1_commander
    att_tactic  = p1_tactic   if first_attacker == 'p1' else p2_tactic
    def_tactic  = p2_tactic   if first_attacker == 'p1' else p1_tactic
    att_matchup = p1_matchup  if first_attacker == 'p1' else p2_matchup
    def_matchup = p2_matchup  if first_attacker == 'p1' else p1_matchup

    def_side    = 'p2' if first_attacker == 'p1' else 'p1'
    att_penalty = -2 if tenacity_penalty_for == first_attacker else 0
    def_penalty = -2 if tenacity_penalty_for == def_side else 0

    await channel.send(
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚔️ **ROUND {round_number}**\n\n"
        f"**Exchange 1:** {attacker.mention} attacks · {defender.mention} defends\n"
        f"Use `/battle roll`"
    )

    exchange_1 = await _resolve_exchange(
        bot         = bot,
        channel     = channel,
        attacker    = attacker,
        defender    = defender,
        att_cmd     = att_cmd,
        def_cmd     = def_cmd,
        att_tactic  = att_tactic,
        def_tactic  = def_tactic,
        att_matchup = att_matchup,
        def_matchup = def_matchup,
        att_penalty = att_penalty,
        def_penalty = def_penalty,
        theme       = theme,
    )

    if exchange_1 is None:
        return None

    await channel.send(
        f"**Exchange 2:** {defender.mention} attacks · {attacker.mention} defends\n"
        f"Use `/battle roll`"
    )


    exchange_2 = await _resolve_exchange(
        bot         = bot,
        channel     = channel,
        attacker    = defender,
        defender    = attacker,
        att_cmd     = def_cmd,
        def_cmd     = att_cmd,
        att_tactic  = def_tactic,
        def_tactic  = att_tactic,
        att_matchup = def_matchup,
        def_matchup = att_matchup,
        att_penalty = 0,
        def_penalty = 0,
        theme       = theme,
    )

    if exchange_2 is None:
        return None

    if first_attacker == 'p1':
        round_ems = battle_domain.calculate_round_ems(exchange_1, exchange_2, battle_size)
    else:
        round_ems = battle_domain.calculate_round_ems(exchange_2, exchange_1, battle_size)

    return {'p1_ems_lost': round_ems['p1_lost'], 'p2_ems_lost': round_ems['p2_lost']}


# =============================================================================
# EXCHANGE RESOLUTION
# =============================================================================

async def _resolve_exchange(
        bot: discord.Client,
        channel: discord.TextChannel,
        attacker: discord.Member,
        defender: discord.Member,
        att_cmd: dict,
        def_cmd: dict,
        att_tactic: str,
        def_tactic: str,
        att_matchup: int,
        def_matchup: int,
        att_penalty: int,
        def_penalty: int,
        theme: dict,
) -> dict | None:
    def att_check(m):
        return (
            m.channel == channel and
            m.content.strip() == f'__ERIS_ROLL__{attacker.id}'
        )

    def def_check(m):
        return (
            m.channel == channel and
            m.content.strip() == f'__ERIS_ROLL__{defender.id}'
        )

    # Attacker rolls
    try:
        await bot.wait_for('message', check=att_check, timeout=ROLL_TIMEOUT)
    except asyncio.TimeoutError:
        return None

    att_roll   = dice.roll_dice(DICE_COMBAT)
    att_raw    = att_roll['rolls'][0]
    att_result = dice.calculate_round_total(DICE_COMBAT, att_tactic, att_matchup, theme)
    att_total  = att_result['total'] + att_penalty

    await channel.send(f"🎲 {att_cmd['commander_name']} — **{att_total}**")

    if battle_domain.is_nat20(att_raw):
        await channel.send(f"🔥 **Critical hit!** {att_cmd['commander_name']} — auto-hit, double damage!")
        return {'hit': True, 'double': True}

    if battle_domain.is_nat1(att_raw):
        await channel.send(f"💨 **Critical miss!** {att_cmd['commander_name']} — auto-miss!")
        return {'hit': False, 'double': False}

    try:
        await bot.wait_for('message', check=def_check, timeout=ROLL_TIMEOUT)
    except asyncio.TimeoutError:
        return None

    def_roll   = dice.roll_dice(DICE_COMBAT)
    def_raw    = def_roll['rolls'][0]
    def_result = dice.calculate_round_total(DICE_COMBAT, def_tactic, def_matchup, theme)
    def_total  = def_result['total'] + def_penalty

    await channel.send(f"🎲 {def_cmd['commander_name']} — **{def_total}**")

    result = battle_domain.resolve_exchange(att_raw, att_total, def_raw, def_total)

    if battle_domain.is_nat20(def_raw):
        await channel.send(f"🛡️ **Perfect defense!** {def_cmd['commander_name']} — attack blocked!")
    elif battle_domain.is_nat1(def_raw):
        await channel.send(f"💥 **Defense crumbles!** {def_cmd['commander_name']} — double damage!")
    elif result['hit']:
        await channel.send(f"💥 Hit! ({att_total} vs {def_total})")
    else:
        await channel.send(f"🛡️ Blocked! ({def_total} vs {att_total})")

    return result


# =============================================================================
# TENACIOUSNESS
# =============================================================================

async def _check_tenacity(
    bot:         discord.Client,
    loser:       discord.Member,
    winner:      discord.Member,
    loser_cmd:   dict,
    db:          sqlite3.Connection,
    campaign_id: int,
) -> bool:
    lp = campaign_repo.get_lp(db, loser_cmd['commander_id'], campaign_id)

    if lp < TENACITY_LP_COST:
        return False

    def loser_check(m):
        return m.author.id == loser.id and isinstance(m.channel, discord.DMChannel)

    def winner_check(m):
        return m.author.id == winner.id and isinstance(m.channel, discord.DMChannel)

    try:
        await loser.send(
            f"⚔️ **TENACIOUSNESS**\n\n"
            f"You are losing. Invoke tenaciousness for 2 more rounds?\n"
            f"Cost: {TENACITY_LP_COST} LP (you have {lp}). Your first roll takes a -2 penalty.\n\n"
            f"Reply `yes` to invoke or `no` to concede."
        )
    except discord.Forbidden:
        return False

    try:
        loser_reply = await bot.wait_for('message', check=loser_check, timeout=TENACITY_TIMEOUT)
    except asyncio.TimeoutError:
        await _try_dm(loser, "⏲️ Timed out — battle concluded.")
        return False

    if loser_reply.content.strip().lower() not in ('yes', 'y'):
        return False

    try:
        campaign_repo.spend_lp(db, loser_cmd['commander_id'], campaign_id, TENACITY_LP_COST)
    except ValueError:
        await _try_dm(loser, "❌ Insufficient LP. Tenaciousness failed.")
        return False

    try:
        await winner.send(
            "⚔️ **TENACIOUSNESS REQUEST**\n\n"
            "Your opponent has invoked tenaciousness — 2 more rounds.\n\n"
            "Reply `yes` to accept or `no` to decline."
        )
    except discord.Forbidden:
        return False

    try:
        winner_reply = await bot.wait_for('message', check=winner_check, timeout=TENACITY_TIMEOUT)
    except asyncio.TimeoutError:
        await _try_dm(winner, "⏲️ No response — tenaciousness declined.")
        return False

    return winner_reply.content.strip().lower() in ('yes', 'y')


# =============================================================================
# BATTLE COMPLETION
# =============================================================================

async def _complete_battle(
    bot:               discord.Client,
    channel:           discord.TextChannel,
    guild:             discord.Guild,
    db:                sqlite3.Connection,
    battle_id:         int,
    campaign_id:       int,
    p1:                discord.Member,
    p2:                discord.Member,
    p1_commander:      dict,
    p2_commander:      dict,
    result:            str,
    p1_total_ems_lost: int,
    p2_total_ems_lost: int,
    p1_round_wins:     int,
    p2_round_wins:     int,
    battle_size:       str,
    battle_type:       str,
    tenacity_fired:    bool,
    theme:             dict,
) -> None:

    # EMS table losses
    campaign = campaign_repo.get_campaign_by_id(db, campaign_id) if campaign_id else {}
    p1_side_label = (campaign.get('side_a_factions') or 'Side A').replace(',', ', ')
    p2_side_label = (campaign.get('side_b_factions') or 'Side B').replace(',', ', ')

    if result == 'p1':
        winner_name = p1_commander['commander_name']
        win_faction = p1_side_label
    elif result == 'p2':
        winner_name = p2_commander['commander_name']
        win_faction = p2_side_label
    else:
        winner_name = None
        win_faction = 'Tie'

    battle_data = {
        'contentionSize': battle_size,
        'winsOne':        p1_round_wins,
        'winsTwo':        p2_round_wins,
        'p1Faction':      p1_commander.get('faction', ''),
        'p2Faction':      p2_commander.get('faction', ''),
        'p1Side':         p1_side_label,
        'p2Side':         p2_side_label,
    }
    table_losses = ems_tables.calculate_ems_losses(battle_data)
    print(f"DEBUG table_losses: size={battle_size!r} wins={p1_round_wins}/{p2_round_wins} losses={table_losses}")

    # Apply EMS losses
    pool = 'fleet' if battle_type == 'space' else 'army'
    ems_repo.apply_ems_change(
        db,
        commander_id=p1_commander['commander_id'],
        pool=pool,
        change_amount=-p1_total_ems_lost,
        reason='Battle loss',
        changed_by_id=0,
    )
    ems_repo.apply_ems_change(
        db,
        commander_id=p2_commander['commander_id'],
        pool=pool,
        change_amount=-p2_total_ems_lost,
        reason='Battle loss',
        changed_by_id=0,
    )

    # Mark battle complete
    battle_repo.complete_battle(
        db,
        battle_id   = battle_id,
        wins_one    = p1_round_wins,
        wins_two    = p2_round_wins,
        p1_ems_lost = p1_total_ems_lost,
        p2_ems_lost = p2_total_ems_lost,
    )

    # Update campaign progress
    if campaign_id:
        campaign_repo.update_campaign_progress_after_battle(
            db,
            campaign_id=campaign_id,
            p1_commander_id=p1_commander['commander_id'],
            p2_commander_id=p2_commander['commander_id'],
            p1_ems_lost=p1_total_ems_lost,
            p2_ems_lost=p2_total_ems_lost,
        )
        db.commit()

        # Re-fetch campaign so progress embed shows updated EMS totals
        campaign = campaign_repo.get_campaign_by_id(db, campaign_id) or campaign

        # Post progress update to campaign thread
        thread_id = campaign.get('progress_thread_id') if campaign else None
        if thread_id:
            thread = guild.get_channel_or_thread(thread_id)
            if thread:
                try:
                    from core.shared.progress_renderer import build_progress_embed

                    battle_number = campaign_repo.get_campaign_battle_count(db, campaign_id)
                    side_a_label = (campaign.get('side_a_factions') or 'Side A').replace(',', ', ')
                    side_b_label = (campaign.get('side_b_factions') or 'Side B').replace(',', ', ')

                    # Determine winner/loser display names based on round wins
                    if p1_round_wins > p2_round_wins:
                        embed_winner = p1_commander['commander_name']
                        embed_loser = p2_commander['commander_name']
                    elif p2_round_wins > p1_round_wins:
                        embed_winner = p2_commander['commander_name']
                        embed_loser = p1_commander['commander_name']
                    else:
                        embed_winner = None
                        embed_loser = None

                    embed = build_progress_embed(
                        campaign_name=campaign['campaign_name'],
                        battle_number=battle_number,
                        side_a_label=side_a_label,
                        side_b_label=side_b_label,
                        a_current=campaign['side_a_ems_current'],
                        a_start=campaign['side_a_ems_start'],
                        b_current=campaign['side_b_ems_current'],
                        b_start=campaign['side_b_ems_start'],
                        cap=campaign['side_a_ems_start'] + campaign['side_b_ems_start'],
                        battle_winner=embed_winner,
                        battle_loser=embed_loser,
                        a_lost_this=p1_total_ems_lost if p1_commander['side'] == 'a' else p2_total_ems_lost,
                        b_lost_this=p1_total_ems_lost if p1_commander['side'] == 'b' else p2_total_ems_lost,
                    )
                    await thread.send(embed=embed)

                    # 20% critical nudge — fires once when a side just crosses the threshold
                    from core.shared.progress_renderer import build_critical_nudge_embed
                    a_start = campaign['side_a_ems_start']
                    b_start = campaign['side_b_ems_start']
                    a_current = campaign['side_a_ems_current']
                    b_current = campaign['side_b_ems_current']
                    a_lost = p1_total_ems_lost if p1_commander['side'] == 'a' else p2_total_ems_lost
                    b_lost = p1_total_ems_lost if p1_commander['side'] == 'b' else p2_total_ems_lost
                    a_before = a_current + a_lost
                    b_before = b_current + b_lost
                    nudge_cap = (a_start + b_start) or max(a_start, b_start, 1)

                    for which, before, current, start in (
                            ('a', a_before, a_current, a_start),
                            ('b', b_before, b_current, b_start),
                    ):
                        if nudge_cap > 0:
                            just_crossed = (
                                (before / nudge_cap) > 0.20
                                and (current / nudge_cap) <= 0.20
                            )
                            if just_crossed:
                                nudge_label = side_a_label if which == 'a' else side_b_label
                                nudge_embed = build_critical_nudge_embed(
                                    campaign_name=campaign['campaign_name'],
                                    side_label=nudge_label,
                                    side=which,
                                    ems_current=current,
                                    ems_start=start,
                                    cap=nudge_cap,
                                )
                                await thread.send(embed=nudge_embed)
                except Exception:
                    log.exception("Failed to post progress update to campaign thread %s", thread_id)

    # Post result to battle channel
    if winner_name:
        outcome = f"🎉 **{win_faction} Victory!**\n👑 {winner_name} emerges victorious!"
    else:
        outcome = "🤝 **TIE** — Both commanders fought to a stalemate!"

    await channel.send(
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎊 **BATTLE COMPLETE** 🎊\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{outcome}\n\n"
        f"📊 **EMS lost:**\n"
        f"  {p1_commander['commander_name']}: **{p1_total_ems_lost}**\n"
        f"  {p2_commander['commander_name']}: **{p2_total_ems_lost}**\n\n"
        f"🏅 **Round wins:** {p1_round_wins} – {p2_round_wins}\n\n"
        f"⚠️ Post your casualties!\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    # Post summary to results channel
    row = db.execute(
        'SELECT results_channel_id FROM campaign_channels WHERE campaign_id = ?',
        (campaign_id,),
    ).fetchone()
    results_channel_id = row['results_channel_id'] if row else None

    if results_channel_id:
        results_channel = guild.get_channel(results_channel_id)
        if results_channel:
            tenacious_note = " — Tenaciousness" if tenacity_fired else ""
            record = (
                f"{channel.name.capitalize()} — "
                f"{p1.mention} ({p1_commander['commander_name']}) vs "
                f"{p2.mention} ({p2_commander['commander_name']}) — "
                f"{battle_type.capitalize()} — {battle_size.capitalize()} — "
                f"{'Tie' if not winner_name else win_faction + ' Victory'} "
                f"({p1_round_wins}–{p2_round_wins}) "
                f"({p1_total_ems_lost} EMS – {p2_total_ems_lost} EMS)"
                f"{tenacious_note}"
            )
            await results_channel.send(record)

    await channel.send("✅ **Battle concluded. Thank you for your service, commanders.** ⚔️")


# =============================================================================
# HELPERS
# =============================================================================

async def _try_dm(user: discord.User, message: str) -> None:
    try:
        await user.send(message)
    except discord.Forbidden:
        pass
    except discord.HTTPException:
        log.exception("Failed to DM user %s", user.id)
