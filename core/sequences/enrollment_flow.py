"""
sequences/enrollment_flow.py
============================
E.R.I.S. Bot — Commander Enrollment Flow

PURPOSE
-------
This file owns the complete enrollment sequence — everything that happens
between a player reacting ⚔️ to a campaign message and their commander
being locked into that campaign in the database.

WHAT THIS FILE IS
-----------------
An async coordinator. It:
  - Listens for the ⚔️ reaction
  - Drives the DM conversation with the player
  - Calls domain checks before any write
  - Decides when to commit
  - Handles all timeouts, retries, and Discord failures

WHAT THIS FILE IS NOT
---------------------
It does not own rules (domain/), data reads/writes (data/), or
embed formatting (shared/embeds.py). It calls those layers and
coordinates their results.

LAYER IMPORTS (downward only)
------------------------------
    sequences → domain   ✓
    sequences → data     ✓
    sequences → shared   ✓
    sequences → math     ✓ (if needed)
    sequences → cogs     ✗ NEVER

STRUCTURE
---------
    on_raw_reaction_add()      ← ⚔️/🗡️ reaction listener, hands off immediately
    on_raw_reaction_remove()   ← unenrollment on reaction removal
    _handle_enrollment()       ← owns the full DM enrollment conversation
    _get_commander_selection() ← isolated prompt for commander choice
    _get_force_choice()        ← isolated prompt for force type selection
    _build_selection_embed()   ← builds the commander picker embed
    _commander_ems()           ← returns EMS total for a deployment choice

TIMEOUTS AND ATTEMPTS
---------------------
    Selection timeout:  300 seconds (5 minutes) per attempt
    Max attempts:       3 before cancellation
    Force choice timeout: 300 seconds

    These are module-level constants — change them here only.

COMMIT POLICY
-------------
Sequences commits. Repos never commit themselves.
No database write happens before domain checks pass.
If any check raises DomainError, the function returns without writing.
"""

import asyncio
import difflib
import logging
import re

import discord

from core.data     import campaign_repo, commander_repo, guild_repo
from core.domain   import campaign as campaign_domain
from core.domain   import commander as commander_domain
from core.domain.exceptions import DomainError
from core.shared   import embeds

log = logging.getLogger(__name__)

# =============================================================================
# CONSTANTS
# =============================================================================

SELECTION_TIMEOUT = 300   # seconds per attempt
FORCE_TIMEOUT     = 300   # seconds for force type choice
MAX_ATTEMPTS      = 3     # selection attempts before cancellation
ENROLL_EMOJI_A = '⚔️'
ENROLL_EMOJI_B = '🗡️'


# =============================================================================
# ENTRY POINT — REACTION LISTENER
# =============================================================================

async def on_raw_reaction_add(payload: discord.RawReactionActionEvent, bot: discord.Client) -> None:
    """
    Entry point for the ⚔️ reaction enrollment trigger.

    Kept intentionally thin — validates the reaction is relevant, then
    hands everything off to _handle_enrollment(). Any error here exits
    silently because reactions fire on every message and most won't
    match a campaign.

    Args:
        payload (discord.RawReactionActionEvent): The raw reaction event.
        bot     (discord.Client):                 The bot instance.
    """
    # Ignore bot's own reactions
    if payload.user_id == bot.user.id:
        return

    # Only handle the enrollment emoji
    emoji_str = str(payload.emoji)
    if emoji_str not in (ENROLL_EMOJI_A, ENROLL_EMOJI_B):
        return

    side = 'a' if emoji_str == ENROLL_EMOJI_A else 'b'

    # Look up the campaign by message ID
    db      = bot.db.conn
    campaign = campaign_repo.get_campaign_by_message(db, payload.message_id)

    if not campaign:
        return  # Reaction on a non-campaign message — ignore silently

    if campaign.get('status') not in ('enrolling', 'active'):
        return  # Campaign exists but is closed — ignore silently

    # Fetch the user — try cache first, then API
    user = bot.get_user(payload.user_id)
    if user is None:
        try:
            user = await bot.fetch_user(payload.user_id)
        except discord.HTTPException:
            log.exception("Failed to fetch user %s for enrollment", payload.user_id)
            return

    full_col = 'side_a_full' if side == 'a' else 'side_b_full'
    if campaign.get(full_col):
        try:
            await user.send(
                f"⚔️ **{campaign['campaign_name']}** — that side's enrollment is currently "
                f"**closed** (EMS cap reached). You may join the opposing side if available."
            )
        except discord.Forbidden:
            pass
        return

    # Load theme for this guild
    theme = bot.get_theme(payload.guild_id)

    # Hand off to the full enrollment flow
    await _handle_enrollment(bot, db, user, campaign, payload.channel_id, theme, side)

