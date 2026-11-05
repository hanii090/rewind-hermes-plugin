"""World-state snapshots.

git mode  : the working tree (tracked + untracked, honouring .gitignore) is written to a commit via a
            private temporary index and pinned under refs/hermes-rewind/<instance-id>/<sha>. The user's index, HEAD
            and branches are never touched while snapshotting.
files mode: content-addressed copies (sha256) of the files in scope plus a JSON manifest, stored in
            the plugin data dir. Used outside git repositories.
"""
import fnmatch
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from . import store

GIT_TIMEOUT = 20


class SnapshotError(Exception):
    pass


# ---------------------------------------------------------------- git helpers
def _git(root, *args, env=None, check=True, input_=None) -> str:
    full_env = dict(os.environ)
    full_env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "hermes-rewind",
                     "GIT_AUTHOR_EMAIL": "rewind@localhost", "GIT_COMMITTER_NAME": "hermes-rewind",
                     "GIT_COMMITTER_EMAIL": "rewind@localhost"})
    if env:
        full_env.update(env)
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                       env=full_env, timeout=GIT_TIMEOUT, input=input_)
    if check and p.returncode != 0:
        raise SnapshotError(f"git {' '.join(args)}: {p.stderr.strip()[:300]}")
    return p.stdout.strip()


