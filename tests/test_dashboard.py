"""FastAPI route tests for dashboard/plugin_api.py. Skip when fastapi isn't installed."""
import importlib.util
import sys
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REWIND_DATA_DIR", str(tmp_path / "data"))
    # Drop any prior dashboard package so it rebinds to this data dir.
    for name in list(sys.modules):
        if name == "rewind_dashboard_pkg" or name.startswith("rewind_dashboard_pkg."):
            del sys.modules[name]
    # Also clear agent-side package if present so store/journal don't fight.
    for name in list(sys.modules):
        if name == "rewind_plugin" or name.startswith("rewind_plugin."):
            del sys.modules[name]
    api_path = ROOT / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("rewind_dash_test_api", api_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rewind_dash_test_api"] = mod
    spec.loader.exec_module(mod)
    app = fastapi.FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/rewind")
    # seed journal via the same package the router loads
    core, journal = mod._rw()
    journal.reset()
    from test_rewind import make_repo
    d = make_repo(tmp_path)
    core.capture("write_file", {"path": str(d / "app.py")},
                 lambda a: (d / "app.py").write_text("v2\n"), "sess-desk", core.Settings())
    core.capture("write_file", {"path": str(d / "app.py")},
                 lambda a: (d / "app.py").write_text("v3\n"), "sess-desk", core.Settings())
    yield TestClient(app), d, journal, core


def test_health(client):
    c, *_ = client
    r = c.get("/api/plugins/rewind/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_timeline_lists_steps(client):
    c, *_ = client
    r = c.get("/api/plugins/rewind/timeline", params={"session": "sess-desk"})
    assert r.status_code == 200
    body = r.json()
    assert body["session"] == "sess-desk"
    assert len(body["steps"]) == 2
    assert body["steps"][0]["seq"] == 1 and body["steps"][0]["tool"] == "write_file"
    assert set(body["steps"][0]) >= {"seq", "kind", "tool", "summary", "root", "mode", "status"}


def test_timeline_defaults_to_latest_session(client):
    c, *_ = client
    r = c.get("/api/plugins/rewind/timeline")
    assert r.json()["session"] == "sess-desk"


def test_diff_and_rewind_and_fork(client):
    c, d, journal, core = client
    r = c.get("/api/plugins/rewind/diff", params={"step": 2, "session": "sess-desk"})
    assert r.status_code == 200 and isinstance(r.json()["lines"], list)

    r = c.post("/api/plugins/rewind/fork", json={"step": 2, "session": "sess-desk"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert (d / "app.py").read_text() == "v3\n"  # current work untouched

    r = c.post("/api/plugins/rewind/rewind", json={"step": 1, "session": "sess-desk"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert (d / "app.py").read_text() == "print('v1')\n"


def test_bad_step_rejected(client):
    c, *_ = client
    assert c.post("/api/plugins/rewind/rewind", json={"step": 0, "session": "sess-desk"}).status_code == 422
    assert c.get("/api/plugins/rewind/diff", params={"step": 0}).status_code == 400