async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent, bot: discord.Client) -> None:
    """
    Entry point for ⚔️/🗡️ reaction removal — unenrolls the commander
    if the player had one enrolled in this campaign.
    """
    db       = bot.db.conn
    campaign = campaign_repo.get_campaign_by_message(db, payload.message_id)

    if not campaign:
        return
    if campaign.get('status') not in ('enrolling', 'active'):
        return

    # Find any active enrollment for this user in this campaign
    row = db.execute(
        '''
        SELECT cc.enrollment_id, cc.commander_id, cc.deployed_force, cc.side,
               c.commander_name
        FROM commander_campaigns cc
        JOIN commanders c ON c.commander_id = cc.commander_id
        WHERE cc.campaign_id = ?
          AND c.user_id      = ?
          AND cc.status      = 'active'
        LIMIT 1
        ''',
        (campaign['campaign_id'], payload.user_id),
    ).fetchone()

    if not row:
        return  # No active enrollment — nothing to remove

    row = dict(row)

    # Only unenroll if the reaction removed matches the side they're enrolled on
    emoji = str(payload.emoji)
    expected_emoji = '⚔️' if row['side'] == 'a' else '🗡️'
    if emoji != expected_emoji:
        return  # Reaction removed was for the other side — ignore

    # Unenroll
    db.execute(
        "DELETE FROM commander_campaigns WHERE enrollment_id = ?",
        (row['enrollment_id'],)
    )
    commander_repo.set_status(db, row['commander_id'], 'active')
    db.commit()

    # Reopen the side if it was marked full
    col = 'side_a_full' if row['side'] == 'a' else 'side_b_full'
    db.execute(
        f"UPDATE campaigns SET {col} = 0 WHERE campaign_id = ?",
        (campaign['campaign_id'],)
    )
    db.commit()

    # Remove Discord role if no other active campaign uses it
    guild = bot.get_guild(payload.guild_id)
    if guild:
        guild_config = guild_repo.get_guild_config(db, guild.id)
        roles_config = guild_config.get('roles', {}) if guild_config else {}
        member       = guild.get_member(payload.user_id)

        if member:
            still_deployed = db.execute(
                '''
                SELECT COUNT(*) AS cnt FROM commander_campaigns cc
                JOIN commanders c ON c.commander_id = cc.commander_id
                WHERE c.user_id = ? AND cc.status = 'active'
                ''',
                (payload.user_id,)
            ).fetchone()
            still_count = still_deployed['cnt'] if still_deployed else 0

            if still_count == 0:
                for role_key in ('Fleet Commander', 'Army Commander'):
                    role_id = roles_config.get(role_key)
                    if role_id:
                        role = guild.get_role(role_id)
                        if role and role in member.roles:
                            await member.remove_roles(role, reason="Campaign unenrollment")

    # Notify in channel
    channel = bot.get_channel(payload.channel_id)
    if channel:
        try:
            await channel.send(
                f"↩️ {member.mention if guild else f'<@{payload.user_id}>'} "
                f"withdrew **{row['commander_name']}** from **{campaign['campaign_name']}**."
            )
        except discord.HTTPException:
            log.warning("Could not post unenrollment notice to channel %s", payload.channel_id)


