# Rewind — Hackathon judge one-pager

**Undo for agent workspaces.** Every file write, patch, and terminal call becomes a numbered
timeline step. `/rewind 3` restores files *and* git HEAD to just before step 3. Fork any step
into a sibling worktree to try the other approach. Irreversible side-effects are flagged, not lied about.

## The problem (why this can place top-2)
**Primary pain:** the agent broke my files/repo *N* steps ago. `git checkout` / manual undo is
hard under agent churn; built-in `/rollback` is coarse (≤1 FS checkpoint/turn + chat); and
anything that already hit a remote (`git push`, publish, DB) **cannot** be undone by a local
plugin. Judges reward usefulness beyond a prompt/skill — middleware that journals *before*
mutations, then makes undo one command, is the gap.

**90s-demo enhancements that most reduce that pain**
1. **`/rewind last`** — one-shot undo of the latest tool mutation (no timeline arithmetic).
2. **Irreversible footer on the tool result** — the moment a push/publish runs, the model and
   operator see that local rewind will not undo the remote (honesty beats silent false hope).
3. **Fork + compare** — materialise an alternate past beside the live tree and show a one-line
   `vs_current` so "try the other approach" is visible without a second repo tour.

(Shipped 1–3; v0.3.4 adds `/rewind preview` + timeline “last mutation” / confirm copy so restores across irreversible steps are obvious before you tap undo.)


## Surfaces used (Hermes plugin APIs)
| Surface | What |
|---|---|
| `tool_execution` middleware | Snapshot before/after mutating tools — no model cooperation required |
| Tool `rewind_restore` | Agent self-repair: list / restore / fork / unfork (approval-friendly schema) |
| Slash `/timeline`, `/rewind`, `/rewind-diff` | Operator UX (`last` / `preview` / fork/unfork/gc) |
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
Beats: rewrite → delete+commit → irreversible push flagged (+ result footer) → timeline →
fork last + vs_current → `/rewind last` (+ hand-edit / irreversible_summary / HEAD) → unfork.

Live Hermes (optional): `scripts/live_probe.sh` → `tests/manual_hermes_live_probe.py`.

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
