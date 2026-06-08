"""
sequences/ems_flow.py
=====================
E.R.I.S. Bot — EMS Request and Adjustment Flow

PURPOSE
-------
This file owns all EMS management that happens outside of battle:
  - Player-initiated EMS requests (guided conversation)
  - Admin approval and denial of requests
  - Direct admin EMS adjustments
  - Post-campaign block refits (player-initiated, admin-approved)

WHAT THIS FILE IS NOT
---------------------
- Shop purchases      → shop_flow.py (separate, built when ready)
- Battle EMS losses   → sequences/battle_flow.py
- EMS math            → math/ems_tables.py and domain/ems.py

GUIDED CONVERSATION APPROACH
-----------------------------
/ems request takes no parameters. It opens a DM conversation that
walks the player through four steps in order:

    1. Fleet, Ground, or Both?
    2. How much EMS?
    3. What is the reason?
    4. Confirmation before submitting

ADMIN REVIEW FLOW
-----------------
After a request is submitted, the admin review channel receives an
embed with all request details. The admin reacts ✅ to approve or
❌ to deny. On denial, the bot DMs the admin for a reason before
notifying the player.

Approval: no reason required — the request already has the context.
Denial:   reason required — player needs to know what to resubmit.

COMMIT POLICY
-------------
Sequences commits. Repos never commit.
No write happens before all validation passes.

LAYER IMPORTS (downward only)
------------------------------
    sequences → domain   ✓
    sequences → data     ✓
    sequences → shared   ✓
    sequences → cogs     ✗ NEVER

FUNCTIONS
---------
request_ems()            — guided DM flow for player EMS requests
approve_ems()            — admin approves a pending request
deny_ems()               — admin denies a pending request with reason
adjust_ems()             — admin direct add/subtract via channel conversation
refit_ems()              — guided DM flow for post-campaign block refits
"""

import asyncio
import logging

import discord

from core.data           import commander_repo, ems_repo, guild_repo
from core.domain         import ems as ems_domain
from core.domain         import commander as commander_domain
from core.domain.exceptions import DomainError
from core.shared         import embeds

log = logging.getLogger(__name__)

# =============================================================================
# CONSTANTS
# =============================================================================

CONVERSATION_TIMEOUT = 300   # seconds per step in the guided flow
ADMIN_REVIEW_TIMEOUT = 86400 # seconds admin has to approve/deny (24 hours)
DENY_REASON_TIMEOUT  = 300   # seconds admin has to provide denial reason

APPROVE_EMOJI = '✅'
DENY_EMOJI    = '❌'

POOL_OPTIONS = {
    '1': 'fleet',
    '2': 'ground',
    '3': 'both',
    'fleet':  'fleet',
    'ground': 'ground',
    'both':   'both',
    'f': 'fleet',
    'g': 'ground',
    'b': 'both',
}


# =============================================================================
# /EMS REQUEST — GUIDED CONVERSATION
# =============================================================================

