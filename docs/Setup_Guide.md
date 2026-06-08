# E.R.I.S. Setup Guide
**Engagement Resource & Intelligence System**
*Read this before running `/setup` in manual mode.*

---

## Choosing a Mode

When you run `/setup`, the bot will ask you to choose:

| Mode | When to use |
|---|---|
| 🤖 **Auto** | Fresh server, or a server where you don't mind the bot creating new channels and roles |
| 🔧 **Manual** | Existing community with established structure — you already have the channels and roles, you just want to point ERIS at them |

**If you choose manual, read this entire document first.**  
The bot will ask you to mention each channel and role one at a time. You have 2 minutes per prompt. If you're not ready, choose Auto or come back when you are.

---

## What You Need Before Manual Setup

### Channels (8 required)

Create these text channels before running `/setup`. The names below are the internal keys ERIS uses — your actual channel names can be whatever you want, as long as you point ERIS at the right one.

#### Category: E.R.I.S. SYSTEM
These should be **admin-only** — regular members should not see them.

| Internal Key | Suggested Name | Purpose |
|---|---|---|
| `eris-setup` | `#eris-setup` | Permanent setup log and requirements doc |
| `eris-notifications` | `#eris-notifications` | Player DM fallback and announcements |
| `eris-admin-log` | `#eris-admin-log` | All admin actions logged here |

#### Category: COMMAND CENTER
These should be **visible to all members** (read-only for regular members).

| Internal Key | Suggested Name | Purpose |
|---|---|---|
| `campaign-board` | `#campaign-board` | Campaign embeds with ⚔️ enrollment reaction |
| `battle-record` | `#battle-record` | Battle result log |
| `commander-submissions` | `#commander-submissions` | Where players paste their commander blocks |

#### Category: ADMINISTRATION
These should be **admin-only**.

| Internal Key | Suggested Name | Purpose |
|---|---|---|
| `commander-approvals` | `#commander-approvals` | Pending commander block review |
| `ems-requests` | `#ems-requests` | Pending EMS adjustment requests |

---

### Roles (4 required)

Create these roles before running `/setup`. Again, names are flexible — you'll point ERIS at whatever you have.

| Internal Key | Suggested Name | Purpose | Recommended Color |
|---|---|---|---|
| `ERIS Admin` | `ERIS Admin` | Can run all admin commands | `#00d4ff` (cyan) |
| `Commander` | `Commander` | Granted automatically when first commander is approved | `#ffb700` (gold) |
| `Fleet Commander` | `Fleet Commander` | Has a Fleet force type deployed | `#00ff88` (green) |
| `Army Commander` | `Army Commander` | Has an Army force type deployed | `#ff6b35` (orange) |

---

## Bot Permissions Required

Before running `/setup`, make sure the ERIS bot role has these permissions **at the server level**:

- ✅ Read Messages / View Channels
- ✅ Send Messages
- ✅ Manage Messages
- ✅ Embed Links
- ✅ Add Reactions
- ✅ Read Message History
- ✅ Manage Roles *(needed only during setup — revoked automatically after)*
- ✅ Manage Channels *(needed only during setup — revoked automatically after)*

**In manual mode**, ERIS will also verify it has **Read Messages** and **Send Messages** in each channel you point it at. If it doesn't, setup will stop and tell you which channel to fix.

---

## Channel Permissions ERIS Needs

In each channel you assign to ERIS, the bot role must have at minimum:

- Read Messages
- Send Messages

For `#commander-submissions` and `#campaign-board`, it also needs:
- Add Reactions
- Manage Messages

---

## What Happens During Setup

1. You run `/setup [token]`
2. Pre-flight checks run (permissions, capacity, database)
3. You choose **Auto** or **Manual**
4. **Manual:** The bot asks you to mention each channel and role, one at a time
5. The bot validates permissions in every channel you provide
6. The configuration is saved to the database
7. The bot **revokes its own Manage Roles and Manage Channels permissions** — it cannot create or delete channels/roles after this point
8. A permanent requirements document is posted to your `#eris-setup` channel

---

## After Setup — Manual Steps

Regardless of which mode you chose, these steps are required before going live:

1. **Assign ERIS Admin** to your moderation team
2. Run `/admin config set fleet_url <your fleet builder URL>`
3. Run `/setup status` to confirm everything looks correct
4. Run `/admin config view` to review all settings
5. **Register your organizations** — run `/admin org add` for each guild or faction group
   your community uses. Players will see these alongside the standard factions when
   submitting commanders. If your server only uses the canonical factions (Galactic
   Republic, Sith Empire, etc.), you can skip this step.
6. **Adjust commander rank limits** if needed — the defaults are `max_mains = 1`,
   `max_seniors = 2`, `max_juniors = 0`. To change them:
   ```
   /admin config set max_mains 2
   /admin config set max_seniors 3
   /admin config set max_juniors 5
   ```
   These are per-guild — each server sets its own limits independently.

---

## Security Notes

- `/setup` can only be run by the **server owner**
- It requires a **one-time token** from your `.env` file
- The token is invalidated after setup completes — it cannot be reused
- After setup, the bot **cannot create or delete channels or roles** (permissions are revoked)
- The bot will never DM users unprompted except as a fallback when a player's notification channel is unavailable

---

## To Run Setup Again

If you need to reconfigure:

1. Manually add **Manage Roles** and **Manage Channels** back to the ERIS bot role in Server Settings → Roles
2. Generate a new token in your `.env` file
3. Run `/setup reset` — clears the bot's internal config, does **not** delete your channels or roles
4. Run `/setup [new token]`

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Pre-flight fails: missing permissions | Add the missing permission to the ERIS bot role in Server Settings → Roles |
| Manual mode: ERIS can't find the channel | Make sure you mentioned it with `#` or pasted the exact channel ID |
| Manual mode: permission error on a channel | Check that the ERIS bot role has Read + Send Messages in that channel |
| Setup token invalid | Check your `.env` file — `SETUP_TOKEN=yourtoken` with no spaces |
| "Already configured" error | Run `/setup reset` first, then `/setup [token]` |
| Bot won't start at all | Check `logs/eris.log` — import errors show up there |
| Commander submission shows no organizations | Run `/admin org add` to register at least one, or leave it — players can still pick from the standard faction list |

---

*This guide is for guild masters. Players do not need to read it.*  
*Official bot: https://discord.com/oauth2/authorize?client_id=984824419128066078&permissions=2416274512&scope=bot%20applications.commands · [top.gg link]*