def git_toplevel(path) -> Optional[Path]:
    p = Path(path)
    d = p if p.is_dir() else p.parent
    while not d.exists() and d != d.parent:
        d = d.parent
    try:
        out = subprocess.run(["git", "-C", str(d), "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def git_head(root) -> Optional[str]:
    out = _git(root, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return out or None


def _tmp_index(root):
    fd, path = tempfile.mkstemp(prefix="rewind-index-")
    os.close(fd)
    os.unlink(path)  # git creates it
    return path


def git_snapshot(root) -> str:
    idx = _tmp_index(root)
    env = {"GIT_INDEX_FILE": idx}
    try:
        head = git_head(root)
        if head:
            _git(root, "read-tree", head, env=env)
        _git(root, "add", "-A", "--", ".", env=env)
        tree = _git(root, "write-tree", env=env)
        args = ["commit-tree", tree, "-m", f"rewind snapshot {time.time():.3f}"]
        if head:
            args[2:2] = ["-p", head]
        commit = _git(root, *args, env=env)
        _git(root, "update-ref", f"refs/hermes-rewind/{store.instance_id()}/{commit}", commit)
        return commit
    finally:
        for f in (idx, idx + ".lock"):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass


def _git_files(root, ref) -> set:
    out = _git(root, "ls-tree", "-r", "-z", "--name-only", ref)
    return {x for x in out.split("\0") if x}


def _prune_empty_dirs(root, rels):
    root = Path(root)
    for rel in rels:
        d = (root / rel).parent
        while d != root and root in d.parents:
            try:
                d.rmdir()
            except OSError:
                break
            d = d.parent


def git_restore(root, ref, head=None, *, move_head=True) -> dict:
    """Restore the worktree to `ref`. When move_head=True (default) and `head` differs from
    current HEAD, also `git reset` the branch tip back (mixed). When move_head=False, only the
    worktree/index contents are rewritten — the branch tip stays put (dirty vs HEAD is expected)."""
    current = git_snapshot(root)
    now_files, target_files = _git_files(root, current), _git_files(root, ref)
    removed = sorted(now_files - target_files)
    for rel in removed:
        try:
            (Path(root) / rel).unlink()
        except FileNotFoundError:
            pass
    _prune_empty_dirs(root, removed)
    idx = _tmp_index(root)
    env = {"GIT_INDEX_FILE": idx}
    try:
        _git(root, "read-tree", ref, env=env)
        _git(root, "checkout-index", "-a", "-f", env=env)
    finally:
        try:
            os.unlink(idx)
        except FileNotFoundError:
            pass
    moved_head = False
    if move_head and head and git_head(root) != head:
        _git(root, "reset", "-q", head)  # mixed: moves branch back, keeps restored worktree
        moved_head = True
    changed = [c for c in _git(root, "diff", "--name-only", current, ref).splitlines() if c not in removed]
    return {"changed": changed, "removed": removed, "moved_head": moved_head,
            "restore_mode": "move_head" if move_head else "worktree_only", "pre_restore": current}


# ---------------------------------------------------------------- files mode
def _blobs() -> Path:
    p = store.data_dir() / "blobs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _manifests() -> Path:
    p = store.data_dir() / "manifests"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ignored(rel, globs) -> bool:
    parts = rel.split("/")
    for g in globs:
        if fnmatch.fnmatch(rel, g) or any(fnmatch.fnmatch(p + "/", g.rstrip("*")) or fnmatch.fnmatch(p, g.rstrip("/*")) for p in parts[:-1]):
            return True
    return False


def _store_blob(path: Path) -> str:
    data = path.read_bytes()
    h = hashlib.sha256(data).hexdigest()
    dest = _blobs() / h
    if not dest.exists():
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    return h


def files_snapshot(root, paths=None, *, globs=(), max_bytes=5_000_000, max_files=5000) -> dict:
    """paths=None → whole directory (full=True). Returns {'ref', 'skipped', 'truncated'}."""
    root = Path(root)
    entries, skipped, truncated = {}, [], False
    if paths is None:
        for dirpath, dirnames, filenames in os.walk(root):
            reld = os.path.relpath(dirpath, root)
            dirnames[:] = [d for d in dirnames
                           if not _ignored((d if reld == "." else f"{reld}/{d}") + "/x", globs)]
            for fn in filenames:
                rel = fn if reld == "." else f"{reld}/{fn}"
                if _ignored(rel, globs):
                    continue
                if len(entries) >= max_files:
                    truncated = True
                    break
                full = root / rel
                if full.is_symlink() or not full.is_file():
                    continue
                if full.stat().st_size > max_bytes:
                    skipped.append(rel)
                    continue
                entries[rel] = _store_blob(full)
            if truncated:
                break
    else:
        for p in paths:
            rel = os.path.relpath(Path(p), root)
            full = root / rel
            if full.is_file() and full.stat().st_size <= max_bytes:
                entries[rel] = _store_blob(full)
            elif full.is_file():
                skipped.append(rel)
            else:
                entries[rel] = None  # absent before → delete on restore
    manifest = {"root": str(root), "full": paths is None and not truncated, "entries": entries}
    body = json.dumps(manifest, sort_keys=True).encode()
    ref = hashlib.sha256(body).hexdigest()
    (_manifests() / f"{ref}.json").write_bytes(body)
    return {"ref": ref, "skipped": skipped, "truncated": truncated}


def load_manifest(ref) -> dict:
    return json.loads((_manifests() / f"{ref}.json").read_text())


def files_restore(ref, *, globs=(), root=None) -> dict:
    m = load_manifest(ref)
    root = Path(root or m["root"])
    changed, removed = [], []
    if m["full"]:
        for dirpath, dirnames, filenames in os.walk(root):
            reld = os.path.relpath(dirpath, root)
            dirnames[:] = [d for d in dirnames
                           if not _ignored((d if reld == "." else f"{reld}/{d}") + "/x", globs)]
            for fn in filenames:
                rel = fn if reld == "." else f"{reld}/{fn}"
                if not _ignored(rel, globs) and rel not in m["entries"]:
                    (root / rel).unlink()
                    removed.append(rel)
    for rel, h in m["entries"].items():
        full = root / rel
        if h is None:
            if full.exists():
                full.unlink()
                removed.append(rel)
            continue
        data = (_blobs() / h).read_bytes()
        if not full.exists() or full.read_bytes() != data:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(data)
            changed.append(rel)
    _prune_empty_dirs(root, removed)
    return {"changed": changed, "removed": removed}


def files_diff(ref_a, ref_b) -> list:
    a, b = load_manifest(ref_a)["entries"], load_manifest(ref_b)["entries"]
    out = []
    for rel in sorted(set(a) | set(b)):
        if a.get(rel) != b.get(rel):
            kind = "added" if not a.get(rel) else "deleted" if not b.get(rel) else "modified"
            out.append(f"{kind}: {rel}")
    return out


# ---------------------------------------------------------------- fork
def git_fork(root, ref, head, branch, dest, message) -> None:
    """New branch whose tip is the snapshot (parented on the HEAD of that time), checked out in a
    separate worktree. The current checkout, index and branch are not touched."""
    tree = _git(root, "rev-parse", f"{ref}^{{tree}}")
    args = ["commit-tree", tree, "-m", message] + (["-p", head] if head else [])
    commit = _git(root, *args)
    _git(root, "branch", branch, commit)
    _git(root, "worktree", "add", "-q", str(dest), branch)


def git_unfork(root, worktree_path, branch) -> None:
    """Remove a Rewind fork worktree and its branch. Force-remove if dirty."""
    wt = Path(worktree_path)
    if wt.exists():
        try:
            _git(root, "worktree", "remove", "--force", str(wt))
        except SnapshotError:
            import shutil
            shutil.rmtree(wt, ignore_errors=True)
            try:
                _git(root, "worktree", "prune")
            except SnapshotError:
                pass
    if branch:
        try:
            _git(root, "branch", "-D", branch)
        except SnapshotError:
            pass


def files_fork(ref, dest, *, globs=()) -> None:
    import shutil
    m = load_manifest(ref)
    dest = Path(dest)
    if dest.exists():
        raise SnapshotError(f"{dest} already exists")
    if m["full"]:
        dest.mkdir(parents=True)
    else:  # partial snapshot: start from today's directory, then put the snapshotted files back
        shutil.copytree(m["root"], dest, symlinks=True,
                        ignore=lambda d, names: [n for n in names if _ignored(
                            os.path.relpath(os.path.join(d, n), m["root"]), globs)])
    files_restore(ref, globs=globs, root=dest)


# ---------------------------------------------------------------- gc
def _ref_prefix() -> str:
    return f"refs/hermes-rewind/{store.instance_id()}/"


def git_list_refs(root) -> set:
    """SHAs pinned under THIS instance's namespace only (never another HERMES_HOME)."""
    prefix = _ref_prefix()
    out = _git(root, "for-each-ref", "--format=%(refname)", prefix)
    return {x[len(prefix):] for x in out.splitlines() if x.startswith(prefix)}


def git_drop_ref(root, sha) -> None:
    _git(root, "update-ref", "-d", f"{_ref_prefix()}{sha}", check=False)


def sweep_files(live_manifests: set) -> dict:
    """Delete manifests not in live_manifests, then blobs no surviving manifest references."""
    dropped_m = 0
    keep_blobs = set()
    for mf in _manifests().glob("*.json"):
        if mf.stem in live_manifests:
            try:
                keep_blobs.update(h for h in json.loads(mf.read_text())["entries"].values() if h)
            except Exception:
                pass
        else:
            mf.unlink(missing_ok=True)
            dropped_m += 1
    dropped_b = freed = 0
    for b in _blobs().iterdir():
        if b.name not in keep_blobs:
            freed += b.stat().st_size
            b.unlink(missing_ok=True)
            dropped_b += 1
    return {"manifests": dropped_m, "blobs": dropped_b, "bytes_freed": freed}


def store_bytes() -> int:
    return sum(p.stat().st_size for p in _blobs().iterdir() if p.is_file())