async def request_ems(
    bot: discord.Client,
    member: discord.Member,
    guild: discord.Guild,
    db,
    theme: dict,
) -> None:
    """
    Entry point for /ems request (slash command).

    Opens a guided DM conversation that walks the player through:
        Step 1: Fleet, Ground, or Both?
        Step 2: How much EMS?
        Step 3: What is the reason?
        Step 4: Confirm before submitting
    """
    user = member

    # Validate the player has at least one active commander
    commanders = commander_repo.get_commanders_for_user(db, user.id, guild.id)
    for c in commanders:
        print(f"DEBUG commander dict: {c}")
    active = [c for c in commanders if c.get('active_campaign') is not None]

    if not active:
        await user.send(
            "❌ You don't have any commanders currently deployed in a campaign.\n"
            "EMS requests are only available to deployed commanders."
        )
        return

    # If multiple active commanders, ask which one
    commander = await _get_commander_choice(bot, user, active)
    if commander is None:
        return

    # Open DMs for the guided conversation
    try:
        await user.send(
            f"📋 **EMS Request — {commander['commander_name']}**\n\n"
            f"I'll walk you through this step by step.\n"
            f"Reply `cancel` at any time to cancel."
        )
    except discord.Forbidden:
        log.warning("Could not DM user %s for EMS request", user.id)
        return

    await user.send(f"📬 {user.mention} — check your DMs to complete your EMS request.")

    def dm_check(m):
        return m.author.id == user.id and isinstance(m.channel, discord.DMChannel)

    # ------------------------------------------------------------------
    # Step 1: Pool choice
    # ------------------------------------------------------------------
    pool = await _get_pool_choice(bot, user, dm_check, commander)
    if pool is None:
        return

    # ------------------------------------------------------------------
    # Step 2: Amount
    # ------------------------------------------------------------------
    amount = await _get_amount_choice(bot, user, dm_check, pool)
    if amount is None:
        return

    # ------------------------------------------------------------------
    # Step 3: Reason
    # ------------------------------------------------------------------
    reason = await _get_reason_choice(bot, user, dm_check)
    if reason is None:
        return

    # ------------------------------------------------------------------
    # Step 4: Confirmation
    # ------------------------------------------------------------------
    pool_display = pool.capitalize() if pool != 'both' else 'Both (split equally)'
    fleet_amount, ground_amount = _split_ems(amount, pool)

    confirm_text = (
        f"📋 **Confirm your EMS request:**\n\n"
        f"Commander: **{commander['commander_name']}**\n"
        f"Pool:      **{pool_display}**\n"
    )

    if pool == 'both':
        confirm_text += (
            f"Amount:    **{fleet_amount}** Fleet + **{ground_amount}** Ground "
            f"(split from {amount})\n"
        )
    else:
        confirm_text += f"Amount:    **{amount}** EMS\n"

    confirm_text += (
        f"Reason:    {reason}\n\n"
        f"Reply `yes` to submit or `no` to cancel."
    )

    await _try_dm(user, confirm_text)

    try:
        confirm_msg = await bot.wait_for('message', check=dm_check, timeout=CONVERSATION_TIMEOUT)
    except asyncio.TimeoutError:
        await _try_dm(user, "⏲️ Request timed out. Use `/ems request` to start over.")
        return

    if confirm_msg.content.strip().lower() not in ('yes', 'y'):
        await _try_dm(user, "❌ Request cancelled.")
        return

    # ------------------------------------------------------------------
    # Write the pending request
    # ------------------------------------------------------------------
    try:
        request_id = ems_repo.create_request(
            db,
            guild_id=guild.id,
            user_id=user.id,
            commander_id=commander['commander_id'],
            pool=pool,
            amount=amount,
            reason=reason,
        )
    except Exception:
        log.exception("Failed to write EMS request for user %s", user.id)
        await _try_dm(
            user,
            "❌ Something went wrong submitting your request. "
            "Please contact an admin."
        )
        return

    await _try_dm(
        user,
        f"✅ **Request submitted!**\n\n"
        f"An admin will review your request soon. "
        f"You'll receive a DM when a decision is made.\n\n"
        f"Request ID: `#{request_id}`"
    )

    # Post to admin review channel
    await _post_admin_review(bot, db, guild, request_id, user, commander,
                              pool, fleet_amount, ground_amount, amount, reason)


# =============================================================================
# GUIDED CONVERSATION STEPS
# =============================================================================

async def _get_commander_choice(
    bot:       discord.Client,
    user:      discord.User,
    commanders: list,
) -> dict | None:
    """
    If the player has multiple deployed commanders, ask which one the
    request is for. If only one, return it directly.

    Args:
        bot:        The bot instance.
        ctx:        Command context.
        user:       The requesting player.
        commanders: List of active commander dicts.

    Returns:
        dict | None: The chosen commander, or None on cancel/timeout.
    """
    if len(commanders) == 1:
        return commanders[0]

    def channel_check(m):
        return m.author.id == user.id and m.channel == user.dm_channel

    lines = '\n'.join(
        f"{i + 1}. **{c['commander_name']}** — deployed in {c.get('active_campaign_name') or c.get('active_campaign') or '?'}"
        for i, c in enumerate(commanders)
    )

    await user.send(
        f"**Which commander is this request for?**\n{lines}\n\n"
        f"Reply with the number."
    )

    try:
        msg = await bot.wait_for('message', check=channel_check, timeout=CONVERSATION_TIMEOUT)
    except asyncio.TimeoutError:
        await user.send("⏲️ Request timed out.")
        return None

    text = msg.content.strip()

    if text.lower() == 'cancel':
        await user.send("❌ Request cancelled.")
        return None

    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(commanders):
            return commanders[idx]

    await user.send("❌ Invalid selection. Use `/ems request` to try again.")
    return None


