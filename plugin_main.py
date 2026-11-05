"""Rewind — undo for agents. Registration only; logic lives in core.py."""
import json
import logging
import os
import time

from . import commands, core
from . import gc as gc_mod

log = logging.getLogger(__name__)

REWIND_RESTORE = {
    "name": "rewind_restore",
    "description": (
        "Undo or branch off THIS session's earlier file/terminal changes. "
        "ALWAYS call action='list' first — each step shows mark ⚠ when irreversible (push/publish/DB/infra), "
        "⊘ when remote (not snapshotted), ✗ when the tool failed. "
        "action='restore' overwrites files and (by default) moves git HEAD back to before `step` "
        "(config restore_mode=worktree_only keeps the branch tip; confirm with the user; "
        "does NOT undo chat — pair with /undo; does NOT undo pushes/publishes — see irreversible_summary). "
        "action='fork' leaves current work untouched and creates a sibling worktree/dir. "
        "action='unfork' removes a previous fork. Prefer restore only when the user wants to discard later work."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "restore", "fork", "unfork"],
                "description": (
                    "list = numbered timeline with irreversible markers; "
                    "restore = overwrite workspace to before `step` (destructive — confirm with user); "
                    "fork = copy past state elsewhere (safe); "
                    "unfork = delete a fork worktree/dir"
                ),
            },
            "step": {
                "type": "integer",
                "description": "Timeline step number (required for restore/fork; optional for unfork)",
            },
            "model": {
                "type": "string",
                "description": "Optional label recorded on forks for a future Arena re-run (stub; prints how-to)",
            },
            "path": {
                "type": "string",
                "description": "For unfork: fork path (or omit with step / omit both to remove all forks)",
            },
        },
        "required": ["action"],
    },
}


def register(ctx):
    settings = core.Settings(getattr(ctx, "get_config", None))

    def tool_execution(tool_name=None, args=None, next_call=None, **kw):
        _debug_payload(tool_name, args, kw)
        sid = kw.get("session_id") or kw.get("task_id") or "default"
        return core.capture(
            tool_name, args or {}, next_call, sid, settings,
            task_id=kw.get("task_id"), turn_id=kw.get("turn_id"),
        )

    def rewind_restore(args, **kw):
        try:
            sid = kw.get("session_id") or kw.get("task_id") or "default"
            action = args.get("action")
            if action == "list":
                steps = core.timeline_steps(sid)
                return json.dumps({
                    "count": len(steps),
                    "steps": steps,
                    "timeline": core.timeline(sid),
                    "hint": "⚠ = irreversible side effects; ⊘ = remote backend (cannot restore); "
                            "restore overwrites files — confirm with the user first. "
                            "Chat is separate: use built-in /undo for conversation.",
                })
            if action == "unfork":
                target = args.get("path") or args.get("step")
                return json.dumps(core.unfork(sid, target))
            step = args.get("step")
            if not isinstance(step, int):
                return json.dumps({"error": "step (integer from action=list) is required for restore/fork"})
            if action == "fork":
                return json.dumps(core.fork(sid, step, settings, model=args.get("model")))
            if action == "restore":
                return json.dumps(core.rewind(sid, step, settings))
            return json.dumps({"error": f"unknown action {action!r}; use list|restore|fork|unfork"})
        except Exception as exc:
            return json.dumps({"error": f"rewind failed: {exc}"})

    ctx.register_middleware("tool_execution", tool_execution)
    ctx.register_tool(name="rewind_restore", toolset="rewind", schema=REWIND_RESTORE, handler=rewind_restore)
    t, r, d = commands.make(settings)
    ctx.register_command("timeline", t, description="Show Rewind's timeline of file/terminal changes")
    ctx.register_command(
        "rewind", r,
        description="Restore files+git before step N; fork / unfork / gc",
        args_hint="<step> | fork <step> [--model NAME] | unfork [path|step] | gc",
    )
    ctx.register_command("rewind-diff", d, description="Show what a step changed", args_hint="<step> [to]")
    if hasattr(ctx, "register_cli_command"):
        ctx.register_cli_command(name="rewind", help="Inspect or garbage-collect Rewind snapshots",
                                 setup_fn=_cli_setup, handler_fn=lambda a: _cli(a, settings),
                                 description="hermes rewind timeline [session] | hermes rewind gc [--all]")
    try:  # cheap retention pass at load; never fatal
        gc_mod.gc(settings)
    except Exception as exc:
        log.debug("rewind: startup gc skipped: %s", exc)


def _debug_payload(tool_name, args, kw):
    """Opt-in (REWIND_DEBUG_PAYLOAD=<file>): record the middleware call shape: field names + ids only."""
    path = os.environ.get("REWIND_DEBUG_PAYLOAD")
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(json.dumps({"t": time.time(), "tool_name": tool_name, "arg_keys": sorted((args or {}).keys()),
                                "kwargs": {k: (v if isinstance(v, str) else type(v).__name__)
                                           for k, v in kw.items()}}) + "\n")
    except Exception:
        pass


def _cli_setup(p):
    sub = p.add_subparsers(dest="rewind_cmd")
    t = sub.add_parser("timeline", help="Show a session's timeline (default: most recent)")
    t.add_argument("session", nargs="?")
    g = sub.add_parser("gc", help="Apply retention; --all drops everything")
    g.add_argument("--all", action="store_true")


def _cli(a, settings):
    from . import journal
    if getattr(a, "rewind_cmd", None) == "gc":
        print(json.dumps(gc_mod.gc(settings, everything=a.all)))
    else:
        print(core.timeline(getattr(a, "session", None) or journal.latest_session() or "default"))
