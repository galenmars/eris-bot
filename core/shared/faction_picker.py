"""
core/shared/faction_picker.py
=============================
E.R.I.S. Bot — Shared Faction / Organization Picker UI

PURPOSE
-------
Provides a reusable two-level Discord Select menu for faction/organization
selection. Used by both the admin slot-creation flow
(sequences/commander_flow.py) and the player self-submission flow
(core/cogs/commander_cog.py).

USAGE
-----
    from core.shared.faction_picker import prompt_faction

    # With organizations registered (preferred path)
    result = await prompt_faction(channel=dm, user=user, theme=theme, orgs=orgs)

    # Without organizations (falls back to raw theme factions)
    result = await prompt_faction(channel=dm, user=user, theme=theme)

    if result is None:
        return  # timed out — caller already notified the user

    # result is always an OrgResult namedtuple:
    #   result.name          — "The Red Veil"   (org name, or faction name if no orgs)
    #   result.faction_name  — "Sith Empire"
    #   result.faction_id    — "sith_empire"
    #   result.group         — "empire"

HOW IT WORKS
------------
If orgs are provided:
  Level 1: alignment groups that have at least one org registered.
  Level 2: orgs in that group (not raw factions).

If no orgs are provided:
  Level 1: alignment groups from the theme (original behaviour).
  Level 2: raw theme factions in that group.

On selection the message is edited to show "✅ **Name** · Faction [Alignment]"
and the View stops. On timeout, None is returned and an error message is sent.
"""

import discord
from typing import NamedTuple


class OrgResult(NamedTuple):
    """Returned by prompt_faction regardless of whether orgs or raw factions were used."""
    name:         str   # org name (or faction name when no orgs registered)
    faction_name: str   # canonical theme faction name
    faction_id:   str   # theme faction id key
    group:        str   # alignment group key


# Display labels for each alignment group key.
# Order here controls the order in the Level-1 Select.
_GROUP_LABELS = {
    "republic":    "Republic-Aligned",
    "empire":      "Empire-Aligned",
    "sovereign":   "Sovereign Powers",
    "criminal":    "Criminal / Syndicate",
    "force":       "Force Orders",
    "planetary":   "Planetary / Tribal",
    "independent": "Independent / Neutral",
}


def _group_factions(theme: dict) -> dict[str, list[dict]]:
    """Return factions keyed by group, in JSON order within each group."""
    groups: dict[str, list[dict]] = {}
    for f in theme.get("factions", []):
        groups.setdefault(f.get("group", "independent"), []).append(f)
    return groups


def _merged_groups(theme: dict, orgs: list[dict] | None) -> dict[str, list[dict]]:
    """
    Return all items for the level-2 picker, keyed by group.

    Always includes every theme faction. If orgs are provided, custom orgs
    are appended to their group after the theme factions, marked with
    is_org=True so the display can show the parent faction as a description.

    Each item is a dict with at minimum: name, id/faction_id, group, is_org.
    """
    groups: dict[str, list[dict]] = {}

    # Theme factions first
    for f in theme.get("factions", []):
        entry = dict(f)
        entry['is_org']       = False
        entry['faction_name'] = f['name']
        entry['faction_id']   = f.get('id', '')
        groups.setdefault(f.get("group", "independent"), []).append(entry)

    # Custom orgs appended to their group
    for o in (orgs or []):
        entry = dict(o)
        entry['is_org'] = True
        groups.setdefault(o.get("group", "independent"), []).append(entry)

    return groups


def _faction_by_id(theme: dict, faction_id: str) -> dict | None:
    """Look up a theme faction by its id key."""
    for f in theme.get("factions", []):
        if f.get("id") == faction_id:
            return f
    return None


# =============================================================================
# SELECT: Level 1 — alignment groups
# =============================================================================

class _GroupSelect(discord.ui.Select):
    def __init__(self, theme: dict, orgs: list[dict] | None = None):
        emojis  = theme.get("alignment_groups", {})
        grouped = _merged_groups(theme, orgs)

        options = [
            discord.SelectOption(
                label=_GROUP_LABELS.get(k, k.title()),
                value=k,
                emoji=emojis.get(k, "▫️"),
                description=f"{len(grouped[k])} options",
            )
            for k in _GROUP_LABELS
            if grouped.get(k)
        ]
        super().__init__(placeholder="View factions…", options=options)

    async def callback(self, interaction: discord.Interaction):
        view: _FactionView = self.view
        if interaction.user.id != view.user_id:
            await interaction.response.send_message(
                "This picker isn't for you.", ephemeral=True
            )
            return
        await view.show_group(interaction, self.values[0])


# =============================================================================
# SELECT: Level 2 — orgs or factions within a group
# =============================================================================