async def _get_pool_choice(
    bot:       discord.Client,
    user:      discord.User,
    dm_check,
    commander: dict,
) -> str | None:
    """
    Ask the player which EMS pool this request is for.

    Only shows options that make sense for the commander's force type.
    A Fleet-only commander can't request ground EMS.

    Args:
        bot:       The bot instance.
        user:      The player.
        dm_check:  The DM message check function.
        commander: The selected commander dict.

    Returns:
        str | None: 'fleet', 'ground', or 'both', or None on cancel/timeout.
    """
    force_type = commander.get('force_type', 'Fleet')

    # Build options based on force type
    if force_type == 'Fleet':
        options_text = "1. Fleet"
        valid        = {'1': 'fleet', 'fleet': 'fleet', 'f': 'fleet'}
    elif force_type == 'Army':
        options_text = "1. Ground"
        valid        = {'1': 'ground', 'ground': 'ground', 'g': 'ground'}
    else:
        # Both
        options_text = "1. Fleet\n2. Ground\n3. Both (EMS split equally between pools)"
        valid        = POOL_OPTIONS

    await _try_dm(
        user,
        f"**Step 1 of 3 — Which pool is this EMS for?**\n"
        f"```\n{options_text}\n```\n"
        f"Reply with the number or name."
    )

    while True:
        try:
            msg = await bot.wait_for('message', check=dm_check, timeout=CONVERSATION_TIMEOUT)
        except asyncio.TimeoutError:
            await _try_dm(user, "⏲️ Request timed out. Use `/ems request` to start over.")
            return None

        text = msg.content.strip().lower()

        if text == 'cancel':
            await _try_dm(user, "❌ Request cancelled.")
            return None

        if text in valid:
            pool = valid[text]
            label = pool.capitalize() if pool != 'both' else 'Both (split equally)'
            await _try_dm(user, f"✅ Pool: **{label}**")
            return pool

        await _try_dm(user, "❌ Invalid choice. Reply with the number or name from the list.")


async def _get_amount_choice(
    bot:      discord.Client,
    user:     discord.User,
    dm_check,
    pool:     str,
) -> int | None:
    """
    Ask the player how much EMS they are requesting.

    Validates the input is a positive integer. If pool is 'both',
    shows the player how the split will work before they confirm.

    Args:
        bot:      The bot instance.
        user:     The player.
        dm_check: The DM message check.
        pool:     The selected pool ('fleet', 'ground', or 'both').

    Returns:
        int | None: The requested amount, or None on cancel/timeout.
    """
    split_note = (
        "\n*(This amount will be split equally between Fleet and Ground.)*"
        if pool == 'both' else ""
    )

    await _try_dm(
        user,
        f"**Step 2 of 3 — How much EMS are you requesting?**{split_note}\n\n"
        f"Reply with a whole number (e.g. `100`)."
    )

    while True:
        try:
            msg = await bot.wait_for('message', check=dm_check, timeout=CONVERSATION_TIMEOUT)
        except asyncio.TimeoutError:
            await _try_dm(user, "⏲️ Request timed out. Use `/ems request` to start over.")
            return None

        text = msg.content.strip().lower()

        if text == 'cancel':
            await _try_dm(user, "❌ Request cancelled.")
            return None

        if text.isdigit() and int(text) > 0:
            amount = int(text)

            if pool == 'both':
                fleet_half, ground_half = _split_ems(amount, pool)
                await _try_dm(
                    user,
                    f"✅ Amount: **{amount}** EMS total\n"
                    f"   → **{fleet_half}** Fleet + **{ground_half}** Ground"
                )
            else:
                await _try_dm(user, f"✅ Amount: **{amount}** EMS")

            return amount

        await _try_dm(user, "❌ Please reply with a positive whole number.")


