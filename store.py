"""Storage location. Uses Hermes plugin_db/plugin_data_dir when available, else REWIND_DATA_DIR (tests)."""
import hashlib
import os
import sqlite3
from pathlib import Path

NAME = "rewind"


def data_dir() -> Path:
    override = os.environ.get("REWIND_DATA_DIR")
    if override:
        p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return p
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(NAME)


def instance_id() -> str:
    """Stable short id for this plugin data dir.

    Git snapshot pins live under refs/hermes-rewind/<instance_id>/<sha> so a second
    HERMES_HOME (or REWIND_DATA_DIR) sharing the same repo never GC's the other's refs.
    """
    return hashlib.sha256(str(data_dir().resolve()).encode()).hexdigest()[:12]


def open_db() -> sqlite3.Connection:
    if os.environ.get("REWIND_DATA_DIR"):
        conn = sqlite3.connect(str(data_dir() / "data.db"), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    from plugins.plugin_storage import plugin_db
    return plugin_db(NAME)
