import importlib.util
import json
import sys
import time
from pathlib import Path

from test_rewind import git, make_repo, run_tool

ROOT = Path(__file__).resolve().parents[1]


def _reload(monkeypatch, data_dir):
    """Load a fresh rewind_plugin bound to a specific REWIND_DATA_DIR (new instance_id)."""
    for name in list(sys.modules):
        if name == "rewind_plugin" or name.startswith("rewind_plugin."):
            del sys.modules[name]
    monkeypatch.setenv("REWIND_DATA_DIR", str(data_dir))
    spec = importlib.util.spec_from_file_location(
        "rewind_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rewind_plugin"] = mod
    spec.loader.exec_module(mod)
    mod.journal.reset()
    return mod


def test_remote_backend_not_snapshotted(ctx, tmp_path, monkeypatch):
    d = make_repo(tmp_path)
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    run_tool(ctx, "terminal", {"command": "rm -rf /app/legacy", "workdir": str(d)}, lambda: None)
    assert git(d, "for-each-ref", "refs/hermes-rewind/") == ""  # host cwd NOT snapshotted
    tl = ctx.cmds["timeline"]("", session_id="s1")
    assert "⊘" in tl and "docker" in tl
    out = json.loads(ctx.tools["rewind_restore"]({"action": "restore", "step": 1}, session_id="s1"))
    assert out["ok"] is False and "not snapshotted" in out["error"]


def test_local_backend_still_snapshots(ctx, tmp_path, monkeypatch):
    d = make_repo(tmp_path)
    monkeypatch.setenv("TERMINAL_ENV", "local")
    run_tool(ctx, "terminal", {"command": "echo hi > a.txt", "workdir": str(d)}, lambda: (d / "a.txt").write_text("hi"))
    assert git(d, "for-each-ref", "refs/hermes-rewind/") != ""


def test_fork_git_keeps_current_work(ctx, tmp_path):
    d = make_repo(tmp_path)
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("print('v2')\n"))
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("print('v3')\n"))
    branch_before = git(d, "rev-parse", "--abbrev-ref", "HEAD").strip()
    out = ctx.cmds["rewind"]("fork 2", session_id="s1")
    assert "Forked" in out, out
    assert (d / "app.py").read_text() == "print('v3')\n"            # current work untouched
    assert git(d, "rev-parse", "--abbrev-ref", "HEAD").strip() == branch_before
    fork = next(tmp_path.glob("repo-fork-2-*"))
    assert (fork / "app.py").read_text() == "print('v2')\n"         # state before step 2
    assert "rewind/fork-2-" in git(d, "branch", "--list", "rewind/*")
    assert "[fork]" in ctx.cmds["timeline"]("", session_id="s1")
    # rewind still ignores the fork step and works
    ctx.cmds["rewind"]("1", session_id="s1")
    assert (d / "app.py").read_text() == "print('v1')\n"


def test_fork_files_mode(ctx, tmp_path):
    d = tmp_path / "plain"; d.mkdir(); (d / "a.txt").write_text("1")
    run_tool(ctx, "terminal", {"command": "x", "workdir": str(d)}, lambda: (d / "a.txt").write_text("2"))
    r = json.loads(ctx.tools["rewind_restore"]({"action": "fork", "step": 1}, session_id="s1"))
    assert r["ok"], r
    assert Path(r["forks"][0]["path"], "a.txt").read_text() == "1" and (d / "a.txt").read_text() == "2"


