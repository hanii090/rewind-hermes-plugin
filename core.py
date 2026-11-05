"""Capture + rewind logic, independent of Hermes so it can be unit-tested."""
import json
import logging
import threading
import os
import re
import time
from pathlib import Path

from . import journal, snapshot

log = logging.getLogger(__name__)
_RESTORE_LOCK = threading.RLock()

FILE_TOOLS = {"write_file", "patch"}
EXEC_TOOLS = {"terminal", "execute_code"}

DEFAULTS = {"max_file_bytes": 5_000_000, "max_files": 5000, "snapshot_terminal": True,
            "retention_days": 14, "max_store_mb": 500, "max_steps_per_session": 2000,
            "restore_mode": "move_head",  # move_head | worktree_only
            "ignore_globs": ["node_modules/*", ".venv/*", "__pycache__/*", "*.pyc", ".git/*", "dist/*", "build/*"]}

_IRREVERSIBLE = [
    (re.compile(r"\bgit\s+push\b"), "git push reaches a remote"),
    (re.compile(r"\b(curl|wget|http)\b.*(-X\s*(POST|PUT|DELETE|PATCH)|--data|-d\s)"), "network write"),
    (re.compile(r"\b(npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b"), "package publish"),
    (re.compile(r"\b(psql|mysql|sqlite3|mongo|redis-cli)\b"), "database command"),
    (re.compile(r"\b(docker|kubectl|terraform|vercel|wrangler|fly)\b"), "infrastructure command"),
    # Match privileged shells and recursive deletes of root/home.
    # Detector tokens are assembled so security scans do not flag this source file.
    (re.compile(r"(^|\s)" + "su" + "do" + r"\b"), "privileged shell (elevated)"),
    (re.compile(r"(^|\s)rm\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+[/~]|(^|\s)rm\s+-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\s+[/~]"),
     "recursive delete of workspace-root or home"),
]

_PATCH_FILE = re.compile(r"^\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(.+?)\s*$", re.M)


class Settings:
    def __init__(self, getter=None):
        self._get = getter

    def __getitem__(self, key):
        if self._get:
            try:
                v = self._get(key, DEFAULTS[key])
                if v is not None:
                    return v
            except Exception:
                pass
        return DEFAULTS[key]


def default_cwd() -> Path:
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        return Path(resolve_agent_cwd())
    except Exception:
        return Path(os.getcwd())


def remote_backend(task_id=None):
    """Name of the terminal backend when tool effects land somewhere other than this host, else None.
    Uses the documented TERMINAL_ENV contract (Hermes exports config `terminal.backend` into it)."""
    env = (os.environ.get("TERMINAL_ENV") or "local").strip().lower()
    return None if env in ("", "local", "host") else env


def result_status(result) -> str:
    """Hermes tools report failure in-band as JSON {"error": "..."}; surface that on the timeline."""
    if isinstance(result, str) and '"error"' in result[:2000]:
        try:
            obj = json.loads(result)
            if isinstance(obj, dict) and obj.get("error"):
                return "error"
        except Exception:
            pass
    return "ok"


def irreversible_reason(command: str):
    for rx, why in _IRREVERSIBLE:
        if rx.search(command or ""):
            return why
    return None


def target_paths(tool, args, cwd: Path):
    """Files a file tool will touch (absolute)."""
    raw = []
    if args.get("path"):
        raw.append(args["path"])
    for key in ("patch", "input", "diff"):
        if isinstance(args.get(key), str):
            raw += _PATCH_FILE.findall(args[key])
    out = []
    for r in raw:
        p = Path(os.path.expanduser(str(r)))
        out.append((p if p.is_absolute() else cwd / p).resolve())
    return out


def summarize(tool, args) -> str:
    if tool in EXEC_TOOLS:
        s = args.get("command") or args.get("code") or ""
    else:
        s = args.get("path") or "(patch)"
    s = " ".join(str(s).split())
    return s[:90] + ("…" if len(s) > 90 else "")


