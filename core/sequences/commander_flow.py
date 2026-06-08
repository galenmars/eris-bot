"""
sequences/commander_flow.py
===========================
E.R.I.S. Bot — Commander Creation Flow (UNUSED)

STATUS
------
Not currently imported. Reserved for NPC commander creation flow,
which cannot use the player-facing submission path in commander_cog.py.

WHAT THIS FILE CONTAINS
-----------------------
create_commander_slot()      — admin-initiated slot creation with DM flow
handle_block_submission()    — player pastes EMS block, validates and saves
_post_approval_embed()       — builds and posts to #commander-approvals
_approve_commander()         — approval path, writes commander record
_deny_commander()            — denial path, DMs player with reason
_validate_block_against_slot() — validates parsed block against pending submission
_get_response()              — DM wait_for helper
_try_fetch_user()            — safe user fetch, returns None on failure
"""

import asyncio
import logging

import discord

from core.data                   import commander_repo, guild_repo
from core.domain                 import commander as commander_domain
from core.domain                 import ems as ems_domain
from core.domain.exceptions      import DomainError
from core.shared.faction_picker  import prompt_faction, get_faction_alignment, _GROUP_LABELS

log = logging.getLogger(__name__)

# =============================================================================
# CONSTANTS
# =============================================================================

SUBMIT_TIMEOUT   = 600   # 10 minutes to paste the block
APPROVAL_TIMEOUT = 86400 # 24 hours for admin to approve/deny
DENY_TIMEOUT     = 300   # 5 minutes for admin to provide denial reason

APPROVE_EMOJI = '✅'
DENY_EMOJI    = '❌'


# =============================================================================
# PHASE 1 — ADMIN CREATES THE SLOT
# =============================================================================