async def _get_reason_choice(
    bot:      discord.Client,
    user:     discord.User,
    dm_check,
) -> str | None:
    """
    Ask the player for the reason behind their EMS request.

    No validation beyond length — the reason is freeform text. Minimum
    10 characters so players can't submit an empty or trivial reason.

    Args:
        bot:      The bot instance.
        user:     The player.
        dm_check: The DM message check.

    Returns:
        str | None: The reason string, or None on cancel/timeout.
    """
    await _try_dm(
        user,
        "**Step 3 of 3 — What is the reason for this request?**\n\n"
        "Describe what this EMS represents (e.g. reinforcements from a "
        "completed mission, RP event reward, etc.)\n\n"
        "Reply with your reason (minimum 10 characters)."
    )

    while True:
        try:
            msg = await bot.wait_for('message', check=dm_check, timeout=CONVERSATION_TIMEOUT)
        except asyncio.TimeoutError:
            await _try_dm(user, "⏲️ Request timed out. Use `/ems request` to start over.")
            return None

        text = msg.content.strip()

        if text.lower() == 'cancel':
            await _try_dm(user, "❌ Request cancelled.")
            return None

        if len(text) >= 10:
            await _try_dm(user, f"✅ Reason recorded.")
            return text

        await _try_dm(
            user,
            f"❌ Reason is too short ({len(text)} characters). "
            f"Please provide at least 10 characters."
        )


# =============================================================================
# ADMIN REVIEW
# =============================================================================

async def _post_admin_review(
    bot:           discord.Client,
    db,
    guild:         discord.Guild,
    request_id:    int,
    player:        discord.User,
    commander:     dict,
    pool:          str,
    fleet_amount:  int,
    ground_amount: int,
    total:         int,
    reason:        str,
) -> None:
    """
    Post the EMS request to the admin review channel.

    Admins react ✅ to approve or ❌ to deny. Bot listens for the
    reaction, then handles the approval or denial flow.

    Args:
        All request details needed to build the review embed.
    """
    guild_config = guild_repo.get_guild_config(db, guild.id)
    review_channel_id = guild_config.get('ems_requests_channel_id') if guild_config else None
    if not review_channel_id:
        log.warning("No EMS review channel configured for guild %s", guild.id)
        return

    review_channel = guild.get_channel(review_channel_id)
    if not review_channel:
        return

    # Build pool display
    if pool == 'both':
        pool_display = f"Both ({fleet_amount} Fleet + {ground_amount} Ground)"
    else:
        pool_display = f"{pool.capitalize()} ({total} EMS)"

    embed = discord.Embed(
        title       = f"📋 EMS Request #{request_id}",
        description = f"**{player.display_name}** has submitted an EMS request.",
        color       = discord.Color.blue()
    )
    embed.add_field(name="Commander",  value=commander['commander_name'], inline=True)
    embed.add_field(name="Pool",       value=pool_display,                inline=True)
    embed.add_field(name="Campaign",   value=commander.get('active_campaign_name') or commander.get('active_campaign', '?'), inline=True)
    embed.add_field(name="Reason",     value=reason,                      inline=False)
    embed.set_footer(
        text=f"React {APPROVE_EMOJI} to approve · {DENY_EMOJI} to deny"
    )

    try:
        review_msg = await review_channel.send(embed=embed)
        await review_msg.add_reaction(APPROVE_EMOJI)
        await review_msg.add_reaction(DENY_EMOJI)
    except discord.HTTPException:
        log.exception("Failed to post EMS review embed for request %s", request_id)
        return

    # Listen for admin reaction
    def reaction_check(reaction, user):
        return (
            str(reaction.emoji) in (APPROVE_EMOJI, DENY_EMOJI) and
            reaction.message.id == review_msg.id and
            not user.bot
        )

    try:
        reaction, admin_user = await bot.wait_for(
            'reaction_add', check=reaction_check, timeout=ADMIN_REVIEW_TIMEOUT
        )
    except asyncio.TimeoutError:
        await review_channel.send(
            f"⏲️ EMS Request #{request_id} expired with no admin decision."
        )
        ems_repo.update_request_status(
            db, request_id, 'expired'
        )
        return

    if str(reaction.emoji) == APPROVE_EMOJI:
        await approve_ems(bot, db, guild, admin_user, review_channel,
                          request_id, player, commander, pool,
                          fleet_amount, ground_amount, total)
    else:
        await deny_ems(bot, db, guild, admin_user, review_channel,
                       request_id, player)


# =============================================================================
# APPROVE
# =============================================================================

