import json, subprocess
from pathlib import Path


def git(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True, text=True).stdout


def make_repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    git(d, "init", "-q")
    git(d, "config", "user.email", "t@t"); git(d, "config", "user.name", "t")
    (d / "app.py").write_text("print('v1')\n")
    (d / "legacy").mkdir(); (d / "legacy" / "old.py").write_text("x=1\n")
    git(d, "add", "-A"); git(d, "commit", "-qm", "init")
    return d


def run_tool(ctx, tool, args, effect, sid="s1"):
    def next_call(a):
        effect()
        return json.dumps({"ok": True})
    return ctx.mw["tool_execution"](tool_name=tool, args=args, next_call=next_call, session_id=sid)


def test_git_multi_step_rewind(ctx, tmp_path):
    d = make_repo(tmp_path)
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("print('v2')\n"))
    run_tool(ctx, "write_file", {"path": str(d / "new.py")}, lambda: (d / "new.py").write_text("n\n"))
    import shutil
    run_tool(ctx, "terminal", {"command": "rm -r legacy && git commit -am wip", "workdir": str(d)},
             lambda: (shutil.rmtree(d / "legacy"), git(d, "commit", "-qam", "wip")))
    head_after = git(d, "rev-parse", "HEAD")
    tl = ctx.cmds["timeline"]("", session_id="s1")
    assert "write_file" in tl and "terminal" in tl
    out = ctx.cmds["rewind"]("1", session_id="s1")
    assert "Rewound" in out, out
    assert (d / "app.py").read_text() == "print('v1')\n"
    assert not (d / "new.py").exists()
    assert (d / "legacy" / "old.py").read_text() == "x=1\n"
    assert git(d, "rev-parse", "HEAD") != head_after  # branch moved back
    # undo the rewind via the safety snapshot
    safety = [l for l in ctx.cmds["timeline"]("", session_id="s1").splitlines() if "[safety]" in l][0]
    n = safety.split()[1]
    ctx.cmds["rewind"](n, session_id="s1")
    assert (d / "app.py").read_text() == "print('v2')\n" and (d / "new.py").exists()
    assert not (d / "legacy").exists()


def test_partial_rewind_keeps_earlier_steps(ctx, tmp_path):
    d = make_repo(tmp_path)
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("A\n"))
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("B\n"))
    ctx.cmds["rewind"]("2", session_id="s1")
    assert (d / "app.py").read_text() == "A\n"


def test_untracked_and_user_index_untouched(ctx, tmp_path):
    d = make_repo(tmp_path)
    (d / "scratch.txt").write_text("keep\n")
    git(d, "add", "scratch.txt")
    staged = git(d, "diff", "--cached", "--name-only")
    run_tool(ctx, "write_file", {"path": str(d / "app.py")}, lambda: (d / "app.py").write_text("Z\n"))
    assert git(d, "diff", "--cached", "--name-only") == staged  # snapshot didn't touch real index


def test_files_mode_dir(ctx, tmp_path):
    d = tmp_path / "plain"; d.mkdir()
    (d / "a.txt").write_text("1")
    (d / "node_modules").mkdir(); (d / "node_modules" / "big.js").write_text("ignored")
    def effect():
        (d / "a.txt").write_text("2"); (d / "b.txt").write_text("new")
        (d / "node_modules" / "big.js").unlink()
    run_tool(ctx, "terminal", {"command": "do stuff", "workdir": str(d)}, effect)
    assert "modified: a.txt" in ctx.cmds["rewind-diff"]("1", session_id="s1")
    ctx.cmds["rewind"]("1", session_id="s1")
    assert (d / "a.txt").read_text() == "1" and not (d / "b.txt").exists()
    assert not (d / "node_modules" / "big.js").exists()  # ignored paths are left alone


def test_files_mode_single_file_created(ctx, tmp_path):
    d = tmp_path / "loose"; d.mkdir()
    f = d / "notes.md"
    run_tool(ctx, "write_file", {"path": str(f)}, lambda: f.write_text("hi"))
    ctx.cmds["rewind"]("1", session_id="s1")
    assert not f.exists()


def test_irreversible_flag_and_tool(ctx, tmp_path):
    d = make_repo(tmp_path)
    run_tool(ctx, "terminal", {"command": "git push origin main", "workdir": str(d)}, lambda: None)
    assert "git push" in ctx.cmds["timeline"]("", session_id="s1")
    r = json.loads(ctx.tools["rewind_restore"]({"action": "restore", "step": 1}, session_id="s1"))
    assert r["ok"] and r["not_reversible"]


def test_tool_error_still_journaled_and_reraised(ctx, tmp_path):
    d = make_repo(tmp_path)
    def boom(a):
        (d / "app.py").write_text("half")
        raise RuntimeError("x")
    try:
        ctx.mw["tool_execution"](tool_name="write_file", args={"path": str(d / "app.py")}, next_call=boom, session_id="s1")
    except RuntimeError:
        pass
    assert "✗" in ctx.cmds["timeline"]("", session_id="s1")
    ctx.cmds["rewind"]("1", session_id="s1")
    assert (d / "app.py").read_text() == "print('v1')\n"


def test_non_mutating_tools_pass_through(ctx):
    assert ctx.mw["tool_execution"](tool_name="read_file", args={}, next_call=lambda a: "R", session_id="s1") == "R"
    assert "no recorded changes" in ctx.cmds["timeline"]("", session_id="s1")
