# E.R.I.S. Theming Guide

Welcome, Campaign Creator. E.R.I.S. is not hardcoded to a single universe. By modifying the `themes/` JSON files, you can reskin the bot for Star Wars, Warhammer 40k, Star Trek, or your own original setting.

---

## 1. Overview

A theme file is a single JSON object divided into these key sections:

| Section | Purpose |
|---|---|
| `tiers` | Economy and power caps |
| `battle_sizes` | Risk/reward scaling |
| `tactics` | Roll modifiers |
| `space_ships` / `ground_forces` | The unit library |
| `space_force_types` / `ground_force_types` | Type definitions and colors |
| `space_matchups` / `ground_matchups` | The Rock-Paper-Scissors engine |
| `factions` | Roleplay groupings and emoji mapping |
| `alignment_groups` | Emoji per faction group |
| `force_user_rules` | Special unit tier-gating rules |

When you modify these values, you are directly changing how the battle engine calculates outcomes and how the economy functions.

---

## 2. Tiers & Economy

The `tiers` object defines the "power level" of a commander. There are three tiers, each with its own EMS budget cap.

```json
"tiers": {
  "1": {
    "label": "Tier I",
    "description": "Entry level commanders. Limited EMS pool.",
    "max_ship_ems": 50,
    "max_ground_ems": 30
  },
  "2": {
    "label": "Tier II",
    "description": "Experienced officers. Medium EMS pool.",
    "max_ship_ems": 150,
    "max_ground_ems": 50
  },
  "3": {
    "label": "Tier III",
    "description": "Command level. Full EMS pool. All units unlocked.",
    "max_ship_ems": 750,
    "max_ground_ems": 180
  }
}
```

**Balancing tips:**
- Keep Tier 1 tight to force meaningful choices — players should never be able to afford everything.
- Scale Tier 3 to accommodate "Boss" units (e.g., the Super Dreadnought at 750 EMS exactly fills the Tier 3 cap).
- The jump between tiers doesn't have to be linear. The default theme uses a 50 → 150 → 750 curve to make Tier 3 feel dramatically more powerful.

---

## 3. Battle Sizes

Battle sizes control narrative scale and maximum round count. The `ems_table_index` value maps to an internal EMS loss table that determines how much EMS is spent per round at that engagement scale.

```json
{
  "id": "skirmish",
  "name": "Skirmish",
  "flavor": "A mid-scale battle with real strategic stakes.",
  "ems_table_index": 9,
  "max_rounds": 9
}
```

The default theme ships five sizes: **Brawl → Firefight → Skirmish → Engagement → Battleground**. You can rename these to fit your setting (e.g., "Patrol → Raid → Assault → Siege → Crusade" for Warhammer 40k).

---

## 4. Tactics

Tactics are a flat bonus applied to a commander's roll before matchups are resolved. They are the simplest tuning lever available.

```json
{ "id": "aggressive", "name": "Aggressive", "bonus": 2, "description": "Push hard. Meaningful advantage with manageable risk." }
```

The default range is **0–3**. On a 1d20 roll, even a +1 is statistically significant. Avoid going beyond +5 unless you intentionally want tactics to dominate over unit composition.

---

## 5. The Matchup Engine (Rock-Paper-Scissors)

This is where the bot handles tactical depth. It uses an **Attacker-Modifier** system: when Player A attacks Player B, the bot finds the matching `attacker`/`defender` pair and applies the `attacker_mod` to Player A's roll.

```json
{
  "attacker": "combat",
  "defender": "picket",
  "attacker_mod": 2,
  "defender_mod": -2,
  "flavor": "Raw firepower shreds fast ships that get too close."
}
```

**Design rules:**
- Matchups should be **zero-sum** — if `attacker_mod` is +2, the `defender_mod` should be -2.
- Small numbers (±1 to ±3) have outsized impact on a 1d20. Avoid modifiers beyond ±5.
- Always define the **mirror matchup** (e.g., if `combat` attacks `picket`, also define `picket` attacks `combat`).
- The `balanced` / `combined_arms` type should get a **±1** across the board — a modest penalty for attackers, a modest reward for defenders. This preserves its "no hard counters" identity without making it pointless.

### Default Space Triangle

```
combat → beats → picket → beats → carrier → beats → combat
balanced ← no hard counters →
```

### Default Ground Triangle

```
infantry → beats → heavy_armor → beats → mechanized → beats → infantry
combined_arms ← no hard counters →
```

### Full Matchup Coverage Required

You must define one entry for **every attacker/defender combination**, including same-type matchups (which should be `0 / 0`). Missing entries will cause the bot to error. With 4 types, that's 16 matchups per domain (4 × 4).

