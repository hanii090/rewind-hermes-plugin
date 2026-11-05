"""Slash commands: /timeline, /rewind, /rewind-diff."""
from . import core, journal
from . import gc as gc_mod


def _sid(session_id):
    return session_id or journal.latest_session() or "default"


def _fmt_rewind(r, arg):
    if not r["ok"]:
        msg = f"Rewind failed: {r['error']}"
        if r.get("irreversible_summary"):
            msg += "\n" + r["irreversible_summary"]
        return msg
    out = [f"⏪ Rewound to just before step {arg}."]
    mode = r.get("restore_mode") or "move_head"
    for root in r.get("roots") or []:
        bits = [f"{len(root.get('changed') or [])} restored", f"{len(root.get('removed') or [])} removed"]
        if root.get("moved_head"):
            bits.append("HEAD moved back")
        elif mode == "worktree_only" and root.get("mode") == "git":
            bits.append("HEAD left in place")
        out.append(f"  {root['root']}: " + ", ".join(bits))
    if mode == "worktree_only":
        out.append("  restore_mode=worktree_only — files rewritten; branch tip not moved.")
    if r.get("hand_edits_overwritten"):
        for h in r["hand_edits_overwritten"]:
            files = ", ".join(h["files"][:8]) + ("…" if len(h["files"]) > 8 else "")
            out.append(f"  ⚠ Overwrote hand-edits in {h['root']}: {files or '(files changed since last step)'}")
    if r.get("irreversible_summary"):
        for line in r["irreversible_summary"].splitlines():
            out.append(line if line.startswith("  ") else ("  " + line))
    elif r.get("not_reversible"):
        out.append("  Not undone (outside the workspace): " + "; ".join(r["not_reversible"]))
    if r.get("conversation_hint"):
        out.append("  " + r["conversation_hint"])
    out.append("  A safety snapshot was saved — /timeline shows it; /rewind to it to undo this.")
    return "\n".join(out)


def _fmt_fork(f, step):
    if not f["ok"]:
        return f"Fork failed: {f['error']}"
    out = [f"🌿 Forked the state before step {step} (your current work is untouched):"]
    for x in f["forks"]:
        out.append(f"  {x['path']}" + (f"  on branch {x['branch']}" if x.get("branch") else ""))
        if x.get("re_run_hint"):
            out.append(f"  hint: {x['re_run_hint'].splitlines()[0]}")
            for extra in x["re_run_hint"].splitlines()[1:]:
                out.append(f"        {extra}")
    out.append("  Cleanup later: /rewind unfork   ·  Compare: /rewind-diff <a> <b>")
    return "\n".join(out)


def make(settings):
    def timeline_cmd(raw_args: str = "", *, session_id: str = "", **_):
        return core.timeline(_sid(session_id))

    def rewind_cmd(raw_args: str = "", *, session_id: str = "", **_):
        arg = (raw_args or "").strip()
        if not arg or arg == "list":
            return core.timeline(_sid(session_id))
        parts = arg.split()
        if parts[0] == "gc":
            r = gc_mod.gc(settings, everything=len(parts) > 1 and parts[1] == "--all")
            return (f"🧹 Rewind gc: dropped {r['steps']} step(s), {r['git_refs']} git ref(s), "
                    f"{r['manifests']} manifest(s), {r['blobs']} blob(s) ({r['bytes_freed'] / 1e6:.1f} MB).")
        if parts[0] == "unfork":
            target = parts[1] if len(parts) > 1 else None
            u = core.unfork(_sid(session_id), target)
            if not u.get("removed") and not u["ok"]:
                return f"Unfork failed: {u.get('error', u.get('errors'))}"
            out = [f"🧹 Removed {len(u.get('removed') or [])} fork(s):"]
            for x in u.get("removed") or []:
                out.append(f"  {x['path']}" + (f" ({x['branch']})" if x.get("branch") else ""))
            for e in u.get("errors") or []:
                out.append(f"  ⚠ {e}")
            return "\n".join(out)
        if parts[0] == "fork":
            if len(parts) < 2 or not parts[1].isdigit():
                return "Usage: /rewind fork <step> [--model <name>]"
            model = None
            if "--model" in parts:
                i = parts.index("--model")
                model = parts[i + 1] if i + 1 < len(parts) else None
                if not model:
                    return "Usage: /rewind fork <step> [--model <name>]"
            elif len(parts) >= 3 and not parts[2].startswith("-"):
                # allow: /rewind fork 2 gpt-5  (optional shorthand)
                model = parts[2]
            return _fmt_fork(core.fork(_sid(session_id), int(parts[1]), settings, model=model), parts[1])
        if not arg.isdigit():
            return ("Usage: /rewind <step> | /rewind fork <step> [--model NAME] | "
                    "/rewind unfork [path|step] | /rewind gc [--all]   (see /timeline)")
        return _fmt_rewind(core.rewind(_sid(session_id), int(arg), settings), arg)

    def diff_cmd(raw_args: str = "", *, session_id: str = "", **_):
        parts = (raw_args or "").split()
        if not parts or not all(p.isdigit() for p in parts[:2]):
            return "Usage: /rewind-diff <step> [<to_step>]"
        a = int(parts[0])
        b = int(parts[1]) if len(parts) > 1 else None
        return "\n".join(core.diff(_sid(session_id), a, b))

    return timeline_cmd, rewind_cmd, diff_cmd
