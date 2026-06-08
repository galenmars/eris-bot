"""
cogs/battle_cog.py
==================
E.R.I.S. Bot — Battle Commands

PURPOSE
-------
Discord-facing surface for battle operations.
Owns slash command definitions and the /battle roll trigger.
Delegates ALL battle logic to sequences/battle_flow.py.

COMMANDS
--------
/battle engage @opponent  — enrolled member in bound channel
/battle roll              — active battle participant, triggers the sequence wait_for
/battle status            — any member, active battle in this channel
/battle history           — any member, last 5 battles in this channel
/battle cancel            — admin only, cancels active battle

DESIGN NOTE ON /battle roll
---------------------------
This command exists solely to give players a clean slash-command
interface. The sequence uses bot.wait_for('message') to listen for
rolls. /battle roll sends a message that satisfies that wait_for.
The cog validates that the player is in an active battle before
sending — the sequence does the actual math.

WHAT THIS COG DOES NOT OWN
---------------------------
Any battle logic       → sequences/battle_flow.py
Dice math              → core/math/dice.py
EMS loss tables        → core/math/ems_tables.py
Domain rules           → domain/battle.py
Database ops           → data/battle_repo.py
"""

import logging

import discord
from discord.ext import commands
from discord     import app_commands

from core.data              import battle_repo, campaign_repo, guild_repo
from core.domain.exceptions import DomainError
from core.sequences         import battle_flow
from core.shared            import embeds

log = logging.getLogger(__name__)

# =============================================================================
# BATTLE COG
# =============================================================================