---

## 6. Force Types

`space_force_types` and `ground_force_types` define the categories that power the matchup engine. Each type needs a label, description, color, and its strongest/weakest matchup declared for display purposes.

```json
"combat": {
  "label": "Combat",
  "description": "Firepower and Hull dominant. Built for direct engagement.",
  "strongest_against": "picket",
  "weakest_against": "carrier",
  "color": "#ff6b35"
}
```

The `color` hex is used by the fleet builder UI. Make sure each type has a visually distinct color.

> **Note:** The `balanced` type uses `"strongest_against": "none"` and `"weakest_against": "none"` — this is intentional and required.

---

## 7. Units

Units are defined in `space_ships` and `ground_forces`. Each unit has a fixed cost in EMS, a tier gate, and a type that feeds into the matchup engine.

### Unit Structure

```json
{
  "id": "heavy_cruiser",
  "name": "Heavy Cruiser",
  "description": "Backbone of most fleets. Firepower and Hull dominant.",
  "unit_count": 1,
  "ems": 70,
  "tier": 2,
  "type": "combat",
  "attributes": {
    "speed": 4,
    "firepower": 7,
    "hull": 7,
    "shields": 6,
    "capacity": 2,
    "range": 6
  },
  "raw_score": 32,
  "naming": "individual",
  "force_user": false
}
```

**Field reference:**

| Field | Description |
|---|---|
| `id` | Unique snake_case identifier |
| `ems` | Economic cost — the primary balance lever |
| `tier` | Tier gate (1, 2, or 3) |
| `type` | Must match a key in your force types |
| `naming` | `"individual"` (one named ship) or `"squad"` / `"squadron"` (a unit of many) |
| `force_user` | Set to `true` for Force users / special characters |
| `raw_score` | Informational sum of all attributes — not used in engine math |

### Choice-Type Units

Units with `"type": "choice"` have a `type_options` array. The commander secretly declares which type they're using at battle declaration time; the choice is revealed when results post.

```json
{
  "id": "super_dreadnought",
  "type": "choice",
  "type_options": ["combat", "carrier"],
  ...
}
```

This is the primary source of bluffing and meta-game tension. Use it sparingly — it's most impactful on your most expensive or narrative-significant units.

### Attributes

Space ships use: `speed`, `firepower`, `hull`, `shields`, `capacity`, `range`

Ground forces use: `speed`, `firepower`, `armor`, `morale`, `capacity`, `range`

