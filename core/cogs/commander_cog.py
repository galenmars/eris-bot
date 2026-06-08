"""
core/cogs/commander_cog.py
==========================
E.R.I.S. Bot — Commander Commands

PLAYER COMMANDS
---------------
/commander submit   — start a submission (name, rank, faction, force type)
/commander view     — view own commanders (DM) or another player's (admin)
/commander list     — all active commanders grouped by faction
/commander status   — view own pending submissions and active commanders across all servers

ADMIN COMMANDS
--------------
/commander edit     — edit a commander field directly
/commander delete   — permanently delete a commander and all their records

APPROVAL FLOW (button-driven, in #commander-approvals)
-------------------------------------------------------
1. Player runs /commander submit
2. Bot posts embed to #commander-approvals with Claim button
3. Approver clicks Claim — locked to them
4. Embed updates: Approve / Deny buttons
5. Deny → bot asks reason → posts result → DMs player
6. Approve → bot asks Tier → asks EMS budget → posts result → DMs player
   with submission code and fleet builder instructions
"""

import asyncio
import logging

import discord
from discord.ext import commands
from discord     import app_commands

from core.data                   import commander_repo, guild_repo
from core.domain.exceptions      import DomainError
from core.shared                 import embeds, config
from core.shared.faction_picker  import prompt_faction, get_faction_alignment

log = logging.getLogger(__name__)

VALID_RANKS      = ('main', 'senior', 'junior')
VALID_TIERS      = (1, 2, 3)
VALID_FORCE      = ('Fleet', 'Army')

# Fields an admin can edit directly
EDITABLE_FIELDS = {
    'Name':    'commander_name',
    'Faction': 'faction',
    'Status':  'status',
    'LP':      'current_leadership_points',
}

STATUS_LABELS = {
    'active':   '🟢 Active',
    'deployed': '⚔️ Deployed',
    'inactive': '⚫ Inactive',
}


# =============================================================================
# HELPER VIEWS
# =============================================================================

class ClaimView(discord.ui.View):
    """Posted with every new submission. First approver to click claims it."""

    def __init__(self, submission_id: int, cog):
        super().__init__(timeout=None)   # Persistent — survives bot restarts
        self.submission_id = submission_id
        self.cog           = cog

    @discord.ui.button(
        label="📋 Claim Submission",
        style=discord.ButtonStyle.primary,
        custom_id="claim_submission",
    )
    async def claim(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self.cog._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only ERIS Admins can claim submissions.", ephemeral=True
            )
            return

        sub = commander_repo.get_submission_by_id(
            self.cog.db, self.submission_id
        )
        if not sub:
            await interaction.response.send_message(
                "❌ Submission not found.", ephemeral=True
            )
            return

        if sub['claimed_by_id']:
            claimer = interaction.guild.get_member(sub['claimed_by_id'])
            name    = claimer.display_name if claimer else f"<{sub['claimed_by_id']}>"
            await interaction.response.send_message(
                f"❌ Already claimed by {name}.", ephemeral=True
            )
            return

        commander_repo.claim_submission(
            self.cog.db, self.submission_id, interaction.user.id
        )

        # Rebuild embed with decision buttons
        sub = commander_repo.get_submission_by_id(
            self.cog.db, self.submission_id
        )
        embed  = self.cog._build_approval_embed(sub, interaction.guild)
        view   = DecisionView(self.submission_id, self.cog)

        await interaction.response.edit_message(embed=embed, view=view)
        log.info(
            f"Submission {self.submission_id} claimed by {interaction.user}"
        )


