"""
core/shared/progress_renderer.py
=================================
E.R.I.S. Bot — Campaign Progress Embed Builders

PURPOSE
-------
Builds all post-battle and end-of-campaign Discord embeds posted
to the campaign progress thread. All visual formatting and E.R.I.S.
commentary lives here — change this file to update the look without
touching any logic.

WHAT LIVES HERE
---------------
build_progress_embed()       — post-battle strategic update embed with EMS bars
                               and situational E.R.I.S. commentary
build_critical_nudge_embed() — one-shot critical alert when a side drops below 20% EMS
build_leaderboard_embed()    — end-of-campaign final tally with MVP and per-side stats

INTERNAL HELPERS
----------------
_ems_bar()                   — renders a unicode block progress bar
_classify_situation()        — determines which taunt pool to draw from
get_campaign_duration_days() — calculates campaign length in days

WHAT DOES NOT LIVE HERE
------------------------
- Deciding when to post these embeds  → sequences/battle_flow.py
- Campaign data reads                 → data/campaign_repo.py
- Campaign type progress display      → shared/campaign_display.py

Called from battle_flow.py after each battle completes,
and from campaign_cog.py on /campaign complete and /campaign retreat.
"""

import random
import discord

# =============================================================================
# TAUNT POOLS
# =============================================================================

_TAUNTS = {
    'first_battle': [
        "The opening move has been made. Let the record show who struck first.",
        "Battle lines are drawn. E.R.I.S. is watching.",
        "The campaign begins. One wonders how it will end.",
        "First blood. The rest is history in the making.",
        "So it starts. E.R.I.S. recommends not getting comfortable.",
    ],
    'side_a_dominating': [
        "Side A is pulling ahead. Side B may wish to reconsider their strategy.",
        "The momentum belongs to Side A. For now.",
        "Side B is losing ground. Quietly, but noticeably.",
        "Side A is making this look easy. Side B is making this look painful.",
        "E.R.I.S. notes: Side A appears to have done their homework.",
        "SIDE A IS SURGING. Side B — now would be a good time to do something about that.",
    ],
    'side_b_dominating': [
        "Side B has taken control of this campaign. Side A, the floor is yours.",
        "Side A is falling behind. One wonders if they noticed.",
        "Side B is ahead. Significantly. E.R.I.S. does not editorialize. Much.",
        "The tide favors Side B. Side A commanders are encouraged to check their EMS.",
        "Side A is losing this. Slowly, then all at once.",
        "SIDE B IS ON A RUN. Someone on Side A — please do something.",
    ],
    'close_fight': [
        "Too close to call. Every battle matters now.",
        "The gap is within margin of error. E.R.I.S. finds this... interesting.",
        "Neither side has broken. One slip and this changes everything.",
        "An even fight. Rare. Precious. Possibly short-lived.",
        "This campaign could go either way. E.R.I.S. recommends commanders stay sharp.",
        "NECK AND NECK. This is what a campaign is supposed to look like.",
    ],
    'side_a_critical': [
        "Side A is critically weakened. This may already be decided.",
        "Side A is running out of EMS. And time.",
        "E.R.I.S. is not pessimistic. E.R.I.S. is accurate. Side A is in trouble.",
        "Side A commanders — now would be the moment for a miracle.",
        "Side A is on the edge. Every point of EMS matters now.",
        "SIDE A IS BARELY HOLDING. Someone. Anyone. Now.",
    ],
    'side_b_critical': [
        "Side B is critically weakened. The math is not in their favor.",
        "Side B is running out of EMS. And options.",
        "E.R.I.S. does not sugarcoat: Side B is close to the end.",
        "Side B commanders — the situation calls for something extraordinary.",
        "Side B is on the edge. This campaign is almost over.",
        "SIDE B IS BARELY HOLDING. This is not a drill.",
    ],
    'late_campaign': [
        "Both sides have taken heavy losses. Who blinks first?",
        "The campaign is in its final phase. Every commander remaining matters.",
        "Late stage. High stakes. E.R.I.S. is paying close attention.",
        "The battlefield is thinning. The decisive moment approaches.",
        "Both sides are bleeding. The question is who runs out first.",
        "THIS IS THE ENDGAME. Every battle from here is the battle.",
    ],
    'general': [
        "The campaign continues. E.R.I.S. continues to watch.",
        "Another battle recorded. The war is not over.",
        "Noted. Filed. Analyzed. The campaign goes on.",
        "E.R.I.S. has updated its projections. Commanders should update their plans.",
        "The numbers shift. The campaign evolves.",
    ],
}

# =============================================================================
# INTERNAL HELPERS
# =============================================================================

def _ems_bar(current: int, cap: int, length: int = 20) -> str:
    """Unicode block progress bar representing EMS remaining."""
    if cap <= 0:
        ratio = 0.0
    else:
        ratio = max(0.0, min(1.0, current / cap))
    filled = round(ratio * length)
    return f"{'█' * filled}{'░' * (length - filled)}"


