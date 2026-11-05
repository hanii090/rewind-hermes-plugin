"""Backend routes for the Rewind desktop pane, mounted at /api/plugins/rewind/.
Thin wrappers over the same core used by the slash commands and the agent tool."""
import importlib.util
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

_PKG = "rewind_dashboard_pkg"


def _rw():
    if _PKG not in sys.modules:
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            _PKG, root / "__init__.py", submodule_search_locations=[str(root)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_PKG] = mod
        spec.loader.exec_module(mod)
    import importlib as il
    return il.import_module(f"{_PKG}.core"), il.import_module(f"{_PKG}.journal")


router = APIRouter()


class StepBody(BaseModel):
    step: int = Field(..., ge=1)
    session: str = ""


def _sid(journal, session: Optional[str]) -> str:
    return (session or "").strip() or journal.latest_session() or "default"


def _step_dict(s: dict) -> dict:
    return {k: s[k] for k in (
        "seq", "kind", "tool", "summary", "root", "mode", "status", "irreversible", "created")}


@router.get("/health")
async def health():
    return {"ok": True, "plugin": "rewind"}


@router.get("/timeline")
async def timeline(session: str = ""):
    core, journal = _rw()
    sid = _sid(journal, session)
    return {"session": sid, "steps": [_step_dict(s) for s in journal.list_steps(sid)]}


@router.get("/diff")
async def diff(step: int, to: int = 0, session: str = ""):
    if step < 1:
        raise HTTPException(400, "step must be >= 1")
    core, journal = _rw()
    try:
        lines = core.diff(_sid(journal, session), step, to or None)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"lines": lines}


@router.post("/rewind")
async def rewind(body: StepBody):
    core, journal = _rw()
    try:
        return core.rewind(_sid(journal, body.session), body.step, core.Settings())
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/fork")
async def fork(body: StepBody):
    core, journal = _rw()
    try:
        return core.fork(_sid(journal, body.session), body.step, core.Settings())
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
