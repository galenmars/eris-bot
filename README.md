# E.R.I.S. — Engagement Resource & Intelligence System

> A modular, theme-driven Discord bot for large-scale automated roleplay wargaming.

E.R.I.S. manages complex campaign enrollments, tactical space and ground combat resolution, and a persistent logistics system for commander resources (EMS). It is built to run as a hosted multi-guild service or as a self-hosted single-server bot — with no code changes required between the two.

---

## Screenshots

| Campaign Creation | Commander Approval | Battle Result |
|---|---|---|
| ![Campaign Created](docs/screenshots/campaign_created.png) | ![Submission Approved](docs/screenshots/submission_approved.png) | ![Battle Complete](docs/screenshots/battle_complete.png) |

| EMS Submission | Admin Log |
|---|---|
| ![EMS Submit](docs/screenshots/ems_submit.png) | ![Admin Log](docs/screenshots/admin_log.png) |

---

## Features

- **Theme-Driven Balance** — The entire game engine (matchups, tactics, ship stats, ground forces, faction lists) is defined in a JSON theme file. Swap the theme (Star Wars, Halo, Warhammer) by pointing the bot at a new JSON. No Python changes required.
- **Organization Layer** — Admins register their guild's organizations (e.g. "The Red Veil" under Sith Empire) via `/admin org add`. Players pick from the full faction list plus any custom orgs during commander submission.
- **Strict EMS System** — Commanders have fixed resource budgets for their fleet or army loadout, validated against cryptographically-structured submission blocks.
- **Automated Enrollment** — Reaction-based ⚔️ flow that guides players through a DM-based enrollment process, locking commanders to one campaign at a time.
- **Tactical Combat** — Space and ground battles with tactic selection, initiative rolls, and matchup bonuses resolved automatically round by round.
- **Professional Admin Tooling** — `/setup` auto-provisions roles and channels, then revokes its own elevated permissions. Per-guild config (rank limits, channel IDs, org lists) stored independently per server.

---

## Architecture

E.R.I.S. uses a **strict layered architecture**. The core principle is unidirectional dependency: Data and Domain know nothing about Discord or asyncio.

```
[Discord / discord.py]   ← Entry point (bot.py)
         │
    [Cogs Layer]         ← Slash commands, buttons, select menus
         │
 [Sequences Layer]       ← Stateful DM flows, wait_for loops, timeouts
         │
  [Domain Layer]         ← Pure game rules and validation (no Discord objects)
         │
    [Math Layer]         ← Dice, matchup bonuses, EMS calculation
         │
    [Data Layer]         ← SQLite repositories (WAL mode, Repository pattern)
         │
  [Shared Layer]         ← Theme-agnostic UI, embed builders, faction picker
```

### The Golden Rule

**Code flows down. Data flows up.**

A file may import from any layer below it. A file must never import from a layer above it. If you find yourself importing a Cog inside `domain/`, you have broken the architecture.

### Folder Responsibilities

| Folder | What lives here | Why |
|---|---|---|
| `cogs/` | Slash commands, buttons, select menus | Only what the user sees and clicks |
| `sequences/` | `wait_for` loops, DM flows, timeouts | Any process spanning more than one interaction |
| `core/domain/` | Validation rules, eligibility checks | Game logic that works identically in tests, DMs, or slash commands |
| `core/math/` | Formulas, dice, table lookups | Pure computation. Consumes `theme.json` as configuration |
| `core/data/` | SQL queries only | The rest of the bot never writes raw SQL |
| `core/shared/` | Embed builders, faction picker, constants | Code that would otherwise be copy-pasted across three Cogs |

### Path of a Command: `/battle engage`

1. **`battle_cog.py`** — Sanity checks (user in channel? commander enrolled?). Calls the sequence.
2. **`sequences/battle_flow.py`** — Traffic controller. Handles DM prompts, tactic selection, timeouts.
3. **`core/domain/battle.py`** — "Is this tactic legal?" Returns `True` or raises `DomainError`.
4. **`core/math/dice.py`** — Calculates round total using theme bonuses, rolls dice, returns result.
5. **`core/data/battle_repo.py`** — Saves battle state to SQLite.

### Adding a New System

To add an inventory system (as an example), you would create four new files and touch nothing existing:

```
core/data/inventory_repo.py     ← SQL for the inventory table
core/domain/inventory.py        ← "Can I carry this item?" logic
sequences/inventory_flow.py     ← "Use an item" DM flow
cogs/inventory_cog.py           ← /inventory slash command
```