def test_gc_retention_drops_refs_and_blobs(ctx, rw, tmp_path):
    from rewind_plugin import journal, snapshot
    d = make_repo(tmp_path)
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("v2\n"))
    p = tmp_path / "plain"; p.mkdir(); (p / "f").write_text("x" * 100)
    run_tool(ctx, "terminal", {"command": "y", "workdir": str(p)}, lambda: (p / "f").write_text("y" * 100))
    assert git(d, "for-each-ref", "refs/hermes-rewind/") and snapshot.store_bytes() > 0
    # nothing is old yet → no-op
    assert "dropped 0 step" in ctx.cmds["rewind"]("gc", session_id="s1")
    # age everything past retention
    c = journal.conn(); c.execute("UPDATE steps SET created=?", (time.time() - 30 * 86400,)); c.commit()
    out = ctx.cmds["rewind"]("gc", session_id="s1")
    assert "dropped 2 step" in out, out
    assert git(d, "for-each-ref", "refs/hermes-rewind/") == ""
    assert snapshot.store_bytes() == 0
    assert "no recorded changes" in ctx.cmds["timeline"]("", session_id="s1")


def test_gc_size_cap(rw, tmp_path):
    from rewind_plugin import core, gc, journal, snapshot
    s = core.Settings(lambda k, dflt: 0 if k == "max_store_mb" else dflt)
    p = tmp_path / "plain"; p.mkdir(); (p / "f").write_text("z" * 5000)
    ref = snapshot.files_snapshot(p)["ref"]
    journal.add_step("s", "tool", "terminal", "x", str(p), "files", ref, None)
    r = gc.gc(s)
    assert r["steps"] == 1 and snapshot.store_bytes() == 0


def test_debug_payload_records_shape(ctx, tmp_path, monkeypatch):
    log = tmp_path / "payload.jsonl"
    monkeypatch.setenv("REWIND_DEBUG_PAYLOAD", str(log))
    ctx.mw["tool_execution"](tool_name="read_file", args={"path": "x"}, next_call=lambda a: "{}",
                             original_args={"path": "x"}, task_id="t", session_id="s", tool_call_id="c")
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["tool_name"] == "read_file" and rec["kwargs"]["session_id"] == "s" and rec["arg_keys"] == ["path"]


def test_inband_tool_error_marked(ctx, tmp_path):
    d = make_repo(tmp_path)
    ctx.mw["tool_execution"](tool_name="write_file", args={"path": str(d / "app.py")},
                             next_call=lambda a: json.dumps({"error": "Refusing to overwrite"}), session_id="s1")
    assert "✗" in ctx.cmds["timeline"]("", session_id="s1")


def test_gc_preserves_other_hermes_home_refs(tmp_path, monkeypatch):
    """Two REWIND_DATA_DIRs (≈ two HERMES_HOMEs) sharing one git repo must not GC each other."""
    d = make_repo(tmp_path)
    home_a, home_b = tmp_path / "homeA", tmp_path / "homeB"

    a = _reload(monkeypatch, home_a)
    a.core.capture("write_file", {"path": str(d / "app.py")},
                   lambda _a: (d / "app.py").write_text("A\n"), "sA", a.core.Settings())
    id_a = a.store.instance_id()
    refs_a = git(d, "for-each-ref", "--format=%(refname)", f"refs/hermes-rewind/{id_a}/").strip()
    assert refs_a and f"refs/hermes-rewind/{id_a}/" in refs_a

    b = _reload(monkeypatch, home_b)
    id_b = b.store.instance_id()
    assert id_b != id_a
    b.core.capture("write_file", {"path": str(d / "app.py")},
                   lambda _a: (d / "app.py").write_text("B\n"), "sB", b.core.Settings())
    refs_b = git(d, "for-each-ref", "--format=%(refname)", f"refs/hermes-rewind/{id_b}/").strip()
    assert refs_b

    # age B's journal past retention and GC — must not touch A's namespace
    c = b.journal.conn()
    c.execute("UPDATE steps SET created=?", (time.time() - 30 * 86400,))
    c.commit()
    r = b.gc.gc(b.core.Settings())
    assert r["steps"] >= 1 and r["git_refs"] >= 1
    assert git(d, "for-each-ref", "--format=%(refname)", f"refs/hermes-rewind/{id_b}/").strip() == ""
    still_a = git(d, "for-each-ref", "--format=%(refname)", f"refs/hermes-rewind/{id_a}/").strip()
    assert still_a == refs_a, (still_a, refs_a)


