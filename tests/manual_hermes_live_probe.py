"""MANUAL Hermes-dispatcher probe (not collected by pytest).

Lives under tests/ so catalog validate skips private Hermes imports here.
Prefer scripts/demo.py for the no-Hermes competition story.

    HERMES_HOME=/tmp/hhome REWIND_DEBUG_PAYLOAD=/tmp/payload.jsonl \\
      /path/to/hermes-agent/.venv/bin/python -m pytest -q  # normal tests
      /path/to/hermes-agent/.venv/bin/python tests/manual_hermes_live_probe.py /tmp/probe-repo
(plugin linked at $HERMES_HOME/plugins/rewind and enabled; probe-repo is a git repo with app.py)
"""
import json, os, sys

repo = sys.argv[1]
os.chdir(repo)
from hermes_cli.plugins import discover_plugins  # noqa: E402
discover_plugins()
import model_tools  # noqa: E402

import time
K = dict(task_id="probe-task", session_id=f"probe-{int(time.time())}")
call = lambda n, a, i="": model_tools.handle_function_call(n, a, tool_call_id=i, **K)  # noqa: E731
call("read_file", {"path": f"{repo}/app.py"}, "c0")
print("write ->", call("write_file", {"path": f"{repo}/app.py", "content": "v2\n"}, "c1")[:80])
print("term  ->", call("terminal", {"command": "echo hi > hi.txt && git add -A && git commit -qm agent", "workdir": repo}, "c2")[:80])
print(json.loads(call("rewind_restore", {"action": "list"}))["timeline"])
print("fork  ->", call("rewind_restore", {"action": "fork", "step": 2}))
print("undo  ->", call("rewind_restore", {"action": "restore", "step": 1}))