class BattleCog(commands.Cog, name="Battle"):
    """Battle engagement commands."""

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
    # BATTLE GROUP
    # =========================================================================

    battle_group = app_commands.Group(
        name="battle",
        description="Battle commands.",
    )

    # -------------------------------------------------------------------------
    # /battle engage
    # -------------------------------------------------------------------------

    @battle_group.command(
        name="engage",
        description="Start a battle engagement in this channel.",
    )
    @app_commands.describe(opponent="The player you are challenging")
    async def battle_engage(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member,
    ):
        """
        Entry point for a new battle.
        Validates the channel is bound and both players are enrolled,
        then hands off to sequences/battle_flow.py which owns the full
        setup conversation, DMs, initiative, and round loop.
        """
        # Basic sanity checks before delegating to the sequence
        if opponent.bot:
            await interaction.response.send_message(
                "❌ You can't battle a bot.", ephemeral=True
            )
            return

        if opponent.id == interaction.user.id:
            await interaction.response.send_message(
                "❌ You can't battle yourself.", ephemeral=True
            )
            return

        # Check channel is bound to a campaign
        binding = campaign_repo.get_channel_binding(self.db, interaction.channel.id)
        if not binding:
            await interaction.response.send_message(
                "❌ This channel is not bound to a campaign.\n"
                "An admin must use `/campaign bind` first.",
                ephemeral=True,
            )
            return

        # Defer — the sequence will take over from here
        await interaction.response.defer()

        await battle_flow.engage(
            bot=self.bot,
            interaction=interaction,
            p1=interaction.user,
            p2=opponent,
            channel=interaction.channel,
            guild=interaction.guild,
            db=self.db,
            theme=self._theme(interaction.guild.id),
            binding=binding,
        )

    # -------------------------------------------------------------------------
    # /battle roll
    # -------------------------------------------------------------------------

    @battle_group.command(
        name="roll",
        description="Roll in your active battle.",
    )
    async def battle_roll(self, interaction: discord.Interaction):
        """
        Triggers the roll the sequence is waiting for.
        Validates the player is in an active battle in this channel,
        then sends a message that sequences/battle_flow.py's wait_for
        will pick up.

        The sequence does all dice math — this command just confirms
        the player is eligible to roll right now.
        """
        # Check for active battle in this channel involving this player
        battle = battle_repo.get_active_battle_in_channel(
            self.db, interaction.channel.id
        )

        if not battle:
            await interaction.response.send_message(
                "❌ There's no active battle in this channel.",
                ephemeral=True,
            )
            return

        is_participant = (
            battle.get('player_one_id') == interaction.user.id
            or battle.get('player_two_id') == interaction.user.id
        )

        if not is_participant:
            await interaction.response.send_message(
                "❌ You're not a participant in the active battle here.",
                ephemeral=True,
            )
            return

        # Acknowledge the slash command, then send the message the sequence
        # wait_for is listening for. The sequence receives the follow-up
        # message and processes the roll.
        await interaction.response.send_message(
            f"🎲 {interaction.user.mention} is rolling...",
            ephemeral=False,
        )

        # Send a channel message that the sequence's wait_for will pick up.
        # The sequence filters by author and channel — this message satisfies it.
        await interaction.channel.send(
            f"__ERIS_ROLL__{interaction.user.id}",
            delete_after=1,  # Immediately deleted — the sequence reads and discards it
        )

    # -------------------------------------------------------------------------
    # /battle status
    # -------------------------------------------------------------------------

    @battle_group.command(name="status", description="Show the current battle status in this channel.")
    async def battle_status(self, interaction: discord.Interaction):
        await interaction.response.defer()

        battle = battle_repo.get_active_battle_in_channel(self.db, interaction.channel.id)

        if not battle:
            await interaction.followup.send("No active battle in this channel.")
            return

        p1 = interaction.guild.get_member(battle['player_one_id'])
        p2 = interaction.guild.get_member(battle['player_two_id'])

        embed = discord.Embed(
            title=f"⚔️ Active Battle — {battle.get('battle_name', 'Unknown')}",
            color=discord.Color.red(),
        )
        embed.add_field(
            name=f"{battle['hero_name']} ({battle['p1_faction']})",
            value=(
                f"Wins: {battle['wins_one']}\n"
                f"Tactic: {battle.get('p1_tactic', '?')}\n"
                f"Fleet: {battle.get('p1_fleet_type', '?')}"
            ),
            inline=True,
        )
        embed.add_field(name="VS", value="⚔️", inline=True)
        embed.add_field(
            name=f"{battle['hero_name2']} ({battle['p2_faction']})",
            value=(
                f"Wins: {battle['wins_two']}\n"
                f"Tactic: {battle.get('p2_tactic', '?')}\n"
                f"Fleet: {battle.get('p2_fleet_type', '?')}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Progress",
            value=(
                f"Round {battle.get('current_round', 0)} of {battle['max_rounds']}\n"
                f"Size: {battle['battle_size'].capitalize()}\n"
                f"Type: {battle['battle_type'].capitalize()}"
            ),
            inline=False,
        )

        p1_mention = p1.mention if p1 else f"<@{battle['player_one_id']}>"
        p2_mention = p2.mention if p2 else f"<@{battle['player_two_id']}>"
        embed.set_footer(text=f"{p1_mention} vs {p2_mention}")

        await interaction.followup.send(embed=embed)

    # -------------------------------------------------------------------------
    # /battle history
    # -------------------------------------------------------------------------

    @battle_group.command(name="history", description="Show the last 5 battles in this channel.")
    async def battle_history(self, interaction: discord.Interaction):
        await interaction.response.defer()

        battles = battle_repo.get_recent_battles_in_channel(
            self.db, interaction.channel.id, limit=5
        )

        if not battles:
            await interaction.followup.send("No completed battles in this channel yet.")
            return

        embed = discord.Embed(
            title="📜 Recent Battles",
            color=discord.Color.blue(),
        )

        for b in battles:
            if b['wins_one'] > b['wins_two']:
                result = f"**{b['hero_name']} Victory** ({b['wins_one']}–{b['wins_two']})"
            elif b['wins_two'] > b['wins_one']:
                result = f"**{b['hero_name2']} Victory** ({b['wins_two']}–{b['wins_one']})"
            else:
                result = f"**Tie** ({b['wins_one']}–{b['wins_two']})"

            embed.add_field(
                name=b.get('battle_name', 'Battle'),
                value=(
                    f"{b['hero_name']} vs {b['hero_name2']}\n"
                    f"{b['battle_size'].capitalize()} — {b['battle_type'].capitalize()}\n"
                    f"{result}"
                ),
                inline=False,
            )

        await interaction.followup.send(embed=embed)

    # -------------------------------------------------------------------------
    # /battle cancel  (admin only)
    # -------------------------------------------------------------------------

    @battle_group.command(name="cancel", description="[Admin] Cancel the active battle in this channel.")
    async def battle_cancel(self, interaction: discord.Interaction):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only ERIS Admins can cancel battles.", ephemeral=True
            )
            return

        battle = battle_repo.get_active_battle_in_channel(self.db, interaction.channel.id)
        if not battle:
            await interaction.response.send_message(
                "❌ No active battle in this channel.", ephemeral=True
            )
            return

        try:
            battle_repo.cancel_battle(self.db, battle['battle_id'])
        except Exception as e:
            log.exception("battle_cancel DB error")
            await interaction.response.send_message(
                f"❌ Database error: {e}", ephemeral=True
            )
            return

        # Post cancellation to the battle record channel
        await interaction.channel.send(
            f"❌ **Battle cancelled by admin** — "
            f"{battle.get('battle_name', 'Unknown')} "
            f"({battle['hero_name']} vs {battle['hero_name2']})"
        )

        await interaction.response.send_message(
            f"✅ Battle **{battle.get('battle_name', '')}** cancelled.\n"
            f"{battle['hero_name']} vs {battle['hero_name2']} — result voided."
        )

        # Notify participants via DM
        for player_id in (battle['player_one_id'], battle['player_two_id']):
            member = interaction.guild.get_member(player_id)
            if member:
                try:
                    await member.send(
                        f"⚠️ Your battle in {interaction.channel.mention} was cancelled by an admin.\n"
                        f"Contact them if you have questions."
                    )
                except discord.Forbidden:
                    pass


# =============================================================================
# SETUP
# =============================================================================

async def setup(bot):
    cog = BattleCog(bot)
    await bot.add_cog(cog, override=True)
