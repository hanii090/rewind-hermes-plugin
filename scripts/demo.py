#!/usr/bin/env python3
"""One-command ~75s Rewind story — no LLM, no Hermes install required.

Recreates: write → rm+commit → timeline → fork → rewind → unfork → diff.

    python3 scripts/demo.py                 # uses a temp dir, cleans up
    python3 scripts/demo.py /tmp/demo-repo  # keep the playground
    python3 scripts/demo.py --keep /tmp/x   # always keep

Exit 0 only if every beat passes. Stdlib + local plugin package only.
Prints a judge-facing SCOREBOARD at the end.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT))


def git(d: Path, *a: str) -> str:
    return subprocess.run(
        ["git", "-C", str(d), *a], check=True, capture_output=True, text=True
    ).stdout.strip()


def say(title: str, body: str = "") -> None:
    print(f"\n══ {title} ══")
    if body:
        print(body)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", nargs="?", help="playground path (default: temp dir)")
    ap.add_argument("--keep", action="store_true", help="do not delete the playground on success")
    args = ap.parse_args()

    keep = args.keep or bool(args.repo)
    playground = Path(args.repo) if args.repo else Path(tempfile.mkdtemp(prefix="rewind-demo-"))
    data = Path(tempfile.mkdtemp(prefix="rewind-demo-data-"))
    os.environ["REWIND_DATA_DIR"] = str(data)

    board = {
        "steps_captured": 0,
        "irreversible_flagged": 0,
        "fork_path": "",
        "hand_edit_warning": False,
        "undo_hint": False,
        "unfork_ok": False,
        "legacy_restored": False,
        "empty_session_ok": False,
        "irreversible_summary": False,
        "moved_head": False,
        "irr_footer": False,
        "fork_compare": False,
        "rewind_last": False,
        "preview_ok": False,
    }

    import importlib.util
    for name in list(sys.modules):
        if name == "rewind_plugin" or name.startswith("rewind_plugin."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        "rewind_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rewind_plugin"] = mod
    spec.loader.exec_module(mod)
    from rewind_plugin import journal
    journal.reset()

    class Ctx:
        def __init__(self):
            self.tools, self.mw, self.cmds = {}, {}, {}
        def get_config(self, key, default=None):
            return default
        def register_middleware(self, kind, fn): self.mw[kind] = fn
        def register_tool(self, name, toolset, schema, handler, **kw): self.tools[name] = handler
        def register_command(self, name, handler, description="", args_hint=""): self.cmds[name] = handler
        def register_hook(self, *a, **k): pass

    ctx = Ctx()
    mod.register(ctx)
    SID = "demo"

    def run(tool, args, effect, turn="t1"):
        def next_call(a):
            effect()
            return json.dumps({"ok": True})
        return ctx.mw["tool_execution"](
            tool_name=tool, args=args, next_call=next_call, session_id=SID, turn_id=turn
        )

    if playground.exists() and any(playground.iterdir()):
        say("FAIL", f"{playground} is not empty — refuse to clobber")
        return 2
    playground.mkdir(parents=True, exist_ok=True)
    git(playground, "init", "-q")
    git(playground, "config", "user.email", "demo@rewind.local")
    git(playground, "config", "user.name", "Rewind Demo")
    (playground / "app.py").write_text("print('v1')\n")
    (playground / "legacy").mkdir()
    (playground / "legacy" / "old.py").write_text("x=1\n")
    git(playground, "add", "-A")
    git(playground, "commit", "-qm", "init")
    head0 = git(playground, "rev-parse", "--short", "HEAD")

    say("1. Agent rewrites app.py", f"repo={playground}  HEAD={head0}")
    run("write_file", {"path": str(playground / "app.py")},
        lambda: (playground / "app.py").write_text("print('v2-broken')\n"), turn="turn-write")
    assert (playground / "app.py").read_text() == "print('v2-broken')\n"

    say("2. Agent deletes legacy/ and commits")
    def wipe():
        shutil.rmtree(playground / "legacy")
        git(playground, "add", "-A")
        git(playground, "commit", "-qm", "agent: drop legacy")
    run("terminal",
        {"command": "rm -r legacy && git add -A && git commit -qm agent", "workdir": str(playground)},
        wipe, turn="turn-rm")
    assert not (playground / "legacy").exists()

    say("3. /rewind-diff 2 — what the delete changed")
    diff = ctx.cmds["rewind-diff"]("2", session_id=SID)
    print(diff)
    assert "legacy" in diff.lower() or "old.py" in diff

    say("4. /rewind fork last --model demo-gpt  (alt past + vs_current; last=delete)")
    fork_out = ctx.cmds["rewind"]("fork last --model demo-gpt", session_id=SID)
    print(fork_out)
    assert "Forked" in fork_out and "compare:" in fork_out
    forks = list(playground.parent.glob(f"{playground.name}-fork-*-*"))
    assert forks, "expected a sibling fork worktree"
    fork = forks[0]
    board["fork_path"] = str(fork)
    board["fork_compare"] = "compare:" in fork_out
    assert (fork / "app.py").read_text() == "print('v2-broken')\n"
    assert (fork / "legacy" / "old.py").exists()
    assert not (playground / "legacy").exists()

    say("5. Flag an irreversible push (footer on tool result)")
    def push_call(a):
        return json.dumps({"ok": True, "stdout": "pushed (simulated)"})
    push_raw = ctx.mw["tool_execution"](
        tool_name="terminal",
        args={"command": "git push origin main", "workdir": str(playground)},
        next_call=push_call, session_id=SID, turn_id="turn-push",
    )
    print(push_raw)
    assert "⚠ Rewind" in push_raw and "/rewind last" in push_raw
    board["irr_footer"] = True

    say("6. /timeline (⚠/turn markers)")
    tl = ctx.cmds["timeline"]("", session_id=SID)
    print(tl)
    assert "write_file" in tl and "terminal" in tl
    assert "turn:" in tl
    assert "⚠" in tl
    board["steps_captured"] = sum(1 for l in tl.splitlines() if l[:1] in "•⚠✗⊘◆🌿")
    early = json.loads(ctx.tools["rewind_restore"]({"action": "list"}, session_id=SID))
    board["irreversible_flagged"] = sum(1 for s in early["steps"] if s.get("irreversible"))

    say("6b. Hand-edit drift warning path (touch app.py outside the agent)")
    (playground / "app.py").write_text("print('v2-broken')\n# I fixed it by hand\n")

    say("6c. /rewind preview 2 — confirm copy before destructive restore")
    prev = ctx.cmds["rewind"]("preview 2", session_id=SID)
    print(prev)
    assert "Preview" in prev and "no changes made" in prev
    assert "irreversible" in prev.lower() and "Run: /rewind 2" in prev
    board["preview_ok"] = True

    say("7. /rewind 2 — restore files + HEAD; note hand-edit + /undo hint")
    out = ctx.cmds["rewind"]("2", session_id=SID)
    print(out)
    assert "Restored" in out
    assert (playground / "legacy" / "old.py").read_text() == "x=1\n"
    board["legacy_restored"] = True
    assert (playground / "app.py").read_text() == "print('v2-broken')\n"
    assert "hand" in out.lower() or "Overwrote" in out
    board["hand_edit_warning"] = True
    assert "/undo" in out
    board["undo_hint"] = True
    assert "NOT undone" in out or "outside the workspace" in out
    assert "push" in out.lower()
    board["irreversible_summary"] = True
    assert "HEAD moved" in out
    board["moved_head"] = True

    say("7a. break again, then /rewind last (one-shot undo)")
    run("write_file", {"path": str(playground / "app.py")},
        lambda: (playground / "app.py").write_text("print('v3-oops')\n"), turn="turn-oops")
    assert "v3-oops" in (playground / "app.py").read_text()
    last_out = ctx.cmds["rewind"]("last", session_id=SID)
    print(last_out)
    assert "Restored" in last_out
    assert (playground / "app.py").read_text() == "print('v2-broken')\n"
    board["rewind_last"] = True

    say("7b. unfork still works after a rewind (fork row survives in journal)")
    u = ctx.cmds["rewind"]("unfork", session_id=SID)
    print(u)
    assert "Removed" in u
    assert not fork.exists()
    board["unfork_ok"] = True

    say("8. tool list shows irreversible markers + structured steps")
    listed = json.loads(ctx.tools["rewind_restore"]({"action": "list"}, session_id=SID))
    assert listed["count"] >= 2
    assert any(s.get("irreversible") for s in listed["steps"])
    assert any(s.get("turn_id") for s in listed["steps"])
    print(f"  {listed['count']} steps; irreversible={[s['seq'] for s in listed['steps'] if s.get('irreversible')]}")

    say("9. Empty-session message is human")
    empty = ctx.cmds["timeline"]("", session_id="never-used")
    assert "no recorded" in empty.lower()
    board["empty_session_ok"] = True
    print(empty.splitlines()[0])

    # SCOREBOARD
    checks = [
        ("steps_captured ≥ 3", board["steps_captured"] >= 3),
        ("irreversible flagged", board["irreversible_flagged"] >= 1),
        ("fork path created", bool(board["fork_path"])),
        ("hand-edit warning shown", board["hand_edit_warning"]),
        ("/undo conversation hint", board["undo_hint"]),
        ("legacy/ restored", board["legacy_restored"]),
        ("unfork after rewind", board["unfork_ok"]),
        ("empty session copy OK", board["empty_session_ok"]),
        ("irreversible summary (push)", board["irreversible_summary"]),
        ("HEAD moved on restore", board["moved_head"]),
        ("irr. footer on push result", board["irr_footer"]),
        ("fork vs_current line", board["fork_compare"]),
        ("/rewind last one-shot", board["rewind_last"]),
        ("preview confirm copy", board["preview_ok"]),
    ]
    print("\n╔══════════════════════════════════════════╗")
    print("║         REWIND DEMO SCOREBOARD           ║")
    print("╠══════════════════════════════════════════╣")
    for label, ok in checks:
        print(f"║  {'✓' if ok else '✗'}  {label:<36} ║")
    print("╠══════════════════════════════════════════╣")
    print(f"║  steps captured : {board['steps_captured']:<23} ║")
    print(f"║  irreversible ⚠ : {board['irreversible_flagged']:<23} ║")
    print(f"║  irr. summary   : {('yes' if board['irreversible_summary'] else 'no'):<23} ║")
    print(f"║  HEAD moved     : {('yes' if board['moved_head'] else 'no'):<23} ║")
    print(f"║  hand-edit warn : {('yes' if board['hand_edit_warning'] else 'no'):<23} ║")
    fp = board["fork_path"] or "(none)"
    if len(fp) > 36:
        fp = "…" + fp[-35:]
    print(f"║  fork path      : {fp:<23} ║")
    print("╚══════════════════════════════════════════╝")
    if not all(ok for _, ok in checks):
        print("\nDEMO FAILED: scoreboard incomplete", file=sys.stderr)
        return 1

    say("PASS", "Demo story completed. Competition-ready beats: footer → fork last → rewind last → unfork.")
    if not keep:
        shutil.rmtree(playground, ignore_errors=True)
        print(f"(cleaned {playground})")
    else:
        print(f"playground kept at {playground}")
    shutil.rmtree(data, ignore_errors=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"\nDEMO FAILED (assertion): {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"\nDEMO FAILED: {exc}", file=sys.stderr)
        raise