async def approve_ems(
    bot:           discord.Client,
    db,
    guild:         discord.Guild,
    admin:         discord.Member,
    review_channel,
    request_id:    int,
    player:        discord.User,
    commander:     dict,
    pool:          str,
    fleet_amount:  int,
    ground_amount: int,
    total:         int,
) -> None:
    """
    Approve an EMS request — write EMS to the correct pool(s), notify player.

    Args:
        admin:          The admin who approved.
        review_channel: The channel to confirm approval in.
        All other args: Request details.
    """
    try:
        if fleet_amount:
            ems_repo.apply_ems_change(db, commander['commander_id'], 'fleet', fleet_amount, f"EMS Request #{request_id} approved by {admin.display_name}", admin.id)
        if ground_amount:
            ems_repo.apply_ems_change(db, commander['commander_id'], 'army', ground_amount, f"EMS Request #{request_id} approved by {admin.display_name}", admin.id)
        ems_repo.update_request_status(db, request_id, 'approved')
    except Exception:
        log.exception("Failed to apply EMS approval for request %s", request_id)
        await review_channel.send(
            f"❌ Failed to apply EMS for request #{request_id}. Check logs."
        )
        return

    # Confirm in review channel
    await review_channel.send(
        f"✅ **Request #{request_id} approved** by {admin.mention}.\n"
        f"{player.mention}'s commander **{commander['commander_name']}** "
        f"received **{total} EMS** ({pool.capitalize()})."
    )

    # DM the player
    if pool == 'both':
        ems_detail = f"**{fleet_amount}** Fleet EMS and **{ground_amount}** Ground EMS"
    else:
        ems_detail = f"**{total}** {pool.capitalize()} EMS"

    await _notify_player(
        bot, player, guild,
        f"✅ **EMS Request Approved!**\n\n"
        f"Your request for **{commander['commander_name']}** has been approved.\n"
        f"**{ems_detail}** has been added to your forces.\n\n"
        f"Request ID: `#{request_id}`",
        db=db,
    )


# =============================================================================
# DENY
# =============================================================================

async def deny_ems(
    bot:            discord.Client,
    db,
    guild:          discord.Guild,
    admin:          discord.Member,
    review_channel,
    request_id:     int,
    player:         discord.User,
) -> None:
    """
    Deny an EMS request — ask admin for reason, notify player.

    Args:
        admin:          The admin who denied.
        review_channel: The channel to confirm denial in.
        request_id:     The request being denied.
        player:         The player to notify.
    """
    def admin_dm_check(m):
        return (m.author.id == admin.id and
                isinstance(m.channel, discord.DMChannel))

    # Ask admin for denial reason
    try:
        await admin.send(
            f"❌ **Denying EMS Request #{request_id}**\n\n"
            f"Please provide a reason for the denial.\n"
            f"The player will receive this message.\n\n"
            f"Reply with your reason."
        )
    except discord.Forbidden:
        # Admin has DMs closed — ask in the review channel
        await review_channel.send(
            f"{admin.mention} — I can't DM you. "
            f"Please enable DMs and re-react to deny with a reason."
        )
        return

    try:
        reason_msg = await bot.wait_for(
            'message', check=admin_dm_check, timeout=DENY_REASON_TIMEOUT
        )
        denial_reason = reason_msg.content.strip()
    except asyncio.TimeoutError:
        await _try_dm(
            admin,
            f"⏲️ Denial reason timed out. Request #{request_id} has been "
            f"left pending. Please deny it again."
        )
        return

    # Write denial
    try:
        ems_repo.update_request_status(
            db, request_id, 'denied', denial_reason
        )
    except Exception:
        log.exception("Failed to write denial for request %s", request_id)
        return

    # Confirm in review channel
    await review_channel.send(
        f"❌ **Request #{request_id} denied** by {admin.mention}.\n"
        f"Reason: {denial_reason}"
    )

    # DM the player
    await _notify_player(
        bot, player, guild,
        f"❌ **EMS Request Denied**\n\n"
        f"Your EMS request (#{request_id}) was denied.\n\n"
        f"**Reason:** {denial_reason}\n\n"
        f"You may submit a new request with `/ems request` if you "
        f"believe this was in error.",
        db=db,
    )


# =============================================================================
# DIRECT ADMIN ADJUSTMENT
# =============================================================================