# =============================================================================
# MAIN ENROLLMENT FLOW
# =============================================================================

async def _handle_enrollment(
        bot: discord.Client,
        db,
        user: discord.User,
        campaign: dict,
        channel_id: int,
        theme: dict,
        side: str = 'a',
) -> None:
    """
    Drive the complete DM enrollment conversation for one player.

    Owns the full sequence:
        1. Fetch and split the player's commanders
        2. DM the commander selection list
        3. Wait for selection (up to MAX_ATTEMPTS)
        4. Handle force type choice if needed
        5. Run domain eligibility checks
        6. Write enrollment to the database
        7. Confirm in DM and channel

    Args:
        bot:        The bot instance (for wait_for and channel access).
        db:         The shared database connection.
        user:       The Discord user who reacted.
        campaign:   The campaign dict from the database.
        channel_id: The channel where the reaction happened.
        theme:      The loaded theme dict for this guild.
    """
    campaign_name = campaign.get('campaign_name', 'this campaign')


    # ------------------------------------------------------------------
    # Step 1 — Fetch and split commanders
    # ------------------------------------------------------------------
    all_commanders = commander_repo.get_commanders_for_user(db, user.id, campaign['guild_id'])

    available   = []
    unavailable = []

    for cmd in all_commanders:
        if cmd.get('active_campaign') is None:
            available.append(cmd)
        else:
            unavailable.append(cmd)

    # No available commanders — DM and exit
    if not available:
        await _dm_or_channel(
            bot, user, channel_id,
            f"⚔️ You reacted to enroll in **{campaign_name}**, but you have "
            f"no available commanders.\n\n"
            f"Ask an admin to create a commander for you, then uncheck and "
            f"re-check the {ENROLL_EMOJI_A} or {ENROLL_EMOJI_B} reaction to try again."
        )
        return


    # ------------------------------------------------------------------
    # Step 2 — Build and send the selection embed
    # ------------------------------------------------------------------
    embed = _build_selection_embed(campaign_name, available, unavailable)

    side_label = 'Side A ⚔️' if side == 'a' else 'Side B 🗡️'
    side_factions_key = 'side_a_factions' if side == 'a' else 'side_b_factions'
    side_factions = ', '.join(
        s.strip() for s in (campaign.get(side_factions_key, '') or '').split(',') if s.strip()
    ) or 'Unknown'

    try:
        await user.send(
            f"⚔️ You're joining **{side_label}** — {side_factions}",
            embed=embed
        )

    except discord.Forbidden:
        await _post_in_channel(
            bot, channel_id,
            f"{user.mention} — I couldn't DM you. Enable DMs so I can "
            f"walk you through enrollment."
        )
        return
    except discord.HTTPException:
        log.exception("Failed to send enrollment DM to %s", user.id)
        return


    # ------------------------------------------------------------------
    # Step 3 — Wait for commander selection
    # ------------------------------------------------------------------
    def dm_check(m: discord.Message) -> bool:
        return m.author.id == user.id and isinstance(m.channel, discord.DMChannel)

    selected_cmd = await _get_commander_selection(
        bot, user, dm_check, available, unavailable, campaign_name
    )

    if selected_cmd is None:
        return  # Timed out or cancelled — already messaged the player


    # ------------------------------------------------------------------
    # Step 4 — Handle force type choice if needed
    # ------------------------------------------------------------------
    deployed_force = await _get_force_choice(bot, user, dm_check, selected_cmd)

    if deployed_force is None:
        return  # Timed out — already messaged the player


    # ------------------------------------------------------------------
    # Step 5 — Run all domain eligibility checks
    # ------------------------------------------------------------------
    existing_enrollments = campaign_repo.get_active_enrollments_for_commander(
        db, selected_cmd['commander_id']
    )
    # Look up battle_type from campaign's bound channels
    bindings = db.execute(
        "SELECT * FROM campaign_channels WHERE campaign_id = ? AND battle_type != 'results' LIMIT 1",
        (campaign['campaign_id'],)
    ).fetchone()
    if bindings:
        campaign = {**campaign, 'battle_type': dict(bindings).get('battle_type', '')}

    try:
        campaign_domain.check_enrollment_eligible(
            commander            = selected_cmd,
            campaign             = campaign,
            existing_enrollments = existing_enrollments,
            deployed_force       = deployed_force,
            theme                = theme,
        )
    except DomainError as e:
        await _dm_or_channel(bot, user, channel_id, f"❌ {e}")
        return

    # ------------------------------------------------------------------
    # Step 5b — Side full check
    # ------------------------------------------------------------------
    full_col = 'side_a_full' if side == 'a' else 'side_b_full'
    if campaign.get(full_col):
        await _dm_or_channel(
            bot, user, channel_id,
            f"⚔️ **{campaign['campaign_name']}** — {side_label} enrollment is currently "
            f"**closed** (EMS cap reached). You may join the opposing side if available."
        )
        return

    # ------------------------------------------------------------------
    # Step 5c — EMS cap check
    # ------------------------------------------------------------------
    cap = campaign.get('opposing_ems_total', 0)
    cmd_ems = _commander_ems(selected_cmd, deployed_force)
    side_current = campaign_repo.get_side_ems_enrolled(db, campaign['campaign_id'], side)

    if cap and cmd_ems and (side_current + cmd_ems) > cap:
        overage = (side_current + cmd_ems) - cap
        await _dm_or_channel(
            bot, user, channel_id,
            f"❌ **{selected_cmd['commander_name']}** cannot enroll — "
            f"{side_label} is at `{side_current}` EMS. "
            f"Adding `{cmd_ems}` EMS would exceed the campaign cap of `{cap}` by `{overage}` EMS."
        )
        return


    # ------------------------------------------------------------------
    # Step 6 — Write enrollment (sequences commits)
    # ------------------------------------------------------------------
    try:
        campaign_repo.enroll_commander(
            db,
            commander_id=selected_cmd['commander_id'],
            campaign_id=campaign['campaign_id'],
            deployed_force=deployed_force,
            side=side,
        )

        commander_repo.set_status(db, selected_cmd['commander_id'], 'deployed')

        # Assign Discord role based on deployed force
        guild = bot.get_guild(campaign['guild_id'])
        if guild:
            guild_config = guild_repo.get_guild_config(db, guild.id)
            roles_config = guild_config.get('roles', {}) if guild_config else {}
            member = guild.get_member(user.id)

            if member:
                fleet_role_id = roles_config.get('Fleet Commander')
                army_role_id = roles_config.get('Army Commander')

                if deployed_force == 'Fleet' and fleet_role_id:
                    fleet_role = guild.get_role(fleet_role_id)
                    if fleet_role:
                        await member.add_roles(fleet_role, reason="Campaign enrollment")

                if deployed_force == 'Army' and army_role_id:
                    army_role = guild.get_role(army_role_id)
                    if army_role:
                        await member.add_roles(army_role, reason="Campaign enrollment")

    except Exception:
        log.exception(
            "Failed to write enrollment for commander %s in campaign %s",
            selected_cmd['commander_id'],
            campaign['campaign_id'],
        )
        await _dm_or_channel(
            bot, user, channel_id,
            "❌ Something went wrong saving your enrollment. "
            "Please contact an admin."
        )
        return

    # ------------------------------------------------------------------
    # Step 6b — Post-enrollment cap checks (warn / auto-close)
    # ------------------------------------------------------------------
    db.commit()

    cap = campaign.get('opposing_ems_total', 0)
    side_current = campaign_repo.get_side_ems_enrolled(db, campaign['campaign_id'], side)

    guild = bot.get_guild(campaign['guild_id'])
    guild_config = guild_repo.get_guild_config(db, campaign['guild_id']) if guild else None
    channels_cfg = guild_config.get('channels', {}) if guild_config else {}
    admin_log_id = channels_cfg.get('eris-admin-log')
    thread_id = campaign.get('progress_thread_id')

    if cap:
        if side_current >= cap:
            # Auto-close this side
            col = 'side_a_full' if side == 'a' else 'side_b_full'
            db.execute(
                f"UPDATE campaigns SET {col} = 1 WHERE campaign_id = ?",
                (campaign['campaign_id'],)
            )
            db.commit()

            # Admin log — always fires regardless of thread state
            if admin_log_id and guild:
                admin_log = guild.get_channel(admin_log_id)
                if admin_log:
                    try:
                        await admin_log.send(
                            f"🔒 **{campaign['campaign_name']}** — {side_label} has reached "
                            f"the EMS cap (`{side_current}/{cap}`). **Enrollment is now closed for this side.**"
                        )
                    except discord.HTTPException:
                        log.warning("Could not post cap-closed notice to admin log %s", admin_log_id)

            # Progress thread notice (only if campaign already started)
            if thread_id and guild:
                thread = guild.get_channel_or_thread(thread_id)
                if thread:
                    try:
                        await thread.send(
                            f"⚔️ **{side_label}** enrollment is now **closed** — "
                            f"campaign EMS cap of `{cap}` reached."
                        )
                    except discord.HTTPException:
                        log.warning("Could not post cap notice to progress thread %s", thread_id)

        elif cap > 0 and (side_current / cap) >= 0.90:
            # 90% warning → admin log only
            if admin_log_id and guild:
                admin_log = guild.get_channel(admin_log_id)
                if admin_log:
                    remaining = cap - side_current
                    try:
                        await admin_log.send(
                            f"⚠️ **{campaign['campaign_name']}** — {side_label} is at "
                            f"`{side_current}/{cap}` EMS (`{remaining}` remaining). "
                            f"Enrollment is near capacity."
                        )
                    except discord.HTTPException:
                        log.warning("Could not post 90%% warning to admin log %s", admin_log_id)



    # ------------------------------------------------------------------
    # Step 7 — Confirm
    # ------------------------------------------------------------------
    commander_name = selected_cmd.get('commander_name', 'Your commander')

    try:
        await user.send(
            f"✅ **{commander_name}** is now enrolled in **{campaign_name}** "
            f"(Forces: {deployed_force}) · {side_label}.\n\n"
            f"Good luck, Commander."
        )
    except discord.Forbidden:
        pass

    side_count = db.execute(
        "SELECT COUNT(*) AS cnt FROM commander_campaigns WHERE campaign_id = ? AND side = ? AND status = 'active'",
        (campaign['campaign_id'], side)
    ).fetchone()
    count = side_count['cnt'] if side_count else 1

    await _post_in_channel(
        bot, channel_id,
        f"✅ {user.mention} enrolled **{commander_name}** in "
        f"**{campaign_name}** (Forces: {deployed_force})\n"
        f"{side_label}: **{count}** commander{'s' if count != 1 else ''}"
    )


