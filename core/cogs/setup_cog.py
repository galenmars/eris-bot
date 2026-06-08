"""
cogs/setup_cog.py
=================
E.R.I.S. Bot — Server Setup

PURPOSE
-------
One-time guild configuration. Creates all required roles and channels,
saves IDs to the database, then instructs the owner to revoke elevated
permissions manually.

SECURITY MODEL
--------------
/setup is gated by two conditions that must ALL be true:
  1. Invoker is the server OWNER (not just admin)
  2. No guild_config record exists for this guild yet

After successful setup, the owner must manually remove
Manage Roles and Manage Channels from the ERIS bot role.
Automatic revocation is not currently active.

If setup fails mid-way, permissions are NOT revoked so the owner
can fix the issue and retry. The failure message explains exactly
what went wrong and what to do next.

THREE PHASES
------------
Phase 1 — Pre-flight   (read-only checks, nothing created)
Phase 2 — Creation     (roles, categories, channels — real-time updates)
Phase 3 — Completion   (posts requirements doc, instructs owner to revoke manually)

WHAT THIS COG DOES NOT OWN
---------------------------
Any game logic. This cog only builds the server structure.
All game commands live in other cogs.

COMMANDS
--------
/setup              — server owner only, one-time guild configuration (auto or manual mode)
/setup-status       — any member, shows current ERIS channel/role configuration
/setup-reset        — server owner only, clears config without deleting channels or roles
/db-status          — server owner only, diagnostic DB state inspector
"""


import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord import app_commands

from core.data   import guild_repo
from core.shared import config

log = logging.getLogger(__name__)

# =============================================================================
# CONSTANTS
# =============================================================================

# Channels the bot needs to create, in order.
# Each entry: (name, category_name, purpose, admin_only)
REQUIRED_CHANNELS = [
    # E.R.I.S. SYSTEM category
    ('eris-setup',           'E.R.I.S. SYSTEM',   'Bot setup log and requirements document',    True),
    ('eris-notifications',   'E.R.I.S. SYSTEM',   'Bot developer broadcasts and announcements', False),
    ('eris-admin-log',       'E.R.I.S. SYSTEM',   'All admin actions logged here',              True),

    # COMMAND CENTER category
    ('campaign-board',       'COMMAND CENTER',     'Campaign embeds with ⚔️ enrollment reaction', False),
    ('campaign-progress',    'COMMAND CENTER',     'Live battle log threads per active campaign', False),
    ('holo-broadcast',       'E.R.I.S. SYSTEM',    'Daily recaps and campaign highlights',        False),
    ('commander-submissions','COMMAND CENTER',     '/ems submit — paste your commander block',    False),

    # ADMINISTRATION category
    ('commander-approvals',  'ADMINISTRATION',     'Pending commander block review',             True),
    ('ems-requests',         'ADMINISTRATION',     'Pending EMS request review',                 True),
]

# Roles the bot needs to create, in order.
# Each entry: (name, color_hex, purpose, hoist)
# hoist=True means the role shows separately in the member list
REQUIRED_ROLES = [
    ('ERIS Admin',       0x00d4ff, 'Can run all admin commands',               True),
    ('ERIS Creator',     0xff69b4, 'Can create and manage campaigns',          True),
    ('Commander',        0xffb700, 'Granted when first commander is approved', True),
    ('Fleet Commander',  0x00ff88, 'Has a Fleet force type deployed',          False),
    ('Army Commander',   0xff6b35, 'Has an Army force type deployed',          False),

]

# How many channels and roles each category needs
CHANNELS_NEEDED = len(REQUIRED_CHANNELS)
ROLES_NEEDED    = len(REQUIRED_ROLES)

# Discord hard limits
DISCORD_CHANNEL_LIMIT = 500
DISCORD_ROLE_LIMIT    = 250

# Seconds to wait for CANCEL after pre-flight passes
CANCEL_WINDOW = 10


# =============================================================================
# SETUP MODE SELECTION VIEW
# =============================================================================