---

## Project Structure

```
eris-bot/
├── bot.py                         # Entry point, cog loader, theme loader
├── .env                           # Secrets (not committed)
├── themes/
│   └── star_wars_old_republic.json
├── cogs/
│   ├── admin_cog.py
│   ├── battle_cog.py
│   ├── campaign_cog.py
│   ├── commander_cog.py
│   ├── ems_cog.py
│   └── status_cog.py
├── sequences/
│   ├── battle_flow.py
│   ├── campaign_flow.py
│   ├── commander_flow.py
│   ├── enrollment_flow.py
│   └── ems_flow.py
├── core/
│   ├── domain/
│   │   ├── battle.py
│   │   ├── commander.py
│   │   ├── ems.py
│   │   └── exceptions.py
│   ├── math/
│   │   ├── dice.py
│   │   └── matchups.py
│   ├── data/
│   │   ├── battle_repo.py
│   │   ├── commander_repo.py
│   │   ├── ems_repo.py
│   │   └── guild_repo.py
│   └── shared/
│       └── faction_picker.py
└── docs/
    └── screenshots/
```

---

## Installation

### Prerequisites

- Python 3.10+
- `discord.py`
- `python-dotenv`

### Setup

**1. Clone the repository**

```bash
git clone https://github.com/GalenMars/eris-bot.git
cd eris-bot
```

**2. Create a virtual environment**

```bash
python -m venv .venv
source .venv/bin/activate       # Linux / macOS
.venv\Scripts\activate          # Windows
```

**3. Install dependencies**

```bash
pip install -r requirements.txt
```

**4. Create your `.env` file**

```env
DISCORD_TOKEN=your_bot_token_here
OWNER_ID=your_discord_user_id
DB_PATH=commanders.db
THEME_PATH=themes/star_wars_old_republic.json
SETUP_TOKEN=generate_a_secret_string
```

**5. Run the bot**

```bash
python bot.py
```

---

## First-Time Guild Setup

1. The server owner runs `/setup [token]` using the `SETUP_TOKEN` from `.env`
2. Choose **Auto** (bot creates channels and roles) or **Manual** (point the bot at existing ones)
3. After setup completes, run these before going live:

```
/admin config set fleet_url   <your fleet builder URL>
/admin config set army_url   <your fleet builder URL>
/admin config set max_mains   2        # default is 1
/admin config set max_seniors 3        # default is 2
/admin config set max_juniors 5        # default is 0
```

4. Register your guild's organizations:

```
/admin org add   → bot DMs you the faction picker → org is saved
/admin org list  → view all registered orgs
/admin org remove <name>
```

Players will see registered orgs alongside the standard faction list when submitting commanders.

See [ERIS_Setup_Guide.md](docs/ERIS_Setup_Guide.md) for the full manual setup walkthrough.

---

## Theme System

The entire game balance lives in `themes/star_wars_old_republic.json`. To run E.R.I.S. for a different universe:

1. Create a new theme JSON following the same schema
2. Update `THEME_PATH` in your `.env`
3. Restart the bot

No Python changes required. The math, matchup bonuses, faction lists, ship types, and ground force compositions are all driven by the theme file.

---

## Contributing

Issues and pull requests are welcome.

- **Bug reports** — open an issue with the error log, the command that triggered it, and your Python version
- **New features** — open an issue first to discuss before writing code; the layered architecture has specific conventions (see Architecture above) that PRs are expected to follow
- **Theme contributions** — new theme JSONs for different universes are especially welcome

### Development Rules

1. **No Discord objects in Domain or Math** — pass `int` IDs or plain `dict` records, never `discord.Member` or `discord.Interaction`
2. **Unidirectional imports only** — Cogs → Sequences → Domain → Math; never the reverse
3. **Repo commits, Sequences drive** — the Data layer owns `db.commit()`; the Sequence layer decides when
4. **The JSON is the config** — if you want to change game balance, edit the theme file, not the Python

---

## License

MIT License — use freely, keep the credit.

```
Copyright (c) 2026 GalenMars
```

Full text: [LICENSE](LICENSE)

---

## Support

E.R.I.S. is an open-source project built by one person with too much hot cocoa and love. ⚔️

If it brings order to your campaign: [buymeacoffee.com/galenmars](https://buymeacoffee.com/galenmars)

---

*Bot invite: https://discord.com/oauth2/authorize?client_id=984824419128066078&permissions=2416274512&scope=bot%20applications.commands*