# =============================================================================
# COMMANDER SELECTION LOOP
# =============================================================================

async def _get_commander_selection(
    bot:         discord.Client,
    user:        discord.User,
    dm_check,
    available:   list,
    unavailable: list,
    campaign_name: str,
) -> dict | None:
    """
    Run the selection loop and return the chosen commander dict.

    Accepts number or name input. Fuzzy-matches names with confirmation.
    Blocks selection of unavailable commanders with a clear message.
    Returns None if the player cancelled or exhausted all attempts.

    Args:
        bot:           The bot instance.
        user:          The Discord user to wait on.
        dm_check:      The message check function for wait_for.
        available:     List of available commander dicts.
        unavailable:   List of unavailable commander dicts.
        campaign_name: Campaign name for messages.

    Returns:
        dict | None: The selected commander, or None on cancel/timeout.
    """
    def normalize(s: str) -> str:
        return re.sub(r'\s+', ' ', s).strip().lower()

    available_by_name   = {normalize(c['commander_name']): c for c in available}
    unavailable_by_name = {normalize(c['commander_name']): c for c in unavailable}

    attempts = 0

    while attempts < MAX_ATTEMPTS:
        attempts += 1

        try:
            reply = await bot.wait_for('message', check=dm_check, timeout=SELECTION_TIMEOUT)
        except asyncio.TimeoutError:
            await _try_dm(user, "⏲️ Enrollment timed out. React again to try.")
            return None

        text = reply.content.strip()

        # Cancel
        if text.lower() == 'cancel':
            await _try_dm(user, "❌ Enrollment cancelled.")
            return None

        # Numeric selection
        if text.isdigit():
            idx = int(text) - 1
            if 0 <= idx < len(available):
                return available[idx]
            elif 0 <= idx < len(unavailable):
                await _try_dm(
                    user,
                    "⚠️ That commander is already deployed in another campaign. "
                    "Choose an **available** commander from the list."
                )
                continue
            else:
                await _try_dm(
                    user,
                    f"❌ Invalid number. Reply with a number from the list, "
                    f"or the commander's exact name."
                )
                continue

        # Name selection
        lowered = normalize(text)

        if lowered in available_by_name:
            return available_by_name[lowered]

        if lowered in unavailable_by_name:
            await _try_dm(
                user,
                "⚠️ That commander is already deployed elsewhere. "
                "Choose an **available** commander."
            )
            continue

        # Fuzzy match against available names only
        suggestion = difflib.get_close_matches(
            lowered, list(available_by_name.keys()), n=1, cutoff=0.7
        )

        if suggestion:
            match_key  = suggestion[0]
            match_cmd  = available_by_name[match_key]
            match_name = match_cmd['commander_name']

            await _try_dm(
                user,
                f"Did you mean **{match_name}**? Reply `yes` to confirm or `no` to try again."
            )

            try:
                confirm = await bot.wait_for('message', check=dm_check, timeout=60)
            except asyncio.TimeoutError:
                await _try_dm(user, "⏲️ No response — please reply with your selection.")
                continue

            if confirm.content.strip().lower() in ('yes', 'y'):
                return match_cmd
            else:
                await _try_dm(user, "Okay — please reply with the number or name of an available commander.")
                continue

        # No match found
        remaining = MAX_ATTEMPTS - attempts
        if remaining > 0:
            await _try_dm(
                user,
                f"❌ Commander not found. {remaining} attempt{'s' if remaining != 1 else ''} remaining.\n"
                f"Reply with the number or exact name of an available commander."
            )
        else:
            await _try_dm(
                user,
                "❌ Too many failed attempts. Enrollment cancelled. React again to try."
            )

    return None


