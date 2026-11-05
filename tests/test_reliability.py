"""Edge cases: safety abort, unfork-after-rewind, concurrent restore serialization, empty timeline."""
import json
import threading
from pathlib import Path
from unittest import mock

from test_rewind import git, make_repo, run_tool


def test_restore_aborts_when_safety_snapshot_fails(ctx, tmp_path, monkeypatch):
    d = make_repo(tmp_path)
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("Z\n"))
    assert (d / "app.py").read_text() == "Z\n"
    from rewind_plugin import core
    calls = {"n": 0}
    real_take = core.take

    def boom(root, mode, paths, settings):
        calls["n"] += 1
        if calls["n"] == 1:  # safety snapshot inside rewind
            raise RuntimeError("disk full")
        return real_take(root, mode, paths, settings)

    monkeypatch.setattr(core, "take", boom)
    out = json.loads(ctx.tools["rewind_restore"]({"action": "restore", "step": 1}, session_id="s1"))
    assert out["ok"] is False and "safety snapshot failed" in out["error"]
    # workspace untouched
    assert (d / "app.py").read_text() == "Z\n"


def test_unfork_after_rewind(ctx, tmp_path):
    d = make_repo(tmp_path)
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("v2\n"))
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("v3\n"))
    assert "Forked" in ctx.cmds["rewind"]("fork 2", session_id="s1")
    forks = list(tmp_path.glob("repo-fork-2-*"))
    assert forks and forks[0].exists()
    assert "Restored" in ctx.cmds["rewind"]("2", session_id="s1")
    # fork worktree still on disk; journal still has 🌿 row
    assert forks[0].exists()
    u = ctx.cmds["rewind"]("unfork", session_id="s1")
    assert "Removed" in u
    assert not forks[0].exists()


def test_concurrent_restores_serialize(ctx, tmp_path):
    d = make_repo(tmp_path)
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("A\n"))
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("B\n"))
    results = []

    def go(step):
        results.append(ctx.tools["rewind_restore"]({"action": "restore", "step": step}, session_id="s1"))

    t1 = threading.Thread(target=go, args=(2,))
    t2 = threading.Thread(target=go, args=(1,))
    t1.start(); t2.start(); t1.join(); t2.join()
    parsed = [json.loads(r) for r in results]
    assert all("ok" in p for p in parsed)
    # Final tree is consistent (one of the two restore targets), not a torn mix
    text = (d / "app.py").read_text()
    assert text in ("print('v1')\n", "A\n")


def test_empty_timeline_tool_and_slash(ctx):
    listed = json.loads(ctx.tools["rewind_restore"]({"action": "list"}, session_id="brand-new"))
    assert listed["count"] == 0
    assert "no recorded" in listed["timeline"].lower()
    assert "no recorded" in ctx.cmds["timeline"]("", session_id="brand-new").lower()
    bad = json.loads(ctx.tools["rewind_restore"]({"action": "fork", "step": 1}, session_id="brand-new"))
    assert bad["ok"] is False and "no recorded" in bad["error"].lower()


def test_irreversible_privileged_and_root_delete(ctx, tmp_path):
    from rewind_plugin.core import irreversible_reason
    assert irreversible_reason("su" + "do make install")
    assert irreversible_reason("rm -rf /")
    assert irreversible_reason("rm -rf ~")
    d = make_repo(tmp_path)
    run_tool(ctx, "terminal", {"command": "sudo true", "workdir": str(d)}, lambda: None)
    tl = ctx.cmds["timeline"]("", session_id="s1")
    assert "⚠" in tl


def test_restore_across_push_summarizes_what_stays(ctx, tmp_path):
    d = make_repo(tmp_path)
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("local\n"))
    run_tool(ctx, "terminal", {"command": "git push origin main", "workdir": str(d)}, lambda: None)
    out = json.loads(ctx.tools["rewind_restore"]({"action": "restore", "step": 1}, session_id="s1"))
    assert out["ok"] is True
    assert out.get("stays_outside") and any("push" in x["reason"] for x in out["stays_outside"])
    assert "NOT undone" in (out.get("irreversible_summary") or "")
    assert (d / "app.py").read_text() == "print('v1')\n"
    text = ctx.cmds["rewind"]("1", session_id="s1")  # already rewound; still formats
    # fresh session narrative via formatted restore already covered; check slash on new push span
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("again\n"))
    run_tool(ctx, "terminal", {"command": "git push origin main", "workdir": str(d)}, lambda: None)
    # find push step seq from timeline
    listed = json.loads(ctx.tools["rewind_restore"]({"action": "list"}, session_id="s1"))
    push_seq = next(s["seq"] for s in listed["steps"] if s.get("irreversible") and "push" in (s.get("irreversible") or ""))
    # restore from the write before latest push — use the write_file seq just before that push
    write_seq = push_seq - 1
    slash = ctx.cmds["rewind"](str(write_seq), session_id="s1")
    assert "NOT undone" in slash or "outside the workspace" in slash
    assert "git push" in slash.lower() or "push" in slash.lower()