async def adjust_ems(
    bot: discord.Client,
    admin: discord.Member,
    target_member: discord.Member,
    channel: discord.TextChannel,
    guild: discord.Guild,
    db,
    theme: dict,
) -> None:
    """
    Entry point for /ems adjust — direct admin EMS change, no approval queue.

    Guided conversation:
        1. Which commander? (if target has multiple deployed)
        2. Fleet, Ground, or Both?
        3. Amount (positive = add, negative = subtract)
        4. Reason

    Args:
        bot:           The bot instance.
        admin:         The admin running the command.
        target_member: The player whose EMS is being adjusted.
        channel:       The channel to run the conversation in.
        guild:         The guild.
        db:            Database connection.
        theme:         Guild theme dict.
    """
    commanders = commander_repo.get_commanders_for_user(db, target_member.id, guild.id)
    active     = commanders

    if not active:
        await admin.send(
            f"❌ {target_member.mention} has no commanders."
        )
        return

    def channel_check(m):
        return m.author.id == admin.id and isinstance(m.channel, discord.DMChannel)

    # Commander choice
    if len(active) > 1:
        def _domain_label(c):
            has_fleet = bool(c.get('fleet_total_ems') or c.get('fleet_ems_block'))
            has_army  = bool(c.get('army_total_ems')  or c.get('army_ems_block'))
            if has_fleet and has_army: return 'Fleet + Ground'
            if has_army:  return 'Ground'
            return 'Fleet'
        lines = '\n'.join(
            f"{i + 1}. **{c['commander_name']}** ({_domain_label(c)})"
            for i, c in enumerate(active)
        )
        await admin.send(
            f"**Which commander?**\n{lines}\n\nReply with the number."
        )
        try:
            msg = await bot.wait_for('message', check=channel_check, timeout=CONVERSATION_TIMEOUT)
        except asyncio.TimeoutError:
            await admin.send("⏲️ Adjustment timed out.")
            return

        if not msg.content.strip().isdigit():
            await admin.send("❌ Invalid selection.")
            return

        idx = int(msg.content.strip()) - 1
        if not (0 <= idx < len(active)):
            await admin.send("❌ Invalid number.")
            return

        commander = active[idx]
    else:
        commander = active[0]

    # Pool choice
    await admin.send(
        "**Which pool?**\n"
        "```\n1. Fleet\n2. Ground\n3. Both (amount split equally)\n```"
    )
    try:
        pool_msg = await bot.wait_for('message', check=channel_check, timeout=CONVERSATION_TIMEOUT)
    except asyncio.TimeoutError:
        await admin.send("⏲️ Adjustment timed out.")
        return

    pool = POOL_OPTIONS.get(pool_msg.content.strip().lower())
    if not pool:
        await admin.send("❌ Invalid pool choice.")
        return

    # Amount — accepts negative for subtractions
    await admin.send(
        "**Amount?**\n"
        "Positive number to add, negative to subtract (e.g. `100` or `-50`).\n"
        "If Both is selected, the amount is split equally."
    )
    try:
        amount_msg = await bot.wait_for('message', check=channel_check, timeout=CONVERSATION_TIMEOUT)
    except asyncio.TimeoutError:
        await admin.send("⏲️ Adjustment timed out.")
        return

    try:
        amount = int(amount_msg.content.strip())
        if amount == 0:
            await admin.send("❌ Amount cannot be zero.")
            return
    except ValueError:
        await admin.send("❌ Please enter a whole number.")
        return

    # Reason
    await admin.send("**Reason for this adjustment?**")
    try:
        reason_msg = await bot.wait_for('message', check=channel_check, timeout=CONVERSATION_TIMEOUT)
    except asyncio.TimeoutError:
        await admin.send("⏲️ Adjustment timed out.")
        return

    reason = reason_msg.content.strip()

    # Calculate split amounts
    # For subtractions on Both, split the loss equally
    abs_amount    = abs(amount)
    fleet_amount, ground_amount = _split_ems(abs_amount, pool)

    if amount < 0:
        # Subtracting — use apply_ems_loss via domain to respect the floor
        fleet_amount  = -fleet_amount
        ground_amount = -ground_amount

    # Apply
    try:
        if fleet_amount:
            ems_repo.apply_ems_change(db, commander['commander_id'], 'fleet', fleet_amount, f"Admin adjustment by {admin.display_name}: {reason}", admin.id)
        if ground_amount:
            ems_repo.apply_ems_change(db, commander['commander_id'], 'army', ground_amount, f"Admin adjustment by {admin.display_name}: {reason}", admin.id)
    except Exception:
        log.exception(
            "Failed to apply EMS adjustment for commander %s",
            commander['commander_id']
        )
        await admin.send("❌ Something went wrong. Check logs.")
        return

    # Confirm in channel
    direction = "added to" if amount > 0 else "deducted from"
    pool_label = f"{pool.capitalize()} pool" if pool != 'both' else 'Both pools'
    await admin.send(
        f"✅ **EMS Adjustment Applied**\n"
        f"Commander: **{commander['commander_name']}**\n"
        f"Pool: **{pool_label}**\n"
        f"Amount: **{abs_amount} EMS** {direction} their {pool_label.lower()}\n"
        f"Reason: {reason}"
    )

    # DM the player
    target_user = bot.get_user(target_member.id) or await bot.fetch_user(target_member.id)
    if target_user:
        await _notify_player(
            bot, target_user, guild,
            f"📋 **EMS Adjustment**\n\n"
            f"An admin has adjusted EMS for **{commander['commander_name']}**.\n"
            f"**{abs_amount} EMS** was {direction} your **{pool_label.lower()}**.\n\n"
            f"Reason: {reason}\n\n"
            f"Contact an admin if you believe this is an error.",
            db=db,
        )


