"""Rewind — undo for agents. Hermes imports this package and calls register(ctx)."""
if __package__:  # guard so test collectors that import this file standalone don't fail
    from .plugin_main import REWIND_RESTORE, register  # noqa: F401