class DecisionView(discord.ui.View):
    """Shown after an approver claims a submission."""

    def __init__(self, submission_id: int, cog):
        super().__init__(timeout=None)
        self.submission_id = submission_id
        self.cog           = cog

    @discord.ui.button(
        label="✅ Approve",
        style=discord.ButtonStyle.success,
        custom_id="approve_submission",
    )
    async def approve(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        sub = commander_repo.get_submission_by_id(
            self.cog.db, self.submission_id
        )
        if not self.cog._is_claimer(interaction.user, sub):
            await interaction.response.send_message(
                "❌ Only the approver who claimed this can approve it.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Select the tier for this commander:",
            view=TierSelectView(self.submission_id, self.cog, interaction.message),
            ephemeral=True,
        )

    @discord.ui.button(
        label="❌ Deny",
        style=discord.ButtonStyle.danger,
        custom_id="deny_submission",
    )
    async def deny(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        sub = commander_repo.get_submission_by_id(
            self.cog.db, self.submission_id
        )
        if not self.cog._is_claimer(interaction.user, sub):
            await interaction.response.send_message(
                "❌ Only the approver who claimed this can deny it.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            DenyModal(self.submission_id, self.cog, interaction.message)
        )


class TierSelectView(discord.ui.View):
    """Ephemeral tier picker shown to the approver after clicking Approve."""

    def __init__(self, submission_id: int, cog, approval_message: discord.Message):
        super().__init__(timeout=120)
        self.submission_id   = submission_id
        self.cog             = cog
        self.approval_message = approval_message

    @discord.ui.button(label="Tier I",   style=discord.ButtonStyle.secondary)
    async def tier1(self, interaction: discord.Interaction, _): await self._pick(interaction, 1)

    @discord.ui.button(label="Tier II",  style=discord.ButtonStyle.secondary)
    async def tier2(self, interaction: discord.Interaction, _): await self._pick(interaction, 2)

    @discord.ui.button(label="Tier III", style=discord.ButtonStyle.secondary)
    async def tier3(self, interaction: discord.Interaction, _): await self._pick(interaction, 3)

    async def _pick(self, interaction: discord.Interaction, tier: int) -> None:
        await interaction.response.send_modal(
            EMSBudgetModal(
                self.submission_id, tier, self.cog, self.approval_message
            )
        )


class EMSBudgetModal(discord.ui.Modal, title="Set EMS Budget"):
    """Approver types the EMS budget after selecting tier."""

    ems = discord.ui.TextInput(
        label="EMS Budget",
        placeholder="e.g. 200",
        min_length=1,
        max_length=5,
    )

    def __init__(
        self,
        submission_id:    int,
        tier:             int,
        cog,
        approval_message: discord.Message,
    ):
        super().__init__()
        self.submission_id   = submission_id
        self.tier            = tier
        self.cog             = cog
        self.approval_message = approval_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            budget = int(self.ems.value.strip())
            if budget <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ EMS budget must be a positive number.", ephemeral=True
            )
            return

        sub = commander_repo.get_submission_by_id(
            self.cog.db, self.submission_id
        )

        commander_repo.set_submission_budget(
            self.cog.db, self.submission_id, self.tier, budget
        )
        commander_repo.approve_submission(
            self.cog.db, self.submission_id, interaction.user.id
        )

        # Create the commander record
        sub = commander_repo.get_submission_by_id(
            self.cog.db, self.submission_id
        )
        commander_repo.create_commander_from_submission(self.cog.db, sub)

        # Re-fetch so status reflects the committed write
        sub = commander_repo.get_submission_by_id(
            self.cog.db, self.submission_id
        )

        # Update the approval embed
        embed = self.cog._build_approval_embed(
            sub, interaction.guild, resolved=True
        )
        await self.approval_message.edit(embed=embed, view=None)

        await interaction.response.send_message(
            "✅ Commander approved.", ephemeral=True
        )

        # DM the player
        player = interaction.guild.get_member(sub['user_id'])
        if player:
            try:
                await player.send(
                    f"✅ **Your commander has been approved!**\n\n"
                    f"**{sub['commander_name']}** — {sub['rank'].title()} "
                    f"| Tier {self.tier} | {budget} EMS\n\n"
                    f"**Next step — build your fleet:**\n"
                    f"1. Open the E.R.I.S. Fleet Builder\n"
                    f"2. Enter your Submission ID: `{sub['submission_code']}`\n"
                    f"3. Build your loadout within your **{budget} EMS** budget\n"
                    f"4. Copy the block and paste it in "
                    f"`#commander-submissions` using `/ems submit`"
                )
            except discord.Forbidden:
                pass   # Player has DMs closed

        log.info(
            f"Submission {self.submission_id} approved by {interaction.user} "
            f"— Tier {self.tier}, {budget} EMS"
        )


class DenyModal(discord.ui.Modal, title="Deny Submission"):
    """Approver types the denial reason."""

    reason = discord.ui.TextInput(
        label="Reason for denial",
        style=discord.TextStyle.paragraph,
        placeholder="Explain why this submission is being denied...",
        min_length=5,
        max_length=500,
    )

    def __init__(
        self,
        submission_id:    int,
        cog,
        approval_message: discord.Message,
    ):
        super().__init__()
        self.submission_id   = submission_id
        self.cog             = cog
        self.approval_message = approval_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        sub = commander_repo.get_submission_by_id(
            self.cog.db, self.submission_id
        )

        commander_repo.deny_submission(
            self.cog.db,
            self.submission_id,
            interaction.user.id,
            self.reason.value,
        )

        sub = commander_repo.get_submission_by_id(
            self.cog.db, self.submission_id
        )

        # Update approval embed
        embed = self.cog._build_approval_embed(
            sub, interaction.guild, resolved=True
        )
        await self.approval_message.edit(embed=embed, view=None)

        await interaction.response.send_message(
            "✅ Submission denied.", ephemeral=True
        )

        # DM the player
        player = interaction.guild.get_member(sub['user_id'])
        if player:
            try:
                await player.send(
                    f"❌ **Your commander submission was denied.**\n\n"
                    f"**Commander:** {sub['commander_name']}\n"
                    f"**Reason:** {self.reason.value}\n\n"
                    f"You may resubmit with `/commander submit` after "
                    f"addressing the reason above."
                )
            except discord.Forbidden:
                pass

        log.info(
            f"Submission {self.submission_id} denied by {interaction.user}"
        )


# =============================================================================
# COMMANDER COG
# =============================================================================

class CommanderCog(commands.Cog, name="Commander"):

    _active_submissions: set[int] = set()  # tracks user_ids with open DM flows

    def __init__(self, bot):
        self.bot = bot
        self.db  = bot.db.conn

    # -------------------------------------------------------------------------
    # HELPERS
    # -------------------------------------------------------------------------

    def _is_admin(self, member: discord.Member) -> bool:
        guild_config  = guild_repo.get_guild_config(self.db, member.guild.id)
        admin_role_id = guild_config.get('roles', {}).get('ERIS Admin') if guild_config else None
        if not admin_role_id:
            return member.guild_permissions.administrator
        return any(r.id == admin_role_id for r in member.roles)

    def _is_claimer(self, member: discord.Member, sub: dict) -> bool:
        return sub and sub.get('claimed_by_id') == member.id

    def _get_channel(self, guild: discord.Guild, key: str) -> discord.TextChannel | None:
        guild_config = guild_repo.get_guild_config(self.db, guild.id)
        if not guild_config:
            return None
        ch_id = guild_config.get('channels', {}).get(key)
        return guild.get_channel(ch_id) if ch_id else None

    def _player_summary(
        self, guild: discord.Guild, user_id: int
    ) -> tuple[list[dict], int]:
        """Return (commanders, total_ems) for a player."""
        commanders = commander_repo.get_by_user(self.db, user_id, guild.id)
        total_ems  = commander_repo.get_total_ems_by_user(self.db, user_id, guild.id)
        return commanders, total_ems

    # -------------------------------------------------------------------------
    # EMBED BUILDERS
    # -------------------------------------------------------------------------

    def _build_approval_embed(
        self,
        sub:      dict,
        guild:    discord.Guild,
        resolved: bool = False,
    ) -> discord.Embed:
        """Build the embed posted to #commander-approvals."""

        commanders, total_ems = self._player_summary(guild, sub['user_id'])
        guild_config          = guild_repo.get_guild_config(self.db, guild.id)

        # Status color
        if resolved:
            # Approval writes 'pending_block' (see commander_repo.create_commander_from_submission).
            # Treat both 'approved' and 'pending_block' as approved for display. Computed once here
            # and reused everywhere below so the title, fields, and footer can never disagree.
            is_approved = sub['status'] in ('approved', 'pending_block')
            if is_approved:
                color = config.COLOR_SUCCESS
                title = "✅ SUBMISSION APPROVED"
            else:
                color = config.COLOR_ERROR
                title = "❌ SUBMISSION DENIED"
        else:
            claimed = sub.get('claimed_by_id')
            color   = config.COLOR_WARNING if claimed else config.COLOR_INFO
            title   = "📋 COMMANDER SUBMISSION" + (" — CLAIMED" if claimed else "")

        embed = discord.Embed(title=title, color=color)

        # Submission details
        player = guild.get_member(sub['user_id'])
        embed.add_field(
            name="Player",
            value=player.mention if player else f"<@{sub['user_id']}>",
            inline=True,
        )
        embed.add_field(name="Commander", value=sub['commander_name'], inline=True)
        embed.add_field(name="Rank",      value=sub['rank'].title(),   inline=True)
        _theme        = self.bot.get_theme()
        alignment     = get_faction_alignment(sub['faction'], _theme)
        faction_label = f"{sub['faction']} [{alignment}]" if alignment else sub['faction']
        embed.add_field(name="Faction",   value=faction_label,         inline=True)
        embed.add_field(name="Force",     value=sub['force_type'],     inline=True)

        if resolved and is_approved:
            embed.add_field(name="Tier",       value=f"Tier {sub['tier']}",         inline=True)
            embed.add_field(name="EMS Budget", value=str(sub['ems_budget']),         inline=True)
            embed.add_field(name="Sub ID",     value=f"`{sub['submission_code']}`",  inline=True)

        if resolved and not is_approved:
            embed.add_field(
                name="Denial Reason",
                value=sub.get('denial_reason', 'No reason given'),
                inline=False,
            )

        # Player's current commander roster
        if commanders:
            lines = []
            for c in commanders:
                ems   = c['fleet_total_ems'] + c['army_total_ems']
                label = STATUS_LABELS.get(c['status'], c['status'])
                lines.append(
                    f"**{c['commander_name']}** — {c['rank'].title()} "
                    f"| {c['force_type']} | {ems} EMS | {label}"
                )
            lines.append(f"\n**Total EMS: {total_ems}**")

            # Show guild EMS cap if set
            if guild_config:
                cap = guild_config.get('max_total_ems', 0)
                if cap:
                    lines.append(f"Guild cap: {cap} EMS")

            embed.add_field(
                name=f"Current Commanders ({len(commanders)})",
                value='\n'.join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="Current Commanders",
                value="None — this would be their first.",
                inline=False,
            )

        # Claimer info
        if sub.get('claimed_by_id') and not resolved:
            claimer = guild.get_member(sub['claimed_by_id'])
            embed.set_footer(
                text=f"Claimed by {claimer.display_name if claimer else sub['claimed_by_id']}"
            )

        if resolved and sub.get('approved_by_id'):
            approver = guild.get_member(sub['approved_by_id'])
            action   = "Approved" if is_approved else "Denied"
            embed.set_footer(
                text=f"{action} by {approver.display_name if approver else sub['approved_by_id']}"
            )

        return embed

    # =========================================================================
    # COMMAND GROUP
    # =========================================================================

    commander_group = app_commands.Group(
        name="commander",
        description="Commander management commands.",
    )

    # -------------------------------------------------------------------------
    # /commander submit
    # -------------------------------------------------------------------------

    @commander_group.command(
        name="submit",
        description="Submit a new commander for approval.",
    )
    async def commander_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        guild_config = guild_repo.get_guild_config(self.db, interaction.guild.id)
        if not guild_config:
            await interaction.followup.send(
                "❌ ERIS is not configured on this server. Ask an admin to run `/setup`.",
                ephemeral=True,
            )
            return

        # Check for existing pending submission
        existing = commander_repo.get_active_submission_for_user(
            self.db, interaction.user.id, interaction.guild.id
        )
        if existing:
            await interaction.followup.send(
                f"❌ You already have a pending submission for "
                f"**{existing['commander_name']}** (`{existing['status']}`). "
                f"Wait for it to be reviewed before submitting again.",
                ephemeral=True,
            )
            return

        if interaction.user.id in CommanderCog._active_submissions:
            await interaction.followup.send(
                "❌ You already have an active submission in progress in your DMs.\n"
                "Type `cancel` in your DMs to abort it, then try again.",
                ephemeral=True,
            )
            return

        CommanderCog._active_submissions.add(interaction.user.id)

        try:
            def check(m: discord.Message) -> bool:
                return (
                    m.author.id == interaction.user.id and
                    isinstance(m.channel, discord.DMChannel)
                )

            # Try to DM the player
            try:
                dm = await interaction.user.create_dm()
            except discord.Forbidden:
                await interaction.followup.send(
                    "❌ I can't DM you. Please enable DMs from server members and try again.",
                    ephemeral=True,
                )
                return

            await interaction.followup.send(
                "📬 Check your DMs — I'll guide you through the submission there.",
                ephemeral=True,
            )

            async def ask(prompt: str | None = None) -> str | None:
                if prompt:
                    prompt_msg = await dm.send(prompt)
                    after = prompt_msg.created_at
                else:
                    after = discord.utils.utcnow()

                def reply_check(m: discord.Message) -> bool:
                    return (
                            m.author.id == interaction.user.id and
                            isinstance(m.channel, discord.DMChannel) and
                            m.created_at >= after
                    )

                try:
                    msg = await self.bot.wait_for('message', check=reply_check, timeout=300)
                except asyncio.TimeoutError:
                    await dm.send("⏱️ Submission timed out. Run `/commander submit` again to start over.")
                    return None
                if msg.content.strip().lower() == 'cancel':
                    await dm.send("❌ Submission cancelled.")
                    return None
                return msg.content.strip()

            await dm.send(
                "**E.R.I.S. Commander Submission**\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Server: **{interaction.guild.name}**\n"
                "Type `cancel` at any point to abort.\n"
            )

            # --- Commander name ---
            name = await ask("**What is your commander's name?**")
            if not name:
                return

            # Check for similar existing names for this player
            existing_commanders = commander_repo.get_by_user(
                self.db, interaction.user.id, interaction.guild.id
            )
            similar = [
                c for c in existing_commanders
                if name.lower() in c['commander_name'].lower() or
                   c['commander_name'].lower() in name.lower()
            ]
            if similar:
                names = ', '.join(f"**{c['commander_name']}**" for c in similar)
                confirm = await ask(
                    f"⚠️ You already have a commander with a similar name: {names}\n"
                    f"Are you sure you want to submit **{name}** as a new commander? (yes/no)"
                )
                if not confirm or confirm.lower() not in ('yes', 'y'):
                    await dm.send("❌ Submission cancelled. Use `/commander edit` to update an existing commander.")
                    return

            # --- Rank ---
            while True:
                rank_raw = await ask(
                    "**What rank is this commander?**\n"
                    "Options: `main` | `senior` | `junior`"
                )
                if not rank_raw:
                    return
                rank = rank_raw.lower()
                if rank not in VALID_RANKS:
                    await dm.send(f"❌ Invalid rank `{rank_raw}`. Must be: main, senior, or junior.")
                    continue
                current_count = commander_repo.count_by_rank(
                    self.db, interaction.user.id, interaction.guild.id, rank
                )
                guild_limits = {
                    'main':   guild_config.get('max_mains',   1),
                    'senior': guild_config.get('max_seniors', 2),
                    'junior': guild_config.get('max_juniors', 0),
                }
                limit = guild_limits.get(rank, 0)
                if limit and current_count >= limit:
                    await dm.send(
                        f"❌ You already have {current_count} {rank} commander(s). "
                        f"This guild's limit is {limit}. Try a different rank."
                    )
                    continue
                break

            # --- Faction / Organization ---
            _theme  = self.bot.get_theme()
            _orgs   = guild_repo.get_organizations(self.db, interaction.guild.id)
            _result = await prompt_faction(
                channel=dm, user=interaction.user, theme=_theme, orgs=_orgs or None
            )
            if _result is None:
                return
            faction       = _result.name          # org name (or faction name if no orgs)
            faction_name  = _result.faction_name  # canonical theme faction
            faction_align = _result.group         # alignment group key

            # --- Force type ---
            force_raw = await ask(
                "**What force type does this commander lead?**\n"
                "Options: `Fleet` | `Army`"
            )
            if not force_raw:
                return
            force_type = force_raw.strip().title()
            if force_type not in VALID_FORCE:
                await dm.send(f"❌ Invalid force type. Must be: Fleet or Army. Run `/commander submit` to try again.")
                return

            # --- Confirmation ---
            _faction_display = (
                f"{faction} · {faction_name}"
                if faction != faction_name else faction
            )
            confirm = await ask(
                f"**Please confirm your submission:**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"**Name:**    {name}\n"
                f"**Rank:**    {rank.title()}\n"
                f"**Faction:** {_faction_display}\n"
                f"**Force:**   {force_type}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Type `confirm` to submit or `cancel` to abort."
            )
            if not confirm or confirm.lower() != 'confirm':
                await dm.send("❌ Submission cancelled.")
                return

            # --- Create submission ---
            sub = commander_repo.create_pending_submission(
                self.db,
                user_id        = interaction.user.id,
                guild_id       = interaction.guild.id,
                commander_name = name,
                rank           = rank,
                faction        = faction,
                force_type     = force_type,
            )

            await dm.send(
                f"✅ **Submission received!**\n\n"
                f"Your submission for **{name}** has been sent to the approvers.\n"
                f"You'll receive a DM when it's been reviewed.\n\n"
                f"**Submission ID:** `{sub['submission_code']}`\n"
                f"_(You'll need this ID for the fleet builder once approved.)_"
            )

            # --- Post to #commander-approvals ---
            approvals_ch = self._get_channel(interaction.guild, 'commander-approvals')
            if approvals_ch:
                embed = self._build_approval_embed(sub, interaction.guild)
                view  = ClaimView(sub['submission_id'], self)
                await approvals_ch.send(embed=embed, view=view)
            else:
                log.error(f"#commander-approvals channel not found for guild {interaction.guild.id}")

            log.info(
                f"Commander submission created: {name} ({rank}) "
                f"by {interaction.user} [{sub['submission_code']}]"
            )

        finally:
            CommanderCog._active_submissions.discard(interaction.user.id)

    # -------------------------------------------------------------------------
    # /commander view
    # -------------------------------------------------------------------------

    @commander_group.command(
        name="view",
        description="View commander info. Sends a DM. Admins in admin channels get full view here.",
    )
    @app_commands.describe(member="The player to look up (admin only).")
    async def commander_view(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        is_admin = self._is_admin(interaction.user)

        # Non-admins can only see their own
        if member and member != interaction.user and not is_admin:
            await interaction.followup.send(
                "❌ You can only view your own commanders.", ephemeral=True
            )
            return

        target = member or interaction.user
        commanders, total_ems = self._player_summary(interaction.guild, target.id)

        if not commanders:
            await interaction.followup.send(
                f"{'You have' if target == interaction.user else f'{target.display_name} has'} "
                f"no active commanders.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"Commanders — {target.display_name}",
            color=config.COLOR_INFO,
        )
        for c in commanders:
            ems   = c['fleet_total_ems'] + c['army_total_ems']
            label = STATUS_LABELS.get(c['status'], c['status'])
            _theme        = self.bot.get_theme()
            alignment     = get_faction_alignment(c['faction'], _theme)
            faction_label = f"{c['faction']} [{alignment}]" if alignment else c['faction']
            embed.add_field(
                name=f"{c['commander_name']} (Tier {c['tier']})",
                value=(
                    f"Rank: {c['rank'].title()}\n"
                    f"Faction: {faction_label}\n"
                    f"Force: {c['force_type']}\n"
                    f"EMS: {ems}\n"
                    f"Status: {label}"
                ),
                inline=True,
            )
        embed.set_footer(text=f"Total EMS across all commanders: {total_ems}")

        if is_admin:
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            try:
                await target.send(embed=embed)
                await interaction.followup.send("📬 Sent to your DMs.", ephemeral=True)
            except discord.Forbidden:
                await interaction.followup.send(embed=embed, ephemeral=True)

    # -------------------------------------------------------------------------
    # /commander list
    # -------------------------------------------------------------------------

    @commander_group.command(
        name="list",
        description="List all commanders on this server, grouped by faction.",
    )
    async def commander_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        commanders = commander_repo.get_all_for_guild(self.db, interaction.guild.id)

        if not commanders:
            await interaction.followup.send(
                "No active commanders on this server yet.", ephemeral=True
            )
            return

        # Group by faction
        factions: dict[str, list[dict]] = {}
        for c in commanders:
            factions.setdefault(c['faction'], []).append(c)

        embed = discord.Embed(
            title="Commander Registry",
            color=config.COLOR_INFO,
        )
        _theme = self.bot.get_theme()
        for faction, cmds in sorted(factions.items()):
            alignment    = get_faction_alignment(faction, _theme)
            faction_header = f"{faction} [{alignment}]" if alignment else faction
            lines = []
            for c in cmds:
                fleet_ems = c['fleet_total_ems'] or 0
                army_ems  = c['army_total_ems']  or 0
                ems       = fleet_ems + army_ems
                if fleet_ems and army_ems:
                    domain = 'Fleet + Ground'
                elif army_ems:
                    domain = 'Ground'
                else:
                    domain = 'Fleet'
                label  = STATUS_LABELS.get(c['status'], c['status'])
                member = interaction.guild.get_member(c['user_id'])
                player = member.display_name if member else f"<@{c['user_id']}>"
                lines.append(
                    f"**{c['commander_name']}** ({c['rank'].title()} — {domain}) "
                    f"— {player} | {ems} EMS | {label}"
                )
            embed.add_field(
                name=faction_header,
                value='\n'.join(lines),
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # -------------------------------------------------------------------------
    # /commander edit
    # -------------------------------------------------------------------------

    @commander_group.command(
        name="edit",
        description="[Admin] Edit a commander's fields.",
    )
    @app_commands.describe(
        commander_name="Name of the commander to edit.",
        field="Field to edit.",
        value="New value.",
    )
    @app_commands.choices(field=[
        app_commands.Choice(name=k, value=k) for k in EDITABLE_FIELDS
    ])
    async def commander_edit(
        self,
        interaction: discord.Interaction,
        commander_name: str,
        field:          app_commands.Choice[str],
        value:          str,
    ) -> None:
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only ERIS Admins can edit commanders.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        matches = commander_repo.get_by_name(
            self.db, interaction.guild.id, commander_name
        )
        if not matches:
            await interaction.followup.send(
                f"❌ No commander found matching `{commander_name}`.", ephemeral=True
            )
            return
        if len(matches) > 1:
            names = ', '.join(f"**{c['commander_name']}**" for c in matches)
            await interaction.followup.send(
                f"❌ Multiple matches found: {names}. Be more specific.", ephemeral=True
            )
            return

        commander = matches[0]
        db_field  = EDITABLE_FIELDS[field.value]

        commander_repo.update_field(self.db, commander['commander_id'], db_field, value)

        await interaction.followup.send(
            f"✅ **{commander['commander_name']}** — `{field.value}` updated to `{value}`.",
            ephemeral=True,
        )
        log.info(
            f"Commander {commander['commander_name']} field {db_field} "
            f"updated to {value!r} by {interaction.user}"
        )

    # -------------------------------------------------------------------------
    # /commander delete
    # -------------------------------------------------------------------------

    @commander_group.command(
        name="delete",
        description="[Admin] Permanently delete a commander and all their records.",
    )
    @app_commands.describe(commander_name="Exact name of the commander to delete.")
    async def commander_delete(
            self, interaction: discord.Interaction, commander_name: str
    ) -> None:
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only ERIS Admins can delete commanders.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        results = commander_repo.get_by_name(
            self.db, interaction.guild.id, commander_name
        )
        if not results:
            await interaction.followup.send(
                f"❌ No commander named `{commander_name}` found in this server.",
                ephemeral=True,
            )
            return

        if len(results) > 1:
            names = ', '.join(f"`{r['commander_name']}`" for r in results)
            await interaction.followup.send(
                f"❌ Multiple matches found: {names}. Use the exact name.",
                ephemeral=True,
            )
            return

        cmd = results[0]

        self.db.execute(
            "DELETE FROM commander_campaigns WHERE commander_id = ?",
            (cmd['commander_id'],)
        )
        self.db.execute(
            "UPDATE commander_submissions SET status = 'deleted' WHERE commander_name = ? AND guild_id = ?",
            (cmd['commander_name'], interaction.guild.id)
        )
        self.db.execute(
            "DELETE FROM commanders WHERE commander_id = ?",
            (cmd['commander_id'],)
        )
        self.db.commit()

        await interaction.followup.send(
            f"🗑️ **{cmd['commander_name']}** has been permanently deleted.",
            ephemeral=True,
        )
        log.info(
            "Commander %s deleted by %s in guild %s",
            cmd['commander_name'], interaction.user, interaction.guild.id
        )

    # -------------------------------------------------------------------------
    # /commander status
    # -------------------------------------------------------------------------

    @commander_group.command(
        name="status",
        description="Show all your pending submissions and active commanders across all servers.",
    )
    async def commander_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        submissions = commander_repo.get_all_submissions_for_user(
            self.db, interaction.user.id
        )
        commanders = commander_repo.get_all_commanders_for_user(
            self.db, interaction.user.id
        )

        if not submissions and not commanders:
            await interaction.followup.send(
                "You have no pending submissions or active commanders.",
                ephemeral=True,
            )
            return

        lines = [f"📋 **E.R.I.S. Commander Status — {interaction.user.display_name}**",
                 "━━━━━━━━━━━━━━━━━━━━━━"]

        if submissions:
            lines.append("\n**⏳ Pending Submissions**")
            for s in submissions:
                guild = self.bot.get_guild(s['guild_id'])
                guild_name = guild.name if guild else f"Unknown Server ({s['guild_id']})"
                lines.append(
                    f"  `{s['submission_code']}` — **{s['commander_name']}** "
                    f"({s['rank']}) | {s['status'].replace('_', ' ')} | {guild_name}"
                )

        if commanders:
            lines.append("\n**⚔️ Active Commanders**")
            current_guild = None
            for c in commanders:
                guild = self.bot.get_guild(c['guild_id'])
                guild_name = guild.name if guild else f"Unknown Server ({c['guild_id']})"
                if guild_name != current_guild:
                    lines.append(f"\n  __{guild_name}__")
                    current_guild = guild_name
                lines.append(
                    f"  • **{c['commander_name']}** — {c['rank']} | "
                    f"{c['force_type']} | {c['status']}"
                )

        lines.append("\n━━━━━━━━━━━━━━━━━━━━━━")
        await interaction.followup.send('\n'.join(lines), ephemeral=True)


# =============================================================================
# SETUP
# =============================================================================

async def setup(bot) -> None:
    await bot.add_cog(CommanderCog(bot), override=True)