# =============================================================================
# REFIT FLOW
# =============================================================================

async def refit_ems(
    bot: discord.Client,
    admin: discord.Member,
    target_member: discord.Member,
    guild: discord.Guild,
    db,
    theme: dict,
    available_commanders: list[dict],
) -> None:
    """
    Entry point for /ems refit — guides admin through initiating a refit
    for a player's commander between campaigns.

    Flow:
        1. Which commander? (if multiple available)
        2. Which pool? (Fleet / Ground / Both)
        3. DM the player their current block + submission ID
        4. Mark refit as awaiting_block in DB
    """
    def dm_check(m):
        return m.author.id == admin.id and isinstance(m.channel, discord.DMChannel)

    # Commander choice
    if len(available_commanders) > 1:
        def _domain_label(c):
            has_fleet = bool(c.get('fleet_total_ems') or c.get('fleet_ems_block'))
            has_army  = bool(c.get('army_total_ems')  or c.get('army_ems_block'))
            if has_fleet and has_army: return 'Fleet + Ground'
            if has_army:  return 'Ground'
            return 'Fleet'

        lines = '\n'.join(
            f"{i + 1}. **{c['commander_name']}** ({_domain_label(c)})"
            for i, c in enumerate(available_commanders)
        )
        await admin.send(
            f"**Which commander is refitting?**\n{lines}\n\nReply with the number."
        )
        try:
            msg = await bot.wait_for('message', check=dm_check, timeout=CONVERSATION_TIMEOUT)
        except asyncio.TimeoutError:
            await admin.send("⏲️ Refit timed out.")
            return

        if not msg.content.strip().isdigit():
            await admin.send("❌ Invalid selection.")
            return

        idx = int(msg.content.strip()) - 1
        if not (0 <= idx < len(available_commanders)):
            await admin.send("❌ Invalid number.")
            return

        commander = available_commanders[idx]
    else:
        commander = available_commanders[0]

    # Pool choice
    await admin.send(
        "**Which pool is being refitted?**\n"
        "```\n1. Fleet\n2. Ground\n```"
    )
    try:
        pool_msg = await bot.wait_for('message', check=dm_check, timeout=CONVERSATION_TIMEOUT)
    except asyncio.TimeoutError:
        await admin.send("⏲️ Refit timed out.")
        return

    pool = POOL_OPTIONS.get(pool_msg.content.strip().lower())
    if pool not in ('fleet', 'ground'):
        await admin.send("❌ Invalid pool choice. Reply with 1 (Fleet) or 2 (Ground).")
        return

    # Get the current block to send to the player
    old_block = ''
    if pool == 'fleet':
        old_block = (commander.get('fleet_ems_block') or '(no fleet block on file)')
    else:
        old_block = (commander.get('army_ems_block') or '(no army block on file)')

    # Get the most recent approved submission code for this commander/pool
    block_col = 'fleet_ems_block' if pool == 'fleet' else 'army_ems_block'
    force_type = 'Fleet' if pool == 'fleet' else 'Army'
    sub_row = db.execute(
        """
        SELECT submission_code FROM commander_submissions
        WHERE commander_name = ? AND guild_id = ?
          AND force_type = ? AND status = 'approved'
        ORDER BY created_at DESC LIMIT 1
        """,
        (commander['commander_name'], guild.id, force_type),
    ).fetchone()
    sub_code = sub_row['submission_code'] if sub_row else '?'

    # Create refit record as awaiting_block
    from core.data import ems_repo
    refit_id = ems_repo.create_refit_request(
        db,
        guild_id=guild.id,
        user_id=target_member.id,
        commander_id=commander['commander_id'],
        pool=pool,
        old_block=old_block,
        new_block='',   # filled in when player submits
    )
    db.execute(
        "UPDATE refit_requests SET status = 'awaiting_block' WHERE refit_id = ?",
        (refit_id,),
    )
    db.commit()

    # DM the player their current block and instructions
    target_user = bot.get_user(target_member.id) or await bot.fetch_user(target_member.id)
    if target_user:
        await target_user.send(
            f"🔧 **Refit Initiated — {commander['commander_name']}**\n\n"
            f"A refit has been opened for your **{pool.capitalize()} pool**.\n\n"
            f"**Your current block:**\n```\n{old_block}\n```\n"
            f"Use the builder tool to update your block, then run `/ems submit` "
            f"with your **Submission ID: `{sub_code}`** and the new block.\n\n"
            f"Your new block must stay within your current EMS pool budget."
        )

    await admin.send(
        f"📬 Refit flow started for **{commander['commander_name']}** ({pool.capitalize()} pool).\n"
        f"The player has been DMed their current block and instructions.\n"
        f"Refit ID: `{refit_id}` — awaiting player block submission."
    )


