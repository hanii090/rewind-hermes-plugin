# Rewind — Hackathon judge one-pager

**Undo for agent workspaces.** Every file write, patch, and terminal call becomes a numbered
timeline step. `/rewind 3` restores files *and* git HEAD to just before step 3. Fork any step
into a sibling worktree to try the other approach. Irreversible side-effects are flagged, not lied about.

## Surfaces used (Hermes plugin APIs)
| Surface | What |
|---|---|
| `tool_execution` middleware | Snapshot before/after mutating tools — no model cooperation required |
| Tool `rewind_restore` | Agent self-repair: list / restore / fork / unfork (approval-friendly schema) |
| Slash `/timeline`, `/rewind`, `/rewind-diff` | Operator UX (`/rewind fork`, `/rewind unfork`, `/rewind gc`) |
| CLI `hermes rewind` | `timeline` + `gc` outside chat |
| Dashboard `/api/plugins/rewind/*` | Desktop scrubber backend |
| Desktop `desktop/plugin.js` | Opt-in right-dock scrubber (Empty/Error/busy states) |
| `broadcast_plugin_event` | Live pane refresh |

**Not used:** hooks that rewrite prompts, core patches, network, secrets, self-updaters.

## Why a plugin (not a skill / prompt)
A skill cannot take a filesystem snapshot *before* `write_file` runs. Middleware sits in the tool
chain and journals every mutating call whether or not the model "remembers to be careful".

## vs built-ins & catalog peers
| | `/rollback` | git-autosave | memory-rewind | **Rewind** |
|---|---|---|---|---|
| Tracks | ≤1 FS checkpoint/turn | shadow refs (observe) | MEMORY.md / skills | **every** mutating tool |
| Moves HEAD | files (+ chat) | never | memory files only | **yes, on explicit restore** |
| Agent tool | no | no | no | **yes** |
| Fork-the-past | no | no | no | **`/rewind fork`** |
| Irreversible flags | no | no | no | **yes** |
| Chat undo | yes | no | no | pair with **`/undo`** |

## Demo (≤75 s, no LLM)
```bash
python3 scripts/demo.py          # scoreboard at end; exit 0 = green
```
Beats: rewrite → delete+commit → irreversible push flagged → timeline → fork --model →
hand-edit warning + irreversible_summary + HEAD-moved on rewind → unfork → empty-session copy.

Live Hermes (optional): `scripts/live_probe.py` → `tests/manual_hermes_live_probe.py`.

## Risks (honest)
- Restore overwrites hand-edits (we **warn**; `/rollback` preserves by default).
- Moving HEAD is powerful — default `restore_mode=move_head`; opt into `worktree_only` to keep the tip.
- Rewinding across push/publish restores **local** files only; response includes `irreversible_summary`.
- Remote backends (`TERMINAL_ENV`) are ⊘ not snapshotted.
- Chat not auto-undone — show `turn_id` + `/undo` hint.
- Desktop GUI not exercised on headless CI boxes (API + SDK-hardened pane covered by tests).

## Install (judge machine)
```bash
hermes plugins install hanii090/rewind-hermes-plugin   # or symlink checkout → $HERMES_HOME/plugins/rewind
hermes plugins enable rewind
# Desktop: Capabilities → Plugins → Rescan → toggle Rewind
python3 scripts/demo.py
```

## Disclosure (exact — also in README + catalog description)
Registers 1 tool, 1 middleware, 3 slash commands, 1 CLI, dashboard router, opt-in Desktop pane.
Writes SQLite + blobs under plugin data dir and `refs/hermes-rewind/<instance-id>/*` in touched
git repos; fork adds branch+worktree. Restore overwrites files / may reset HEAD (`restore_mode`,
default `move_head`; `worktree_only` available) only on explicit action after a safety snapshot;
irreversible side-effects get an `irreversible_summary`. No network, no secrets, stdlib + `git`,
no core patches.