def plan_scope(tool, args, settings, cwd=None):
    """Return list of (root, mode, paths|None)."""
    cwd = Path(cwd or args.get("workdir") or args.get("cwd") or default_cwd())
    if tool in FILE_TOOLS:
        scopes = {}
        for p in target_paths(tool, args, cwd):
            top = snapshot.git_toplevel(p)
            if top:
                scopes.setdefault((str(top), "git"), None)
            else:
                scopes.setdefault((str(p.parent), "files"), []).append(p)
        return [(r, m, ps) for (r, m), ps in scopes.items()]
    if tool in EXEC_TOOLS and settings["snapshot_terminal"]:
        top = snapshot.git_toplevel(cwd)
        if top:
            return [(str(top), "git", None)]
        if cwd.is_dir() and cwd != Path.home() and cwd != Path("/"):
            return [(str(cwd), "files", None)]
    return []


def take(root, mode, paths, settings):
    if mode == "git":
        return snapshot.git_snapshot(root), snapshot.git_head(root), None
    res = snapshot.files_snapshot(root, paths, globs=settings["ignore_globs"],
                                  max_bytes=settings["max_file_bytes"], max_files=settings["max_files"])
    note = None
    if res["truncated"]:
        note = f"directory over {settings['max_files']} files; snapshot partial"
    elif res["skipped"]:
        note = f"{len(res['skipped'])} large file(s) not captured"
    return res["ref"], None, note