def test_gc_ignores_foreign_flat_ref(tmp_path, monkeypatch):
    """A legacy flat refs/hermes-rewind/<sha> that is not under our namespace survives GC."""
    d = make_repo(tmp_path)
    mod = _reload(monkeypatch, tmp_path / "home")
    foreign = git(d, "rev-parse", "HEAD").strip()
    git(d, "update-ref", f"refs/hermes-rewind/{foreign}", foreign)  # flat, no instance id
    mod.core.capture("write_file", {"path": str(d / "app.py")},
                     lambda _a: (d / "app.py").write_text("Z\n"), "s", mod.core.Settings())
    c = mod.journal.conn()
    c.execute("UPDATE steps SET created=?", (time.time() - 30 * 86400,))
    c.commit()
    mod.gc.gc(mod.core.Settings())
    # our namespaced refs gone…
    assert git(d, "for-each-ref", "--format=%(refname)",
               f"refs/hermes-rewind/{mod.store.instance_id()}/").strip() == ""
    # …but the foreign flat pin remains
    assert foreign in git(d, "for-each-ref", "--format=%(objectname)", "refs/hermes-rewind/")


def test_empty_session_messages(ctx):
    assert "no recorded" in ctx.cmds["timeline"]("", session_id="empty").lower()
    r = json.loads(ctx.tools["rewind_restore"]({"action": "restore", "step": 1}, session_id="empty"))
    assert r["ok"] is False and "no recorded" in r["error"].lower()


def test_turn_id_and_list_markers(ctx, tmp_path):
    d = make_repo(tmp_path)
    def next_call(a):
        (d / "app.py").write_text("Z\n")
        return json.dumps({"ok": True})
    ctx.mw["tool_execution"](tool_name="write_file", args={"path": str(d / "app.py")},
                             next_call=next_call, session_id="s1", turn_id="abc12345-turn")
    listed = json.loads(ctx.tools["rewind_restore"]({"action": "list"}, session_id="s1"))
    assert listed["count"] >= 1
    assert listed["steps"][0]["turn_id"].startswith("abc12345")
    assert listed["steps"][0]["mark"] in ("•", "✗", "⚠", "⊘", "◆", "🌿")
    assert "turn:abc12345" in ctx.cmds["timeline"]("", session_id="s1")


def test_hand_edit_warning_and_undo_hint(ctx, tmp_path):
    d = make_repo(tmp_path)
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("A\n"))
    (d / "app.py").write_text("A\n# hand\n")
    out = ctx.cmds["rewind"]("1", session_id="s1")
    assert "Restored" in out
    assert "hand" in out.lower() or "Overwrote" in out
    assert "/undo" in out
    assert (d / "app.py").read_text() == "print('v1')\n"


def test_unfork_cleans_worktree(ctx, tmp_path):
    d = make_repo(tmp_path)
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("v2\n"))
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("v3\n"))
    out = ctx.cmds["rewind"]("fork 2 --model arena-x", session_id="s1")
    assert "Forked" in out and "arena-x" in out or "model" in out.lower() or "Forked" in out
    forks = list(tmp_path.glob("repo-fork-2-*"))
    assert forks
    u = ctx.cmds["rewind"]("unfork", session_id="s1")
    assert "Removed" in u
    assert not forks[0].exists()
    assert "rewind/fork-2-" not in git(d, "branch", "--list", "rewind/*")


def test_irreversible_marked_on_push(ctx, tmp_path):
    d = make_repo(tmp_path)
    run_tool(ctx, "terminal", {"command": "git push origin main", "workdir": str(d)}, lambda: None)
    tl = ctx.cmds["timeline"]("", session_id="s1")
    assert "⚠" in tl and "git push" in tl
    listed = json.loads(ctx.tools["rewind_restore"]({"action": "list"}, session_id="s1"))
    assert listed["steps"][0]["irreversible"]