# =============================================================================
# FORCE TYPE CHOICE
# =============================================================================

async def _get_force_choice(
    bot:      discord.Client,
    user:     discord.User,
    dm_check,
    commander: dict,
) -> str | None:
    """
    Return the force the commander will deploy.

    Every commander is specialized into a single arm at submission, so
    there is no choice to make — this returns the commander's force_type
    directly. Kept as a function (rather than inlined) so the enrollment
    sequence's call site is unchanged.

    Args:
        bot:       The bot instance. Unused; kept for signature stability.
        user:      The player. Unused; kept for signature stability.
        dm_check:  The message check. Unused; kept for signature stability.
        commander: The selected commander dict.

    Returns:
        str: The commander's force type (its single deployable arm).
    """
    return commander.get('force_type', '')


# =============================================================================
# EMBED BUILDER
# =============================================================================

def _build_selection_embed(
    campaign_name: str,
    available:     list,
    unavailable:   list,
) -> discord.Embed:
    """
    Build the commander selection embed sent to the player in DMs.

    Args:
        campaign_name: The campaign being enrolled in.
        available:     List of available commander dicts.
        unavailable:   List of unavailable commander dicts.

    Returns:
        discord.Embed: The formatted selection embed.
    """
    RANK_EMOJI = {'Main': '⭐', 'Senior': '🎖️', 'Junior': '🔰'}
    FORCE_EMOJI = {'Fleet': '🚀', 'Army': '🪖'}

    embed = discord.Embed(
        title=f"🎖️ Campaign Enrollment: {campaign_name}",
        description="Which commander do you want to enroll?",
        color=discord.Color.gold()
    )

    # Available commanders
    avail_lines = []
    for i, cmd in enumerate(available, start=1):
        emoji = RANK_EMOJI.get(cmd.get('rank', ''), '')
        force = cmd.get('force_type', '')
        force_emoji = FORCE_EMOJI.get(force, '')
        lp = f"{cmd.get('current_leadership_points', '?')}/{cmd.get('max_leadership_points', '?')}"
        avail_lines.append(
            f"{i}. {emoji} **{cmd['commander_name']}** "
            f"({cmd.get('rank', '?')}, {force_emoji} {force}, {lp} LP)"
        )

    embed.add_field(
        name  = "✅ AVAILABLE",
        value = '\n'.join(avail_lines) or '*None*',
        inline = False
    )

    # Unavailable commanders
    if unavailable:
        unavail_lines = []
        for cmd in unavailable:
            emoji = RANK_EMOJI.get(cmd.get('rank', ''), '')
            force = cmd.get('force_type', '')
            force_emoji = FORCE_EMOJI.get(force, '')
            unavail_lines.append(
                f"{emoji} **{cmd['commander_name']}** "
                f"({cmd.get('rank', '?')}, {force_emoji} {force}) — deployed in \"{cmd.get('active_campaign_name', 'another campaign')}\""
            )

        embed.add_field(
            name  = "⚠️ UNAVAILABLE",
            value = '\n'.join(unavail_lines),
            inline = False
        )

    embed.add_field(
        name  = "📋 Instructions",
        value = (
            "Reply with the **number** or **exact name** of an available commander.\n"
            "Reply `cancel` to cancel.\n\n"
            "⚠️ Once enrolled, this commander is locked to this campaign."
        ),
        inline = False
    )

    embed.set_footer(text=f"You have {MAX_ATTEMPTS} attempts and {SELECTION_TIMEOUT // 60} minutes per attempt.")

    return embed


