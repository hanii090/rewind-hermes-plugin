"""SQLite journal of steps. One row per mutating tool call (plus safety snapshots)."""
import json
import threading
import time
from typing import Optional

from . import store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,              -- tool | safety | fork
  tool TEXT,
  summary TEXT,
  root TEXT NOT NULL,              -- snapshot root (git toplevel or dir/file)
  mode TEXT NOT NULL,              -- git | files | none (remote backend)
  before_ref TEXT,                 -- snapshot taken BEFORE the step
  after_ref TEXT,                  -- snapshot taken AFTER the step
  head TEXT,                       -- git HEAD before the step
  status TEXT,                     -- ok | error
  irreversible TEXT,               -- reason when side effects escape the snapshot
  turn_id TEXT,                    -- Hermes turn_id when known (pairs with /undo)
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS steps_session ON steps(session_id, seq);
"""

_lock = threading.Lock()
_conn = None


def conn():
    global _conn
    with _lock:
        if _conn is None:
            c = store.open_db()
            c.executescript(_SCHEMA)
            # migrate older DBs that pre-date turn_id
            cols = {r[1] for r in c.execute("PRAGMA table_info(steps)")}
            if "turn_id" not in cols:
                c.execute("ALTER TABLE steps ADD COLUMN turn_id TEXT")
            c.commit()
            _conn = c
        return _conn


def reset():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None


def add_step(session_id, kind, tool, summary, root, mode, before_ref, head,
             irreversible=None, turn_id=None) -> int:
    c = conn()
    with _lock:
        row = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM steps WHERE session_id=?", (session_id,)).fetchone()
        cur = c.execute(
            "INSERT INTO steps(session_id,seq,kind,tool,summary,root,mode,before_ref,head,irreversible,turn_id,created)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, row[0], kind, tool, summary, root, mode, before_ref, head, irreversible,
             turn_id or None, time.time()))
        c.commit()
        return cur.lastrowid


def finish_step(step_id, after_ref, status):
    c = conn()
    with _lock:
        c.execute("UPDATE steps SET after_ref=?, status=? WHERE id=?", (after_ref, status, step_id))
        c.commit()


_COLS = ["id", "session_id", "seq", "kind", "tool", "summary", "root", "mode", "before_ref",
         "after_ref", "head", "status", "irreversible", "turn_id", "created"]


def list_steps(session_id) -> list:
    rows = conn().execute(f"SELECT {','.join(_COLS)} FROM steps WHERE session_id=? ORDER BY seq",
                          (session_id,)).fetchall()
    return [dict(zip(_COLS, r)) for r in rows]


def get_step(session_id, seq) -> Optional[dict]:
    r = conn().execute(f"SELECT {','.join(_COLS)} FROM steps WHERE session_id=? AND seq=?",
                       (session_id, seq)).fetchone()
    return dict(zip(_COLS, r)) if r else None


def all_steps() -> list:
    rows = conn().execute(f"SELECT {','.join(_COLS)} FROM steps ORDER BY created, id").fetchall()
    return [dict(zip(_COLS, r)) for r in rows]


def delete_steps(ids) -> None:
    ids = list(ids)
    if not ids:
        return
    c = conn()
    with _lock:
        c.executemany("DELETE FROM steps WHERE id=?", [(i,) for i in ids])
        c.commit()


def latest_session() -> Optional[str]:
    r = conn().execute("SELECT session_id FROM steps ORDER BY id DESC LIMIT 1").fetchone()
    return r[0] if r else None


def dumps(obj) -> str:
    return json.dumps(obj, default=str)