def test_worktree_only_restores_files_without_moving_head(ctx, tmp_path, monkeypatch):
    from rewind_plugin import core
    d = make_repo(tmp_path)
    head0 = git(d, "rev-parse", "HEAD")
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("v2\n"))
    # commit so HEAD moves forward relative to snapshot head pinned on the write step
    git(d, "add", "-A")
    git(d, "commit", "-qm", "agent commit")
    head1 = git(d, "rev-parse", "HEAD")
    assert head1 != head0
    # force worktree_only via settings getter on a fresh Settings used by rewind
    s = core.Settings(lambda k, dflt: "worktree_only" if k == "restore_mode" else dflt)
    r = core.rewind("s1", 1, s)
    assert r["ok"] and r["restore_mode"] == "worktree_only"
    assert (d / "app.py").read_text() == "print('v1')\n"
    assert git(d, "rev-parse", "HEAD") == head1  # branch tip unchanged
    assert any(x.get("moved_head") is False for x in r["roots"])


def test_move_head_default_still_resets_branch(ctx, tmp_path):
    d = make_repo(tmp_path)
    head0 = git(d, "rev-parse", "HEAD")
    run_tool(ctx, "terminal",
             {"command": "echo x >> app.py && git add -A && git commit -qm c", "workdir": str(d)},
             lambda: (
                 (d / "app.py").write_text((d / "app.py").read_text() + "x\n"),
                 git(d, "add", "-A"),
                 git(d, "commit", "-qm", "c"),
             ))
    head1 = git(d, "rev-parse", "HEAD")
    assert head1 != head0
    out = json.loads(ctx.tools["rewind_restore"]({"action": "restore", "step": 1}, session_id="s1"))
    assert out["ok"] and out.get("restore_mode") == "move_head"
    assert any(r.get("moved_head") for r in out["roots"])
    assert git(d, "rev-parse", "HEAD") == head0


def test_exec_in_home_still_journaled(ctx, tmp_path, monkeypatch):
    """cwd=home is too broad to snapshot, but /timeline must not stay silent."""
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Force default_cwd / workdir to the fake home
    run_tool(ctx, "terminal", {"command": "echo hi", "workdir": str(home)}, lambda: None)
    listed = json.loads(ctx.tools["rewind_restore"]({"action": "list"}, session_id="s1"))
    assert listed["count"] == 1
    step = listed["steps"][0]
    assert step["mode"] == "none"
    assert "not snapshotted" in (step.get("irreversible") or "")
    tl = ctx.cmds["timeline"]("", session_id="s1")
    assert "⊘" in tl


def test_unfork_idempotent_when_path_already_gone(ctx, tmp_path):
    d = make_repo(tmp_path)
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("x\n"))
    assert "Forked" in ctx.cmds["rewind"]("fork 1", session_id="s1")
    forks = list(tmp_path.glob("repo-fork-1-*"))
    assert forks
    import shutil
    shutil.rmtree(forks[0])
    u = ctx.cmds["rewind"]("unfork", session_id="s1")
    assert "Removed" in u
    # second unfork: journal still has fork row; path gone → still ok
    u2 = ctx.cmds["rewind"]("unfork", session_id="s1")
    assert "Removed" in u2



def test_rewind_last_undoes_latest_tool_step(ctx, tmp_path):
    d = make_repo(tmp_path)
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("A\n"))
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("B\n"))
    out = ctx.cmds["rewind"]("last", session_id="s1")
    assert "Restored" in out and "step 2" in out, out
    assert (d / "app.py").read_text() == "A\n"
    # tool: omit step → last
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("C\n"), sid="s1")
    r = json.loads(ctx.tools["rewind_restore"]({"action": "restore"}, session_id="s1"))
    assert r["ok"] is True
    assert (d / "app.py").read_text() == "A\n"  # undid C; prior rewind's world was A


def test_irreversible_appended_to_tool_result(ctx, tmp_path):
    d = make_repo(tmp_path)
    def next_call(a):
        return json.dumps({"ok": True, "stdout": "pushed"})
    raw = ctx.mw["tool_execution"](
        tool_name="terminal",
        args={"command": "git push origin main", "workdir": str(d)},
        next_call=next_call,
        session_id="s1",
    )
    assert "⚠ Rewind" in raw and "irreversible" in raw.lower()
    assert "/rewind last" in raw


def test_fork_last_includes_vs_current(ctx, tmp_path):
    d = make_repo(tmp_path)
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("X\n"))
    msg = ctx.cmds["rewind"]("fork last", session_id="s1")
    assert "Forked" in msg and "compare:" in msg, msg


def test_preview_and_timeline_last_marker(ctx, tmp_path):
    d = make_repo(tmp_path)
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("A\n"))
    run_tool(ctx, "terminal", {"command": "git push origin main", "workdir": str(d)}, lambda: None)
    tl = ctx.cmds["timeline"]("", session_id="s1")
    assert "Last mutation:" in tl and "← last" in tl
    assert "Confirm copy:" in tl
    prev = ctx.cmds["rewind"]("preview last", session_id="s1")
    assert "Preview" in prev and "no changes made" in prev
    assert "irreversible" in prev.lower()
    # workspace untouched by preview
    assert (d / "app.py").read_text() == "A\n"
    listed = json.loads(ctx.tools["rewind_restore"]({"action": "list"}, session_id="s1"))
    assert listed["suggested"] and listed["suggested"]["ok"]
    assert "confirm" in listed["suggested"]
    assert any(s.get("is_last_tool") for s in listed["steps"])
    # restore message leads with irreversible when crossing push
    out = ctx.cmds["rewind"]("1", session_id="s1")
    assert "Restored" in out and "Crossed" in out and "irreversible" in out.lower()