# =============================================================================
# MATH HELPERS
# =============================================================================

def _commander_ems(commander: dict, deployed_force: str) -> int:
    """Return the EMS a commander contributes based on their deployed force."""
    if deployed_force == 'Fleet':
        return commander.get('fleet_total_ems', 0)
    if deployed_force == 'Army':
        return commander.get('army_total_ems', 0)
    return 0


# =============================================================================
# DISCORD HELPERS
# =============================================================================

async def _dm_or_channel(
    bot:        discord.Client,
    user:       discord.User,
    channel_id: int,
    message:    str,
) -> None:
    """
    Send a message to the user via DM, falling back to channel if DMs are blocked.

    Args:
        bot:        The bot instance.
        user:       The target user.
        channel_id: Fallback channel ID.
        message:    The message string to send.
    """
    try:
        await user.send(message)
    except discord.Forbidden:
        await _post_in_channel(bot, channel_id, f"{user.mention} — {message}")
    except discord.HTTPException:
        log.exception("Failed to DM user %s", user.id)


async def _post_in_channel(bot: discord.Client, channel_id: int, message: str) -> None:
    """
    Post a message in a channel by ID.

    Args:
        bot:        The bot instance.
        channel_id: The channel to post in.
        message:    The message string.
    """
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            log.exception("Failed to fetch channel %s", channel_id)
            return
    try:
        await channel.send(message)
    except discord.HTTPException:
        log.exception("Failed to post in channel %s", channel_id)


async def _try_dm(user: discord.User, message: str) -> None:
    """
    Attempt to DM a user. Swallows Forbidden silently — if they closed
    DMs mid-conversation there is nothing useful we can do.

    Args:
        user:    The target user.
        message: The message string.
    """
    try:
        await user.send(message)
    except discord.Forbidden:
        pass
    except discord.HTTPException:
        log.exception("Failed to DM user %s", user.id)