Attributes are **display-only** in the current engine (they feed `raw_score` but don't change roll math directly). Use them to communicate a unit's identity and flavor to players.

### Balancing Costs

When pricing a new unit, compare it to existing units at the same tier. Key reference points from the default theme:

| Unit | EMS | Type | Note |
|---|---|---|---|
| Starfighter Squadron | 10 | picket | Cheapest space unit |
| Light Cruiser | 50 | balanced | Tier 1 cap |
| Escort Carrier | 70 | carrier | Same cost as Heavy Cruiser |
| Battleship | 150 | combat | Tier 2 cap |
| Super Dreadnought | 750 | choice | Tier 3 cap (one fills the entire pool) |

The note `"75 squadrons = 750 EMS = one Super Dreadnought"` is a useful mental model: any flagship should cost enough that fielding it represents a real strategic commitment, not a free upgrade.

---

## 8. Force User Rules

Force users (Jedi, Sith, etc.) are special-cased units with additional restrictions managed in the `force_user_rules` block.

```json
"force_user_rules": {
  "description": "Force users are tier-gated attachments to any army.",
  "rules": [
    "You cannot field a Force user equal to or above your own rank tier.",
    "Maximum one Darth per army.",
    "Force user type choice is locked at battle declaration and secret until results post.",
    "Force user EMS counts toward the ratio of their declared type."
  ],
  "tier_access": {
    "1": ["apprentice_padawan"],
    "2": ["apprentice_padawan", "lord_knight"],
    "3": ["apprentice_padawan", "lord_knight", "darth"]
  }
}
```

When adapting this to another setting, this block maps cleanly to any "hero unit" system — Space Marine Captains, Admiral-class commanders, powerful psykers, etc. The tier access table controls which commander ranks can field which heroes.

---

## 9. Factions & Alignment

Factions define playable groups and connect to the alignment emoji system.

```json
{
  "id": "sith_empire",
  "name": "Sith Empire",
  "group": "empire",
  "preferred_space_type": "combat",
  "preferred_ground_type": "heavy_armor"
}
```

The `group` key maps to `alignment_groups`, which assigns a display emoji:

```json
"alignment_groups": {
  "republic": "🔵",
  "empire": "🔴",
  "sovereign": "🟢",
  "criminal": "🟡",
  "force": "🟣",
  "planetary": "🟠",
  "independent": "⚪"
}
```

The `preferred_space_type` and `preferred_ground_type` fields are used by the bot to suggest starter fleets to new commanders. Set these to `null` for factions that are primarily ground-based, intelligence-focused, or otherwise non-military.

Some factions support `aliases` for alternate lookup names:

```json
{
  "id": "resistance",
  "name": "Resistance movements",
  "aliases": ["balmorra", "corellian resistance", "balmorran resistance"],
  ...
}
```

---

## 10. ⚠️ Hardcoded Force Type Maps (Critical)

The theme JSON controls most of the bot's behavior, but **force type names in the battle flow are hardcoded** in `sequences/battle_flow.py`. If you rename or add force types in your theme, you must also update these two constants or the tactical setup DMs will break:

```python
FLEET_MAP = {
    '1': 'balanced',
    '2': 'carrier',
    '3': 'combat',
    '4': 'picket',
}

ARMY_MAP = {
    '1': 'combined_arms',
    '2': 'infantry',
    '3': 'mechanized',
    '4': 'heavy_armor',
}
```

These maps are what players see when they pick their fleet/army type in the pre-battle DM. The keys (`'1'`, `'2'`, etc.) are the numbers players type; the values must exactly match the type IDs in your theme JSON.

**Rules:**
- Every type ID in your `space_force_types` must appear as a value in `FLEET_MAP`.
- Every type ID in your `ground_force_types` must appear as a value in `ARMY_MAP`.
- The `"choice"` pseudo-type used by units like the Super Dreadnought does **not** need an entry here — it resolves at battle time from `type_options`.
- If you add a 5th type, add a `'5'` key to the relevant map.

This is the one place where a theme change requires a code change. Everything else is JSON-only.

---

## 11. Creating a New Theme

1. **Copy the template.** Start from `star_wars_old_republic.json` and rename it `my_new_theme.json`.
2. **Update metadata.** Change `theme_name`, `theme_version`, `description`, and `author` at the top.
3. **Define your tiers.** Set EMS caps based on how expensive you want your most powerful units to be. Work backwards from your intended flagship cost.
4. **Create your force types.** Define 3–4 types per domain (space and ground). Map out your Rock-Paper-Scissors triangle before writing any matchup entries.
5. **Write all matchups.** For N types, you need N × N matchup entries per domain. Don't leave any pairs undefined.
6. **Build your unit library.** Price units relative to each other, not in isolation. Use the tier caps as hard ceilings.
7. **Define factions.** Assign each to a group. Set `preferred_space_type` and `preferred_ground_type` based on faction identity.
8. **Update `FLEET_MAP` / `ARMY_MAP`** in `sequences/battle_flow.py` to match your new type IDs. See Section 10.
9. **Update `.env`.** Set `THEME_PATH=themes/my_new_theme.json`.
10. **Restart the bot** to load the new JSON.

---

## 12. Pro-Tips for Balancing

**The Balanced Type** acts as a control variable. If you're struggling to balance a unit, make it `balanced`. It gives the player flexibility without providing a mathematical edge. If everyone is running balanced, your other types may be too risky.

**The 20% Warning** triggers a critical alert when a side's remaining EMS drops below 20% of their starting pool. Make sure your campaign's total EMS is large enough that hitting 20% feels like a dramatic mid-battle turning point, not an instant loss.

**The Fleet Builder** (`eris-fleet-builder.html`) lets you preview balance changes in real-time. After editing your theme JSON, update the `UNITS` array in the builder script to see how your costs and matchups interact before going live.

**Flavor text matters.** The `flavor` field on matchups and tactics is read aloud during battle resolution. Write it to reinforce the fiction — players remember "Fighter swarms pick apart a combat fleet before it can engage" more than a raw +2 modifier.

**Tenaciousness** is a live mechanic, not a theme value. The losing commander can invoke it after the final round for 2 extra rounds at a cost of 5 LP — their first tenacity roll takes a **-2 penalty**. The winner must accept. This is hardcoded in `battle_flow.py` (`TENACITY_LP_COST = 5`, `TENACITY_ROUNDS = 2`). It isn't configurable via the theme JSON, but it's worth understanding when playtesting — it means a declared loser can still swing the result.

**Test with actual players** before declaring a theme final. Simulated balance rarely survives first contact with creative list-building.