async def create_commander_slot(
    bot:       discord.Client,
    ctx,
    player:    discord.Member,
    theme:     dict,
) -> None:
    """
    Admin-driven guided conversation to create a pending commander slot.

    Collects name, rank, faction, force type, tier, and EMS budget.
    Validates all inputs through domain. Writes a pending record.
    DMs the player their tier and EMS budget with fleet builder instructions.

    Args:
        bot:    The bot instance.
        ctx:    The command context (admin's channel).
        player: The Discord member getting the commander.
        theme:  The loaded theme dict.
    """
    db = bot.db

    def check(m):
        return m.author.id == ctx.author.id and m.channel == ctx.channel

    await ctx.send(
        f"📋 **Creating commander slot for {player.mention}**\n"
        f"Answer each question. Reply `cancel` at any time to abort.\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )

    # --- Commander name ---
    await ctx.send("**Commander name?**")
    name = await _get_response(bot, ctx, check, "Commander name")
    if name is None:
        return

    # --- Rank ---
    await ctx.send(
        "**Rank?**\n"
        "```\n1. Main\n2. Senior\n3. Junior\n```"
    )
    rank_map = {'1': 'Main', '2': 'Senior', '3': 'Junior',
                'main': 'Main', 'senior': 'Senior', 'junior': 'Junior'}
    rank_raw = await _get_response(bot, ctx, check, "Rank")
    if rank_raw is None:
        return
    rank = rank_map.get(rank_raw.lower())
    if not rank:
        await ctx.send("❌ Invalid rank. Use `/commander create` to try again.")
        return

    # Check Main limit before continuing
    existing = commander_repo.get_commanders_for_user(db, player.id)
    if not commander_domain.can_create_main(existing) and rank == 'Main':
        main_cmd = commander_domain.find_main_commander(existing)
        await ctx.send(
            f"❌ {player.mention} already has a Main commander "
            f"(**{main_cmd['commander_name']}**). "
            f"A player can only have one Main."
        )
        return

    # --- Faction / Organization ---
    _orgs   = guild_repo.get_organizations(db, ctx.guild.id)
    _result = await prompt_faction(channel=ctx, user=ctx.author, theme=theme, orgs=_orgs or None)
    if _result is None:
        return
    faction       = _result.name          # org name (or faction name if no orgs)
    faction_name  = _result.faction_name  # canonical theme faction
    faction_align = _result.group         # alignment group key

    # --- Force type ---
    await ctx.send(
        "**Force type?**\n"
        "```\n1. Fleet\n2. Army\n```"
    )
    force_map = {
        '1': 'Fleet', '2': 'Army',
        'fleet': 'Fleet', 'army': 'Army'
    }
    force_raw  = await _get_response(bot, ctx, check, "Force type")
    if force_raw is None:
        return
    force_type = force_map.get(force_raw.lower())
    if not force_type:
        await ctx.send("❌ Invalid force type. Use `/commander create` to try again.")
        return

    try:
        commander_domain.validate_force_type(rank, force_type)
    except DomainError as e:
        await ctx.send(f"❌ {e}")
        return

    # --- Tier ---
    await ctx.send(
        "**Tier?**\n"
        "```\n1. Tier 1\n2. Tier 2\n3. Tier 3\n```"
    )
    tier_raw = await _get_response(bot, ctx, check, "Tier")
    if tier_raw is None:
        return
    if not tier_raw.isdigit() or int(tier_raw) not in (1, 2, 3):
        await ctx.send("❌ Tier must be 1, 2, or 3.")
        return
    tier = int(tier_raw)

    # --- EMS Budget ---
    await ctx.send(
        f"**EMS budget for this commander?**\n"
        f"This is the total EMS the player may allocate.\n"
        f"Tier {tier} cap per unit: "
        f"Fleet {ems_domain.get_tier_ems_cap(tier, 'space',  bot.get_theme(ctx.guild.id))} · "
        f"Ground {ems_domain.get_tier_ems_cap(tier, 'ground', bot.get_theme(ctx.guild.id))}"
    )
    budget_raw = await _get_response(bot, ctx, check, "EMS budget")
    if budget_raw is None:
        return
    if not budget_raw.isdigit() or int(budget_raw) < 1:
        await ctx.send("❌ EMS budget must be a positive number.")
        return
    ems_budget = int(budget_raw)

    # --- Confirm ---
    _faction_display = (
        f"{faction} · {faction_name} [{_GROUP_LABELS.get(faction_align, faction_align)}]"
        if faction != faction_name
        else f"{faction} [{_GROUP_LABELS.get(faction_align, faction_align)}]"
    )
    await ctx.send(
        f"**Confirm commander slot:**\n"
        f"```\n"
        f"Player:     {player.display_name}\n"
        f"Name:       {name}\n"
        f"Rank:       {rank}\n"
        f"Faction:    {_faction_display}\n"
        f"Force type: {force_type}\n"
        f"Tier:       {tier}\n"
        f"EMS Budget: {ems_budget}\n"
        f"```\n"
        f"Reply `yes` to create or `no` to cancel."
    )
    confirm = await _get_response(bot, ctx, check, "Confirmation")
    if confirm is None or confirm.lower() not in ('yes', 'y'):
        await ctx.send("❌ Commander slot creation cancelled.")
        return

    # --- Write pending record ---
    try:
        cursor = db.get_cursor()
        request_id = commander_repo.create_pending_commander(
            cursor,
            player_id      = player.id,
            commander_name = name,
            rank           = rank,
            faction        = faction,
            force_type     = force_type,
            tier           = tier,
            ems_budget     = ems_budget,
        )
        db.conn.commit()
    except Exception:
        log.exception("Failed to write pending commander for player %s", player.id)
        await ctx.send("❌ Database error. Please try again.")
        return

    await ctx.send(
        f"✅ **Commander slot created!** (Request ID: `#{request_id}`)\n"
        f"{player.mention} has been notified."
    )

    # --- DM the player ---
    guild_config = guild_repo.get_guild_config(db, ctx.guild.id)
    fleet_url    = guild_config.get('fleet_url', '[fleet builder URL not configured]')
    submit_ch_id = guild_config.get('channels', {}).get('commander-submissions')
    submit_ch    = f"<#{submit_ch_id}>" if submit_ch_id else '#commander-submissions'

    battle_type_label = force_type

    try:
        await player.send(
            f"🎖️ **Your commander slot has been reserved!**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Commander:** {name}\n"
            f"**Rank:**      {rank}\n"
            f"**Faction:**   {_faction_display}\n"
            f"**Force:**     {battle_type_label}\n"
            f"**Tier:**      {tier}\n"
            f"**EMS Budget:**{ems_budget} EMS\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Next steps:**\n"
            f"1. Build your {force_type.lower()} at:\n"
            f"   {fleet_url}\n"
            f"2. Copy your completed block from the builder\n"
            f"3. Go to {submit_ch} and use `/ems submit`\n"
            f"4. Paste your block when prompted\n\n"
            f"An admin will review and approve your submission.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Request ID: `#{request_id}` — keep this for reference."
        )
    except discord.Forbidden:
        await ctx.send(
            f"⚠️ Could not DM {player.mention} — they have DMs disabled.\n"
            f"Please share their slot details manually:\n"
            f"Tier: {tier} · EMS Budget: {ems_budget} · Use `/ems submit` in {submit_ch}"
        )


# =============================================================================
# PHASE 2 — PLAYER SUBMITS THE BLOCK
# =============================================================================

async def handle_block_submission(
    bot:     discord.Client,
    ctx,
    theme:   dict,
) -> None:
    """
    Handle /ems submit — prompt the player to paste their commander block.

    Validates the block through domain, then posts a preview to
    #commander-approvals for admin review. No database write yet.

    Args:
        bot:   The bot instance.
        ctx:   The command context (must be in #commander-submissions).
        theme: The loaded theme dict.
    """
    db   = bot.db
    user = ctx.author

    # Check the player has a pending commander slot
    pending = commander_repo.get_pending_commander(db, user.id)
    if not pending:
        await ctx.send(
            f"{user.mention} — you don't have a pending commander slot.\n"
            f"Ask an admin to use `/commander create @you` first.",
            ephemeral=True
        )
        return

    await ctx.send(
        f"📋 **Commander Block Submission**\n"
        f"{user.mention} — paste your complete commander block below.\n"
        f"Copy it from the fleet builder and paste the entire block.\n\n"
        f"You have 10 minutes. Reply `cancel` to abort.",
        ephemeral=False
    )

    def check(m):
        return m.author.id == user.id and m.channel == ctx.channel

    try:
        msg = await bot.wait_for('message', check=check, timeout=SUBMIT_TIMEOUT)
    except asyncio.TimeoutError:
        await ctx.send(f"⏲️ {user.mention} — submission timed out. Use `/ems submit` to try again.")
        return

    if msg.content.strip().lower() == 'cancel':
        await ctx.send(f"❌ {user.mention} — submission cancelled.")
        return

    block_text = msg.content.strip()

    # Parse and validate through domain
    try:
        parsed = commander_domain.parse_commander_block(block_text, theme)
    except DomainError as e:
        await ctx.send(
            f"❌ {user.mention} — your block has an error:\n\n"
            f"```\n{e}\n```\n"
            f"Fix the issue in the fleet builder and use `/ems submit` again."
        )
        return

    # Cross-check parsed block against the pending slot
    slot_errors = _validate_block_against_slot(parsed, pending)
    if slot_errors:
        await ctx.send(
            f"❌ {user.mention} — your block doesn't match your reserved slot:\n\n"
            + '\n'.join(f"  • {err}" for err in slot_errors) +
            f"\n\nCheck your slot details (Request ID `#{pending['request_id']}`) "
            f"and resubmit."
        )
        return

    # Save the raw block text against the pending record
    cursor = db.get_cursor()
    commander_repo.attach_block_to_pending(
        cursor,
        request_id = pending['request_id'],
        raw_block  = block_text,
        parsed     = parsed,
    )
    db.conn.commit()

    await ctx.send(
        f"✅ {user.mention} — block received and validated!\n"
        f"An admin will review your submission soon.\n"
        f"You'll receive a DM when a decision is made."
    )

    # Post preview to #commander-approvals
    await _post_approval_embed(bot, db, ctx.guild, user, pending, parsed)


# =============================================================================
# PHASE 3 — ADMIN APPROVES OR DENIES
# =============================================================================

async def _post_approval_embed(
    bot:     discord.Client,
    db,
    guild:   discord.Guild,
    player:  discord.User,
    pending: dict,
    parsed:  dict,
) -> None:
    """
    Post the parsed commander preview to #commander-approvals.

    Admins react ✅ to approve or ❌ to deny.
    """
    guild_config      = guild_repo.get_guild_config(db, guild.id)
    approvals_ch_id   = guild_config.get('channels', {}).get('commander-approvals')
    approvals_channel = guild.get_channel(approvals_ch_id) if approvals_ch_id else None

    if not approvals_channel:
        log.warning("No commander-approvals channel configured for guild %s", guild.id)
        return

    # Build unit list for the embed
    unit_lines = '\n'.join(
        f"  ({u['qty']}) [{u['current']}/{u['max']}] "
        f"{u['unit_type']} — "
        f"{', '.join(u['names'])}"
        for u in parsed.get('units', [])
    )

    embed = discord.Embed(
        title       = f"📋 Commander Submission #{pending['request_id']}",
        description = f"**{player.display_name}** has submitted a commander block.",
        color       = discord.Color.blue()
    )
    embed.add_field(name="Name",       value=parsed['name'],       inline=True)
    embed.add_field(name="Rank",       value=parsed['rank'],       inline=True)
    _alignment    = get_faction_alignment(parsed['faction'], bot.get_theme())
    _faction_disp = f"{parsed['faction']} [{_alignment}]" if _alignment else parsed['faction']
    embed.add_field(name="Faction",    value=_faction_disp,        inline=True)
    embed.add_field(name="Force Type", value=parsed['force_type'], inline=True)
    embed.add_field(name="Tier",       value=str(parsed['tier']),  inline=True)
    embed.add_field(name="Total EMS",  value=str(parsed['total_ems']), inline=True)
    embed.add_field(
        name   = "Fleet Composition",
        value  = f"```\n{unit_lines}\n```" if unit_lines else "*None*",
        inline = False
    )
    embed.set_footer(
        text=f"React {APPROVE_EMOJI} to approve · {DENY_EMOJI} to deny"
    )

    try:
        review_msg = await approvals_channel.send(embed=embed)
        await review_msg.add_reaction(APPROVE_EMOJI)
        await review_msg.add_reaction(DENY_EMOJI)
    except discord.HTTPException:
        log.exception("Failed to post approval embed for request %s", pending['request_id'])
        return

    # Listen for admin reaction
    def reaction_check(reaction, user):
        return (
            str(reaction.emoji) in (APPROVE_EMOJI, DENY_EMOJI) and
            reaction.message.id == review_msg.id and
            not user.bot
        )

    try:
        reaction, admin = await bot.wait_for(
            'reaction_add', check=reaction_check, timeout=APPROVAL_TIMEOUT
        )
    except asyncio.TimeoutError:
        await approvals_channel.send(
            f"⏲️ Commander submission #{pending['request_id']} expired with no decision."
        )
        return

    if str(reaction.emoji) == APPROVE_EMOJI:
        await _approve_commander(bot, db, guild, admin, approvals_channel,
                                  player, pending, parsed)
    else:
        await _deny_commander(bot, db, guild, admin, approvals_channel,
                               player, pending)


async def _approve_commander(
    bot, db, guild, admin, approvals_channel,
    player, pending, parsed,
) -> None:
    """Write the commander to the database and notify the player."""
    try:
        cursor = db.get_cursor()
        commander_repo.create_commander_from_pending(
            cursor,
            pending_id = pending['request_id'],
            parsed     = parsed,
        )
        db.conn.commit()
    except Exception:
        log.exception("Failed to write commander from pending %s", pending['request_id'])
        await approvals_channel.send(
            f"❌ Database error approving submission #{pending['request_id']}. Check logs."
        )
        return

    _alignment    = get_faction_alignment(parsed['faction'], bot.get_theme())
    _faction_disp = f"{parsed['faction']} [{_alignment}]" if _alignment else parsed['faction']

    await approvals_channel.send(
        f"✅ **Submission #{pending['request_id']} approved** by {admin.mention}.\n"
        f"**{parsed['name']}** ({parsed['rank']}, {_faction_disp}) is now active."
    )

    # DM the player
    player_obj = guild.get_member(player.id) or await _try_fetch_user(bot, player.id)
    if player_obj:
        try:
            await player_obj.send(
                f"✅ **Commander Approved!**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"**{parsed['name']}** has been activated.\n\n"
                f"**Rank:**      {parsed['rank']}\n"
                f"**Faction:**   {_faction_disp}\n"
                f"**Force:**     {parsed['force_type']}\n"
                f"**Tier:**      {parsed['tier']}\n"
                f"**Total EMS:** {parsed['total_ems']}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"You can now enroll in campaigns. Good luck, Commander."
            )
        except discord.Forbidden:
            pass


async def _deny_commander(
    bot, db, guild, admin, approvals_channel,
    player, pending,
) -> None:
    """Ask admin for denial reason, notify player, keep slot open for resubmission."""
    def admin_dm_check(m):
        return m.author.id == admin.id and isinstance(m.channel, discord.DMChannel)

    try:
        await admin.send(
            f"❌ **Denying Commander Submission #{pending['request_id']}**\n\n"
            f"Please provide a reason. The player will receive this message.\n"
            f"Reply with your reason."
        )
    except discord.Forbidden:
        await approvals_channel.send(
            f"{admin.mention} — I can't DM you. Enable DMs and re-react to deny."
        )
        return

    try:
        reason_msg    = await bot.wait_for('message', check=admin_dm_check, timeout=DENY_TIMEOUT)
        denial_reason = reason_msg.content.strip()
    except asyncio.TimeoutError:
        await admin.send("⏲️ Denial reason timed out. The submission remains pending.")
        return

    # Update the pending record — keep it open so player can resubmit
    cursor = db.get_cursor()
    commander_repo.update_pending_status(
        cursor,
        request_id = pending['request_id'],
        status     = 'denied',
        reason     = denial_reason,
    )
    db.conn.commit()

    await approvals_channel.send(
        f"❌ **Submission #{pending['request_id']} denied** by {admin.mention}.\n"
        f"Reason: {denial_reason}\n"
        f"The player may revise and resubmit."
    )

    # DM the player
    player_obj = guild.get_member(player.id) or await _try_fetch_user(bot, player.id)
    if player_obj:
        try:
            await player_obj.send(
                f"❌ **Commander Submission Denied**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Your submission for **{pending['commander_name']}** "
                f"(#{pending['request_id']}) was not approved.\n\n"
                f"**Reason:** {denial_reason}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Your slot is still reserved. Fix the issue in the fleet builder\n"
                f"and use `/ems submit` to resubmit."
            )
        except discord.Forbidden:
            pass


# =============================================================================
# HELPERS
# =============================================================================

def _validate_block_against_slot(parsed: dict, pending: dict) -> list:
    """
    Cross-check the parsed block against the admin-reserved slot.

    Returns a list of error strings. Empty list = all good.

    Fields checked:
        - Commander name must match
        - Rank must match
        - Faction must match
        - Force type must match
        - Tier must match
        - Total EMS must not exceed budget
    """
    errors = []

    if parsed['name'].lower() != pending['commander_name'].lower():
        errors.append(
            f"Name mismatch: block has '{parsed['name']}', "
            f"slot has '{pending['commander_name']}'"
        )
    if parsed['rank'].lower() != pending['rank'].lower():
        errors.append(
            f"Rank mismatch: block has '{parsed['rank']}', "
            f"slot has '{pending['rank']}'"
        )
    if parsed['faction'].lower() != pending['faction'].lower():
        errors.append(
            f"Faction mismatch: block has '{parsed['faction']}', "
            f"slot has '{pending['faction']}'"
        )
    if parsed['force_type'].lower() != pending['force_type'].lower():
        errors.append(
            f"Force type mismatch: block has '{parsed['force_type']}', "
            f"slot has '{pending['force_type']}'"
        )
    if parsed['tier'] != pending['tier']:
        errors.append(
            f"Tier mismatch: block has Tier {parsed['tier']}, "
            f"slot has Tier {pending['tier']}"
        )
    if parsed['total_ems'] > pending['ems_budget']:
        errors.append(
            f"EMS over budget: block has {parsed['total_ems']} EMS, "
            f"budget is {pending['ems_budget']}"
        )

    return errors


async def _get_response(
    bot,
    ctx,
    check,
    field_name: str,
    timeout:    int = 120,
) -> str | None:
    """
    Wait for a single message response in a guided conversation.

    Returns the message content, or None on timeout/cancel.
    """
    try:
        msg = await bot.wait_for('message', check=check, timeout=timeout)
    except asyncio.TimeoutError:
        await ctx.send(f"⏲️ Timed out waiting for {field_name}. Command cancelled.")
        return None

    text = msg.content.strip()
    if text.lower() == 'cancel':
        await ctx.send("❌ Cancelled.")
        return None

    return text


async def _try_fetch_user(bot, user_id: int):
    """Fetch a user by ID, returning None on failure."""
    try:
        return await bot.fetch_user(user_id)
    except discord.HTTPException:
        return None
