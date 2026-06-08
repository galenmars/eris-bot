"""
cogs/admin_cog.py
=================
E.R.I.S. Bot — Admin Commands

PURPOSE
-------
Server configuration and emergency controls.
All commands here are restricted to ERIS Admin or server owner.

COMMANDS
--------
/admin config set [key] [value] — owner only, guild config values
/admin config view              — admin, shows current config
/admin cancel battle            — admin, cancels active battle in a channel
/admin cancel request [id]      — admin, cancels pending EMS/commander request
/admin clearbattles             — owner only, cancels ALL active battles
/admin log                      — admin, last 20 entries from eris-admin-log
/admin reload                   — owner only, reloads all cogs without restart
/admin rolessync @member        — admin, syncs Discord roles to commander force types
/admin org add [name]           — admin, registers an organization with faction alignment
/admin org list                 — admin, lists all registered organizations
/admin org remove [name]        — admin, removes a registered organization

WHAT THIS COG DOES NOT OWN
---------------------------
Game logic of any kind — calls sequences and repos directly only for
emergency operations, never for normal game flow.
"""

import logging
import importlib

import discord
from discord.ext import commands
from discord     import app_commands

from core.data              import battle_repo, ems_repo, commander_repo, guild_repo
from core.domain.exceptions import DomainError

log = logging.getLogger(__name__)

# Keys the owner is allowed to set via /admin config set
# Maps display key → database key (never derived from user input)
VALID_CONFIG_KEYS = {
    'fleet_url':          'fleet_builder_url',
    'army_url':           'army_builder_url',
    'dice':               'dice_string',
    'record_channel':     'battle_record_channel_id',
    'submissions_channel':'commander_submissions_channel_id',
    'approvals_channel':  'commander_approvals_channel_id',
    'ems_channel':        'ems_requests_channel_id',
    'notification_channel':'notification_channel_id',
    'max_mains':          'max_mains',
    'max_seniors':        'max_seniors',
    'max_juniors':        'max_juniors',
}


# =============================================================================
# ADMIN COG
# =============================================================================

