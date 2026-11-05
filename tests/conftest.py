import importlib.util, sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    if "rewind_plugin" in sys.modules:
        return sys.modules["rewind_plugin"]
    spec = importlib.util.spec_from_file_location("rewind_plugin", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rewind_plugin"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def rw(tmp_path, monkeypatch):
    monkeypatch.setenv("REWIND_DATA_DIR", str(tmp_path / "data"))
    mod = _load()
    from rewind_plugin import journal
    journal.reset()
    yield mod
    journal.reset()


class FakeCtx:
    def __init__(self):
        self.tools, self.mw, self.cmds, self.hooks = {}, {}, {}, {}
    def get_config(self, key, default=None):
        return default
    def register_middleware(self, kind, fn): self.mw[kind] = fn
    def register_tool(self, name, toolset, schema, handler, **kw): self.tools[name] = handler
    def register_hook(self, name, fn): self.hooks[name] = fn
    def register_command(self, name, handler, description="", args_hint=""): self.cmds[name] = handler


@pytest.fixture
def ctx(rw):
    c = FakeCtx()
    rw.register(c)
    return c