def _classify_situation(
    a_current: int,
    a_start:   int,
    b_current: int,
    b_start:   int,
    battle_number: int,
) -> str:
    """Determine which taunt pool to draw from based on campaign state."""
    if battle_number == 1:
        return 'first_battle'

    a_pct = a_current / a_start if a_start > 0 else 0.0
    b_pct = b_current / b_start if b_start > 0 else 0.0

    # Critical thresholds — check these first, most urgent
    if a_pct <= 0.30:
        return 'side_a_critical'
    if b_pct <= 0.30:
        return 'side_b_critical'

    # Late campaign — both sides have taken heavy overall damage
    total_lost  = (a_start - a_current) + (b_start - b_current)
    total_start = a_start + b_start
    if total_start > 0 and (total_lost / total_start) >= 0.70:
        return 'late_campaign'

    # Dominance — 15%+ gap in remaining percentage
    gap = abs(a_pct - b_pct)
    if gap >= 0.15:
        return 'side_a_dominating' if a_pct > b_pct else 'side_b_dominating'

    # Close fight — within 10%
    if gap <= 0.10:
        return 'close_fight'

    return 'general'

def get_campaign_duration_days(campaign: dict) -> int:
    """Return campaign duration in whole days from created_at to campaign_end_date."""
    from datetime import datetime
    fmt = '%Y-%m-%d %H:%M:%S'
    try:
        start = datetime.strptime(campaign['created_at'][:19],      fmt)
        end   = datetime.strptime(campaign['campaign_end_date'][:19], fmt)
        return max(1, (end - start).days)
    except Exception:
        return 0

# =============================================================================
# PUBLIC INTERFACE
# =============================================================================

def build_progress_embed(
    campaign_name: str,
    battle_number: int,
    side_a_label:  str,
    side_b_label:  str,
    a_current:     int,
    a_start:       int,
    b_current:     int,
    b_start:       int,
    cap:           int,
    battle_winner: str | None = None,
    battle_loser:  str | None = None,
    a_lost_this:   int = 0,
    b_lost_this:   int = 0,
) -> discord.Embed:
    """
    Build the post-battle E.R.I.S. Strategic Update embed.
    Returns a discord.Embed ready to post in the campaign thread.
    """
    situation     = _classify_situation(a_current, a_start, b_current, b_start, battle_number)
    taunt         = random.choice(_TAUNTS.get(situation, _TAUNTS['general']))
    effective_cap = cap if cap > 0 else max(a_start, b_start, 1)

    a_pct = round(a_current / effective_cap * 100)
    b_pct = round(b_current / effective_cap * 100)

    # Battle result line
    if battle_winner:
        result_line = f"⚔️ **{battle_winner}** defeated **{battle_loser}**"
    else:
        result_line = "⚔️ **Stalemate** — both commanders stood their ground"

    # EMS change lines
    ems_delta = []
    if a_lost_this:
        ems_delta.append(f"🔵 Side A lost `{a_lost_this}` EMS this battle")
    if b_lost_this:
        ems_delta.append(f"🔴 Side B lost `{b_lost_this}` EMS this battle")

    description = (
        f"{result_line}\n"
        + (f"\n" + "\n".join(ems_delta) + "\n" if ems_delta else "\n")
        + f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"**🔵 Side A — {side_a_label}**\n"
        f"`{_ems_bar(a_current, effective_cap)}` {a_pct}%\n"
        f"`{a_current}` / `{a_start}` EMS remaining\n\n"
        f"**🔴 Side B — {side_b_label}**\n"
        f"`{_ems_bar(b_current, effective_cap)}` {b_pct}%\n"
        f"`{b_current}` / `{b_start}` EMS remaining\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*{taunt}*"
    )

    # Embed color tracks who's currently ahead
    if a_current > b_current:
        color = discord.Color.blue()
    elif b_current > a_current:
        color = discord.Color.red()
    else:
        color = discord.Color.greyple()

    embed = discord.Embed(
        title=f"📡 E.R.I.S. Strategic Update — {campaign_name} · Battle {battle_number}",
        description=description,
        color=color,
    )
    embed.set_footer(text="E.R.I.S. Campaign Intelligence System")
    return embed

def build_critical_nudge_embed(
    campaign_name: str,
    side_label:    str,
    side:          str,   # 'a' or 'b'
    ems_current:   int,
    ems_start:     int,
    cap:           int,
) -> discord.Embed:
    """
    One-shot critical alert posted when a side crosses below 20% EMS.
    Separate from the regular battle update — meant to be alarming.
    """
    _NUDGE_LINES = [
        "E.R.I.S. is not in the habit of repeating itself. This is a warning.",
        "The math is becoming difficult to ignore.",
        "At this rate, the outcome will not require a vote.",
        "E.R.I.S. recommends the surviving commanders have a conversation.",
        "This is not a drill. This is arithmetic.",
        "Statistically speaking, this is the part where things get decided.",
    ]

    effective_cap = cap if cap > 0 else max(ems_start, 1)
    pct      = round(ems_current / effective_cap * 100)
    emoji    = '🔵' if side == 'a' else '🔴'
    color    = discord.Color.blue() if side == 'a' else discord.Color.red()
    bar      = _ems_bar(ems_current, effective_cap)

    description = (
        f"{emoji} **{side_label}** has fallen below **20% EMS**.\n\n"
        f"`{bar}` {pct}%\n"
        f"`{ems_current}` / `{ems_start}` EMS remaining\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*{random.choice(_NUDGE_LINES)}*"
    )

    return discord.Embed(
        title=f"⚠️ CRITICAL EMS WARNING — {campaign_name}",
        description=description,
        color=color,
    )

