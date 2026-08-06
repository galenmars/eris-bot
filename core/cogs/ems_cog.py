"""
cogs/ems_cog.py
===============
E.R.I.S. Bot — EMS Commands

PURPOSE
-------
Discord-facing surface for EMS management.
Players request EMS. Admins approve, deny, or adjust directly.
All multi-step flows delegate to sequences/ems_flow.py.

COMMANDS
--------
/ems request          — deployed member, guided DM conversation
/ems submit           — player, pastes fleet/army builder block after approval
/ems balance          — any member, DMs own EMS pool balances
/ems view @player     — any member, DMs requester that player's commander block
/ems history [name]   — any member (own), admin (any), last 10 changes
/ems refit            — player, requests post-campaign block refit via DM
/ems adjust @player   — admin only, direct add/subtract
/ems approve [id]     — admin only
/ems deny [id]        — admin only, asks for reason
/ems pending          — admin only, lists pending requests
/ems refit_approve    — admin only, approve a pending refit request
/ems refit_deny       — admin only, deny a pending refit request

WHAT THIS COG DOES NOT OWN
---------------------------
EMS flow logic        → sequences/ems_flow.py
EMS math / parsing    → domain/ems.py
Database ops          → data/ems_repo.py, data/commander_repo.py
Shop / bank           → future cogs/shop_cog.py
"""

import logging

import discord
from discord.ext import commands
from discord     import app_commands

from core.data              import ems_repo, commander_repo, guild_repo
from core.domain            import ems as ems_domain
from core.domain.exceptions import DomainError
from core.sequences         import ems_flow
from core.shared            import embeds

log = logging.getLogger(__name__)

# =============================================================================
# BLOCK PARSER
# =============================================================================

def _parse_block(text: str) -> dict | None:
    """
    Parse an ERIS commander block — fleet or army format.

    Fleet blocks start with:  === ERIS COMMANDER BLOCK ===
    Army blocks start with:   === ERIS GROUND COMMANDER BLOCK ===
    Both end with:            === END BLOCK ===

    Army field names are normalised to their fleet equivalents before
    parsing so the rest of the submit flow is format-agnostic:
        GROUND_EMS_POOL -> EMS_POOL
        ARMY_TYPE       -> FLEET_TYPE

    Handles both newline-separated and space-collapsed (Discord slash
    command) formats.
    """
    text = text.strip().strip('`')
    if text.startswith('glsl'):
        text = text[4:].strip()

    # Accept both block headers; reject anything else
    is_fleet = '=== ERIS COMMANDER BLOCK ===' in text
    is_army  = '=== ERIS GROUND COMMANDER BLOCK ===' in text
    if (not is_fleet and not is_army) or '=== END BLOCK ===' not in text:
        return None

    # Normalise army field names to the shared keys expected downstream
    if is_army:
        text = text.replace('GROUND_EMS_POOL:', 'EMS_POOL:')
        text = text.replace('ARMY_TYPE:',       'FLEET_TYPE:')

    # Normalize — replace known field keys with newline+key so parsing works
    # whether Discord collapsed newlines or not
    FIELDS = [
        'SUBMISSION_ID', 'NAME', 'TITLE', 'FLEET_NAME', 'ARMY_NAME',
        'TIER', 'EMS_POOL', 'EMS_SPENT', 'FLEET_TYPE', 'BATTLE_OPTIONS',
        'COMPOSITION', 'SHIPS',
    ]
    for field in FIELDS:
        text = text.replace(f' {field}:', f'\n{field}:')

    result = {}
    for line in text.splitlines():
        line = line.strip()
        if ':' in line and not line.startswith('==='):
            key, _, val = line.partition(':')
            key = key.strip()
            if key in FIELDS:
                result[key] = val.strip()

    # Tag which format this block came from so callers can branch if needed
    result['_block_type'] = 'army' if is_army else 'fleet'

    return result if 'SUBMISSION_ID' in result else None


