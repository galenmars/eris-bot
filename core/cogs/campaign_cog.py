"""
cogs/campaign_cog.py
====================
E.R.I.S. Bot — Campaign Commands

PURPOSE
-------
Discord-facing surface for all campaign operations.
Defines slash commands, checks permissions, and hands off
to sequences/enrollment_flow.py for the enrollment reaction flow.

PERMISSION MODEL
----------------
ERIS Admin      — God mode. All commands.
ERIS Creator    — Discord role. Can create campaigns (becomes owner).
Campaign Owner  — Creator who made this campaign. Can complete, edit,
                  bind channels, start, host, create NPCs, add collaborators.
Collaborator    — Another Creator invited by the owner. Can start,
                  host battles, create NPCs, bind channels.
                  Cannot complete, delete, or add collaborators.

Helper methods (used throughout this cog):
  _is_admin(member)                       — ERIS Admin role check
  _is_creator(member)                     — ERIS Creator role check
  _is_campaign_owner(campaign_id, uid)    — owner row in campaign_staff
  _is_campaign_staff(campaign_id, uid)    — owner OR collaborator row

COMMANDS
--------
/campaign create            — ERIS Creator
/campaign collaborator add  — campaign owner
/campaign collaborator remove — campaign owner
/campaign collaborator list — anyone
/campaign edit              — campaign owner or admin
/campaign list              — any member
/campaign view [name]       — any member
/campaign start [name]      — campaign staff or admin
/campaign complete [name]   — campaign owner or admin
/campaign retreat [name]    — campaign staff or admin
/campaign delete [name]     — admin only
/campaign record-bind       — campaign staff or admin
/campaign unbind #ch        — admin only
/campaign results-bind      — campaign staff or admin
/campaign progress [name]   — campaign staff or admin
/campaign ems [name]        — ERIS Creator or admin
/campaign roster [name]     — any member
/campaign announce          — campaign staff or admin

WHAT THIS COG DOES NOT OWN
---------------------------
Enrollment logic           → sequences/enrollment_flow.py
Campaign rule validation   → domain/campaign.py
Database ops               → data/campaign_repo.py, data/commander_repo.py
"""

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands
from discord     import app_commands

from core.data              import campaign_repo, commander_repo, guild_repo
from core.domain            import campaign as campaign_domain
from core.domain.exceptions import DomainError
from core.sequences         import enrollment_flow
from core.shared            import embeds

log = logging.getLogger(__name__)

# Valid battle types for channel binding
VALID_BATTLE_TYPES = ('space', 'ground')

# Valid campaign types (validated by domain, listed here for the prompt)
CAMPAIGN_TYPE_LABELS = {
    'Tug-of-War': 'Progress split evenly; factions fight for the centre.',
    'Invasion':   'Attacker starts at 0%, defender at 100%.',
    'Defense':    'Inverse of Invasion — first faction is the defender.',
}

# Edit menu options
EDIT_OPTIONS = {
    '1': 'Opposing EMS total',
    '2': 'Max commanders',
    '3': 'Enrollment deadline',
}


# =============================================================================
# HELPERS
# =============================================================================

def _format_deadline(deadline_iso: str | None) -> str:
    """Return a Discord timestamp string, or 'Not set'."""
    if not deadline_iso:
        return 'Not set'
    try:
        dt  = datetime.fromisoformat(deadline_iso)
        ts  = int(dt.timestamp())
        return f"<t:{ts}:F>"
    except (ValueError, TypeError):
        return deadline_iso


# =============================================================================
# CAMPAIGN COG
# =============================================================================