# =============================================================================
# LEADERBOARD
# =============================================================================

def build_leaderboard_embed(
    campaign:      dict,
    side_a_label:  str,
    side_b_label:  str,
    leaderboard:   dict,
    total_battles: int,
    winner_side:   str | None = None,  # 'a', 'b', or None → infer from EMS
) -> discord.Embed:
    """
    Build the end-of-campaign final tally embed.
    Posted to the campaign progress thread on /campaign complete or /campaign retreat.
    winner_side forces the outcome line; None falls back to EMS comparison.
    """
    a_current = campaign.get('side_a_ems_current', 0)
    b_current = campaign.get('side_b_ems_current', 0)

    if winner_side == 'a':
        outcome = f"🔵 **Side A victorious.** {side_a_label} holds the field."
        color   = discord.Color.blue()
    elif winner_side == 'b':
        outcome = f"🔴 **Side B victorious.** {side_b_label} holds the field."
        color   = discord.Color.red()
    elif a_current > b_current:
        outcome = f"🔵 **Side A victorious.** {side_a_label} holds the field."
        color   = discord.Color.blue()
    elif b_current > a_current:
        outcome = f"🔴 **Side B victorious.** {side_b_label} holds the field."
        color   = discord.Color.red()
    else:
        outcome = "⚖️ **Contested.** Both sides end at equal strength."
        color   = discord.Color.greyple()

    duration = get_campaign_duration_days(campaign)
    duration_str = f"{duration} day{'s' if duration != 1 else ''}"

    def _side_block(commanders: list, label: str, emoji: str) -> str:
        if not commanders:
            return f"{emoji} **{label}**\n*No battle data recorded.*\n"

        # Top Damage — most round wins
        top_damage = max(commanders, key=lambda c: c['ems_dealt'])

        # Most Active — most battles
        most_active = max(commanders, key=lambda c: c['battles'])

        # Efficiency — most matchup advantages (min 2 battles)
        eligible = [c for c in commanders if c['battles'] >= 2]
        if eligible:
            best_eff  = max(eligible, key=lambda c: c['matchup_wins'] / c['battles'])
            eff_pct   = round(best_eff['matchup_wins'] / best_eff['battles'] * 100)
            eff_line  = f"  **Efficiency**   {_name(best_eff)} — {eff_pct}% matchup advantage"
        else:
            best_eff  = max(commanders, key=lambda c: c['matchup_wins'])
            eff_line  = f"  **Efficiency**   {_name(best_eff)} — {best_eff['matchup_wins']} advantaged battles"

        # Valor — most tenacity activations
        top_valor = max(commanders, key=lambda c: c['tenacity_wins'])
        valor_line = (
            f"  **Valor**        {_name(top_valor)} — {top_valor['tenacity_wins']} tenacity push{'es' if top_valor['tenacity_wins'] != 1 else ''}"
            if top_valor['tenacity_wins'] > 0
            else f"  **Valor**        — no tenacity recorded"
        )

        return (
            f"{emoji} **{label}**\n"
            f"  **Top Damage**   {_name(top_damage)} — {top_damage['ems_dealt']} EMS dealt\n"
            f"  **Most Active**  {_name(most_active)} — {most_active['battles']} battle{'s' if most_active['battles'] != 1 else ''}\n"
            f"{eff_line}\n"
            f"{valor_line}\n"
        )

    def _name(c: dict) -> str:
        return f"{c['name']} (NPC)" if c.get('is_npc') else c['name']

    # MVP — highest combined ems_dealt + round_wins across both sides
    all_commanders = leaderboard['side_a'] + leaderboard['side_b']
    if all_commanders:
        mvp = max(all_commanders, key=lambda c: c['ems_dealt'] + c['round_wins'] * 10)
        mvp_line = f"\n⭐ **Campaign MVP: {_name(mvp)}**"
    else:
        mvp_line = ""

    result_note = campaign.get('result_note')
    note_line   = f"\n*\"{result_note}\"*" if result_note else ""

    description = (
        f"**Outcome:** {outcome}\n"
        f"**Battles:** {total_battles} · "
        f"**Duration:** {duration_str}"
        f"{note_line}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _side_block(leaderboard['side_a'], side_a_label, '🔵')
        + f"\n"
        + _side_block(leaderboard['side_b'], side_b_label, '🔴')
        + f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        + mvp_line
    )

    embed = discord.Embed(
        title=f"🏆 {campaign['campaign_name'].upper()} — FINAL TALLY",
        description=description,
        color=color,
    )
    embed.set_footer(text="E.R.I.S. Campaign Intelligence System")
    return embed