class SetupModeView(discord.ui.View):
    """
    Two-button prompt shown after pre-flight passes.

    Auto   — bot creates all channels and roles from scratch.
    Manual — bot asks the owner to point it at existing ones.
    """

    def __init__(self, owner_id: int):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.choice   = None          # 'auto' | 'manual' | None (timeout)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the server owner can choose the setup mode.",
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="🤖 Auto — create everything for me",
                       style=discord.ButtonStyle.primary)
    async def auto(self, interaction: discord.Interaction,
                   button: discord.ui.Button) -> None:
        self.choice = 'auto'
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="🔧 Manual — I already have channels and roles",
                       style=discord.ButtonStyle.secondary)
    async def manual(self, interaction: discord.Interaction,
                     button: discord.ui.Button) -> None:
        self.choice = 'manual'
        await interaction.response.defer()
        self.stop()


# =============================================================================
# SETUP COG
# =============================================================================

class SetupCog(commands.Cog, name="Setup"):
    """One-time server setup for E.R.I.S."""

    def __init__(self, bot):
        self.bot = bot
        self.db  = bot.db.conn

    # -------------------------------------------------------------------------
    # /setup
    # -------------------------------------------------------------------------

    @app_commands.command(name="setup", description="One-time ERIS server setup. Server owner only.")
    async def setup(self, interaction: discord.Interaction) -> None:
        """
        Run the full ERIS server setup sequence.

        Gated by: server ownership, no prior config, bot permissions.
        """
        await interaction.response.defer(ephemeral=True)

        # ------------------------------------------------------------------
        # Gate 1 — Server owner only
        # ------------------------------------------------------------------
        if interaction.user.id != interaction.guild.owner_id:
            await interaction.followup.send(
                "❌ `/setup` can only be run by the server owner.",
                ephemeral=True
            )
            return

        # ------------------------------------------------------------------
        # Gate 2 — No prior setup
        # ------------------------------------------------------------------
        existing = guild_repo.get_guild_config(self.db, interaction.guild.id)
        if existing:
            await interaction.followup.send(
                "❌ This server has already been configured.\n"
                "Use `/setup-status` to see the current configuration.\n"
                "Use `/setup-reset` (owner only) to clear and start over.",
                ephemeral=True
            )
            return

        # ------------------------------------------------------------------
        # Find the system channel for public announcements
        # ------------------------------------------------------------------
        system_channel = interaction.guild.system_channel or interaction.channel

        await system_channel.send(
            f"⚠️ **ERIS SETUP INITIATED** by {interaction.user.mention}\n"
            f"If you did not request this, kick the ERIS bot immediately."
        )

        # ------------------------------------------------------------------
        # PHASE 1 — PRE-FLIGHT
        # ------------------------------------------------------------------
        preflight_msg = await system_channel.send("⚙️ **ERIS PRE-FLIGHT CHECK**\n━━━━━━━━━━━━━━━━━━━━━━\nRunning checks...")

        checks      = []
        all_passed  = True

        # Owner check already done
        checks.append(("✅", "Invoker",           "server owner confirmed"))

        # Bot permissions
        bot_member = interaction.guild.get_member(self.bot.user.id)
        bot_perms = bot_member.guild_permissions

        manage_roles = bot_perms.manage_roles
        manage_channels = bot_perms.manage_channels
        manage_threads = bot_perms.manage_threads

        checks.append((
            "✅" if manage_roles else "❌",
            "Bot: Manage Roles",
            "present" if manage_roles else "MISSING — add this permission to the ERIS bot role"
        ))
        checks.append((
            "✅" if manage_channels else "❌",
            "Bot: Manage Channels",
            "present" if manage_channels else "MISSING — add this permission to the ERIS bot role"
        ))
        checks.append((
            "✅" if manage_threads else "❌",
            "Bot: Manage Threads",
            "present" if manage_threads else "MISSING — add this permission to the ERIS bot role"
        ))

        if not manage_roles or not manage_channels or not manage_threads:
            all_passed = False

        # Channel capacity
        current_channels  = len(interaction.guild.channels)
        channels_available = DISCORD_CHANNEL_LIMIT - current_channels
        channel_ok        = channels_available >= CHANNELS_NEEDED

        checks.append((
            "✅" if channel_ok else "❌",
            "Channel capacity",
            f"{CHANNELS_NEEDED} required, {channels_available} available "
            f"({DISCORD_CHANNEL_LIMIT} max)" if channel_ok else
            f"NEED {CHANNELS_NEEDED}, only {channels_available} slots left — "
            f"delete at least {CHANNELS_NEEDED - channels_available} channels"
        ))

        if not channel_ok:
            all_passed = False

        # Role capacity
        current_roles  = len(interaction.guild.roles)
        roles_available = DISCORD_ROLE_LIMIT - current_roles
        role_ok        = roles_available >= ROLES_NEEDED

        checks.append((
            "✅" if role_ok else "❌",
            "Role capacity",
            f"{ROLES_NEEDED} required, {roles_available} available "
            f"({DISCORD_ROLE_LIMIT} max)" if role_ok else
            f"NEED {ROLES_NEEDED}, only {roles_available} slots left — "
            f"delete at least {ROLES_NEEDED - roles_available} roles"
        ))

        if not role_ok:
            all_passed = False

        # Database reachable
        try:
            guild_repo.ping(self.db)
            checks.append(("✅", "Database", "reachable"))
        except Exception:
            checks.append(("❌", "Database", "UNREACHABLE — check your database config"))
            all_passed = False

        # Build pre-flight report
        check_lines = '\n'.join(
            f"  {icon} {name:<28} — {detail}"
            for icon, name, detail in checks
        )

        if not all_passed:
            await preflight_msg.edit(content=(
                f"⚙️ **ERIS PRE-FLIGHT CHECK**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{check_lines}\n\n"
                f"❌ **PRE-FLIGHT FAILED — setup has not started.**\n"
                f"Fix the issues above and run `/setup` again.\n"
                f"Nothing was created. No permissions were changed.\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            ))
            return

        # All checks passed — show results and open cancel window
        await preflight_msg.edit(content=(
            f"⚙️ **ERIS PRE-FLIGHT CHECK**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{check_lines}\n\n"
            f"✅ **All checks passed.**\n"
            f"Starting setup in {CANCEL_WINDOW} seconds...\n"
            f"Reply `CANCEL` to abort.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        ))

        # Cancel window
        def cancel_check(m):
            return (
                m.author.id == interaction.user.id and
                m.channel == system_channel and
                m.content.strip().upper() == 'CANCEL'
            )

        try:
            await self.bot.wait_for('message', check=cancel_check, timeout=CANCEL_WINDOW)
            await system_channel.send("❌ **Setup cancelled by server owner.**")
            return
        except asyncio.TimeoutError:
            pass  # No cancel — proceed

        # ------------------------------------------------------------------
        # MODE SELECTION — Auto or Manual
        # ------------------------------------------------------------------
        mode_view = SetupModeView(interaction.user.id)
        mode_msg  = await system_channel.send(
            "⚙️ **ERIS SETUP — CHOOSE MODE**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🤖 **Auto** — ERIS creates all channels, categories, and roles from scratch.\n"
            "🔧 **Manual** — You already have channels and roles. ERIS connects to them.\n\n"
            "📄 Manual mode requires: `SETUP_GUIDE.md` — read it before choosing manual.\n"
            "━━━━━━━━━━━━━━━━━━━━━━",
            view=mode_view
        )

        await mode_view.wait()

        if mode_view.choice is None:
            await mode_msg.edit(content="⏱️ Mode selection timed out. Run `/setup` again.", view=None)
            return

        await mode_msg.edit(
            content=f"{'🤖 Auto mode selected.' if mode_view.choice == 'auto' else '🔧 Manual mode selected.'}",
            view=None
        )

        # ------------------------------------------------------------------
        # PHASE 2 — CREATION (auto) or COLLECTION (manual)
        # ------------------------------------------------------------------
        created_roles    = {}   # name → role object
        created_channels = {}   # name → channel object
        setup_log        = []   # lines for requirements doc

        if mode_view.choice == 'auto':
            setup_msg = await system_channel.send(
                f"⚙️ **ERIS SETUP — PHASE 2: CREATION**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Initiated by: {interaction.user.mention}\n"
                f"Time: {_now()}\n"
            )

            categories    = {}
            phase2_failed = False
            fail_reason   = ""

            # --- Create roles ---
            await _update(setup_msg, setup_log, "\n**Creating roles...**")

            for role_name, color, purpose, hoist in REQUIRED_ROLES:
                try:
                    existing_role = discord.utils.get(interaction.guild.roles, name=role_name)
                    if existing_role:
                        created_roles[role_name] = existing_role
                        await _update(setup_msg, setup_log,
                                      f"  ⏭️ {role_name:<22} — already exists, reusing")
                        continue
                    role = await interaction.guild.create_role(
                        name=role_name,
                        color=discord.Color(color),
                        hoist=hoist,
                        reason="ERIS setup"
                    )
                    created_roles[role_name] = role
                    await _update(setup_msg, setup_log,
                                  f"  ✅ {role_name:<22} — {purpose}")
                except discord.HTTPException as e:
                    await _update(setup_msg, setup_log,
                                  f"  ❌ {role_name:<22} — FAILED: {e}")
                    phase2_failed = True
                    fail_reason   = f"Failed to create role '{role_name}': {e}"
                    break

            if phase2_failed:
                await _post_failure(system_channel, setup_log, fail_reason)
                return

            # --- Create categories and channels ---
            await _update(setup_msg, setup_log,
                          "\n**Creating categories and channels...**")

            eris_admin_role = created_roles.get('ERIS Admin')

            for channel_name, category_name, purpose, admin_only in REQUIRED_CHANNELS:
                try:
                    if category_name not in categories:
                        existing_cat = discord.utils.get(interaction.guild.categories, name=category_name)
                        if existing_cat:
                            categories[category_name] = existing_cat
                            await _update(setup_msg, setup_log,
                                          f"  ⏭️ Category: {category_name} — already exists, reusing")
                        else:
                            overwrites = {
                                interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                                bot_member: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                            }
                            if not admin_only and eris_admin_role:
                                overwrites[eris_admin_role] = discord.PermissionOverwrite(read_messages=True)

                            category = await interaction.guild.create_category(
                                name=category_name,
                                overwrites=overwrites,
                                reason="ERIS setup"
                            )
                            categories[category_name] = category
                            await _update(setup_msg, setup_log,
                                          f"  ✅ Category: {category_name}")

                    category = categories[category_name]
                    eris_creator_role = created_roles.get('ERIS Creator')

                    if channel_name == 'eris-notifications':
                        overwrites = {
                            interaction.guild.default_role: discord.PermissionOverwrite(
                                read_messages=True,
                                send_messages=False,
                                read_message_history=True,
                                add_reactions=True,
                            ),
                            bot_member: discord.PermissionOverwrite(
                                read_messages=True,
                                send_messages=True,
                                manage_messages=True,
                            ),
                        }
                    elif channel_name == 'holo-broadcast':

                        overwrites = {
                            interaction.guild.default_role: discord.PermissionOverwrite(
                                read_messages=True,
                                send_messages=False,
                                read_message_history=True,
                                add_reactions=True,
                            ),
                            bot_member: discord.PermissionOverwrite(
                                read_messages=True,
                                send_messages=True,
                                manage_messages=True,
                            ),
                        }
                        if eris_admin_role:
                            overwrites[eris_admin_role] = discord.PermissionOverwrite(
                                read_messages=True,
                                send_messages=True,
                            )
                        if eris_creator_role:
                            overwrites[eris_creator_role] = discord.PermissionOverwrite(
                                read_messages=True,
                                send_messages=True,
                            )
                    elif channel_name == 'campaign-progress':
                        overwrites = {
                            interaction.guild.default_role: discord.PermissionOverwrite(
                                read_messages=True,
                                send_messages=False,
                                send_messages_in_threads=True,
                                read_message_history=True,
                                add_reactions=True,
                            ),
                            bot_member: discord.PermissionOverwrite(
                                read_messages=True,
                                send_messages=True,
                                send_messages_in_threads=True,
                                create_public_threads=True,
                                manage_messages=True,
                            ),
                        }
                        if eris_admin_role:
                            overwrites[eris_admin_role] = discord.PermissionOverwrite(
                                read_messages=True,
                                send_messages=True,
                                send_messages_in_threads=True,
                                manage_messages=True,
                            )
                        if eris_creator_role:
                            overwrites[eris_creator_role] = discord.PermissionOverwrite(
                                read_messages=True,
                                send_messages=True,
                                send_messages_in_threads=True,
                            )

                    else:
                        overwrites = {
                            interaction.guild.default_role: discord.PermissionOverwrite(
                                read_messages=not admin_only,
                                send_messages=not admin_only,
                            ),
                            bot_member: discord.PermissionOverwrite(
                                read_messages=True,
                                send_messages=True,
                                manage_messages=True,
                            ),
                        }
                        if admin_only and eris_admin_role:
                            overwrites[eris_admin_role] = discord.PermissionOverwrite(
                                read_messages=True,
                                send_messages=True,
                            )

                    existing_ch = discord.utils.get(interaction.guild.text_channels, name=channel_name)
                    if existing_ch:
                        created_channels[channel_name] = existing_ch
                        await _update(setup_msg, setup_log,
                                      f"  ⏭️ #{channel_name:<30} — already exists, reusing")
                    else:
                        channel = await interaction.guild.create_text_channel(
                            name=channel_name,
                            category=category,
                            overwrites=overwrites,
                            topic=purpose,
                            reason="ERIS setup"
                        )
                        created_channels[channel_name] = channel
                        if channel_name == 'eris-notifications':
                            visibility = "Developer broadcast — read only"
                        elif channel_name == 'holo-broadcast':
                            visibility = "Creators & Admins can post — read only for members"
                        else:
                            visibility = "Admin only" if admin_only else "All members"

                        await _update(setup_msg, setup_log,
                                      f"  ✅ #{channel_name:<30} — {visibility}")

                except discord.HTTPException as e:
                    await _update(setup_msg, setup_log,
                                  f"  ❌ #{channel_name:<30} — FAILED: {e}")
                    phase2_failed = True
                    fail_reason   = f"Failed to create channel '{channel_name}': {e}"
                    break

            # --- Post welcome message to #eris-notifications ---
            notif_channel = created_channels.get('eris-notifications')
            if notif_channel:
                await notif_channel.send(
                    "📡 **E.R.I.S. Notifications**\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "This channel is used by the E.R.I.S. bot to broadcast "
                    "announcements, updates, and maintenance notices to all servers.\n\n"
                    "Messages here come directly from the bot developer.\n"
                    "No action is required — just stay tuned. ⚔️"
                )

            if phase2_failed:
                await _post_failure(system_channel, setup_log, fail_reason)
                return

        else:
            # ------------------------------------------------------------------
            # MANUAL MODE — collect existing channels and roles from the owner
            # ------------------------------------------------------------------
            setup_msg = await system_channel.send(
                f"⚙️ **ERIS SETUP — PHASE 2: MANUAL COLLECTION**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"I'll ask you to mention each required channel and role.\n"
                f"Mention them with # or @ — e.g. `#battle-record` or `@Commander`.\n"
                f"Type `CANCEL` at any prompt to abort.\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )

            def owner_reply(m: discord.Message) -> bool:
                return (
                    m.author.id == interaction.user.id and
                    m.channel   == system_channel
                )

            manual_failed = False
            fail_reason   = ""

            # --- Collect channels ---
            await _update(setup_msg, setup_log, "\n**Collecting channels...**")

            for channel_name, _, purpose, admin_only in REQUIRED_CHANNELS:
                visibility = "admin-only" if admin_only else "members can see"
                prompt = await system_channel.send(
                    f"Mention the channel for **#{channel_name}**\n"
                    f"Purpose: {purpose} ({visibility})"
                )

                try:
                    reply = await interaction.client.wait_for(
                        'message', check=owner_reply, timeout=120
                    )
                except asyncio.TimeoutError:
                    await system_channel.send("⏱️ Timed out waiting for a response. Setup cancelled.")
                    return

                if reply.content.strip().upper() == 'CANCEL':
                    await system_channel.send("❌ **Setup cancelled.**")
                    return

                # Parse channel mention or raw ID
                channel = (
                    reply.channel_mentions[0]
                    if reply.channel_mentions
                    else interaction.guild.get_channel(_parse_id(reply.content))
                )

                if not channel:
                    await system_channel.send(
                        f"❌ Couldn't find that channel. Setup cancelled.\n"
                        f"Tip: mention it with # or paste the channel ID."
                    )
                    return

                # Validate bot permissions in that channel
                bot_ch_perms = channel.permissions_for(bot_member)
                if not (bot_ch_perms.read_messages and bot_ch_perms.send_messages):
                    await system_channel.send(
                        f"❌ ERIS doesn't have **Read Messages** and **Send Messages** "
                        f"in {channel.mention}.\n"
                        f"Fix the permissions and run `/setup` again."
                    )
                    return

                try:
                    await prompt.delete()
                    await reply.delete()
                except discord.HTTPException:
                    pass

                created_channels[channel_name] = channel
                await _update(setup_msg, setup_log,
                              f"  ✅ #{channel_name:<30} → {channel.mention}")

            # --- Collect roles ---
            await _update(setup_msg, setup_log, "\n**Collecting roles...**")

            for role_name, _, purpose, _ in REQUIRED_ROLES:
                prompt = await system_channel.send(
                    f"Mention the role for **{role_name}**\n"
                    f"Purpose: {purpose}"
                )

                try:
                    reply = await interaction.client.wait_for(
                        'message', check=owner_reply, timeout=120
                    )
                except asyncio.TimeoutError:
                    await system_channel.send("⏱️ Timed out. Setup cancelled.")
                    return

                if reply.content.strip().upper() == 'CANCEL':
                    await system_channel.send("❌ **Setup cancelled.**")
                    return

                role = (
                    reply.role_mentions[0]
                    if reply.role_mentions
                    else interaction.guild.get_role(_parse_id(reply.content))
                )

                if not role:
                    await system_channel.send(
                        f"❌ Couldn't find that role. Setup cancelled.\n"
                        f"Tip: mention it with @ or paste the role ID."
                    )
                    return

                try:
                    await prompt.delete()
                    await reply.delete()
                except discord.HTTPException:
                    pass

                created_roles[role_name] = role
                await _update(setup_msg, setup_log,
                              f"  ✅ {role_name:<22} → {role.mention}")

            await _update(setup_msg, setup_log,
                          "\n✅ All channels and roles collected.")

        # --- Save guild config ---
        await _update(setup_msg, setup_log, "\n**Saving configuration...**")

        try:
            guild_repo.create_guild_config(
                self.db,
                guild_id=interaction.guild.id,
                channels={name: ch.id for name, ch in created_channels.items()},
                roles={name: r.id for name, r in created_roles.items()},
            )
            await _update(setup_msg, setup_log, "  ✅ Guild config written to database")
        except Exception as e:
            await _update(setup_msg, setup_log, f"  ❌ Database write FAILED: {e}")
            await _post_failure(system_channel, setup_log, f"Database error: {e}")
            return

        # ------------------------------------------------------------------
        # PHASE 3 — REVOCATION
        # ------------------------------------------------------------------
        await _update(setup_msg, setup_log, "\n**🔒 SETUP PERMISSIONS**")
        await _update(setup_msg, setup_log,
                      "  ⚠️ Remove Manage Roles and Manage Channels from the ERIS bot role manually.")
        revocation_failed = True

        # ------------------------------------------------------------------
        # COMPLETION MESSAGE
        # ------------------------------------------------------------------
        revocation_note = (
            "\n⚠️ **Automatic permission revocation failed.**\n"
            "Please manually remove **Manage Roles** and **Manage Channels** "
            "from the ERIS bot role in Server Settings → Roles.\n"
        ) if revocation_failed else ""

        await _update(
            setup_msg, setup_log,
            f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ **SETUP COMPLETE**\n{revocation_note}\n"
            f"⚠️ **MANUAL STEPS REQUIRED:**\n"
            f"1. Assign **ERIS Admin** role to your moderation team\n"
            f"1. Assign **ERIS Creator** role to your story master team\n"
            f"2.  — fleet builder URL\n"
            f"2.  — army builder URL\n"
            f"3. `/setup-status` — verify everything looks correct\n"
            f"4. Last but not least... **Enjoy!**\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )

        # ------------------------------------------------------------------
        # POST REQUIREMENTS DOCUMENT to #eris-setup
        # ------------------------------------------------------------------
        eris_setup_channel = created_channels.get('eris-setup')
        if eris_setup_channel:
            await _post_requirements_doc(
                eris_setup_channel,
                interaction.guild,
                interaction.user,
                created_roles,
                created_channels,
                revocation_failed,
            )

        await interaction.followup.send("✅ Setup complete.", ephemeral=True)

    # -------------------------------------------------------------------------
    # /setup-status
    # -------------------------------------------------------------------------

    @app_commands.command(name="setup-status", description="Show current ERIS configuration.")
    async def setup_status(self, interaction: discord.Interaction) -> None:
        """Show the current guild config — any member can run this."""
        guild_config = guild_repo.get_guild_config(self.db, interaction.guild.id)

        if not guild_config:
            await interaction.response.send_message(
                "⚠️ ERIS has not been configured on this server yet.\n"
                "The server owner should run `/setup` to get started.",
                ephemeral=True
            )
            return

        channels = guild_config.get('channels', {})
        roles    = guild_config.get('roles',    {})

        channel_lines = '\n'.join(
            f"  {'✅' if interaction.guild.get_channel(ch_id) else '❌ MISSING'} "
            f"#{name} ({ch_id})"
            for name, ch_id in channels.items()
        )
        role_lines = '\n'.join(
            f"  {'✅' if interaction.guild.get_role(r_id) else '❌ MISSING'} "
            f"{name} ({r_id})"
            for name, r_id in roles.items()
        )

        await interaction.response.send_message(
            f"📋 **ERIS Configuration Status**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Channels:**\n{channel_lines or '  None configured'}\n\n"
            f"**Roles:**\n{role_lines or '  None configured'}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            ephemeral=True
        )

    # -------------------------------------------------------------------------
    # /setup-reset
    # -------------------------------------------------------------------------

    @app_commands.command(name="setup-reset", description="Clear ERIS config. Owner only. Does NOT delete channels.")
    async def setup_reset(self, interaction: discord.Interaction) -> None:
        """
        Clears guild config regardless of state.
        Does NOT delete channels or roles in Discord — only clears the bot's record.
        """
        if interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("❌ Owner only.", ephemeral=True)
            return

        if not guild_repo.is_guild_configured(self.db, interaction.guild.id):
            await interaction.response.send_message(
                "⚠️ This server has no ERIS configuration. Run `/setup` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            guild_repo.clear_guild_config(self.db, interaction.guild.id)
            await interaction.followup.send(
                "✅ **ERIS configuration cleared.**\n\n"
                "Channels and roles were **not** deleted — they remain in the server.\n"
                "To run setup again:\n"
                "  1. Ensure the ERIS bot role has **Manage Roles**, **Manage Channels**, and **Manage Threads**\n"
                "  2. Run `/setup`",
                ephemeral=True,
            )
            log.info("Guild config reset by owner %s in guild %s",
                     interaction.user.id, interaction.guild.id)
        except Exception as e:
            await interaction.followup.send(
                f"❌ Force reset failed: {e}\n"
                f"You will need to delete `commanders.db` manually.",
                ephemeral=True,
            )
            log.error("Force reset failed for guild %s: %s", interaction.guild.id, e)

    # -------------------------------------------------------------------------
    # /db-status
    # -------------------------------------------------------------------------

    @app_commands.command(name="db-status", description="Inspect ERIS database state. Owner only.")
    async def db_status(self, interaction: discord.Interaction) -> None:
        """
        Diagnostic command — shows raw DB state without touching Discord objects.
        Useful for debugging setup failures without needing an external SQL tool.
        """
        if interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("❌ Owner only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            lines = ["📊 **ERIS DB STATUS**\n━━━━━━━━━━━━━━━━━━━━━━"]

            # guild_config row
            config = guild_repo.get_guild_config(self.db, interaction.guild.id)
            if config:
                channels = config.get('channels', {})
                roles    = config.get('roles',    {})
                lines.append(f"**guild_config:** ✅ row exists")
                lines.append(f"  Channels stored: {len(channels)}")
                lines.append(f"  Roles stored:    {len(roles)}")
            else:
                lines.append("**guild_config:** ❌ no row — server is unconfigured")

            # Row counts for every table
            tables = [
                'guild_config', 'commanders', 'commander_submissions',
                'campaigns', 'campaign_staff', 'campaign_factions',
                'campaign_board_messages', 'commander_campaigns',
                'battles', 'battle_maneuvers', 'ems_history', 'ems_requests',
            ]
            lines.append("\n**Row counts:**")
            for table in tables:
                try:
                    count = self.db.execute(
                        f'SELECT COUNT(*) FROM {table}'
                    ).fetchone()[0]
                    lines.append(f"  {table:<32} {count}")
                except Exception as e:
                    lines.append(f"  {table:<32} ERROR: {e}")

            # DB file integrity
            integrity = self.db.execute('PRAGMA integrity_check').fetchone()[0]
            lines.append(f"\n**Integrity check:** {'✅ ok' if integrity == 'ok' else f'❌ {integrity}'}")

            # WAL mode
            journal = self.db.execute('PRAGMA journal_mode').fetchone()[0]
            lines.append(f"**Journal mode:**   {journal}")

            lines.append("━━━━━━━━━━━━━━━━━━━━━━")
            await interaction.followup.send('\n'.join(lines), ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ DB status check failed: {e}", ephemeral=True)
            log.error("db-status failed for guild %s: %s", interaction.guild.id, e)


# =============================================================================
# HELPERS
# =============================================================================

async def _update(msg: discord.Message, log_lines: list, new_line: str) -> None:
    """
    Append a line to the setup log and edit the live message.

    The owner sees each step appear in real time as the bot works.
    """
    log_lines.append(new_line)
    content = '\n'.join(log_lines)

    # Discord message limit is 2000 chars — truncate from the top if needed
    if len(content) > 1900:
        lines    = content.splitlines()
        content  = '...(earlier steps omitted)\n' + '\n'.join(lines[-30:])

    try:
        await msg.edit(content=content)
    except discord.HTTPException:
        pass  # If edit fails, the log still builds — don't crash setup


async def _post_failure(
    channel:    discord.TextChannel,
    log_lines:  list,
    reason:     str,
) -> None:
    """Post a setup failure message. Permissions are NOT revoked on failure."""
    await channel.send(
        f"❌ **SETUP FAILED**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Reason: {reason}\n\n"
        f"**Permissions have NOT been revoked** — you can fix the issue and try again.\n\n"
        f"Steps to retry:\n"
        f"  1. Fix the issue described above\n"
        f"  2. Run `/setup-reset` to clear the partial config\n"
        f"Partial channels and roles created before the failure have been left in place.\n"
        f"You may delete them manually or leave them — `/setup` will not create duplicates.\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


async def _post_requirements_doc(
    channel:          discord.TextChannel,
    guild:            discord.Guild,
    owner:            discord.Member,
    created_roles:    dict,
    created_channels: dict,
    revocation_failed: bool,
) -> None:
    """
    Post the permanent requirements document to #eris-setup.

    This is the server's record of what ERIS built and what remains.
    """
    role_lines = '\n'.join(
        f"  {name:<22} ID: {role.id}"
        for name, role in created_roles.items()
    )
    channel_lines = '\n'.join(
        f"  #{name:<30} ID: {ch.id}"
        for name, ch in created_channels.items()
    )

    remaining_channels = DISCORD_CHANNEL_LIMIT - len(guild.channels)
    remaining_roles    = DISCORD_ROLE_LIMIT    - len(guild.roles)

    revocation_status = (
        "⚠️ MANUAL ACTION REQUIRED — automatic revocation failed.\n"
        "  Remove Manage Roles and Manage Channels from the ERIS bot role manually."
        if revocation_failed else
        "✅ Manage Roles    — removed\n"
        "  ✅ Manage Channels — removed\n"
    )

    doc = (
        f"📋 **ERIS REQUIREMENTS DOCUMENT**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Generated: {_now()}\n"
        f"Setup by:  {owner.mention}\n\n"
        f"**ROLES CREATED ({len(created_roles)})**\n"
        f"{role_lines}\n\n"
        f"**CHANNELS CREATED ({len(created_channels)})**\n"
        f"{channel_lines}\n\n"
        f"**DISCORD LIMITS REMAINING**\n"
        f"  Channels: {remaining_channels} / {DISCORD_CHANNEL_LIMIT} available\n"
        f"  Roles:    {remaining_roles} / {DISCORD_ROLE_LIMIT} available\n\n"
        f"**PERMISSIONS REVOKED**\n"
        f"  {revocation_status}\n\n"
        f"**MANUAL STEPS**\n"
        f"  1. Assign **ERIS Admin** role to your moderation team\n"
        f"  2. — fleet builder URL\n"
        f"  3. — army builder URL\n"
        f"  4. `/setup-status` — verify everything looks correct\n"
        f"**TO RUN SETUP AGAIN**\n"
        f"  1. Manually add Manage Roles + Manage Channels to ERIS bot role\n"
        f"  2. `/setup-reset` — clears config, does NOT delete channels\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*This document is the permanent record of your ERIS installation.*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"**☕ Support E.R.I.S. Development**\n"
        f"This bot is free & open source — built with too much hot cocoa and love.\n"
        f"If it brought some fun to your community, consider buying me a hot cocoa:\n"
        f"https://buymeacoffee.com/galenmars"
    )

    try:
        await channel.send(doc)
    except discord.HTTPException:
        log.exception("Failed to post requirements document to #eris-setup")


def _parse_id(text: str) -> int | None:
    """Extract a bare snowflake ID from a string, or return None."""
    text = text.strip().strip('<>#@&!')
    try:
        return int(text)
    except ValueError:
        return None


def _now() -> str:
    """Return current UTC time as a formatted string."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


# =============================================================================
# COG SETUP
# =============================================================================

async def setup(bot):
    await bot.add_cog(SetupCog(bot))