class _ItemSelect(discord.ui.Select):
    """Level-2 select. Items are merged dicts from _merged_groups — each has an is_org flag."""
    def __init__(self, emoji: str, items: list[dict]):
        self._items = items
        options = [
            discord.SelectOption(
                label=item["name"],
                value=str(i),
                emoji=emoji,
                description=f"· {item['faction_name']}" if item.get('is_org') else None,
            )
            for i, item in enumerate(items)
        ]
        super().__init__(placeholder="Choose…", options=options)

    async def callback(self, interaction: discord.Interaction):
        view: _FactionView = self.view
        if interaction.user.id != view.user_id:
            await interaction.response.send_message(
                "This picker isn't for you.", ephemeral=True
            )
            return
        item = self._items[int(self.values[0])]
        await view.resolve(interaction, item)


# =============================================================================
# BUTTON: back to group list
# =============================================================================

class _BackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="← Factions", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view: _FactionView = self.view
        if interaction.user.id != view.user_id:
            await interaction.response.send_message(
                "This picker isn't for you.", ephemeral=True
            )
            return
        await view.show_root(interaction)


# =============================================================================
# VIEW: orchestrates level 1 → level 2 → resolution
# =============================================================================

class _FactionView(discord.ui.View):
    def __init__(
        self,
        user_id: int,
        theme:   dict,
        orgs:    list[dict] | None,
        timeout: int = 180,
    ):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.theme   = theme
        self.orgs    = orgs          # None = fall back to raw theme factions
        self.choice: OrgResult | None = None
        self._show_root_items()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _clear(self):
        for child in list(self.children):
            self.remove_item(child)

    def _show_root_items(self):
        self._clear()
        self.add_item(_GroupSelect(self.theme, self.orgs))

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    async def show_root(self, interaction: discord.Interaction):
        self._show_root_items()
        await interaction.response.edit_message(
            content="**Organization / Faction?** Choose a group:", view=self
        )

    async def show_group(self, interaction: discord.Interaction, group_key: str):
        self._clear()
        emoji = self.theme.get("alignment_groups", {}).get(group_key, "▫️")
        label = _GROUP_LABELS.get(group_key, group_key.title())
        items = _merged_groups(self.theme, self.orgs).get(group_key, [])

        self.add_item(_ItemSelect(emoji, items))
        self.add_item(_BackButton())
        await interaction.response.edit_message(
            content=f"**{label}** — pick a faction or organization:", view=self
        )

    async def resolve(self, interaction: discord.Interaction, item: dict):
        is_org = item.get('is_org', False)
        if is_org:
            result = OrgResult(
                name=item['name'],
                faction_name=item['faction_name'],
                faction_id=item['faction_id'],
                group=item['group'],
            )
            display = (
                f"✅ **{item['name']}** · {item['faction_name']} "
                f"[{_GROUP_LABELS.get(item['group'], item['group'])}]"
            )
        else:
            result = OrgResult(
                name=item['name'],
                faction_name=item['name'],
                faction_id=item.get('id', ''),
                group=item.get('group', 'independent'),
            )
            alignment = _GROUP_LABELS.get(item.get('group', ''), '')
            display   = f"✅ **{item['name']}** [{alignment}]" if alignment else f"✅ **{item['name']}**"

        self.choice = result
        self._clear()
        await interaction.response.edit_message(content=display, view=self)
        self.stop()


# =============================================================================
# PUBLIC API
# =============================================================================

async def prompt_faction(
    channel,
    user:    discord.abc.User,
    theme:   dict,
    orgs:    list[dict] | None = None,
    timeout: int = 180,
) -> OrgResult | None:
    """
    Send a two-level picker to `channel`, locked to `user`.

    If `orgs` is a non-empty list (guild's registered organizations), the
    picker offers those. Otherwise falls back to raw theme factions.

    Returns an OrgResult namedtuple on success, or None on timeout.
    On timeout an error message is sent to `channel` automatically.

    OrgResult fields:
        .name         — selected display name (org name or faction name)
        .faction_name — canonical theme faction name
        .faction_id   — theme faction id key
        .group        — alignment group key (e.g. "empire")
    """
    using_orgs = bool(orgs)

    if not using_orgs and not theme.get("factions"):
        await channel.send(
            "❌ No factions or organizations have been configured for this "
            "server yet. Ask an admin to add organizations via "
            "`/admin org add` or configure the theme."
        )
        return None

    view = _FactionView(user.id, theme, orgs or None, timeout=timeout)
    await channel.send("**Faction / Organization?** Choose a group:", view=view)
    await view.wait()

    if view.choice is None:
        await channel.send(
            "❌ No selection made (timed out). Please try the command again."
        )
        return None

    return view.choice


def get_faction_alignment(faction_name: str, theme: dict) -> str:
    """
    Return the display label for the alignment group a faction belongs to.

    Used as a fallback display helper when only the faction name is stored.
    Returns an empty string if the faction is not found in the theme.
    """
    for f in theme.get("factions", []):
        if f.get("name", "").lower() == faction_name.lower():
            group = f.get("group", "")
            return _GROUP_LABELS.get(group, "")
    return ""