class CampaignCog(commands.Cog, name="Campaign"):
    """Campaign creation, management, and enrollment commands."""

    def __init__(self, bot):
        self.bot = bot
        self.db  = bot.db.conn

    def _theme(self, guild_id: int) -> dict:
        return self.bot.get_theme(guild_id)

    # -------------------------------------------------------------------------
    # PERMISSION HELPERS
    # -------------------------------------------------------------------------

    def _is_admin(self, member: discord.Member) -> bool:
        """True if the member holds the ERIS Admin role (or server administrator)."""
        guild_config  = guild_repo.get_guild_config(self.db, member.guild.id)
        admin_role_id = guild_config.get('roles', {}).get('ERIS Admin') if guild_config else None
        if not admin_role_id:
            return member.guild_permissions.administrator
        return any(r.id == admin_role_id for r in member.roles)

    def _is_creator(self, member: discord.Member) -> bool:
        """True if the member holds the ERIS Creator role."""
        guild_config    = guild_repo.get_guild_config(self.db, member.guild.id)
        creator_role_id = guild_config.get('roles', {}).get('ERIS Creator') if guild_config else None
        if not creator_role_id:
            return False
        return any(r.id == creator_role_id for r in member.roles)

    def _is_campaign_owner(self, campaign_id: int, user_id: int) -> bool:
        """True if user_id is the owner of this specific campaign."""
        return campaign_repo.is_campaign_owner(self.db, campaign_id, user_id)

    def _is_campaign_staff(self, campaign_id: int, user_id: int) -> bool:
        """True if user_id is owner OR collaborator on this campaign."""
        return campaign_repo.is_campaign_staff(self.db, campaign_id, user_id)

    def _can_manage_campaign(self, interaction: discord.Interaction, campaign_id: int) -> bool:
        """
        True if the invoker can perform owner-level actions on this campaign.
        Owner-level = complete, edit, add/remove collaborators, bind channels.
        """
        return (
            self._is_admin(interaction.user)
            or self._is_campaign_owner(campaign_id, interaction.user.id)
        )

    def _can_staff_campaign(self, interaction: discord.Interaction, campaign_id: int) -> bool:
        """
        True if the invoker can perform staff-level actions on this campaign.
        Staff-level = start, host battles, create NPCs, bind channels.
        """
        return (
            self._is_admin(interaction.user)
            or self._is_campaign_staff(campaign_id, interaction.user.id)
        )

    async def _edit_board_message_complete(
        self,
        interaction: discord.Interaction,
        campaign: dict,
    ) -> None:
        """Edit the #campaign-board announcement to show the campaign is complete."""
        board_ref = campaign_repo.get_board_message_for_campaign(
            self.db, campaign['campaign_id']
        )
        if not board_ref:
            return
        board_ch = interaction.guild.get_channel(board_ref['channel_id'])
        if not board_ch:
            return
        try:
            board_msg  = await board_ch.fetch_message(board_ref['message_id'])
            thread_id  = campaign.get('progress_thread_id')
            thread_ref = f" · <#{thread_id}>" if thread_id else ""
            await board_msg.edit(content=(
                f"~~{board_msg.content}~~\n\n"
                f"✅ **CAMPAIGN COMPLETE — {campaign['campaign_name'].upper()}**{thread_ref}"
            ))
        except (discord.NotFound, discord.HTTPException):
            log.warning(
                "Could not edit campaign-board message for campaign %s",
                campaign['campaign_id'],
            )

    # -------------------------------------------------------------------------
    # CONTEXT PROXY — lets sequences call ctx.send() without knowing they're
    # inside a slash command interaction.
    # -------------------------------------------------------------------------

    class _ContextProxy:
        """Minimal proxy so sequences can call ctx.send / ctx.author."""

        def __init__(self, interaction: discord.Interaction):
            self.interaction = interaction
            self.author = interaction.user
            self.guild = interaction.guild
            self.channel = interaction.channel

        async def send(self, content=None, **kwargs):
            await self.interaction.followup.send(content, **kwargs)

        async def defer(self):
            pass  # Already deferred before proxy is created

    # =========================================================================
    # CAMPAIGN GROUP
    # =========================================================================

    campaign_group = app_commands.Group(
        name="campaign",
        description="Campaign management commands.",
    )

    # -------------------------------------------------------------------------
    # /campaign create
    # Requires: @ERIS Creator role (or admin).
    # Effect:   invoker automatically becomes the campaign owner.
    # -------------------------------------------------------------------------

    @campaign_group.command(name="create", description="[Creator] Create a new campaign.")
    async def campaign_create(self, interaction: discord.Interaction):
        if not (self._is_admin(interaction.user) or self._is_creator(interaction.user)):
            await interaction.response.send_message(
                "❌ Only ERIS Creators can create campaigns.", ephemeral=True
            )
            return

        await interaction.response.defer()
        ctx   = self._ContextProxy(interaction)
        theme = self._theme(interaction.guild.id)

        def check(m):
            return m.author == interaction.user and m.channel == interaction.channel

        # --- Campaign name ---
        await ctx.send(
            "📋 **Campaign Setup**\n"
            "Type `cancel` (no slash) at any point to abort.\n\n"
            "**What is the name of this campaign?**"
        )

        try:
            msg = await self.bot.wait_for('message', check=check, timeout=300)
        except TimeoutError:
            await ctx.send("❌ Setup timed out.")
            return
        if msg.content.lower() == 'cancel':
            await ctx.send("❌ Campaign creation cancelled.")
            return
        campaign_name = msg.content.strip()

        # --- Campaign type ---
        type_lines = "\n\n".join(
            f"`{i + 1}` **{k}**\n{v}"
            for i, (k, v) in enumerate(CAMPAIGN_TYPE_LABELS.items())
        )
        await ctx.send(f"**Campaign type:**\n\n{type_lines}")

        type_map = {str(i+1): t for i, t in enumerate(CAMPAIGN_TYPE_LABELS)}
        type_map.update({t.lower(): t for t in CAMPAIGN_TYPE_LABELS})

        while True:
            try:
                msg = await self.bot.wait_for('message', check=check, timeout=300)
            except TimeoutError:
                await ctx.send("❌ Setup timed out.")
                return
            if msg.content.lower() == 'cancel':
                await ctx.send("❌ Cancelled.")
                return
            campaign_type = type_map.get(msg.content.strip().lower()) or type_map.get(msg.content.strip())
            if not campaign_type:
                await ctx.send("❌ Please reply with 1, 2, or 3.")
                continue
            try:
                campaign_domain.validate_campaign_type(campaign_type)
                break
            except DomainError as e:
                await ctx.send(f"❌ {e}")

        # --- Side A Factions ---
        side_a_label = "Attacking factions" if campaign_type == 'Defense' else "Your side's factions"
        await ctx.send(
            f"**{side_a_label}:**\n"
            f"Enter faction names separated by commas.\n"
            f"Example: `Deep Stellar Coalition, Mandalorians`"
        )
        while True:
            try:
                msg = await self.bot.wait_for('message', check=check, timeout=300)
            except TimeoutError:
                await ctx.send("❌ Setup timed out.")
                return
            if msg.content.lower() == 'cancel':
                await ctx.send("❌ Cancelled.")
                return
            side_a_factions = [f.strip() for f in msg.content.split(',') if f.strip()]
            if not side_a_factions:
                await ctx.send("❌ Please enter at least one faction name.")
                continue
            break

        # --- Side B factions ---
        side_b_label = "Defending factions" if campaign_type == 'Defense' else "Opposing factions"
        if campaign_type == 'Tug-of-War':
            side_b_label = "Opposing factions"
        await ctx.send(
            f"**{side_b_label}:**\n"
            f"Enter faction names separated by commas.\n"
            f"Example: `The Scavengers Guild, Pirates`"
        )
        while True:
            try:
                msg = await self.bot.wait_for('message', check=check, timeout=300)
            except TimeoutError:
                await ctx.send("❌ Setup timed out.")
                return
            if msg.content.lower() == 'cancel':
                await ctx.send("❌ Cancelled.")
                return
            side_b_factions = [f.strip() for f in msg.content.split(',') if f.strip()]
            if not side_b_factions:
                await ctx.send("❌ Please enter at least one faction name.")
                continue
            break

        selected_factions = side_a_factions + side_b_factions

        # --- Opposing EMS ---
        await ctx.send(
            "**What is the total Opposing EMS for this campaign?**\n"
            "This is the pool players must deplete to win.\n"
            "Enter a number (e.g. `1000`):"
        )
        while True:
            try:
                msg = await self.bot.wait_for('message', check=check, timeout=300)
            except TimeoutError:
                await ctx.send("❌ Setup timed out.")
                return
            if msg.content.lower() == 'cancel':
                await ctx.send("❌ Cancelled.")
                return
            val = msg.content.strip()
            if val.isdigit() and int(val) > 0:
                opposing_ems_total = int(val)
                break
            await ctx.send("❌ Please enter a positive number.")

        # --- Max commanders ---
        await ctx.send(
            "**Maximum commanders allowed to enroll?**\n"
            "Enter a number, or `0` for no limit:"
        )
        while True:
            try:
                msg = await self.bot.wait_for('message', check=check, timeout=300)
            except TimeoutError:
                await ctx.send("❌ Setup timed out.")
                return
            if msg.content.lower() == 'cancel':
                await ctx.send("❌ Cancelled.")
                return
            val = msg.content.strip()
            if val.isdigit():
                max_commanders = int(val)
                break
            await ctx.send("❌ Please enter a number (0 for no limit).")

        # --- Enrollment window ---
        await ctx.send(
            "**Enrollment window — how many days?**\n"
            "Enter `1` or `2`:"
        )
        while True:
            try:
                msg = await self.bot.wait_for('message', check=check, timeout=300)
            except TimeoutError:
                await ctx.send("❌ Setup timed out.")
                return
            if msg.content.lower() == 'cancel':
                await ctx.send("❌ Cancelled.")
                return
            val = msg.content.strip()
            if val.isdigit() and int(val) > 0:
                enrollment_days = int(val)
                break
            await ctx.send("❌ Please enter a positive number.")

        enrollment_deadline = (
            datetime.now(timezone.utc) + timedelta(days=enrollment_days)
        ).isoformat()

        # --- Write campaign — invoker becomes owner ---
        try:
            campaign_id = campaign_repo.create_campaign(
                db=self.db,
                name=campaign_name,
                campaign_type=campaign_type,
                factions=selected_factions,
                side_a_factions=','.join(side_a_factions),
                side_b_factions=','.join(side_b_factions),
                guild_id=interaction.guild.id,
                owner_user_id=interaction.user.id,
                opposing_ems_total=opposing_ems_total,
                max_commanders=max_commanders,
                enrollment_deadline=enrollment_deadline,
            )
        except Exception as e:
            log.exception("campaign_create DB error")
            await ctx.send(f"❌ Database error: {e}")
            return

        deadline_display  = _format_deadline(enrollment_deadline)
        cap_display       = str(max_commanders) if max_commanders else "No limit"

        embed = discord.Embed(
            title="✅ Campaign Created",
            description=(
                f"**{campaign_name}**\n"
                f"Type: {campaign_type}\n"
                f"Side A: {', '.join(side_a_factions)}\n"
                f"Side B: {', '.join(side_b_factions)}\n"
                f"Opposing EMS: `{opposing_ems_total}`\n"
                f"Max commanders: `{cap_display}`\n"
                f"Enrollment closes: {deadline_display}\n\n"
                f"You are the **Campaign Owner**.\n\n"
                f"Next steps:\n"
                f"1. `/campaign collaborator add` — invite collaborators\n"
                f"2. `/campaign record-bind` — bind your battle channels\n"
                f"3. `/campaign results-bind` — bind your results channel\n"
                f"4. `/campaign announce` — post the enrollment embed"
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"Campaign ID: {campaign_id}")
        await ctx.send(embed=embed)

    # =========================================================================
    # COLLABORATOR SUBGROUP
    # =========================================================================

    collaborator_group = app_commands.Group(
        name="collaborator",
        description="Manage campaign collaborators.",
        parent=campaign_group,
    )

    # -------------------------------------------------------------------------
    # /campaign collaborator add
    # Requires: campaign owner or admin.
    # Target must hold the @ERIS Creator role.
    # -------------------------------------------------------------------------

    @collaborator_group.command(
        name="add",
        description="[Owner] Add a collaborator to your campaign.",
    )
    @app_commands.describe(
        campaign_name="Campaign name",
        user="The ERIS Creator to add as collaborator",
    )
    async def collaborator_add(
        self,
        interaction: discord.Interaction,
        campaign_name: str,
        user: discord.Member,
    ):
        campaign = campaign_repo.find_campaign_by_name(
            self.db, interaction.guild.id, campaign_name
        )
        if not campaign:
            await interaction.response.send_message(
                f"❌ No campaign found matching '{campaign_name}'.", ephemeral=True
            )
            return

        if not self._can_manage_campaign(interaction, campaign['campaign_id']):
            await interaction.response.send_message(
                "❌ Only the campaign owner or an ERIS Admin can add collaborators.",
                ephemeral=True,
            )
            return

        # Target must be a Creator (or admin adding someone — admin bypass)
        if not self._is_admin(interaction.user) and not self._is_creator(user):
            await interaction.response.send_message(
                f"❌ {user.display_name} does not have the @ERIS Creator role.",
                ephemeral=True,
            )
            return

        if user.id == interaction.user.id:
            await interaction.response.send_message(
                "❌ You're already the owner — you can't add yourself as a collaborator.",
                ephemeral=True,
            )
            return

        if self._is_campaign_owner(campaign['campaign_id'], user.id):
            await interaction.response.send_message(
                f"❌ {user.display_name} is already the campaign owner.",
                ephemeral=True,
            )
            return

        campaign_repo.add_campaign_staff(
            db=self.db,
            campaign_id=campaign['campaign_id'],
            user_id=user.id,
            guild_id=interaction.guild.id,
            role='collaborator',
            assigned_by=interaction.user.id,
        )

        await interaction.response.send_message(
            f"✅ {user.mention} added as a collaborator on **{campaign['campaign_name']}**.\n"
            f"They can now host battles, create NPCs, start the campaign, and bind channels."
        )

    # -------------------------------------------------------------------------
    # /campaign collaborator remove
    # Requires: campaign owner or admin.
    # -------------------------------------------------------------------------

    @collaborator_group.command(
        name="remove",
        description="[Owner] Remove a collaborator from your campaign.",
    )
    @app_commands.describe(
        campaign_name="Campaign name",
        user="The collaborator to remove",
    )
    async def collaborator_remove(
        self,
        interaction: discord.Interaction,
        campaign_name: str,
        user: discord.Member,
    ):
        campaign = campaign_repo.find_campaign_by_name(
            self.db, interaction.guild.id, campaign_name
        )
        if not campaign:
            await interaction.response.send_message(
                f"❌ No campaign found matching '{campaign_name}'.", ephemeral=True
            )
            return

        if not self._can_manage_campaign(interaction, campaign['campaign_id']):
            await interaction.response.send_message(
                "❌ Only the campaign owner or an ERIS Admin can remove collaborators.",
                ephemeral=True,
            )
            return

        if self._is_campaign_owner(campaign['campaign_id'], user.id):
            await interaction.response.send_message(
                "❌ You can't remove the campaign owner via this command. "
                "Use `/campaign transfer` (admin only) to change ownership.",
                ephemeral=True,
            )
            return

        removed = campaign_repo.remove_campaign_staff(
            self.db, campaign['campaign_id'], user.id
        )

        if not removed:
            await interaction.response.send_message(
                f"❌ {user.display_name} is not a collaborator on this campaign.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ {user.display_name} removed as a collaborator from **{campaign['campaign_name']}**."
        )

    # -------------------------------------------------------------------------
    # /campaign collaborator list
    # Requires: anyone.
    # -------------------------------------------------------------------------

    @collaborator_group.command(
        name="list",
        description="List the owner and collaborators for a campaign.",
    )
    @app_commands.describe(campaign_name="Campaign name")
    async def collaborator_list(
        self,
        interaction: discord.Interaction,
        campaign_name: str,
    ):
        await interaction.response.defer()

        campaign = campaign_repo.find_campaign_by_name(
            self.db, interaction.guild.id, campaign_name
        )
        if not campaign:
            await interaction.followup.send(
                f"❌ No campaign found matching '{campaign_name}'."
            )
            return

        staff = campaign_repo.get_campaign_staff(self.db, campaign['campaign_id'])

        if not staff:
            await interaction.followup.send(
                f"No staff assigned to **{campaign['campaign_name']}** yet."
            )
            return

        lines = []
        for s in staff:
            member  = interaction.guild.get_member(s['user_id'])
            mention = member.mention if member else f"<@{s['user_id']}>"
            label   = '👑 Owner' if s['role'] == 'owner' else '🤝 Collaborator'
            lines.append(f"{label} — {mention}")

        embed = discord.Embed(
            title=f"🎖️ Staff — {campaign['campaign_name']}",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await interaction.followup.send(embed=embed)

    # =========================================================================
    # CAMPAIGN GROUP — continued
    # =========================================================================

    # -------------------------------------------------------------------------
    # /campaign start
    # Requires: campaign staff or admin.
    # Snapshots EMS, locks enrollment, posts opening bar to board thread.
    # -------------------------------------------------------------------------

    @campaign_group.command(name="start", description="[Staff] Start a campaign and lock enrollment.")
    @app_commands.describe(name="Campaign name to start")
    async def campaign_start(self, interaction: discord.Interaction, name: str):
        campaign = campaign_repo.find_campaign_by_name(self.db, interaction.guild.id, name)
        if not campaign:
            await interaction.response.send_message(
                f"❌ No campaign found matching '{name}'.", ephemeral=True
            )
            return

        if not self._can_staff_campaign(interaction, campaign['campaign_id']):
            await interaction.response.send_message(
                "❌ Only campaign staff or an ERIS Admin can start a campaign.",
                ephemeral=True,
            )
            return

        if campaign['status'] != 'active':
            await interaction.response.send_message(
                f"❌ '{campaign['campaign_name']}' is either already started or complete.",
                ephemeral=True,
            )
            return

        enrolled = campaign_repo.get_enrolled_commanders(self.db, campaign['campaign_id'])
        side_a = [e for e in enrolled if e.get('side') == 'a']
        side_b = [e for e in enrolled if e.get('side') == 'b']

        if not side_a or not side_b:
            await interaction.response.send_message(
                "❌ Both sides need at least one enrolled commander before starting.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        # ----- Guild config & channel resolution ---------------------------
        guild_config = guild_repo.get_guild_config(self.db, interaction.guild.id)
        if not guild_config:
            await interaction.followup.send(
                "❌ This server hasn't been configured yet. Run `/setup` first.",
                ephemeral=True,
            )
            return

        progress_ch_id = guild_config.get('channels', {}).get('campaign-progress')
        progress_ch = interaction.guild.get_channel(progress_ch_id) if progress_ch_id else None
        if not progress_ch:
            await interaction.followup.send(
                "❌ `#campaign-progress` channel not found in config. "
                "Run `/setup` again or add the channel manually via DB.",
                ephemeral=True,
            )
            return

        # ----- Snapshot EMS (flips status to 'battle') ---------------------
        side_a_total, side_b_total = campaign_repo.snapshot_campaign_ems(
            self.db, campaign['campaign_id']
        )

        side_a_factions = [s.strip() for s in (campaign.get('side_a_factions') or '').split(',') if s.strip()]
        side_b_factions = [s.strip() for s in (campaign.get('side_b_factions') or '').split(',') if s.strip()]

        embed = discord.Embed(
            title=f"⚔️ {campaign['campaign_name']} — Battles Begin!",
            description=(
                f"Enrollment is now **locked**.\n\n"
                f"🔵 **Side A** — {', '.join(side_a_factions)}\n"
                f"`{side_a_total}` EMS · {len(side_a)} commanders\n\n"
                f"🔴 **Side B** — {', '.join(side_b_factions)}\n"
                f"`{side_b_total}` EMS · {len(side_b)} commanders\n\n"
                f"Use `/battle engage` in a bound channel to begin."
            ),
            color=discord.Color.red(),
        )

        # ----- Post anchor message in #campaign-progress -------------------
        try:
            anchor_msg = await progress_ch.send(embed=embed)
        except discord.HTTPException as e:
            log.exception("Failed to post anchor message to #campaign-progress")
            await interaction.followup.send(
                f"❌ Couldn't post to {progress_ch.mention}: {e}",
                ephemeral=True,
            )
            return

        # ----- Create thread off the anchor --------------------------------
        try:
            thread = await anchor_msg.create_thread(
                name=f"{campaign['campaign_name']} — Battle Log"[:100],
                auto_archive_duration=10080,  # 7 days
                reason=f"Campaign {campaign['campaign_id']} started",
            )
        except discord.HTTPException as e:
            log.exception("Failed to create progress thread")
            await interaction.followup.send(
                f"❌ Anchor posted but thread creation failed: {e}\n"
                f"Anchor: {anchor_msg.jump_url}",
                ephemeral=True,
            )
            return

        campaign_repo.update_progress_thread_id(
            self.db, campaign['campaign_id'], thread.id
        )

        # ----- Cross-link: edit the original #campaign-board announcement --
        board_ref = campaign_repo.get_board_message_for_campaign(
            self.db, campaign['campaign_id']
        )
        if board_ref:
            board_ch = interaction.guild.get_channel(board_ref['channel_id'])
            if board_ch:
                try:
                    board_msg = await board_ch.fetch_message(board_ref['message_id'])
                    new_content = (
                        f"{board_msg.content}\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"⚔️ **Battles have begun!** Follow the action: {thread.mention}"
                    )
                    await board_msg.edit(content=new_content)
                except (discord.NotFound, discord.HTTPException):
                    log.warning(
                        "Could not edit campaign-board message for campaign %s",
                        campaign['campaign_id'],
                    )
                    
        # ----- Confirm to the staff member ---------------------------------
        await interaction.followup.send(
            f"✅ **{campaign['campaign_name']}** has begun.\n"
            f"Battle log: {thread.mention}",
            ephemeral=True,
        )


    # -------------------------------------------------------------------------
    # /campaign list  (unchanged — anyone)
    # -------------------------------------------------------------------------

    @campaign_group.command(name="list", description="List all active campaigns.")
    async def campaign_list(self, interaction: discord.Interaction):
        await interaction.response.defer()

        campaigns = campaign_repo.get_active_campaigns(self.db, interaction.guild.id)

        if not campaigns:
            await interaction.followup.send("No active campaigns right now.")
            return

        embed = discord.Embed(
            title="📋 Active Campaigns",
            color=discord.Color.blue(),
        )

        for c in campaigns:
            enrolled = campaign_repo.get_enrolled_commanders(self.db, c['campaign_id'])
            side_a_label = (c.get('side_a_factions') or 'Side A').replace(',', ', ')
            side_b_label = (c.get('side_b_factions') or 'Side B').replace(',', ', ')
            side_a_count = sum(1 for e in enrolled if e.get('side') == 'a')
            side_b_count = sum(1 for e in enrolled if e.get('side') == 'b')
            faction_lines = [
                f"{side_a_label} ({side_a_count} commanders)",
                f"{side_b_label} ({side_b_count} commanders)",
            ]

            embed.add_field(
                name=f"{c['campaign_name']} [{c['campaign_type']}]",
                value="\n".join(faction_lines) or "No factions yet.",
                inline=False,
            )

        await interaction.followup.send(embed=embed)

    # -------------------------------------------------------------------------
    # /campaign view  (unchanged — anyone)
    # -------------------------------------------------------------------------

    @campaign_group.command(name="view", description="View details of a campaign.")
    @app_commands.describe(name="Campaign name (partial match works)")
    async def campaign_view(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()

        campaign = campaign_repo.find_campaign_by_name(self.db, interaction.guild.id, name)
        if not campaign:
            await interaction.followup.send(f"❌ No campaign found matching '{name}'.")
            return

        factions = campaign_repo.get_campaign_factions(self.db, campaign['campaign_id'])
        enrolled = campaign_repo.get_enrolled_commanders(self.db, campaign['campaign_id'])

        opposing_ems_total   = campaign.get('opposing_ems_total', 0)
        opposing_ems_current = campaign.get('opposing_ems_current', 0)
        ems_dealt         = opposing_ems_total - opposing_ems_current
        cap_display       = str(campaign.get('max_commanders', 0)) if campaign.get('max_commanders') else 'No limit'

        embed = discord.Embed(
            title=f"📋 {campaign['campaign_name']}",
            description=(
                f"**Type:** {campaign['campaign_type']}\n"
                f"**Status:** {campaign['status'].capitalize()}\n"
                f"**Enrollment closes:** {_format_deadline(campaign.get('enrollment_deadline'))}\n"
                f"**Max commanders:** {cap_display}\n"
                f"**Commanders enrolled:** {len(enrolled)}\n\n"
                f"**Opposing EMS:** `{opposing_ems_current}` / `{opposing_ems_total}` remaining "
                f"*(dealt: `{ems_dealt}`)*"
            ),
            color=discord.Color.blue(),
        )

        side_a_label = (campaign.get('side_a_factions') or 'Side A').replace(',', ', ')
        side_b_label = (campaign.get('side_b_factions') or 'Side B').replace(',', ', ')

        for side, label in [('a', side_a_label), ('b', side_b_label)]:
            commanders_on_side = [e for e in enrolled if e.get('side') == side]
            if commanders_on_side:
                lines = [
                    f"• {e['commander_name']} ({e['rank']}) — {e.get('deployed_force', 'N/A')} [{e.get('faction', '?')}]"
                    for e in commanders_on_side
                ]
            else:
                lines = ["*No commanders enrolled.*"]
            embed.add_field(
                name=label,
                value="\n".join(lines),
                inline=False,
            )

        await interaction.followup.send(embed=embed)

    # -------------------------------------------------------------------------
    # /campaign roster  (unchanged — anyone)
    # -------------------------------------------------------------------------

    @campaign_group.command(name="roster", description="View enrolled commanders for a campaign.")
    @app_commands.describe(name="Campaign name")
    async def campaign_roster(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()

        campaign = campaign_repo.find_campaign_by_name(self.db, interaction.guild.id, name)
        if not campaign:
            await interaction.followup.send(f"❌ No campaign found matching '{name}'.")
            return

        enrolled = campaign_repo.get_enrolled_commanders(self.db, campaign['campaign_id'])

        if not enrolled:
            await interaction.followup.send(
                f"No commanders enrolled in **{campaign['campaign_name']}** yet."
            )
            return

        embed = discord.Embed(
            title=f"⚔️ Roster — {campaign['campaign_name']}",
            color=discord.Color.blue(),
        )

        side_a_label = (campaign.get('side_a_factions') or 'Side A').replace(',', ', ')
        side_b_label = (campaign.get('side_b_factions') or 'Side B').replace(',', ', ')

        for side, label in [('a', side_a_label), ('b', side_b_label)]:
            commanders_on_side = [e for e in enrolled if e.get('side') == side]
            if not commanders_on_side:
                continue
            lines = []
            for e in commanders_on_side:
                member = interaction.guild.get_member(e.get('user_id'))
                mention = member.mention if member else f"<@{e.get('user_id')}>"
                lines.append(
                    f"• {e['commander_name']} ({e['rank']}) — "
                    f"{e.get('deployed_force', 'N/A')} [{e.get('faction', '?')}] — {mention}"
                )
            embed.add_field(name=label, value="\n".join(lines), inline=False)

        await interaction.followup.send(embed=embed)

    # -------------------------------------------------------------------------
    # /campaign ems
    # -------------------------------------------------------------------------

    @campaign_group.command(
        name="ems",
        description="[Creator/Staff] View current EMS enrollment per side.",
    )
    @app_commands.describe(name="Campaign name")
    async def campaign_ems(self, interaction: discord.Interaction, name: str) -> None:
        if not (self._is_admin(interaction.user) or self._is_creator(interaction.user)):
            await interaction.response.send_message(
                "❌ Only ERIS Admins and Creators can view EMS status.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        campaign = campaign_repo.find_campaign_by_name(self.db, interaction.guild.id, name)
        if not campaign:
            await interaction.followup.send(f"❌ No campaign found matching '{name}'.", ephemeral=True)
            return

        campaign_type = (campaign.get('campaign_type') or '').lower()
        if campaign_type == 'tug-of-war':
            cap_a = campaign.get('side_a_ems_start') or 0
            cap_b = campaign.get('side_b_ems_start') or 0
            if cap_a:
                # Battle has started — use live current values
                side_a_ems = campaign.get('side_a_ems_current', 0)
                side_b_ems = campaign.get('side_b_ems_current', 0)
            else:
                # Pre-battle — show enrolled EMS, cap from opposing_ems_total
                side_a_ems = campaign_repo.get_side_ems_enrolled(self.db, campaign['campaign_id'], 'a')
                side_b_ems = campaign_repo.get_side_ems_enrolled(self.db, campaign['campaign_id'], 'b')
                cap_a = campaign.get('opposing_ems_total', 0)
                cap_b = cap_a
        else:
            cap_a = campaign.get('opposing_ems_total', 0)
            cap_b = cap_a
            side_a_ems = campaign_repo.get_side_ems_enrolled(self.db, campaign['campaign_id'], 'a')
            side_b_ems = campaign_repo.get_side_ems_enrolled(self.db, campaign['campaign_id'], 'b')
        side_a_full = campaign.get('side_a_full', 0)
        side_b_full = campaign.get('side_b_full', 0)

        def ems_bar(current, cap):
            if not cap:
                return "No cap set"
            pct = current / cap
            filled = int(pct * 10)
            bar = "█" * filled + "░" * (10 - filled)
            return f"`{bar}` {current}/{cap} ({int(pct * 100)}%)"

        embed = discord.Embed(
            title=f"📊 EMS Status — {campaign['campaign_name']}",
            color=discord.Color.orange(),
        )

        side_a_factions = campaign.get('side_a_factions') or 'Side A'
        side_b_factions = campaign.get('side_b_factions') or 'Side B'

        embed.add_field(
            name=f"⚔️ {side_a_factions}{' 🔒' if side_a_full else ''}",
            value=ems_bar(side_a_ems, cap_a),
            inline=False,
        )
        embed.add_field(
            name=f"🗡️ {side_b_factions}{' 🔒' if side_b_full else ''}",
            value=ems_bar(side_b_ems, cap_b),
            inline=False,
        )

        if cap_a:
            embed.set_footer(text=f"Starting EMS — Side A: {cap_a} · Side B: {cap_b}")
        else:
            embed.set_footer(text="No EMS cap configured for this campaign.")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # -------------------------------------------------------------------------
    # /campaign announce
    # Requires: campaign staff or admin.
    # -------------------------------------------------------------------------

    @campaign_group.command(
        name="announce",
        description="[Staff] Post the enrollment embed to #campaign-board.",
    )
    @app_commands.describe(name="Campaign name to announce.")
    async def campaign_announce(
            self, interaction: discord.Interaction, name: str
    ) -> None:
        campaign = campaign_repo.find_campaign_by_name(
            self.db, interaction.guild.id, name
        )
        if not campaign:
            await interaction.response.send_message(
                f"❌ No campaign found matching '{name}'.", ephemeral=True
            )
            return

        if not self._can_staff_campaign(interaction, campaign['campaign_id']):
            await interaction.response.send_message(
                "❌ Only campaign staff or an ERIS Admin can post announcements.",
                ephemeral=True,
            )
            return

        if campaign['status'] != 'active':
            await interaction.response.send_message(
                f"❌ Campaign '{campaign['campaign_name']}' is not active.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        guild_config = guild_repo.get_guild_config(self.db, interaction.guild.id)
        if not guild_config:
            await interaction.followup.send(
                "❌ This server hasn't been configured yet. Run `/setup` first.",
                ephemeral=True,
            )
            return

        thread_id = campaign.get('progress_thread_id')
        board_ch = None
        if thread_id:
            board_ch = interaction.guild.get_channel_or_thread(thread_id)
        if not board_ch:
            settings = guild_config.get('settings') or guild_config
            channels = settings.get('channels', {}) if isinstance(settings, dict) else {}
            board_channel_id = channels.get('campaign-board')
            board_ch = interaction.guild.get_channel(board_channel_id) if board_channel_id else None
        if not board_ch:
            await interaction.followup.send("❌ No campaign-board channel found. Run `/setup` first.", ephemeral=True)
            return

        side_a = [s.strip() for s in (campaign.get('side_a_factions') or '').split(',') if s.strip()]
        side_b = [s.strip() for s in (campaign.get('side_b_factions') or '').split(',') if s.strip()]
        theme  = self._theme(interaction.guild.id)

        from core.shared.campaign_display import get_faction_emoji

        def faction_lines(factions):
            return '\n'.join(
                f"{get_faction_emoji(f, theme)} {f}" for f in factions
            ) or '*None assigned*'

        cap_display = str(campaign.get('max_commanders', 0)) if campaign.get('max_commanders') else 'No limit'
        border = '━' * 30

        content = (
            f"## ⚔️ {campaign['campaign_name'].upper()}\n"
            f"**{campaign['campaign_type']}**\n"
            f"{border}\n\n"
            f"⚔️ **Side A**\n"
            f"{faction_lines(side_a)}\n\n"
            f"🗡️ **Side B**\n"
            f"{faction_lines(side_b)}\n\n"
            f"{border}\n"
            f"**Opposing EMS:** `{campaign.get('opposing_ems_total', 0)}` · "
            f"**Cap:** `{cap_display}` · "
            f"**Closes:** {_format_deadline(campaign.get('enrollment_deadline'))}\n\n"
            f"React ⚔️ to join Side A · 🗡️ to join Side B"
        )

        board_msg = await board_ch.send(content)
        await board_msg.add_reaction('⚔️')
        await board_msg.add_reaction('🗡️')

        campaign_repo.register_board_message(
            db=self.db,
            campaign_id=campaign['campaign_id'],
            message_id=board_msg.id,
            channel_id=board_ch.id,
        )

        await interaction.followup.send(
            f"✅ Announcement posted to {board_ch.mention}.", ephemeral=True
        )

    # -------------------------------------------------------------------------
    # /campaign record-bind
    # Requires: campaign staff or admin.
    # -------------------------------------------------------------------------

    @campaign_group.command(
        name="record-bind",
        description="[Staff] Bind a channel to a campaign as a space or ground battle channel.",
    )
    @app_commands.describe(
        channel="The channel where battles will be fought",
        battle_type="space or ground",
        campaign_name="Campaign name to bind this channel to",
    )
    async def campaign_bind(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        battle_type: str,
        campaign_name: str,
    ):
        battle_type = battle_type.lower()
        if battle_type not in VALID_BATTLE_TYPES:
            await interaction.response.send_message(
                "❌ Battle type must be `space` or `ground`.", ephemeral=True
            )
            return

        campaign = campaign_repo.find_campaign_by_name(
            self.db, interaction.guild.id, campaign_name
        )
        if not campaign:
            await interaction.response.send_message(
                f"❌ No campaign found matching '{campaign_name}'.", ephemeral=True
            )
            return

        if not self._can_staff_campaign(interaction, campaign['campaign_id']):
            await interaction.response.send_message(
                "❌ Only campaign staff or an ERIS Admin can bind channels.",
                ephemeral=True,
            )
            return

        if campaign['status'] not in ('active', 'battle'):
            await interaction.response.send_message(
                f"❌ Campaign '{campaign['campaign_name']}' is not active.", ephemeral=True
            )
            return

        try:
            campaign_repo.bind_channel(
                db=self.db,
                channel_id=channel.id,
                campaign_id=campaign['campaign_id'],
                battle_type=battle_type,
            )
        except Exception as e:
            log.exception("campaign_bind DB error")
            await interaction.response.send_message(
                f"❌ Database error: {e}", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ {channel.mention} is now a **{battle_type}** battle channel "
            f"for **{campaign['campaign_name']}**."
        )

    # -------------------------------------------------------------------------
    # /campaign unbind
    # Requires: ERIS Admin only.
    # -------------------------------------------------------------------------

    @campaign_group.command(
        name="unbind",
        description="[Admin] Remove a channel's campaign binding.",
    )
    @app_commands.describe(channel="The channel to unbind")
    async def campaign_unbind(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only ERIS Admins can unbind channels.", ephemeral=True
            )
            return

        removed = campaign_repo.unbind_channel(self.db, channel.id)

        if not removed:
            await interaction.response.send_message(
                f"❌ {channel.mention} is not bound to any campaign.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ {channel.mention} has been unbound."
        )

    # -------------------------------------------------------------------------
    # /campaign results-bind
    # Requires: campaign staff or admin.
    # -------------------------------------------------------------------------

    @campaign_group.command(
        name="results-bind",
        description="[Staff] Bind a channel as the results posting channel.",
    )
    @app_commands.describe(
        channel="The channel where battle results will be posted",
        campaign_name="Campaign name to bind this channel to",
    )
    async def campaign_results_bind(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        campaign_name: str,
    ):
        campaign = campaign_repo.find_campaign_by_name(
            self.db, interaction.guild.id, campaign_name
        )
        if not campaign:
            await interaction.response.send_message(
                f"❌ No campaign found matching '{campaign_name}'.", ephemeral=True
            )
            return

        if not self._can_staff_campaign(interaction, campaign['campaign_id']):
            await interaction.response.send_message(
                "❌ Only campaign staff or an ERIS Admin can bind channels.",
                ephemeral=True,
            )
            return

        if campaign['status'] not in ('active', 'battle'):
            await interaction.response.send_message(
                f"❌ Campaign '{campaign['campaign_name']}' is not active.", ephemeral=True
            )
            return

        try:
            campaign_repo.bind_results_channel(
                db=self.db,
                campaign_id=campaign['campaign_id'],
                results_channel_id=channel.id,
            )
        except Exception as e:
            log.exception("campaign_results_bind DB error")
            await interaction.response.send_message(
                f"❌ Database error: {e}", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ {channel.mention} is now the **results** channel "
            f"for **{campaign['campaign_name']}**."
        )

    # -------------------------------------------------------------------------
    # /campaign progress  — post live campaign progress to the board
    # -------------------------------------------------------------------------

    @campaign_group.command(
        name="progress",
        description="[Staff] Post the current campaign progress bar to the board.",
    )
    @app_commands.describe(name="Campaign name")
    async def campaign_progress(self, interaction: discord.Interaction, name: str):
        from core.shared import campaign_display

        await interaction.response.defer(ephemeral=True)

        campaign = campaign_repo.find_campaign_by_name(self.db, interaction.guild.id, name)
        if not campaign:
            await interaction.followup.send(f"❌ No campaign found matching '{name}'.", ephemeral=True)
            return

        if not self._can_staff_campaign(interaction, campaign['campaign_id']):
            await interaction.followup.send(
                "❌ Only campaign staff or an ERIS Admin can post progress.", ephemeral=True
            )
            return

        thread_id = campaign.get('progress_thread_id')
        board_ch = interaction.guild.get_channel_or_thread(thread_id) if thread_id else None

        if not board_ch:
            await interaction.followup.send("❌ No battle log thread found for this campaign. Has it been started?", ephemeral=True)
            return

        campaign_type = (campaign.get('campaign_type') or '').lower()
        campaign_name = campaign.get('campaign_name', 'Unknown Campaign')
        side_a_label = (campaign.get('side_a_factions') or 'Side A').replace(',', ', ')
        side_b_label = (campaign.get('side_b_factions') or 'Side B').replace(',', ', ')
        opposing_ems = campaign.get('opposing_ems_total', 1000)
        opposing_current = campaign.get('opposing_ems_current', opposing_ems)
        opposing_dealt = opposing_ems - opposing_current
        threshold = round(opposing_ems * campaign_display.VICTORY_PCT)
        battles_fought = campaign_repo.get_campaign_battle_count(self.db, campaign['campaign_id'])

        if campaign_type == 'tug-of-war':
            side_a_ems = campaign.get('side_a_ems_current', 0)
            side_b_ems = campaign.get('side_b_ems_current', 0)
            total_ems = (campaign.get('side_a_ems_start') or 0) + (campaign.get('side_b_ems_start') or 0)
            text = campaign_display.format_tug_of_war(
                campaign_name=campaign_name,
                guild_name=side_a_label,
                opposing_name=side_b_label,
                guild_ems=side_a_ems,
                opposing_ems_dealt=side_b_ems,
                total_ems=total_ems or opposing_ems * 2,
                battles_fought=battles_fought,
                threshold=threshold,
            )
        elif campaign_type == 'invasion':
            side_a_ems = campaign.get('side_a_ems_current', 0)
            text = campaign_display.format_invasion(
                campaign_name=campaign_name,
                guild_name=side_a_label,
                opposing_name=side_b_label,
                guild_ems=side_a_ems,
                opposing_total_ems=opposing_ems,
                battles_fought=battles_fought,
                threshold=threshold,
            )
        elif campaign_type == 'defense':
            guild_total = campaign.get('side_a_ems_start') or opposing_ems
            text = campaign_display.format_defense(
                campaign_name=campaign_name,
                guild_name=side_a_label,
                opposing_name=side_b_label,
                opposing_ems_dealt=opposing_dealt,
                guild_total_ems=guild_total,
                battles_fought=battles_fought,
                threshold=round(guild_total * campaign_display.VICTORY_PCT),
            )
        else:
            await interaction.followup.send(
                f"❌ Campaign type '{campaign_type}' doesn't have a progress display yet.", ephemeral=True
            )
            return

        await board_ch.send(text)
        await interaction.followup.send("✅ Progress posted to the board.", ephemeral=True)

    # -------------------------------------------------------------------------
    # /campaign complete
    # Requires: campaign owner or admin.
    # Marks campaign complete and unenrolls commanders.
    # (Leaderboard post will be added here once that system is built.)
    # -------------------------------------------------------------------------

    @campaign_group.command(name="complete", description="[Owner] Mark a campaign as complete.")
    @app_commands.describe(name="Campaign name to complete")
    async def campaign_complete(self, interaction: discord.Interaction, name: str):
        campaign = campaign_repo.find_campaign_by_name(self.db, interaction.guild.id, name)
        if not campaign:
            await interaction.response.send_message(
                f"❌ No campaign found matching '{name}'.", ephemeral=True
            )
            return

        if not self._can_manage_campaign(interaction, campaign['campaign_id']):
            await interaction.response.send_message(
                "❌ Only the campaign owner or an ERIS Admin can complete a campaign.",
                ephemeral=True,
            )
            return

        if campaign['status'] not in ('active', 'battle'):
            await interaction.response.send_message(
                f"❌ '{campaign['campaign_name']}' is already complete.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        ctx = self._ContextProxy(interaction)

        def check(m):
            return m.author == interaction.user and m.channel == interaction.channel

        await ctx.send(
            f"⚠️ **Complete campaign '{campaign['campaign_name']}'?**\n"
            f"This will unenroll all commanders and mark the campaign complete.\n\n"
            f"Reply `confirm` to proceed or `cancel` to abort."
        )

        try:
            msg = await self.bot.wait_for('message', check=check, timeout=60)
        except TimeoutError:
            await ctx.send("❌ Timed out. Campaign not completed.")
            return

        if msg.content.lower() != 'confirm':
            await ctx.send("❌ Cancelled.")
            return

        await ctx.send("**Optional: Enter a final result note** (or type `skip`):")
        try:
            msg = await self.bot.wait_for('message', check=check, timeout=120)
        except TimeoutError:
            result_note = None
        else:
            result_note = None if msg.content.lower() == 'skip' else msg.content.strip()

        try:
            enrolled = campaign_repo.get_enrolled_commanders(
                self.db, campaign['campaign_id']
            )
            for e in enrolled:
                commander_repo.set_commander_available(self.db, e['commander_id'])

            campaign_repo.close_campaign(
                db=self.db,
                campaign_id=campaign['campaign_id'],
                result_note=result_note,
            )
        except Exception as e:
            log.exception("campaign_complete DB error")
            await ctx.send(f"❌ Database error: {e}")
            return

        embed = discord.Embed(
            title=f"✅ Campaign Complete — {campaign['campaign_name']}",
            description=(
                f"**{len(enrolled)}** commanders have been unenrolled.\n"
                + (f"**Result:** {result_note}" if result_note else "")
            ),
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)
        # Post leaderboard to campaign progress thread
        thread_id = campaign.get('progress_thread_id')
        if thread_id:
            thread = interaction.guild.get_channel_or_thread(thread_id)
            if thread:
                try:
                    from core.shared.progress_renderer import build_leaderboard_embed

                    theme = self.bot.get_theme(interaction.guild.id)
                    leaderboard = campaign_repo.get_campaign_leaderboard_data(
                        self.db, campaign['campaign_id'], theme
                    )
                    total_battles = campaign_repo.get_campaign_battle_count(
                        self.db, campaign['campaign_id']
                    )
                    side_a_label = (campaign.get('side_a_factions') or 'Side A').replace(',', ', ')
                    side_b_label = (campaign.get('side_b_factions') or 'Side B').replace(',', ', ')

                    # Re-fetch campaign so end_date and final EMS are current
                    fresh = campaign_repo.get_campaign_by_id(
                        self.db, campaign['campaign_id']
                    )
                    lb_embed = build_leaderboard_embed(
                        campaign=fresh,
                        side_a_label=side_a_label,
                        side_b_label=side_b_label,
                        leaderboard=leaderboard,
                        total_battles=total_battles,
                    )
                    await thread.send(embed=lb_embed)
                except Exception:
                    log.exception(
                        "Failed to post leaderboard to thread %s", thread_id
                    )

    # -------------------------------------------------------------------------
    # /campaign retreat
    # Requires: campaign staff or admin.
    # Declares one side's retreat, closes the campaign, posts leaderboard.
    # -------------------------------------------------------------------------

    @campaign_group.command(
        name="retreat",
        description="[Staff] Declare a side's retreat and close the campaign.",
    )
    @app_commands.describe(
        name="Campaign name",
        side="Which side is retreating",
    )
    @app_commands.choices(side=[
        app_commands.Choice(name="Side A", value="a"),
        app_commands.Choice(name="Side B", value="b"),
    ])
    async def campaign_retreat(
        self,
        interaction: discord.Interaction,
        name: str,
        side: str,
    ):
        campaign = campaign_repo.find_campaign_by_name(
            self.db, interaction.guild.id, name
        )
        if not campaign:
            await interaction.response.send_message(
                f"❌ No campaign found matching '{name}'.", ephemeral=True
            )
            return

        if not self._can_staff_campaign(interaction, campaign['campaign_id']):
            await interaction.response.send_message(
                "❌ Only campaign staff or an ERIS Admin can declare a retreat.",
                ephemeral=True,
            )
            return

        if campaign['status'] not in ('active', 'battle'):
            await interaction.response.send_message(
                f"❌ '{campaign['campaign_name']}' is already complete.",
                ephemeral=True,
            )
            return

        side_a_label = (campaign.get('side_a_factions') or 'Side A').replace(',', ', ')
        side_b_label = (campaign.get('side_b_factions') or 'Side B').replace(',', ', ')
        retreating_label = side_a_label if side == 'a' else side_b_label
        victor_label     = side_b_label if side == 'a' else side_a_label
        winner_side      = 'b'          if side == 'a' else 'a'

        await interaction.response.defer()
        ctx = self._ContextProxy(interaction)

        result_note = (
            f"Side {'A' if side == 'a' else 'B'} — {retreating_label} — "
            f"retreated from the field. {victor_label} victorious."
        )

        try:
            enrolled = campaign_repo.get_enrolled_commanders(
                self.db, campaign['campaign_id']
            )
            for e in enrolled:
                commander_repo.set_commander_available(self.db, e['commander_id'])

            campaign_repo.close_campaign(
                db=self.db,
                campaign_id=campaign['campaign_id'],
                result_note=result_note,
            )
        except Exception as e:
            log.exception("campaign_retreat DB error")
            await ctx.send(f"❌ Database error: {e}")
            return

        embed = discord.Embed(
            title=f"🏳️ Retreat Declared — {campaign['campaign_name']}",
            description=(
                f"**{retreating_label}** has withdrawn from the field.\n"
                f"**{victor_label}** is victorious.\n\n"
                f"**{len(enrolled)}** commanders unenrolled."
            ),
            color=discord.Color.orange(),
        )
        await ctx.send(embed=embed)

        # Post leaderboard to campaign progress thread
        thread_id = campaign.get('progress_thread_id')
        if thread_id:
            thread = interaction.guild.get_channel_or_thread(thread_id)
            if thread:
                try:
                    from core.shared.progress_renderer import build_leaderboard_embed

                    theme         = self.bot.get_theme(interaction.guild.id)
                    leaderboard   = campaign_repo.get_campaign_leaderboard_data(
                        self.db, campaign['campaign_id'], theme
                    )
                    total_battles = campaign_repo.get_campaign_battle_count(
                        self.db, campaign['campaign_id']
                    )

                    # Re-fetch so end_date and final EMS are current
                    fresh = campaign_repo.get_campaign_by_id(
                        self.db, campaign['campaign_id']
                    )
                    lb_embed = build_leaderboard_embed(
                        campaign      = fresh,
                        side_a_label  = side_a_label,
                        side_b_label  = side_b_label,
                        leaderboard   = leaderboard,
                        total_battles = total_battles,
                        winner_side   = winner_side,
                    )
                    await thread.send(embed=lb_embed)
                except Exception:
                    log.exception(
                        "Failed to post leaderboard to thread %s", thread_id
                    )

    # -------------------------------------------------------------------------
    # /campaign edit
    # Requires: campaign owner or admin.
    # -------------------------------------------------------------------------

    async def _autocomplete_campaign_name(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        campaigns = campaign_repo.get_active_campaigns(self.db, interaction.guild.id)
        return [
            app_commands.Choice(name=c['campaign_name'], value=c['campaign_name'])
            for c in campaigns
            if current.lower() in c['campaign_name'].lower()
        ][:25]

    @campaign_group.command(name="edit", description="[Owner] Edit an active campaign's settings.")
    @app_commands.describe(name="Campaign name to edit")
    @app_commands.autocomplete(name=_autocomplete_campaign_name)
    async def campaign_edit(self, interaction: discord.Interaction, name: str):
        campaign = campaign_repo.find_campaign_by_name(self.db, interaction.guild.id, name)
        if not campaign:
            await interaction.response.send_message(
                f"❌ No campaign found matching '{name}'.", ephemeral=True
            )
            return

        if not self._can_manage_campaign(interaction, campaign['campaign_id']):
            await interaction.response.send_message(
                "❌ Only the campaign owner or an ERIS Admin can edit campaigns.",
                ephemeral=True,
            )
            return

        if campaign['status'] not in ('active', 'enrolling', 'battle'):
            await interaction.response.send_message(
                f"❌ Campaign '{campaign['campaign_name']}' is not active.", ephemeral=True
            )
            return

        await interaction.response.defer()
        ctx = self._ContextProxy(interaction)

        def check(m):
            return m.author == interaction.user and m.channel == interaction.channel

        campaign_type = (campaign.get('campaign_type') or '').lower()
        if campaign_type == 'tug-of-war':
            a_lost = (campaign.get('side_a_ems_start') or 0) - (campaign.get('side_a_ems_current') or 0)
            b_lost = (campaign.get('side_b_ems_start') or 0) - (campaign.get('side_b_ems_current') or 0)
            ems_dealt = a_lost + b_lost
        else:
            ems_dealt = campaign.get('opposing_ems_total', 0) - campaign.get('opposing_ems_current', 0)
        cap_display = str(campaign.get('max_commanders', 0)) if campaign.get('max_commanders') else 'No limit'

        class _EditSelect(discord.ui.Select):
            def __init__(self_):
                options = [
                    discord.SelectOption(label="Opposing EMS total",  value="1", emoji="⚔️"),
                    discord.SelectOption(label="Max commanders",       value="2", emoji="👥"),
                    discord.SelectOption(label="Enrollment deadline",  value="3", emoji="📅"),
                    discord.SelectOption(label="Cancel",               value="4", emoji="✖️"),
                ]
                super().__init__(placeholder="What would you like to change?", options=options)

            async def callback(self_, interaction_: discord.Interaction):
                if interaction_.user.id != interaction.user.id:
                    await interaction_.response.send_message("Not your menu.", ephemeral=True)
                    return
                view_.choice = self_.values[0]
                for child in view_.children:
                    child.disabled = True
                label = next(o.label for o in self_.options if o.value == self_.values[0])
                await interaction_.response.edit_message(
                    content=f"📋 **Edit — {campaign['campaign_name']}** — {label}", view=view_
                )
                view_.stop()

        class _EditView(discord.ui.View):
            def __init__(self_):
                super().__init__(timeout=120)
                self_.choice = None
                self_.add_item(_EditSelect())

            async def on_timeout(self_):
                pass

        view_ = _EditView()
        await ctx.send(
            f"📋 **Edit — {campaign['campaign_name']}**\n\n"
            f"Current values:\n"
            f"• Opposing EMS total: `{campaign.get('opposing_ems_total', 0)}`  "
            f"*(EMS already dealt: `{ems_dealt}`)*\n"
            f"• Max commanders: `{cap_display}`\n"
            f"• Enrollment deadline: {_format_deadline(campaign.get('enrollment_deadline'))}",
            view=view_
        )
        await view_.wait()

        choice = view_.choice
        if choice is None:
            await ctx.send("❌ Timed out. Edit cancelled.")
            return
        if choice == '4':
            await ctx.send("❌ Edit cancelled.")
            return

        # --- Opposing EMS ---
        if choice == '1':
            await ctx.send(
                f"**New Opposing EMS total?**\n"
                f"EMS already dealt: `{ems_dealt}` — new total must be higher than this.\n"
                f"Current total: `{campaign.get('opposing_ems_total', 0)}`\n"
                f"Enter a number, or `cancel`:"
            )
            while True:
                try:
                    msg = await self.bot.wait_for('message', check=check, timeout=120)
                except TimeoutError:
                    await ctx.send("❌ Timed out.")
                    return
                if msg.content.lower() == 'cancel':
                    await ctx.send("❌ Cancelled.")
                    return
                val = msg.content.strip()
                if not val.isdigit():
                    await ctx.send("❌ Please enter a positive number.")
                    continue
                new_total = int(val)
                if new_total < ems_dealt:
                    await ctx.send(
                        f"❌ Cannot set below `{ems_dealt}` — "
                        f"that EMS has already been dealt in battle."
                    )
                    continue
                diff        = new_total - campaign.get('opposing_ems_total', 0)
                new_current = max(0, campaign.get('opposing_ems_current', 0) + diff)
                campaign_repo.update_campaign_opposing_ems(
                    self.db, campaign['campaign_id'], new_total, new_current
                )
                await ctx.send(
                    f"✅ Opposing EMS updated.\n"
                    f"New total: `{new_total}` — remaining pool: `{new_current}`"
                )
                break

        # --- Max commanders ---
        elif choice == '2':
            enrolled_count = campaign_repo.get_enrolled_commander_count(
                self.db, campaign['campaign_id']
            )
            await ctx.send(
                f"**New max commanders?**\n"
                f"Currently enrolled: `{enrolled_count}`\n"
                f"Enter a number (must be ≥ `{enrolled_count}`), or `0` for no limit:"
            )
            while True:
                try:
                    msg = await self.bot.wait_for('message', check=check, timeout=120)
                except TimeoutError:
                    await ctx.send("❌ Timed out.")
                    return
                if msg.content.lower() == 'cancel':
                    await ctx.send("❌ Cancelled.")
                    return
                val = msg.content.strip()
                if not val.isdigit():
                    await ctx.send("❌ Please enter a number.")
                    continue
                new_max = int(val)
                if new_max != 0 and new_max < enrolled_count:
                    await ctx.send(
                        f"❌ Cannot set below `{enrolled_count}` — "
                        f"that many commanders are already enrolled."
                    )
                    continue
                campaign_repo.update_campaign_max_commanders(
                    self.db, campaign['campaign_id'], new_max
                )
                label = 'No limit' if new_max == 0 else str(new_max)
                await ctx.send(f"✅ Max commanders updated to `{label}`.")
                break

        # --- Enrollment deadline ---
        elif choice == '3':
            await ctx.send(
                f"**Extend enrollment by how many days from now?**\n"
                f"Enter `1` or `2`:"
            )
            while True:
                try:
                    msg = await self.bot.wait_for('message', check=check, timeout=120)
                except TimeoutError:
                    await ctx.send("❌ Timed out.")
                    return
                if msg.content.lower() == 'cancel':
                    await ctx.send("❌ Cancelled.")
                    return
                if msg.content.strip() not in ('1', '2'):
                    await ctx.send("❌ Please enter `1` or `2`.")
                    continue
                days         = int(msg.content.strip())
                new_deadline = (
                    datetime.now(timezone.utc) + timedelta(days=days)
                ).isoformat()
                campaign_repo.update_campaign_deadline(
                    self.db, campaign['campaign_id'], new_deadline
                )
                await ctx.send(
                    f"✅ Enrollment deadline extended by {days} day(s).\n"
                    f"New deadline: {_format_deadline(new_deadline)}"
                )
                break

    # -------------------------------------------------------------------------
    # /campaign delete
    # Requires: ERIS Admin only.
    # Hard-deletes the campaign and all related records.
    # -------------------------------------------------------------------------

    @campaign_group.command(name="delete", description="[Admin] Permanently delete a campaign.")
    @app_commands.describe(name="Campaign name to delete")
    async def campaign_delete(self, interaction: discord.Interaction, name: str):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only ERIS Admins can delete campaigns.", ephemeral=True
            )
            return

        campaign = campaign_repo.find_campaign_by_name(self.db, interaction.guild.id, name)
        if not campaign:
            await interaction.response.send_message(
                f"❌ No campaign found matching '{name}'.", ephemeral=True
            )
            return

        await interaction.response.defer()
        ctx = self._ContextProxy(interaction)

        def check(m):
            return m.author == interaction.user and m.channel == interaction.channel

        await ctx.send(
            f"⛔ **Permanently delete '{campaign['campaign_name']}'?**\n"
            f"This removes the campaign and all its records. **This cannot be undone.**\n\n"
            f"Type the campaign name exactly to confirm, or `cancel` to abort:"
        )

        try:
            msg = await self.bot.wait_for('message', check=check, timeout=60)
        except TimeoutError:
            await ctx.send("❌ Timed out. Campaign not deleted.")
            return

        if msg.content.strip().lower() == 'cancel':
            await ctx.send("❌ Cancelled.")
            return

        if msg.content.strip() != campaign['campaign_name']:
            await ctx.send(
                "❌ Name did not match exactly. Campaign not deleted."
            )
            return

        try:
            campaign_repo.delete_campaign(self.db, campaign['campaign_id'])
        except Exception as e:
            log.exception("campaign_delete DB error")
            await ctx.send(f"❌ Database error: {e}")
            return

        await ctx.send(
            f"🗑️ Campaign **{campaign['campaign_name']}** has been permanently deleted."
        )

    # =========================================================================
    # REACTION LISTENER — ⚔️ enrollment trigger  (unchanged)
    # =========================================================================

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """
        Listens for ⚔️ reactions on campaign board messages.
        Hands off immediately to sequences/enrollment_flow.py.
        """
        if payload.user_id == self.bot.user.id:
            return
        if str(payload.emoji) not in ('⚔️', '🗡️'):
            return

        log.info(f"Reaction received on message {payload.message_id} by user {payload.user_id}")

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        member = guild.get_member(payload.user_id)
        if not member or member.bot:
            return

        try:
            await enrollment_flow.on_raw_reaction_add(payload, self.bot)
        except Exception as e:
            log.error(f"Enrollment flow error: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """
        Listens for ⚔️/🗡️ reaction removals on campaign board messages.
        Unenrolls the commander if the player un-reacts.
        """
        if payload.user_id == self.bot.user.id:
            return
        if str(payload.emoji) not in ('⚔️', '🗡️'):
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        member = guild.get_member(payload.user_id)
        if not member or member.bot:
            return

        try:
            await enrollment_flow.on_raw_reaction_remove(payload, self.bot)
        except Exception as e:
            log.error(f"Unenrollment flow error: {e}", exc_info=True)


# =============================================================================
# SETUP
# =============================================================================

async def setup(bot):
    cog = CampaignCog(bot)
    await bot.add_cog(cog, override=True)