def _extract_units_section(block: str) -> str:
    """
    Extract just the SHIPS or UNITS lines from a block for diff display.
    Handles both newline-separated and Discord-collapsed (space-separated) formats.
    """
    import re

    # Normalize top-level field keywords to newlines
    for keyword in [
        'SHIPS:', 'UNITS:', 'COMPOSITION:', 'BATTLE_OPTIONS:',
        'FLEET_TYPE:', 'EMS_SPENT:', 'EMS_POOL:', 'TIER:',
        'FLEET_NAME:', 'ARMY_NAME:', 'TITLE:', 'NAME:',
        'SUBMISSION_ID:', '=== END BLOCK ===',
    ]:
        block = block.replace(f' {keyword}', f'\n{keyword}')

    lines = block.splitlines()
    collecting = False
    raw_units = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('SHIPS:') or stripped.startswith('UNITS:'):
            collecting = True
            # Remainder of this line may contain ship entries (collapsed format)
            remainder = stripped.split(':', 1)[1].strip()
            if remainder:
                raw_units.append(remainder)
            continue
        if collecting:
            if stripped.startswith('===') or (
                stripped and not stripped.startswith(' ')
                and ':' in stripped and not stripped.startswith('-')
                and not re.match(r'.+@\s*\d+', stripped)
            ):
                break
            if stripped:
                raw_units.append(stripped)

    # Further split entries that are still joined (e.g. "Ship A   Ship B")
    # Ship lines contain "@" and "EMS" — split on multiple spaces before a capital letter
    result = []
    for entry in raw_units:
        # Split on 2+ spaces followed by a capital letter (next ship entry)
        parts = re.split(r'\s{2,}(?=[A-Z])', entry)
        for p in parts:
            p = p.strip()
            if p and ('@' in p or 'EMS' in p):
                result.append(p)

    return '\n'.join(result) if result else '(none)'

# =============================================================================
# EMS VIEW — embed builder + commander picker (used only by /ems view)
# =============================================================================

def _build_ems_view_embed(commander: dict, owner: discord.Member | None) -> discord.Embed:
    """Build the private EMS block embed for a single commander."""
    force  = commander.get('force_type', 'N/A')
    status = "Deployed" if commander.get('active_campaign') else "Available"

    embed = discord.Embed(
        title=f"🗂️ {commander['commander_name']} [{commander.get('rank', 'N/A')}]",
        color=discord.Color.blue(),
    )
    embed.add_field(name="Player", value=owner.mention if owner else "Unknown", inline=True)
    embed.add_field(name="Force Type", value=force, inline=True)
    embed.add_field(name="Status", value=status, inline=True)

    if force == 'Fleet':
        block = commander.get('fleet_ems_block')
        pool  = commander.get('fleet_total_ems', 0) or 0
        label = commander.get('fleet_name') or "Unnamed Fleet"
        embed.add_field(name="Fleet Name", value=label, inline=True)
        embed.add_field(name="Fleet EMS Pool", value=str(pool), inline=True)
    elif force == 'Army':
        block = commander.get('army_ems_block')
        pool  = commander.get('army_total_ems', 0) or 0
        label = commander.get('army_name') or "Unnamed Army"
        embed.add_field(name="Army Name", value=label, inline=True)
        embed.add_field(name="Army EMS Pool", value=str(pool), inline=True)
    else:
        block = None

    units = _extract_units_section(block) if block else None
    if units and units != '(none)':
        if len(units) > 1000:
            units = units[:1000].rsplit('\n', 1)[0] + "\n… (truncated)"
        embed.add_field(name="Units", value=f"```\n{units}\n```", inline=False)
    else:
        embed.add_field(name="Units", value="No block submitted yet.", inline=False)

    return embed


class _CommanderSelect(discord.ui.Select):
    """Dropdown letting the requester pick which of a player's commanders to view."""

    def __init__(self, commanders: list[dict], owner: discord.Member):
        self.commanders_by_id = {str(c['commander_id']): c for c in commanders}
        self.owner = owner

        options = [
            discord.SelectOption(
                label=c['commander_name'][:100],
                description=f"{c.get('force_type', '?')} — {c.get('rank', 'N/A')}"[:100],
                value=str(c['commander_id']),
            )
            for c in commanders[:25]
        ]
        super().__init__(placeholder="Choose a commander…", options=options)

    async def callback(self, interaction: discord.Interaction):
        commander = self.commanders_by_id[self.values[0]]
        embed = _build_ems_view_embed(commander, self.owner)
        await interaction.response.edit_message(content=None, embed=embed, view=None)


class _CommanderSelectView(discord.ui.View):
    def __init__(self, commanders: list[dict], owner: discord.Member):
        super().__init__(timeout=300)
        self.message = None
        self.add_item(_CommanderSelect(commanders, owner))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

# =============================================================================
# EMS COG
# =============================================================================