# =============================================================================
# INTERNAL HELPERS
# =============================================================================

def _split_ems(amount: int, pool: str) -> tuple:
    """
    Calculate fleet and ground EMS amounts given a total and pool.

    For 'fleet':  all amount goes to fleet, ground gets 0.
    For 'ground': all amount goes to ground, fleet gets 0.
    For 'both':   floor division — each pool gets half. Remainders
                  are discarded (e.g. 101 → 50 fleet, 50 ground).

    Args:
        amount (int): The total EMS amount (always positive).
        pool   (str): 'fleet', 'ground', or 'both'.

    Returns:
        tuple: (fleet_amount, ground_amount)

    Examples:
        _split_ems(100, 'fleet')  → (100, 0)
        _split_ems(100, 'ground') → (0, 100)
        _split_ems(100, 'both')   → (50, 50)
        _split_ems(101, 'both')   → (50, 50)  # remainder discarded
        _split_ems(1,   'both')   → (0, 0)    # too small to split
    """
    if pool == 'fleet':
        return (amount, 0)
    elif pool == 'ground':
        return (0, amount)
    else:
        half = amount // 2
        return (half, half)

async def _notify_player(
    bot:     discord.Client,
    player:  discord.User,
    guild:   discord.Guild,
    message: str,
    db=None,
) -> None:
    """
    DM a player. Falls back to a configured notification channel if DMs
    are blocked. Logs silently if both fail.

    Args:
        bot:     The bot instance.
        player:  The player to notify.
        guild:   The guild (for fallback channel lookup).
        message: The message to send.
        db:      DB connection for fallback channel lookup (optional).
    """
    try:
        await player.send(message)
        return
    except discord.Forbidden:
        pass
    except discord.HTTPException:
        log.exception("Failed to DM player %s", player.id)
        return

    # Fallback — post in configured notification channel with mention
    guild_config     = guild_repo.get_guild_config(db, guild.id) if db else None
    notif_channel_id = guild_config.get('notification_channel_id') if guild_config else None
    if notif_channel_id:
        channel = guild.get_channel(notif_channel_id)
        if channel:
            try:
                await channel.send(f"{player.mention} — {message}")
            except discord.HTTPException:
                log.exception(
                    "Failed to post EMS notification for %s in channel %s",
                    player.id, notif_channel_id
                )


async def _try_dm(user: discord.User, message: str) -> None:
    """Attempt to DM a user. Swallows Forbidden silently."""
    try:
        await user.send(message)
    except discord.Forbidden:
        pass
    except discord.HTTPException:
        log.exception("Failed to DM user %s", user.id)