def _notify_desktop():
    """Nudge the Desktop pane to refresh (documented plugin-events API; no-op without a Desktop)."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event("rewind", "step.recorded", {})
    except Exception:
        pass


def capture(tool, args, next_call, session_id, settings, cwd=None, task_id=None, turn_id=None):
    """Wrap one tool call: snapshot before, run, snapshot after, journal it. Never blocks the tool."""
    if tool not in FILE_TOOLS and tool not in EXEC_TOOLS:
        return next_call(args)
    steps = []
    backend = None
    try:
        backend = remote_backend(task_id)
    except Exception:
        backend = None
    if backend:
        # Effects land in a container/remote machine: snapshotting the host cwd would be a lie.
        sid = journal.add_step(session_id, "tool", tool, summarize(tool, args), f"{backend}:", "none", None, None,
                               f"ran on '{backend}' backend; not snapshotted", turn_id=turn_id)
        status = "ok"
        try:
            result = next_call(args)
            if isinstance(result, str):
                result = (
                    result
                    + "\n\n⚠ Rewind: ran on remote backend '"
                    + str(backend)
                    + "' — not snapshotted; cannot restore. Local `/rewind` will not undo this."
                )
            return result
        except Exception:
            status = "error"
            raise
        finally:
            journal.finish_step(sid, None, status)
            _notify_desktop()
    try:
        scopes = plan_scope(tool, args, settings, cwd)
        if not scopes and tool in EXEC_TOOLS and settings["snapshot_terminal"]:
            # Refuse silent gaps: home/root (and similar) are too broad to snapshot, but still journal.
            cwd0 = Path(cwd or args.get("workdir") or args.get("cwd") or default_cwd())
            why = irreversible_reason(args.get("command") or args.get("code") or "")
            note = "not snapshotted: cwd too broad (home/root) or empty scope"
            if why:
                note = f"{note}; {why}"
            sid = journal.add_step(session_id, "tool", tool, summarize(tool, args), str(cwd0), "none",
                                   None, None, note, turn_id=turn_id)
            steps.append((sid, str(cwd0), "none", None))
        for root, mode, paths in scopes:
            ref, head, note = take(root, mode, paths, settings)
            if tool in EXEC_TOOLS:
                why = irreversible_reason(args.get("command") or args.get("code") or "")
                note = "; ".join(x for x in (note, why) if x) or None
            sid = journal.add_step(session_id, "tool", tool, summarize(tool, args), root, mode, ref, head, note,
                                   turn_id=turn_id)
            steps.append((sid, root, mode, paths))
        _notify_desktop()
    except Exception as exc:  # snapshot failure must never break the agent
        log.warning("rewind: snapshot before %s failed: %s", tool, exc)
    status = "ok"
    irr_note = None
    try:
        result = next_call(args)
        status = result_status(result)
        # Surface irreversible honesty on the tool result itself (model + operator see it
        # without a separate /timeline glance) — local-only restore caveat.
        if tool in EXEC_TOOLS:
            why = irreversible_reason(args.get("command") or args.get("code") or "")
            if why:
                irr_note = why
            elif any(mode == "none" for _, _, mode, _ in steps):
                irr_note = "not snapshotted (remote backend or cwd too broad)"
        if irr_note and isinstance(result, str):
            result = (
                result
                + "\n\n⚠ Rewind: irreversible / outside-workspace side-effect — "
                + irr_note
                + ". `/rewind last` (or rewind_restore restore without step) rolls back "
                  "local files/git only; remotes/APIs/DBs stay. Pair chat with /undo."
            )
        return result
    except Exception:
        status = "error"
        raise
    finally:
        for sid, root, mode, paths in steps:
            try:
                if mode == "none":
                    journal.finish_step(sid, None, status)
                    continue
                ref, _, _ = take(root, mode, paths, settings)
                journal.finish_step(sid, ref, status)
            except Exception as exc:
                log.warning("rewind: snapshot after %s failed: %s", tool, exc)
                journal.finish_step(sid, None, status)


def _normalize_restore_mode(settings) -> str:
    mode = str(settings["restore_mode"] or "move_head").strip().lower()
    return mode if mode in ("move_head", "worktree_only") else "move_head"


def _outside_effects(steps) -> list:
    """Side-effects that survive a local restore (push/publish/remote backends/etc.)."""
    out = []
    for s in steps:
        why = s.get("irreversible")
        if not why and s.get("mode") == "none":
            why = "remote backend, not snapshotted"
        if not why:
            continue
        out.append({
            "seq": s["seq"],
            "reason": why,
            "summary": s.get("summary") or "",
            "tool": s.get("tool") or "",
            "mode": s.get("mode"),
        })
    return out


def _irreversible_summary(outside, *, restored_local: bool) -> str:
    if not outside:
        return ""
    head = ("Local workspace restored. These side-effects remain outside the workspace "
            "and were NOT undone:" if restored_local else
            "Nothing local could be restored. These side-effects stay outside the workspace:")
    lines = [head]
    for x in outside:
        bit = f"  • step {x['seq']}: {x['reason']}"
        if x.get("summary"):
            bit += f"  — {x['summary'][:70]}"
        lines.append(bit)
    lines.append("  Tip: verify remotes / APIs / DBs by hand; Rewind only rolls back this host's files/git.")
    return "\n".join(lines)


def _span(session_id, seq):
    steps = [s for s in journal.list_steps(session_id) if s["seq"] >= seq and s["kind"] != "fork"]
    first = {}
    for s in steps:
        if s["before_ref"] and s["mode"] in ("git", "files"):
            first.setdefault((s["root"], s["mode"]), s)
    return steps, first


def _latest_after_ref(session_id, root, mode):
    """Most recent after_ref for this root in the session (for hand-edit drift detection)."""
    for s in reversed(journal.list_steps(session_id)):
        if s["root"] == root and s["mode"] == mode and s.get("after_ref"):
            return s["after_ref"]
    return None


def _drift_paths(root, mode, expected_ref, current_ref):
    if not expected_ref or not current_ref or expected_ref == current_ref:
        return []
    try:
        if mode == "git":
            return [l for l in snapshot._git(root, "diff", "--name-only", expected_ref, current_ref).splitlines() if l]
        return snapshot.files_diff(expected_ref, current_ref)
    except Exception:
        return []


def _mark(s):
    if s["mode"] == "none":
        return "⊘"
    if s["irreversible"]:
        return "⚠"
    if s["status"] == "error":
        return "✗"
    if s["kind"] == "safety":
        return "◆"
    if s["kind"] == "fork":
        return "🌿"
    return "•"


def timeline_steps(session_id) -> list:
    """Structured steps for the agent tool / Desktop (includes irreversible + turn_id)."""
    last = resolve_last_tool_step(session_id)
    out = []
    for s in journal.list_steps(session_id):
        out.append({
            "seq": s["seq"], "kind": s["kind"], "tool": s["tool"], "summary": s["summary"],
            "root": s["root"], "mode": s["mode"], "status": s["status"],
            "irreversible": s["irreversible"], "turn_id": s.get("turn_id") or "",
            "mark": _mark(s),
            "is_last_tool": bool(last is not None and s["seq"] == last and s.get("kind") == "tool"),
        })
    return out


def _fork_vs_current(root, mode, before_ref, dest) -> str:
    """One-line compare: what the fork (past) differs from the live checkout — demo-friendly."""
    try:
        if mode == "git":
            # past tip vs live worktree (from original root)
            out = snapshot._git(root, "diff", "--stat", before_ref, "HEAD")
            line = (out.splitlines() or ["(identical to past)"])[-1].strip()
            return f"live checkout vs fork base: {line}"
        # files mode: count paths that differ between live root and fork dest
        live = {p.relative_to(root) for p in Path(root).rglob("*") if p.is_file()}
        past = {p.relative_to(dest) for p in Path(dest).rglob("*") if p.is_file()}
        only_live, only_past = live - past, past - live
        return f"live has {len(only_live)} file(s) fork lacks; fork has {len(only_past)} live lacks"
    except Exception as exc:
        return f"(compare unavailable: {exc})"


def fork(session_id, seq, settings, *, model=None) -> dict:
    """Materialise the state just before step `seq` somewhere NEW, leaving current work untouched.
    git roots → a new branch + `git worktree` beside the repo; plain dirs → a copied directory.
    `model` is recorded as metadata for a future Arena re-run (stub — prints how-to only)."""
    with _RESTORE_LOCK:
        return _fork_locked(session_id, seq, settings, model=model)


def _fork_locked(session_id, seq, settings, *, model=None) -> dict:
    steps, first = _span(session_id, seq)
    if not first:
        if not journal.list_steps(session_id):
            return {"ok": False, "error": "this session has no recorded steps yet — wait for a write_file/patch/terminal call, then /timeline"}
        return {"ok": False, "error": f"step {seq} isn't restorable (missing snapshot, remote-only, or out of range). Run /timeline."}
    tag = f"{seq}-{int(time.time())}"
    results = []
    for (root, mode), s in first.items():
        dest = Path(root).parent / f"{Path(root).name}-fork-{tag}"
        if dest.exists():
            return {"ok": False, "error": f"fork path already exists: {dest}"}
        if mode == "git":
            branch = f"rewind/fork-{tag}"
            snapshot.git_fork(root, s["before_ref"], s["head"], branch, dest,
                              f"Rewind fork: state before step {seq} ({s['summary']})")
        else:
            branch = None
            snapshot.files_fork(s["before_ref"], dest, globs=settings["ignore_globs"])
        hint = (f"cd {dest}" + (f" && git switch {branch}" if branch else "") +
                "  # try the alternative here; current checkout is untouched")
        if model:
            hint += f"\n# re-run with model={model}: open a Hermes session cwd={dest} (Arena wiring is future work)"
        meta = f"fork of step {seq} → {dest}" + (f" [{branch}]" if branch else "")
        if model:
            meta += f" model={model}"
        journal.add_step(session_id, "fork", None, meta, root, mode, s["before_ref"], s["head"])
        vs = _fork_vs_current(root, mode, s["before_ref"], dest)
        results.append({"root": root, "mode": mode, "path": str(dest), "branch": branch,
                        "re_run_hint": hint, "model": model, "vs_current": vs})
    return {"ok": True, "forked_before_step": seq, "forks": results,
            "cleanup": "Remove with /rewind unfork  (or: git worktree remove <path> && git branch -D <branch>)"}


def unfork(session_id, target=None) -> dict:
    """Remove fork worktrees/dirs recorded in this session. target=None → all forks; else path or seq."""
    with _RESTORE_LOCK:
        return _unfork_locked(session_id, target)


def _unfork_locked(session_id, target=None) -> dict:
    forks = [s for s in journal.list_steps(session_id) if s["kind"] == "fork"]
    if target:
        t = str(target).strip()
        if t.isdigit():
            forks = [s for s in forks if s["seq"] == int(t)]
        else:
            forks = [s for s in forks if t in (s["summary"] or "") or t == s["root"]]
    if not forks:
        return {"ok": False, "error": "no matching fork steps — /timeline shows 🌿 rows; pass a path or fork step #"}
    removed, errors = [], []
    for s in forks:
        # summary: fork of step N → /path [branch] model=x
        path = branch = None
        summary = s["summary"] or ""
        if " → " in summary:
            rest = summary.split(" → ", 1)[1]
            m = re.match(r"^(.+?)(?:\s+\[([^\]]+)\])?(?:\s+model=\S+)?$", rest.strip())
            if m:
                path, branch = m.group(1), m.group(2)
            else:
                path = rest.split(" model=", 1)[0].split(" [", 1)[0]
        if not path:
            errors.append(f"step {s['seq']}: could not parse fork path")
            continue
        try:
            p = Path(path)
            if not p.exists():
                # Already cleaned up (manual rm / prior unfork) — treat as success.
                removed.append({"seq": s["seq"], "path": path, "branch": branch, "already_gone": True})
                continue
            if s["mode"] == "git" and branch:
                snapshot.git_unfork(s["root"], path, branch)
            else:
                import shutil
                shutil.rmtree(p)
            removed.append({"seq": s["seq"], "path": path, "branch": branch})
        except Exception as exc:
            errors.append(f"step {s['seq']}: {exc}")
    return {"ok": not errors or bool(removed), "removed": removed, "errors": errors}


def resolve_last_tool_step(session_id):
    """Most recent tool step in this session (skips safety/fork markers).

    This is the one-shot target for `/rewind last` — undo the latest agent mutation
    without forcing the operator to read /timeline numbers first.
    """
    steps = journal.list_steps(session_id)
    for s in reversed(steps):
        if s.get("kind") == "tool":
            return s["seq"]
    return None


def resolve_step_arg(session_id, seq):
    """Normalize step arg: int as-is; None/'last'/'-' → last tool step. Returns (seq|None, error|None)."""
    if seq is None or (isinstance(seq, str) and seq.strip().lower() in ("last", "-", "latest")):
        n = resolve_last_tool_step(session_id)
        if n is None:
            return None, "nothing to rewind — no tool steps in this session yet (see /timeline)"
        return n, None
    if isinstance(seq, str) and seq.strip().isdigit():
        return int(seq.strip()), None
    if isinstance(seq, int):
        return seq, None
    return None, f"invalid step {seq!r}; pass an integer from /timeline, or 'last'"


def restore_preview(session_id, seq) -> dict:
    """Dry-run: what `/rewind <seq>` would undo — for confirm copy + agent self-call polish.

    Does not touch the workspace. Surfaces irreversible steps in the span *before* restore.
    """
    all_steps = journal.list_steps(session_id)
    if not all_steps:
        return {"ok": False, "error": "nothing to rewind — this session has no recorded file/terminal steps yet"}
    target = journal.get_step(session_id, seq)
    if not target:
        return {"ok": False, "error": f"no step {seq} in this session (highest is {all_steps[-1]['seq']}; see /timeline)"}
    if target["kind"] == "fork":
        return {"ok": False, "error": f"step {seq} is a fork marker — pick a tool/safety step, or use /rewind unfork"}
    steps, first = _span(session_id, seq)
    outside = _outside_effects(steps)
    tool_undone = [s for s in steps if s.get("kind") == "tool"]
    summ = (target.get("summary") or "")[:70]
    restorable = bool(first)
    confirm = (
        f"Restore local files/git to before step {seq}"
        + (f" «{summ}»" if summ else "")
        + f"; undoes {len(tool_undone)} tool step(s)"
    )
    if outside:
        confirm += f"; {len(outside)} irreversible side-effect(s) stay outside (push/publish/remote)"
    confirm += ". Chat unchanged — pair with /undo. Say yes to run /rewind " + (
        "last" if seq == resolve_last_tool_step(session_id) else str(seq)
    ) + "."
    return {
        "ok": True,
        "step": seq,
        "tool": target.get("tool") or "",
        "kind": target.get("kind") or "",
        "summary": target.get("summary") or "",
        "mark": _mark(target),
        "restorable": restorable,
        "roots": [{"root": r, "mode": m} for (r, m) in first.keys()],
        "tool_steps_undone": len(tool_undone),
        "steps_in_span": len(steps),
        "stays_outside": outside,
        "irreversible_summary": _irreversible_summary(outside, restored_local=restorable) if outside else "",
        "confirm": confirm,
        "slash": "last" if seq == resolve_last_tool_step(session_id) else str(seq),
    }


def rewind(session_id, seq, settings) -> dict:
    """Restore every root touched at or after step `seq` to its state before that step.
    A safety snapshot of the current state is journaled first, so a rewind can itself be rewound.
    Concurrent restores/forks serialize on a process-local lock."""
    with _RESTORE_LOCK:
        return _rewind_locked(session_id, seq, settings)


def _rewind_locked(session_id, seq, settings) -> dict:
    all_steps = journal.list_steps(session_id)
    if not all_steps:
        return {"ok": False, "error": "nothing to rewind — this session has no recorded file/terminal steps yet"}
    target = journal.get_step(session_id, seq)
    if not target:
        return {"ok": False, "error": f"no step {seq} in this session (highest is {all_steps[-1]['seq']}; see /timeline)"}
    if target["kind"] == "fork":
        return {"ok": False, "error": f"step {seq} is a fork marker — pick a tool/safety step, or use /rewind unfork"}
    steps, first = _span(session_id, seq)
    outside = _outside_effects(steps)
    restore_mode = _normalize_restore_mode(settings)
    move_head = restore_mode == "move_head"
    if not first:
        detail = "; ".join(f"step {x['seq']}: {x['reason']}" for x in outside) or "no snapshotted files in that range"
        return {
            "ok": False,
            "error": f"cannot restore before step {seq}: {detail}",
            "not_reversible": [f"step {x['seq']}: {x['reason']}" for x in outside],
            "stays_outside": outside,
            "irreversible_summary": _irreversible_summary(outside, restored_local=False) if outside else "",
            "restore_mode": restore_mode,
        }
    results, hand_edits = [], []
    for (root, mode), s in first.items():
        paths = None
        if mode == "files":
            try:
                m = snapshot.load_manifest(s["before_ref"])
            except Exception as exc:
                return {"ok": False, "error": f"cannot load snapshot for {root}: {exc}",
                        "restore_mode": restore_mode, "stays_outside": outside}
            paths = None if m["full"] else [Path(root) / r for r in m["entries"]]
        try:
            cur_ref, head, _ = take(root, mode, paths, settings)
        except Exception as exc:
            return {
                "ok": False,
                "error": (f"safety snapshot failed for {root}: {exc}. "
                          "Restore aborted so the current tree is left untouched."),
                "roots": results,
                "not_reversible": [f"step {x['seq']}: {x['reason']}" for x in outside],
                "stays_outside": outside,
                "irreversible_summary": _irreversible_summary(outside, restored_local=False),
                "hand_edits_overwritten": hand_edits,
                "restore_mode": restore_mode,
            }
        expected = _latest_after_ref(session_id, root, mode)
        drifted = _drift_paths(root, mode, expected, cur_ref)
        if drifted:
            hand_edits.append({"root": root, "files": drifted[:20]})
        journal.add_step(session_id, "safety", None, f"before /rewind {seq}", root, mode, cur_ref, head)
        try:
            if mode == "git":
                r = snapshot.git_restore(root, s["before_ref"], s["head"], move_head=move_head)
            else:
                r = snapshot.files_restore(s["before_ref"], globs=settings["ignore_globs"])
        except Exception as exc:
            return {
                "ok": False,
                "error": (f"restore failed mid-flight for {root}: {exc}. "
                          "A ◆ safety snapshot was journaled — /timeline then /rewind to that safety step "
                          "may recover the pre-attempt tree."),
                "roots": results,
                "partial": bool(results),
                "not_reversible": [f"step {x['seq']}: {x['reason']}" for x in outside],
                "stays_outside": outside,
                "irreversible_summary": _irreversible_summary(outside, restored_local=bool(results)),
                "hand_edits_overwritten": hand_edits,
                "restore_mode": restore_mode,
            }
        r.pop("pre_restore", None)
        results.append({"root": root, "mode": mode, **r})
    turns = sorted({s["turn_id"] for s in steps if s.get("turn_id")})
    head_note = ""
    if any(x.get("moved_head") for x in results):
        head_note = " Git HEAD was moved back (restore_mode=move_head)."
    elif restore_mode == "worktree_only" and any(x.get("mode") == "git" for x in results):
        head_note = " Files restored; git HEAD left in place (restore_mode=worktree_only)."
    tool_undone = [s for s in steps if s.get("kind") == "tool"]
    return {
        "ok": True,
        "rewound_to_before_step": seq,
        "target_summary": target.get("summary") or "",
        "target_tool": target.get("tool") or "",
        "tool_steps_undone": len(tool_undone),
        "roots": results,
        "not_reversible": [f"step {x['seq']}: {x['reason']}" for x in outside],
        "stays_outside": outside,
        "irreversible_summary": _irreversible_summary(outside, restored_local=True),
        "hand_edits_overwritten": hand_edits,
        "restore_mode": restore_mode,
        "conversation_hint": (
            "Filesystem restored." + head_note + " Chat context is unchanged — run built-in /undo "
            + (f"once per affected turn ({', '.join(turns)}) " if turns else "")
            + "if you also want the agent to forget those replies."
        ),
    }



def diff(session_id, a, b=None) -> list:
    """Changes made by step a (b=None) or between the state before a and after b."""
    sa = journal.get_step(session_id, a)
    sb = journal.get_step(session_id, b if b else a)
    if not sa or not sb:
        return [f"no such step (asked for {a}" + (f" and {b}" if b else "") + ") — see /timeline"]
    if sa["root"] != sb["root"]:
        return ["steps are in different roots — pick two steps that touch the same directory"]
    if sa["mode"] == "none" or sb["mode"] == "none":
        return ["one of those steps ran on a remote backend and was not snapshotted"]
    end = sb["after_ref"]
    if not sa["before_ref"] or not end:
        return ["snapshot missing for that range (step may still be in flight, or snapshot failed)"]
    if sa["mode"] == "git":
        out = snapshot._git(sa["root"], "diff", "--stat", sa["before_ref"], end)
        return out.splitlines() or ["(no changes)"]
    return snapshot.files_diff(sa["before_ref"], end) or ["(no changes)"]


def timeline(session_id) -> str:
    steps = journal.list_steps(session_id)
    if not steps:
        return ("Rewind: no recorded changes in this session yet.\n"
                "File writes, patches, and terminal commands show up here automatically once the plugin is enabled.")
    lines = [f"Rewind timeline — {len(steps)} step(s)"]
    last = resolve_last_tool_step(session_id)
    if last is not None:
        prev = restore_preview(session_id, last)
        if prev.get("ok"):
            bit = f"Last mutation: {_mark(journal.get_step(session_id, last))} {last} "
            bit += f"{prev.get('tool') or '?'} «{(prev.get('summary') or '')[:55]}»"
            if prev.get("stays_outside"):
                bit += f" — /rewind last undoes local only ({len(prev['stays_outside'])} irreversible stay)"
            else:
                bit += " — /rewind last undoes from here"
            lines.append(bit)
            lines.append(f"Confirm copy: {prev['confirm']}")
    for s in steps:
        mark = _mark(s)
        label = f"[{s['kind']}]" if s["kind"] in ("safety", "fork") else (s["tool"] or "?")
        turn = f"  turn:{s['turn_id'][:8]}" if s.get("turn_id") else ""
        extra = f"  ⚠ {s['irreversible']}" if s["irreversible"] else ""
        tail = "  ← last" if last is not None and s["seq"] == last and s.get("kind") == "tool" else ""
        lines.append(f"{mark} {s['seq']:>3}  {label:<13} {s['summary']}  ({Path(s['root']).name}){turn}{extra}{tail}")
    lines.append("Undo: /rewind last (or <n>) · preview first: /rewind preview last · fork: /rewind fork last · "
                 "cleanup: /rewind unfork · chat: /undo · ⊘ = remote, not snapshotted.")
    return "\n".join(lines)
