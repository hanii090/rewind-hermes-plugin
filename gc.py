"""Retention: drop old journal steps, then the git refs / blobs only they referenced.

Git pins are namespaced per plugin data dir (see snapshot._ref_prefix), and GC only
deletes SHAs that *this* journal just dropped — never another HERMES_HOME's snapshots
in the same repo.
"""
import time
from pathlib import Path

from . import journal, snapshot


def _refs_of(steps) -> set:
    return {r for s in steps for r in (s.get("before_ref"), s.get("after_ref")) if r}


def _sweep_git(all_roots, drop_shas) -> int:
    n = 0
    for root in all_roots:
        if not Path(root).is_dir():
            continue
        try:
            owned = snapshot.git_list_refs(root)  # this instance's namespace only
            for sha in drop_shas & owned:
                snapshot.git_drop_ref(root, sha)
                n += 1
        except Exception:
            pass
    return n


def gc(settings, now=None, everything=False) -> dict:
    now = now or time.time()
    steps = journal.all_steps()
    git_roots = {s["root"] for s in steps if s["mode"] == "git"}
    cutoff = now - float(settings["retention_days"]) * 86400
    cap = int(settings["max_steps_per_session"])
    doomed = set()
    per_session = {}
    for s in steps:
        per_session.setdefault(s["session_id"], []).append(s)
        if everything or s["created"] < cutoff:
            doomed.add(s["id"])
    for ss in per_session.values():
        ss.sort(key=lambda s: (s.get("seq") or 0, s.get("id") or 0))
        if len(ss) > cap:
            doomed.update(s["id"] for s in ss[: len(ss) - cap])
    doomed_steps = [s for s in steps if s["id"] in doomed]
    drop_shas = _refs_of(doomed_steps)
    journal.delete_steps(doomed)

    def live():
        return _refs_of(journal.all_steps())

    # Only SHAs this journal owned and just deleted, and only under our namespace.
    res = {"git_refs": _sweep_git(git_roots, drop_shas - live())}
    res.update(snapshot.sweep_files(live()))
    res["steps"] = len(doomed)
    # size cap on the content-hash store: evict oldest non-git steps until under budget
    budget = float(settings["max_store_mb"]) * 1_000_000
    while snapshot.store_bytes() > budget:
        oldest = [s for s in journal.all_steps() if s["mode"] == "files"][:20]
        if not oldest:
            break
        journal.delete_steps(s["id"] for s in oldest)
        res["steps"] += len(oldest)
        r2 = snapshot.sweep_files(live())
        for k in ("manifests", "blobs", "bytes_freed"):
            res[k] = res.get(k, 0) + r2[k]
    return res