class EmsCog(commands.Cog, name="EMS"):
    """EMS management commands."""

    def __init__(self, bot):
        self.bot = bot
        self.db  = bot.db.conn

    def _is_admin(self, member: discord.Member) -> bool:
        guild_config  = guild_repo.get_guild_config(self.db, member.guild.id)
        admin_role_id = guild_config.get('roles', {}).get('ERIS Admin') if guild_config else None
        if not admin_role_id:
            return member.guild_permissions.administrator
        return any(r.id == admin_role_id for r in member.roles)

    def _theme(self, guild_id: int) -> dict:
        return self.bot.get_theme(guild_id)

    # =========================================================================
    # EMS GROUP
    # =========================================================================

    ems_group = app_commands.Group(
        name="ems",
        description="EMS management commands.",
    )

    # -------------------------------------------------------------------------
    # /ems request  — guided conversation in DMs
    # -------------------------------------------------------------------------

    @ems_group.command(
        name="request",
        description="Request EMS for your commander.",
    )
    async def ems_request(self, interaction: discord.Interaction):
        """
        Opens a guided DM conversation collecting:
          1. Pool choice (Fleet / Ground / Both)
          2. Amount
          3. Reason
        Then posts a review embed to #ems-requests for admin approval.
        """
        # Check the player has at least one commander
        commanders = commander_repo.get_commanders_for_user(
            self.db, interaction.user.id, interaction.guild.id
        )
        if not commanders:
            await interaction.response.send_message(
                "❌ You don't have any commanders yet.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "📬 Check your DMs — I'll walk you through the EMS request there.",
            ephemeral=True,
        )

        await ems_flow.request_ems(
            bot=self.bot,
            member=interaction.user,
            guild=interaction.guild,
            db=self.db,
            theme=self._theme(interaction.guild.id),
        )

    # -------------------------------------------------------------------------
    # /ems balance
    # -------------------------------------------------------------------------

    @ems_group.command(name="balance", description="View your current EMS pool balances.")
    async def ems_balance(self, interaction: discord.Interaction):
        """DMs the player their fleet and army EMS totals for each commander."""
        await interaction.response.defer(ephemeral=True)

        commanders = commander_repo.get_commanders_for_user(
            self.db, interaction.user.id, interaction.guild.id
        )
        if not commanders:
            await interaction.followup.send("You don't have any commanders.", ephemeral=True)
            return

        embed = discord.Embed(
            title="📊 Your EMS Balances",
            color=discord.Color.blue(),
        )

        for c in commanders:
            fleet_ems = c.get('fleet_total_ems', 0) or 0
            army_ems  = c.get('army_total_ems',  0) or 0
            force     = c.get('force_type', 'N/A')

            lines = []
            if force == 'Fleet':
                lines.append(f"🚀 Fleet EMS: **{fleet_ems}**")
            if force == 'Army':
                lines.append(f"🪖 Army EMS:  **{army_ems}**")

            status = "Deployed" if c.get('active_campaign') else "Available"
            embed.add_field(
                name=f"{c['commander_name']} [{c['rank']}] — {status}",
                value="\n".join(lines) or "No EMS data.",
                inline=False,
            )

        try:
            await interaction.user.send(embed=embed)
            await interaction.followup.send("📬 EMS balances sent to your DMs.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(embed=embed, ephemeral=True)

    # -------------------------------------------------------------------------
    # /ems view — any member, DMs the requester a player's commander EMS block
    # -------------------------------------------------------------------------

    @ems_group.command(
        name="view",
        description="Privately view a player's commander EMS block in your DMs.",
    )
    @app_commands.describe(player="The player whose commander(s) to look up.")
    async def ems_view(self, interaction: discord.Interaction, player: discord.Member):
        await interaction.response.defer(ephemeral=True)

        if player.id != interaction.user.id and not self._is_admin(interaction.user):
            await interaction.followup.send(
                "❌ You can only view your own commanders.", ephemeral=True
            )
            return

        commanders = commander_repo.get_commanders_for_user(
            self.db, player.id, interaction.guild.id
        )
        if not commanders:
            await interaction.followup.send(
                f"❌ **{player.display_name}** has no commanders.", ephemeral=True
            )
            return

        try:
            if len(commanders) == 1:
                await interaction.user.send(
                    embed=_build_ems_view_embed(commanders[0], player)
                )
            else:
                view = _CommanderSelectView(commanders, player)
                view.message = await interaction.user.send(
                    f"**{player.display_name}** has multiple commanders — pick one:",
                    view=view,
                )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I couldn't DM you — open your DMs for this server and try again.",
                ephemeral=True,
            )
            return

        await interaction.followup.send("📬 Check your DMs.", ephemeral=True)

    # -------------------------------------------------------------------------
    # /ems history
    # -------------------------------------------------------------------------

    @ems_group.command(
        name="history",
        description="View EMS history for a commander.",
    )
    @app_commands.describe(
        commander_name="Commander name (leave blank for your own commanders)",
        player="Admin only — view another player's history",
    )
    async def ems_history(
        self,
        interaction: discord.Interaction,
        commander_name: str | None = None,
        player: discord.Member | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        # Admin can view any player; non-admins can only view their own
        if player and player.id != interaction.user.id:
            if not self._is_admin(interaction.user):
                await interaction.followup.send(
                    "❌ Only admins can view other players' EMS history.",
                    ephemeral=True,
                )
                return
            target_user_id = player.id
        else:
            target_user_id = interaction.user.id

        # Find the commander
        commanders = commander_repo.get_commanders_for_user(
            self.db, target_user_id, interaction.guild.id
        )
        if not commanders:
            await interaction.followup.send("No commanders found.", ephemeral=True)
            return

        if commander_name:
            # Find matching commander (case-insensitive partial match)
            commander = next(
                (c for c in commanders if commander_name.lower() in c['commander_name'].lower()),
                None,
            )
            if not commander:
                await interaction.followup.send(
                    f"❌ No commander matching '{commander_name}' found.",
                    ephemeral=True,
                )
                return
        else:
            if len(commanders) == 1:
                commander = commanders[0]
            else:
                # Ask which commander
                names = "\n".join(f"{i+1}. {c['commander_name']}" for i, c in enumerate(commanders))
                await interaction.followup.send(
                    f"You have multiple commanders. Specify the name:\n{names}",
                    ephemeral=True,
                )
                return

        # Fetch history
        history = ems_repo.get_ems_history(self.db, commander['commander_id'], limit=10)

        if not history:
            await interaction.followup.send(
                f"No EMS history found for **{commander['commander_name']}**.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"📋 EMS History — {commander['commander_name']}",
            color=discord.Color.blue(),
        )

        for h in history:
            change     = h.get('change_amount', 0)
            pool       = h.get('pool', '?').capitalize()
            reason     = h.get('reason', 'No reason given')
            change_str = f"+{change}" if change >= 0 else str(change)
            embed.add_field(
                name=f"{pool}: {change_str} EMS",
                value=reason,
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # -------------------------------------------------------------------------
    # /ems adjust  — admin only, direct write
    # -------------------------------------------------------------------------

    @ems_group.command(
        name="adjust",
        description="[Admin] Directly add or subtract EMS from a player's commander.",
    )
    @app_commands.describe(player="The player whose EMS to adjust")
    async def ems_adjust(self, interaction: discord.Interaction, player: discord.Member):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only ERIS Admins can adjust EMS directly.", ephemeral=True
            )
            return

        commanders = commander_repo.get_commanders_for_user(
            self.db, player.id, interaction.guild.id
        )
        if not commanders:
            await interaction.response.send_message(
                f"❌ {player.mention} has no commanders.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"📬 EMS adjustment flow started for {player.mention}. "
            f"Continue in the channel.",
            ephemeral=True,
        )

        await ems_flow.adjust_ems(
            bot=self.bot,
            admin=interaction.user,
            target_member=player,
            channel=interaction.channel,
            guild=interaction.guild,
            db=self.db,
            theme=self._theme(interaction.guild.id),
        )

    # -------------------------------------------------------------------------
    # /ems approve
    # -------------------------------------------------------------------------

    @ems_group.command(
        name="approve",
        description="[Admin] Approve a pending EMS request.",
    )
    @app_commands.describe(request_id="The request ID to approve")
    async def ems_approve(self, interaction: discord.Interaction, request_id: int):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only ERIS Admins can approve EMS requests.", ephemeral=True
            )
            return

        await interaction.response.defer()

        request = ems_repo.get_request(self.db, request_id)
        if not request:
            await interaction.followup.send(f"❌ No request found with ID {request_id}.")
            return

        if request.get('status') != 'pending':
            await interaction.followup.send(
                f"❌ Request {request_id} is already **{request['status']}**."
            )
            return

        # Resolve all args the flow function needs
        commander = commander_repo.get_by_id(self.db, request['commander_id'])
        if not commander:
            await interaction.followup.send(f"❌ Commander for request {request_id} not found.")
            return

        player = await self.bot.fetch_user(request['user_id'])
        if not player:
            await interaction.followup.send(f"❌ Could not resolve player for request {request_id}.")
            return

        guild_config = guild_repo.get_guild_config(self.db, interaction.guild.id)
        review_channel_id = guild_config.get('ems_requests_channel_id') if guild_config else None
        review_channel = interaction.guild.get_channel(review_channel_id) if review_channel_id else interaction.channel

        pool          = request['pool']
        total         = request['amount']
        fleet_amount, ground_amount = ems_flow._split_ems(total, pool)

        await ems_flow.approve_ems(
            bot=self.bot,
            db=self.db,
            guild=interaction.guild,
            admin=interaction.user,
            review_channel=review_channel,
            request_id=request_id,
            player=player,
            commander=commander,
            pool=pool,
            fleet_amount=fleet_amount,
            ground_amount=ground_amount,
            total=total,
        )

    # -------------------------------------------------------------------------
    # /ems deny
    # -------------------------------------------------------------------------

    @ems_group.command(
        name="deny",
        description="[Admin] Deny a pending EMS request.",
    )
    @app_commands.describe(request_id="The request ID to deny")
    async def ems_deny(self, interaction: discord.Interaction, request_id: int):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only ERIS Admins can deny EMS requests.", ephemeral=True
            )
            return

        request = ems_repo.get_request(self.db, request_id)
        if not request:
            await interaction.response.send_message(
                f"❌ No request found with ID {request_id}.", ephemeral=True
            )
            return

        if request.get('status') != 'pending':
            await interaction.response.send_message(
                f"❌ Request {request_id} is already **{request['status']}**.",
                ephemeral=True,
            )
            return

        # Ask for reason via DM
        await interaction.response.send_message(
            "📬 Check your DMs — I'll ask you for the denial reason there.",
            ephemeral=True,
        )

        player = await self.bot.fetch_user(request['user_id'])
        if not player:
            return

        guild_config = guild_repo.get_guild_config(self.db, interaction.guild.id)
        review_channel_id = guild_config.get('ems_requests_channel_id') if guild_config else None
        review_channel = interaction.guild.get_channel(review_channel_id) if review_channel_id else interaction.channel

        await ems_flow.deny_ems(
            bot=self.bot,
            db=self.db,
            guild=interaction.guild,
            admin=interaction.user,
            review_channel=review_channel,
            request_id=request_id,
            player=player,
        )

    # -------------------------------------------------------------------------
    # /ems pending  — admin only
    # -------------------------------------------------------------------------

    @ems_group.command(
        name="pending",
        description="[Admin] List all pending EMS requests.",
    )
    async def ems_pending(self, interaction: discord.Interaction):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only ERIS Admins can view pending EMS requests.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        pending = ems_repo.get_pending_requests(self.db, interaction.guild.id)

        if not pending:
            await interaction.followup.send(
                "✅ No pending EMS requests.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="📋 Pending EMS Requests",
            color=discord.Color.orange(),
        )

        for r in pending:
            member = interaction.guild.get_member(r['user_id'])
            name   = member.display_name if member else str(r['user_id'])
            pool   = r.get('pool', '?').capitalize()
            embed.add_field(
                name=f"ID {r['request_id']} — {name}",
                value=(
                    f"**Pool:** {pool}\n"
                    f"**Amount:** {r['amount']} EMS\n"
                    f"**Reason:** {r.get('reason', 'None given')}\n"
                    f"Approve: `/ems approve {r['request_id']}`   "
                    f"Deny: `/ems deny {r['request_id']}`"
                ),
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # -------------------------------------------------------------------------
    # /ems submit — player pastes their fleet builder block
    # -------------------------------------------------------------------------

    @ems_group.command(
        name="submit",
        description="Submit your fleet builder block after commander approval.",
    )
    @app_commands.describe(block="Paste your full ERIS Commander Block here.")
    async def ems_submit(
        self, interaction: discord.Interaction, block: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        guild_config = guild_repo.get_guild_config(self.db, interaction.guild.id)
        if not guild_config:
            await interaction.followup.send(
                "❌ ERIS is not configured. Ask an admin to run `/setup`.",
                ephemeral=True,
            )
            return

        # Check channel
        submit_ch_id = guild_config.get('channels', {}).get('commander-submissions')
        if submit_ch_id and interaction.channel.id != submit_ch_id:
            ch = interaction.guild.get_channel(submit_ch_id)
            mention = ch.mention if ch else '#commander-submissions'
            await interaction.followup.send(
                f"❌ Please use `/ems submit` in {mention}.",
                ephemeral=True,
            )
            return

        # Parse the block
        parsed = _parse_block(block)
        if not parsed:
            await interaction.followup.send(
                "❌ Invalid block format. Make sure you copied the full block "
                "including `=== ERIS COMMANDER BLOCK ===` (fleet) or "
                "`=== ERIS GROUND COMMANDER BLOCK ===` (army), and `=== END BLOCK ===`.",
                ephemeral=True,
            )
            return

        code = parsed.get('SUBMISSION_ID', '').strip()
        sub  = commander_repo.get_submission_by_code(self.db, code)

        if not sub:
            await interaction.followup.send(
                f"❌ Submission ID `{code}` not found. "
                f"Check the ID from your approval DM.",
                ephemeral=True,
            )
            return

        # Verify this belongs to the player
        if sub['user_id'] != interaction.user.id:
            await interaction.followup.send(
                "❌ That submission ID belongs to a different player.",
                ephemeral=True,
            )
            return

        # Verify status — allow 'approved' through only if there's an active refit
        if sub['status'] != 'pending_block':
            if sub['status'] == 'approved':
                # Check for an active refit before rejecting
                commanders = commander_repo.get_by_user(
                    self.db, interaction.user.id, interaction.guild.id
                )
                match_early = next(
                    (c for c in commanders
                     if c['commander_name'].lower() == sub['commander_name'].lower()),
                    None,
                )
                if match_early:
                    pending_refits = ems_repo.get_refit_requests_for_commander(
                        self.db, match_early['commander_id']
                    )
                    has_refit = any(r['status'] == 'awaiting_block' for r in pending_refits)
                    if not has_refit:
                        await interaction.followup.send(
                            "❌ This submission is already approved. "
                            "If you need a refit, ask an admin to run `/ems refit`.",
                            ephemeral=True,
                        )
                        return
                else:
                    await interaction.followup.send(
                        f"❌ This submission is already `{sub['status']}`. "
                        f"If you need to update your block, contact an admin.",
                        ephemeral=True,
                    )
                    return
            else:
                await interaction.followup.send(
                    f"❌ This submission is already `{sub['status']}`. "
                    f"If you need to update your block, contact an admin.",
                    ephemeral=True,
                )
                return

        # Budget check — skip for refits (pool budget enforced at approve time)
        try:
            spent = int(parsed.get('EMS_SPENT', 0))
        except ValueError:
            spent = 0

        is_refit_submission = sub['status'] == 'approved'
        if not is_refit_submission and spent > sub['ems_budget']:
            await interaction.followup.send(
                f"❌ Your block spends **{spent} EMS** but your approved budget "
                f"is **{sub['ems_budget']} EMS**. Rebuild within your budget and resubmit.",
                ephemeral=True,
            )
            return

        # Save the block
        commander_repo.set_submitted_block(self.db, code, block)

        # Find the commander record for this submission
        commanders = commander_repo.get_by_user(
            self.db, interaction.user.id, interaction.guild.id
        )
        match = next(
            (c for c in commanders
             if c['commander_name'].lower() == sub['commander_name'].lower()),
            None,
        )

        # Check if this is a refit submission
        pending_refit = None
        if match:
            pool = 'fleet' if sub['force_type'] == 'Fleet' else 'army'
            pending_refits = ems_repo.get_refit_requests_for_commander(
                self.db, match['commander_id']
            )
            pending_refit = next(
                (r for r in pending_refits if r['status'] == 'awaiting_block'),
                None,
            )
        print(f"DEBUG pending_refit: {pending_refit}")
        print(f"DEBUG match: {match}")
        if pending_refit:
            # This is a refit — store new block and move to pending_approval
            ems_repo.update_refit_status(
                self.db, pending_refit['refit_id'], 'pending_approval'
            )
            # Update the new_block with what was just submitted
            self.db.execute(
                'UPDATE refit_requests SET new_block = ? WHERE refit_id = ?',
                (block, pending_refit['refit_id']),
            )
            self.db.commit()
            print(f"DEBUG block being saved: {repr(block[:200])}")

            # Post approval request to admin channel
            admin_ch_id = guild_config.get('channels', {}).get('admin-approvals') or \
                          guild_config.get('channels', {}).get('commander-approvals')
            if admin_ch_id:
                admin_ch = interaction.guild.get_channel(admin_ch_id)
                if admin_ch:
                    old_units  = _extract_units_section(pending_refit['old_block'])
                    new_units  = _extract_units_section(block)
                    embed = discord.Embed(
                        title=f"🔧 Refit Request — {match['commander_name']}",
                        color=discord.Color.orange(),
                    )
                    embed.add_field(
                        name="Player",
                        value=interaction.user.mention,
                        inline=True,
                    )
                    embed.add_field(
                        name="Commander",
                        value=match['commander_name'],
                        inline=True,
                    )
                    embed.add_field(
                        name="Pool",
                        value=pool.capitalize(),
                        inline=True,
                    )
                    embed.add_field(
                        name="📦 Before (pre-campaign)",
                        value=f"```\n{old_units}\n```",
                        inline=False,
                    )
                    embed.add_field(
                        name="🆕 After (refit request)",
                        value=f"```\n{new_units}\n```",
                        inline=False,
                    )
                    embed.set_footer(text=f"Refit ID: {pending_refit['refit_id']} | Use /ems refit_approve or /ems refit_deny")
                    await admin_ch.send(embed=embed)

            await interaction.followup.send(
                f"✅ **Refit block submitted for review!**\n\n"
                f"**Commander:** {match['commander_name']}\n"
                f"**EMS Spent:** {spent} / {sub['ems_budget']}\n\n"
                f"An admin will review your refit request shortly.",
                ephemeral=True,
            )
            return

        commander_repo.update_submission_status(self.db, sub['submission_id'], 'approved')

        # Update commander record with EMS and fleet type
        if match:
            pool = 'fleet' if sub['force_type'] == 'Fleet' else 'army'
            commander_repo.update_ems(
                self.db, match['commander_id'], pool, spent
            )
            block_col = 'fleet_ems_block' if pool == 'fleet' else 'army_ems_block'
            commander_repo.update_field(
                self.db, match['commander_id'], block_col, block
            )
            name_col = 'fleet_name' if pool == 'fleet' else 'army_name'
            force_name = parsed.get('FLEET_NAME') or parsed.get('ARMY_NAME')
            if force_name:
                commander_repo.update_field(
                    self.db, match['commander_id'], name_col, force_name
                )

        block_label = "Army" if parsed.get('_block_type') == 'army' else "Fleet"
        type_label  = parsed.get('FLEET_TYPE', 'Unknown')

        await interaction.followup.send(
            f"✅ **{block_label} block submitted!**\n\n"
            f"**Commander:** {sub['commander_name']}\n"
            f"**EMS Spent:** {spent} / {sub['ems_budget']}\n"
            f"**{block_label} Type:** {type_label}\n\n"
            f"Your loadout has been saved. You're ready to enroll in campaigns!",
            ephemeral=True,
        )
        log.info(
            f"EMS block submitted for {sub['commander_name']} "
            f"[{code}] by {interaction.user} — {spent} EMS"
        )

    # -------------------------------------------------------------------------
    # /ems refit — player initiates a post-campaign block refit
    # -------------------------------------------------------------------------

    @ems_group.command(
        name="refit",
        description="Request a refit of your commander's block after a campaign.",
    )
    async def ems_refit(
        self, interaction: discord.Interaction
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        player = interaction.user

        commanders = commander_repo.get_commanders_for_user(
            self.db, player.id, interaction.guild.id
        )
        if not commanders:
            await interaction.followup.send(
                "❌ You have no commanders.", ephemeral=True
            )
            return

        # Only commanders who have been in a campaign are eligible for refit
        available = [c for c in commanders if c.get('active_campaign')]
        if not available:
            await interaction.followup.send(
                "❌ You have no commanders eligible for a refit. "
                "Refits are available after completing a campaign.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "📬 Refit flow started. Check your DMs.",
            ephemeral=True,
        )

        await ems_flow.refit_ems(
            bot=self.bot,
            admin=interaction.user,
            target_member=player,
            guild=interaction.guild,
            db=self.db,
            theme=self._theme(interaction.guild.id),
            available_commanders=available,
        )

    # -------------------------------------------------------------------------
    # /ems refit approve
    # -------------------------------------------------------------------------

    @ems_group.command(
        name="refit_approve",
        description="[Admin] Approve a pending refit request.",
    )
    @app_commands.describe(refit_id="The refit request ID to approve.")
    async def ems_refit_approve(
        self, interaction: discord.Interaction, refit_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not self._is_admin(interaction.user):
            await interaction.followup.send("❌ Admins only.", ephemeral=True)
            return

        refit = ems_repo.get_refit_request(self.db, refit_id)
        if not refit or refit['guild_id'] != interaction.guild.id:
            await interaction.followup.send(f"❌ Refit ID `{refit_id}` not found.", ephemeral=True)
            return

        if refit['status'] != 'pending_approval':
            await interaction.followup.send(
                f"❌ This refit is `{refit['status']}`, not pending approval.", ephemeral=True
            )
            return

        # Parse the new block and apply it
        parsed = _parse_block(refit['new_block'])
        if not parsed:
            await interaction.followup.send("❌ Could not parse new block.", ephemeral=True)
            return

        spent = int(parsed.get('EMS_SPENT', 0))
        pool  = refit['pool']
        block_col = 'fleet_ems_block' if pool == 'fleet' else 'army_ems_block'
        name_col  = 'fleet_name'      if pool == 'fleet' else 'army_name'

        # Enforce pool cap — get current approved pool from DB
        pools = ems_repo.get_ems_pools(self.db, refit['commander_id'])
        current_pool = (pools.get('fleet_total_ems') if pool == 'fleet' else pools.get('army_total_ems')) or 0

        if spent > current_pool:
            # Auto-deny and cancel
            ems_repo.update_refit_status(
                self.db, refit_id, 'denied',
                denial_reason=f"EMS_SPENT ({spent}) exceeds approved pool ({current_pool}). Request cancelled.",
                resolved_by_id=interaction.user.id,
            )
            member = interaction.guild.get_member(refit['user_id'])
            if member:
                try:
                    await member.send(
                        f"❌ **Refit Denied — Pool Exceeded**\n\n"
                        f"Your refit block spends **{spent} EMS** but your approved pool is **{current_pool} EMS**.\n\n"
                        f"This request has been cancelled. If you need more than {current_pool} EMS, "
                        f"use `/ems request` to request a pool increase.\n\n"
                        f"Otherwise, run `/ems refit` to start a new refit within your current pool."
                    )
                except discord.Forbidden:
                    pass
            await interaction.followup.send(
                f"❌ Auto-denied: block spends **{spent} EMS** but pool is **{current_pool} EMS**. "
                f"Player has been notified.",
                ephemeral=True,
            )
            return

        commander_repo.update_ems(self.db, refit['commander_id'], pool, spent)
        commander_repo.update_field(self.db, refit['commander_id'], block_col, refit['new_block'])

        force_name = parsed.get('FLEET_NAME') or parsed.get('ARMY_NAME')
        if force_name:
            commander_repo.update_field(self.db, refit['commander_id'], name_col, force_name)

        ems_repo.update_refit_status(
            self.db, refit_id, 'approved',
            resolved_by_id=interaction.user.id,
        )

        # Notify the player
        member = interaction.guild.get_member(refit['user_id'])
        if member:
            try:
                await member.send(
                    f"✅ **Refit Approved**\n\n"
                    f"Your refit request (ID: `{refit_id}`) has been approved.\n"
                    f"Your block has been updated with **{spent} EMS** in your {pool.capitalize()} pool.\n\n"
                    f"You're ready for your next campaign!"
                )
            except discord.Forbidden:
                pass

        await interaction.followup.send(
            f"✅ Refit `{refit_id}` approved. {pool.capitalize()} block updated to **{spent} EMS**.",
            ephemeral=True,
        )

        # Post outcome to admin channel
        guild_config = guild_repo.get_guild_config(self.db, interaction.guild.id)
        admin_ch_id = guild_config.get('channels', {}).get('commander-approvals') if guild_config else None
        if admin_ch_id:
            admin_ch = interaction.guild.get_channel(admin_ch_id)
            if admin_ch:
                cmd_row = self.db.execute(
                    'SELECT commander_name FROM commanders WHERE commander_id = ?',
                    (refit['commander_id'],)
                ).fetchone()
                cmd_name = cmd_row['commander_name'] if cmd_row else f"ID {refit['commander_id']}"
                await admin_ch.send(
                    f"✅ **Refit Approved** — Refit ID `{refit_id}`\n"
                    f"**Commander:** {cmd_name} | **Pool:** {pool.capitalize()} | **EMS:** {spent}\n"
                    f"Approved by {interaction.user.mention}"
                )

    # -------------------------------------------------------------------------
    # /ems refit deny
    # -------------------------------------------------------------------------

    @ems_group.command(
        name="refit_deny",
        description="[Admin] Deny a pending refit request.",
    )
    @app_commands.describe(
        refit_id="The refit request ID to deny.",
        reason="Reason for denial.",
    )
    async def ems_refit_deny(
        self, interaction: discord.Interaction, refit_id: int, reason: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not self._is_admin(interaction.user):
            await interaction.followup.send("❌ Admins only.", ephemeral=True)
            return

        refit = ems_repo.get_refit_request(self.db, refit_id)
        if not refit or refit['guild_id'] != interaction.guild.id:
            await interaction.followup.send(f"❌ Refit ID `{refit_id}` not found.", ephemeral=True)
            return

        if refit['status'] != 'pending_approval':
            await interaction.followup.send(
                f"❌ This refit is `{refit['status']}`, not pending approval.", ephemeral=True
            )
            return

        ems_repo.update_refit_status(
            self.db, refit_id, 'denied',
            denial_reason=reason,
            resolved_by_id=interaction.user.id,
        )

        # Notify the player
        member = interaction.guild.get_member(refit['user_id'])
        if member:
            try:
                await member.send(
                    f"❌ **Refit Denied**\n\n"
                    f"Your refit request (ID: `{refit_id}`) has been denied.\n"
                    f"**Reason:** {reason}\n\n"
                    f"Contact an admin if you have questions."
                )
            except discord.Forbidden:
                pass

        await interaction.followup.send(
            f"❌ Refit `{refit_id}` denied. Player has been notified.",
            ephemeral=True,
        )

        # Post outcome to admin channel
        guild_config = guild_repo.get_guild_config(self.db, interaction.guild.id)
        admin_ch_id = guild_config.get('channels', {}).get('commander-approvals') if guild_config else None
        if admin_ch_id:
            admin_ch = interaction.guild.get_channel(admin_ch_id)
            if admin_ch:
                await admin_ch.send(
                    f"❌ **Refit Denied** — Refit ID `{refit_id}`\n"
                    f"**Reason:** {reason}\n"
                    f"Denied by {interaction.user.mention}"
                )

# =============================================================================
# SETUP
# =============================================================================

async def setup(bot):
    cog = EmsCog(bot)
    await bot.add_cog(cog, override=True)