class AdminCog(commands.Cog, name="Admin"):
    """Server configuration and emergency admin commands."""

    def __init__(self, bot):
        self.bot = bot
        self.db  = bot.db.conn

    def _is_admin(self, member: discord.Member) -> bool:
        guild_config  = guild_repo.get_guild_config(self.db, member.guild.id)
        admin_role_id = guild_config.get('roles', {}).get('ERIS Admin') if guild_config else None
        if not admin_role_id:
            return member.guild_permissions.administrator
        return any(r.id == admin_role_id for r in member.roles)

    def _is_owner(self, member: discord.Member) -> bool:
        return member.id == member.guild.owner_id

    async def _log_action(self, guild: discord.Guild, message: str):
        """Post to #eris-admin-log if configured."""
        guild_config = guild_repo.get_guild_config(self.db, guild.id)
        log_ch_id    = guild_config.get('channels', {}).get('eris-admin-log') if guild_config else None
        if log_ch_id:
            log_ch = guild.get_channel(log_ch_id)
            if log_ch:
                try:
                    await log_ch.send(message)
                except Exception:
                    pass

    # =========================================================================
    # ADMIN GROUP
    # =========================================================================

    admin_group = app_commands.Group(
        name="admin",
        description="Admin configuration and emergency commands.",
    )

    # -------------------------------------------------------------------------
    # CONFIG SUBGROUP
    # -------------------------------------------------------------------------

    config_group = app_commands.Group(
        name="config",
        description="Guild configuration commands.",
        parent=admin_group,
    )

    @config_group.command(name="set", description="[Owner] Set a guild configuration value.")
    @app_commands.describe(
        key="Config key to set (see /admin config view for valid keys)",
        value="Value to set",
    )
    async def config_set(
        self, interaction: discord.Interaction, key: str, value: str
    ):
        if not self._is_owner(interaction.user):
            await interaction.response.send_message(
                "❌ Only the server owner can change guild config.", ephemeral=True
            )
            return

        db_key = VALID_CONFIG_KEYS.get(key.lower())
        if not db_key:
            valid = ", ".join(f"`{k}`" for k in VALID_CONFIG_KEYS)
            await interaction.response.send_message(
                f"❌ Unknown config key `{key}`.\nValid keys: {valid}",
                ephemeral=True,
            )
            return

        try:
            guild_repo.set_config_value(self.db, interaction.guild.id, db_key, value)
        except Exception as e:
            log.exception("config_set DB error")
            await interaction.response.send_message(
                f"❌ Database error: {e}", ephemeral=True
            )
            return

        await self._log_action(
            interaction.guild,
            f"⚙️ Config updated by {interaction.user.mention}: `{key}` = `{value}`",
        )
        await interaction.response.send_message(
            f"✅ `{key}` set to `{value}`.", ephemeral=True
        )

    @config_group.command(name="view", description="[Admin] View current guild configuration.")
    async def config_view(self, interaction: discord.Interaction):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only ERIS Admins can view guild config.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        guild_config = guild_repo.get_guild_config(self.db, interaction.guild.id)
        if not guild_config:
            await interaction.followup.send(
                "No configuration found. Run `/setup` first.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="⚙️ Guild Configuration",
            color=discord.Color.blurple(),
        )

        for display_key, db_key in VALID_CONFIG_KEYS.items():
            val = guild_config.get(db_key) or guild_config.get('settings', {}).get(db_key)
            embed.add_field(
                name=display_key,
                value=f"`{val}`" if val else "*not set*",
                inline=True,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # -------------------------------------------------------------------------
    # CANCEL SUBGROUP
    # -------------------------------------------------------------------------

    cancel_group = app_commands.Group(
        name="cancel",
        description="Cancel active battles or requests.",
        parent=admin_group,
    )

    @cancel_group.command(
        name="battle",
        description="[Admin] Cancel the active battle in a channel.",
    )
    @app_commands.describe(channel="The battle channel (defaults to current channel)")
    async def cancel_battle(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only ERIS Admins can cancel battles.", ephemeral=True
            )
            return

        target_channel = channel or interaction.channel
        battle = battle_repo.get_active_battle_in_channel(self.db, target_channel.id)

        if not battle:
            await interaction.response.send_message(
                f"❌ No active battle in {target_channel.mention}.", ephemeral=True
            )
            return

        try:
            battle_repo.cancel_battle(self.db, battle['battle_id'])
        except Exception as e:
            log.exception("cancel_battle DB error")
            await interaction.response.send_message(
                f"❌ Database error: {e}", ephemeral=True
            )
            return

        await self._log_action(
            interaction.guild,
            f"❌ Battle **{battle.get('battle_name', '')}** cancelled by "
            f"{interaction.user.mention} in {target_channel.mention}",
        )

        await interaction.response.send_message(
            f"✅ Battle in {target_channel.mention} cancelled.\n"
            f"{battle['hero_name']} vs {battle['hero_name2']} — result voided."
        )

    @cancel_group.command(
        name="request",
        description="[Admin] Cancel a pending EMS or commander request.",
    )
    @app_commands.describe(
        request_id="The request ID to cancel",
        request_type="Type of request: ems or commander",
    )
    async def cancel_request(
        self,
        interaction: discord.Interaction,
        request_id: int,
        request_type: str = "ems",
    ):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only ERIS Admins can cancel requests.", ephemeral=True
            )
            return

        request_type = request_type.lower()

        try:
            if request_type == 'ems':
                request = ems_repo.get_request(self.db, request_id)
                if not request:
                    await interaction.response.send_message(
                        f"❌ No EMS request found with ID {request_id}.", ephemeral=True
                    )
                    return
                ems_repo.update_request_status(self.db, request_id, 'cancelled')

            elif request_type == 'commander':
                request = commander_repo.get_pending_submission(self.db, request_id)
                if not request:
                    await interaction.response.send_message(
                        f"❌ No commander request found with ID {request_id}.", ephemeral=True
                    )
                    return
                commander_repo.update_submission_status(self.db, request_id, 'cancelled')
            else:
                await interaction.response.send_message(
                    "❌ request_type must be `ems` or `commander`.", ephemeral=True
                )
                return

        except Exception as e:
            log.exception("cancel_request DB error")
            await interaction.response.send_message(
                f"❌ Database error: {e}", ephemeral=True
            )
            return

        await self._log_action(
            interaction.guild,
            f"❌ {request_type.capitalize()} request {request_id} cancelled by "
            f"{interaction.user.mention}",
        )
        await interaction.response.send_message(
            f"✅ {request_type.capitalize()} request {request_id} cancelled.",
            ephemeral=True,
        )

    # -------------------------------------------------------------------------
    # /admin clear battles  — nuclear option
    # -------------------------------------------------------------------------

    @admin_group.command(
        name="clearbattles",
        description="[Owner] Cancel ALL active battles across the guild.",
    )
    async def clear_battles(self, interaction: discord.Interaction):
        if not self._is_owner(interaction.user):
            await interaction.response.send_message(
                "❌ Only the server owner can clear all battles.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            count = battle_repo.cancel_all_active_battles(self.db, interaction.guild.id)
        except Exception as e:
            log.exception("clear_battles DB error")
            await interaction.followup.send(
                f"❌ Database error: {e}", ephemeral=True
            )
            return

        await self._log_action(
            interaction.guild,
            f"⚠️ **ALL ACTIVE BATTLES CLEARED** by {interaction.user.mention}. "
            f"({count} battle(s) cancelled)",
        )
        await interaction.followup.send(
            f"✅ {count} active battle(s) cancelled across the guild.",
            ephemeral=True,
        )

    # -------------------------------------------------------------------------
    # /admin log
    # -------------------------------------------------------------------------

    @admin_group.command(
        name="log",
        description="[Admin] View the last 20 entries from the admin log.",
    )
    async def admin_log(self, interaction: discord.Interaction):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only ERIS Admins can view the admin log.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        guild_config = guild_repo.get_guild_config(self.db, interaction.guild.id)
        log_ch_id    = guild_config.get('channels', {}).get('eris-admin-log') if guild_config else None

        if not log_ch_id:
            await interaction.followup.send(
                "❌ No admin log channel configured.", ephemeral=True
            )
            return

        log_ch = interaction.guild.get_channel(log_ch_id)
        if not log_ch:
            await interaction.followup.send(
                "❌ Admin log channel not found — it may have been deleted.", ephemeral=True
            )
            return

        messages = [msg async for msg in log_ch.history(limit=20)]
        if not messages:
            await interaction.followup.send(
                "Admin log is empty.", ephemeral=True
            )
            return

        lines = []
        for msg in reversed(messages):  # Oldest first
            ts    = discord.utils.format_dt(msg.created_at, style='R')
            lines.append(f"{ts} — {msg.content}")

        content = "\n".join(lines)
        if len(content) > 2000:
            content = content[-1900:] + "\n*(truncated — see log channel for full history)*"

        await interaction.followup.send(content, ephemeral=True)

    # -------------------------------------------------------------------------
    # /admin reload
    # -------------------------------------------------------------------------

    @admin_group.command(
        name="reload",
        description="[Owner] Reload all cogs without restarting the bot.",
    )
    async def reload_cogs(self, interaction: discord.Interaction):
        if not self._is_owner(interaction.user):
            await interaction.response.send_message(
                "❌ Only the server owner can reload cogs.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        cog_names = list(self.bot.extensions.keys())
        results   = []

        for name in cog_names:
            try:
                await self.bot.reload_extension(name)
                results.append(f"✅ {name}")
            except Exception as e:
                results.append(f"❌ {name}: {e}")
                log.exception(f"Failed to reload {name}")

        await self._log_action(
            interaction.guild,
            f"🔄 Cogs reloaded by {interaction.user.mention}:\n" + "\n".join(results),
        )
        await interaction.followup.send("\n".join(results), ephemeral=True)

    # -------------------------------------------------------------------------
    # /admin roles sync @member
    # -------------------------------------------------------------------------

    @admin_group.command(
        name="rolessync",
        description="[Admin] Sync Discord roles for a member based on their commanders.",
    )
    @app_commands.describe(member="The member whose roles to sync")
    async def roles_sync(self, interaction: discord.Interaction, member: discord.Member):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only ERIS Admins can sync roles.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        guild_config = guild_repo.get_guild_config(self.db, interaction.guild.id)
        if not guild_config:
            await interaction.followup.send(
                "❌ Guild not configured. Run `/setup` first.", ephemeral=True
            )
            return

        roles_config = guild_config.get('roles', {})
        commanders   = commander_repo.get_commanders_for_user(
            self.db, member.id, interaction.guild.id
        )

        has_fleet = any(c['force_type'] == 'Fleet' for c in commanders)
        has_army  = any(c['force_type'] == 'Army'  for c in commanders)
        has_any   = bool(commanders)

        added   = []
        removed = []

        async def _sync_role(role_name: str, should_have: bool):
            role_id = roles_config.get(role_name)
            if not role_id:
                return
            role = interaction.guild.get_role(role_id)
            if not role:
                return
            if should_have and role not in member.roles:
                await member.add_roles(role, reason="ERIS role sync")
                added.append(role.name)
            elif not should_have and role in member.roles:
                await member.remove_roles(role, reason="ERIS role sync")
                removed.append(role.name)

        await _sync_role('Commander',       has_any)
        await _sync_role('Fleet Commander', has_fleet)
        await _sync_role('Army Commander',  has_army)

        summary = []
        if added:
            summary.append(f"Added: {', '.join(added)}")
        if removed:
            summary.append(f"Removed: {', '.join(removed)}")
        if not added and not removed:
            summary.append("Roles already in sync — no changes made.")

        await self._log_action(
            interaction.guild,
            f"🔄 Roles synced for {member.mention} by {interaction.user.mention}: "
            + "; ".join(summary),
        )
        await interaction.followup.send(
            f"✅ {member.mention} roles synced.\n" + "\n".join(summary),
            ephemeral=True,
        )


    # -------------------------------------------------------------------------
    # ORG SUBGROUP
    # -------------------------------------------------------------------------

    org_group = app_commands.Group(
        name="org",
        description="Manage registered organizations for commander submissions.",
        parent=admin_group,
    )

    @org_group.command(
        name="add",
        description="[Admin] Register a new organization (players can pick it during commander submission).",
    )
    @app_commands.describe(name="Organization name, e.g. 'The Red Veil'")
    async def org_add(self, interaction: discord.Interaction, name: str):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only ERIS Admins can manage organizations.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        theme = self.bot.get_theme()
        if not theme:
            await interaction.followup.send(
                "❌ No theme loaded. Cannot look up factions.", ephemeral=True
            )
            return

        # Use the faction picker so the admin selects alignment from the
        # canonical theme list — same UI the players already know.
        dm = await interaction.user.create_dm()
        await interaction.followup.send(
            "📬 Check your DMs to select the faction alignment for this organization.",
            ephemeral=True,
        )

        from core.shared.faction_picker import prompt_faction
        result = await prompt_faction(channel=dm, user=interaction.user, theme=theme)
        if result is None:
            return  # timed out, user already notified

        name = name.strip()
        ok = guild_repo.add_organization(
            self.db,
            interaction.guild.id,
            name=name,
            faction_id=result.faction_id,
            faction_name=result.faction_name,
            group=result.group,
        )

        if not ok:
            await dm.send(
                f"❌ An organization named **{name}** already exists on this server."
            )
            return

        await self._log_action(
            interaction.guild,
            f"🏛️ Organization added by {interaction.user.mention}: "
            f"**{name}** · {result.faction_name}",
        )
        await dm.send(
            f"✅ **{name}** registered under **{result.faction_name}**. "
            f"Players can now select it during commander submission."
        )

    @org_group.command(
        name="list",
        description="[Admin] View all registered organizations for this server.",
    )
    async def org_list(self, interaction: discord.Interaction):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only ERIS Admins can view organizations.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        orgs = guild_repo.get_organizations(self.db, interaction.guild.id)
        if not orgs:
            await interaction.followup.send(
                "No organizations registered yet. Use `/admin org add` to add one.",
                ephemeral=True,
            )
            return

        from core.shared.faction_picker import _GROUP_LABELS
        embed = discord.Embed(
            title="🏛️ Registered Organizations",
            color=discord.Color.blurple(),
        )

        # Group by alignment for a cleaner display
        by_group: dict[str, list[str]] = {}
        for o in orgs:
            label = _GROUP_LABELS.get(o['group'], o['group'].title())
            by_group.setdefault(label, []).append(
                f"**{o['name']}** · {o['faction_name']}"
            )

        for group_label, entries in by_group.items():
            embed.add_field(
                name=group_label,
                value="\n".join(entries),
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @org_group.command(
        name="remove",
        description="[Admin] Remove a registered organization.",
    )
    @app_commands.describe(name="Exact organization name to remove")
    async def org_remove(self, interaction: discord.Interaction, name: str):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only ERIS Admins can remove organizations.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        ok = guild_repo.remove_organization(self.db, interaction.guild.id, name.strip())
        if not ok:
            await interaction.followup.send(
                f"❌ No organization named **{name}** found. "
                "Use `/admin org list` to see registered organizations.",
                ephemeral=True,
            )
            return

        await self._log_action(
            interaction.guild,
            f"🗑️ Organization removed by {interaction.user.mention}: **{name}**",
        )
        await interaction.followup.send(
            f"✅ **{name}** removed. Players can no longer select it.",
            ephemeral=True,
        )


# =============================================================================
# SETUP
# =============================================================================

async def setup(bot):
    cog = AdminCog(bot)
    await bot.add_cog(cog, override=True)