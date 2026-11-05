# ⏪ Rewind — per-step undo for Hermes Agent workspaces

**Not memory. Not a silent shadow store. A numbered timeline of every file write, patch, and terminal
command your agent runs — so `/rewind 3` puts files *and* git HEAD back to just before step 3.
Even the rewind is undoable. Fork any step into a sibling worktree to try the other approach.**

```
/timeline
  •   1  write_file   app.py                         (repo)  turn:a1b2c3d4
  •   2  patch        app.py                         (repo)
  •   3  terminal     rm -r legacy && git commit…    (repo)
  ⚠   4  terminal     git push origin main           (repo)  ⚠ git push reaches a remote
/rewind-diff 3
/rewind 3              # files + HEAD before step 3; push is flagged, not undone
/rewind fork 2 --model gpt-x   # try the alternative next door; current checkout untouched
/rewind unfork         # clean up fork worktrees when you're done
```

Pair with built-in **`/undo`** when you also want the chat turn forgotten — Rewind restores the
filesystem; `/undo` restores the conversation. Steps store Hermes `turn_id` when the runtime
provides it so you can see which chat turns map to which file changes.

## Why a plugin (not a prompt / skill)
A prompt can't snapshot *before* a tool runs. Rewind sits in the `tool_execution` middleware chain
and journals every mutating call whether or not the model remembers to "be careful".

## Why not just `/rollback` / git-autosave / memory-rewind?

| | built-in `/rollback` | **git-autosave** (catalog) | **memory-rewind** (catalog) | **Rewind (this plugin)** |
|---|---|---|---|---|
| What it tracks | FS checkpoint ≤1/turn | shadow refs before edits | MEMORY.md / USER.md / skills | **every** `write_file` / `patch` / `terminal` |
| Moves HEAD / working tree | restores files; conversation too | **never** (observe-only) | rewrites memory files on restore | **yes**, on explicit `/rewind` (after a safety snapshot) |
| Agent-callable tool | no | no | no | `rewind_restore` list / restore / fork / unfork |
| Try the alternative | — | — | — | **`/rewind fork <n>`** → branch + worktree |
| Irreversible flags | — | — | — | `git push`, publish, curl POST, DB/infra, privileged shells, recursive deletes of workspace-root or home |
| Remote terminals | — | — | — | recorded as **⊘ not snapshotted** (honest) |
| Hand-edits | preserved by default | n/a (no auto-restore) | n/a | **warned** when `/rewind` would overwrite them |
| Chat undo | yes (with restore) | no | no | no — **pair with `/undo`** |
| Name collision risk | — | — | yes if you say "rewind" loosely | pitched as **workspace** undo |

**Use `/rollback` when you want turn-level restore that also rewinds chat and preserves hand-edits.**
**Use Rewind when you need per-tool granularity, git HEAD restore, fork-the-past, agent self-repair,
or a Desktop scrubber.** They coexist.

## Commands, tool, and UI
- `/timeline` — • ok · ✗ failed · ⚠ irreversible · ⊘ remote · ◆ safety · 🌿 fork; shows `turn:` when known
- `/rewind <n>` — restore every touched root to the state before step *n* (files + git HEAD by default)
- `/rewind fork <n> [--model NAME]` — sibling worktree/dir at that past state; optional model label for a future Arena re-run (prints how-to today)
- `/rewind unfork [path|step]` — remove fork worktrees/branches Rewind created
- `/rewind-diff <a> [<b>]` — what step *a* changed (or *a*→*b*)
- `/rewind gc [--all]` · `hermes rewind gc [--all]` · `hermes rewind timeline [session]`
- tool `rewind_restore` — `list` (structured steps + irreversible markers), `restore`, `fork`, `unfork`. Description is approval-friendly: restore is called out as destructive.
- **Desktop** pane (`desktop/plugin.js`, off by default) — scrubber + diff + Rewind/Fork; API under `/api/plugins/rewind/`

## How snapshots work
- **Git repo:** private `GIT_INDEX_FILE` → `write-tree` → `commit-tree`, pinned at
  `refs/hermes-rewind/<instance-id>/<sha>`. Snapshots never touch your real index/branch; `/rewind` may
  `git reset` HEAD on purpose (`restore_mode: move_head`, default). Set `restore_mode: worktree_only` to
  rewrite files without moving the branch tip. Instance ids are derived from this plugin's data dir so
  two `HERMES_HOME`s sharing a repo never GC each other's pins.
- **Outside git:** content-addressed blobs + manifests in the plugin data dir.
- Snapshot failure never blocks the tool.

## Disclosure (exact)
- **Registers:** 1 tool (`rewind_restore`), 1 middleware (`tool_execution`), 3 slash commands
  (`/timeline`, `/rewind`, `/rewind-diff`), 1 CLI (`hermes rewind`), a dashboard router, a Desktop pane
  (defaultEnabled: false). No hooks. No Hermes core patches.
- **Writes:** SQLite journal + blobs under the Hermes plugin data dir; `refs/hermes-rewind/<instance-id>/*`
  inside git repos the agent touches; `/rewind fork` adds a `rewind/fork-*` branch + `git worktree`
  (remove with `/rewind unfork` or `git worktree remove <path>`). `gc` deletes only this instance's
  doomed refs and its own blobs.
- **Restoring overwrites files and can move HEAD**, only when you `/rewind` or the agent calls
  `rewind_restore` with `action=restore`, and always after journaling a safety snapshot. Default
  `restore_mode=move_head`; `worktree_only` keeps the branch tip. Hand-edits since the last step are
  detected and warned about. Rewinding across a push/publish/remote step restores local files and
  prints an **irreversible_summary** of what stays outside the workspace.
- **Debugging:** `REWIND_DEBUG_PAYLOAD=<file>` logs middleware *shape* (tool name, arg keys, id fields) —
  never argument values. Off by default.
- **Network:** none. **Secrets:** none. **Dependencies:** Python stdlib + the `git` binary.
- **Not undoable:** side effects outside the workspace filesystem (pushes, API calls, DB writes, sent
  messages) — flagged ⚠. Remote backends (`TERMINAL_ENV` ≠ local) are journaled as ⊘ and are not restored.
- **Conversation:** not rewound; use built-in `/undo`. `turn_id` is stored on steps when Hermes provides it.

## Install
```bash
hermes plugins install hanii090/rewind-hermes-plugin     # or: git clone https://github.com/hanii090/rewind-hermes-plugin ~/.hermes/plugins/rewind
hermes plugins enable rewind
```
Config (`plugins.rewind.*`): `max_file_bytes` (5 MB), `max_files` (5000), `ignore_globs`,
`snapshot_terminal` (true), `retention_days` (14), `max_store_mb` (500), `max_steps_per_session` (2000),
`restore_mode` (`move_head` | `worktree_only`).

### Desktop pane (manual enable)
1. `hermes plugins enable rewind`
2. Hermes Desktop → **Capabilities → Plugins** → **Rescan**
3. Toggle **Rewind** on; open the *rewind* pane from the dock/menu
4. Mutating tools should populate the scrubber; if the pane says backend unavailable, the agent half isn't enabled on this gateway

## Demo (no LLM)
```bash
python3 scripts/demo.py            # temp playground, auto-cleaned; prints SCOREBOARD
python3 scripts/demo.py --keep /tmp/rewind-demo
```
Walks write → rm+commit → timeline → fork --model → hand-edit warning on rewind → unfork.
For a live Hermes dispatcher probe (still no LLM): `scripts/live_probe.py` → `tests/manual_hermes_live_probe.py` (kept under `tests/` so catalog validate ignores its private Hermes imports; prefer `scripts/demo.py`).

### ~75 s video beats
1. Hook: agent deleted the wrong folder and committed it.
2. Timeline fills: write, rm+commit, push marked ⚠.
3. Scrub to 3 → Rewind → files *and* commit return; toast notes push not undone.
4. Fork step 2 into a worktree; main checkout untouched.
5. Agent calls `rewind_restore` itself. Close on `/rewind gc`.

## Development
```bash
HERMES_HOME=/tmp/hhome pytest -q          # or: /path/to/hermes/.venv/bin/pytest -q
python3 scripts/demo.py
hermes plugins doctor rewind && hermes plugins validate rewind
```
Local commits only during the hackathon polish pass — no push / PR / X / Typeform from this tree unless asked.
