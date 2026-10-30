#!/usr/bin/env python3
"""The role reusable workflow (.github/workflows/role.yml): its steps' scripts, run as written against an invented
acme policy repo, and the rules its file must keep. Run: python3 test/role/test_role.py (needs pyyaml)."""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
WF = yaml.safe_load((ROOT / ".github" / "workflows" / "role.yml").read_text())
RUN = {s.get("name"): s for s in WF["jobs"]["run"]["steps"]}
PLAN_MINUTES = WF["jobs"]["plan"]["steps"][1]["run"]
PLAN = RUN["the role's file"]["run"].split("\n", 1)[1]  # without its pip install: the test's python has pyyaml
SESSIONS = RUN["the MCP sessions, for Claude Code's env"]["run"]
# The steps call `python` and `python3`: this interpreter's own directory first, where both are and whose pyyaml -I
# can see (setup-python's, or a venv's).
BIN = str(Path(sys.executable).parent)

ROLE = """schema: 1
role: triage
level: L3
brief: roles/triage/BRIEF.md
writes:
  repos: [acme/api]
  issues: [comment]
  pull_requests: [open]
tools:
  board: [board_read, board_post]
  other: [x]
run:
  model: claude-sonnet-5-5
  max_turns: 80
  schedule: "0 9 * * 1"
  clone: [acme/api, acme/web]
  bash: [gh issue, git]
  tools: [WebFetch]
  mcp:
    board:
      url: https://mcp.acme.example/mcp
      header: X-API-Key
budget:
  prs_in_flight: 2
  minutes_per_run: 30
"""


def policy(files: dict) -> str:
    d = tempfile.mkdtemp()
    for p, text in files.items():
        Path(d, p).parent.mkdir(parents=True, exist_ok=True)
        Path(d, p).write_text(text)
    return d


def step(script: str, cwd: str, **env) -> tuple[subprocess.CompletedProcess, dict, str]:
    tmp = tempfile.mkdtemp()
    out, genv = Path(tmp, "output"), Path(tmp, "env")
    full = {**os.environ, "PATH": BIN + ":" + os.environ["PATH"], "RUNNER_TEMP": tmp, "GITHUB_OUTPUT": str(out), "GITHUB_ENV": str(genv), **env}
    r = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], cwd=cwd, env=full, capture_output=True, text=True)
    outputs = dict(l.split("=", 1) for l in out.read_text().splitlines()) if out.exists() else {}
    return r, outputs, tmp


repo = policy({"roles/triage/role.yaml": ROLE, "config.yaml": "human_owner: '@owner'\nairlock:\n  app_login: acme-airlock[bot]\n"})

r, o, _ = step(PLAN_MINUTES, repo, ROLE="triage")
assert r.returncode == 0 and o["minutes"] == "45", (r.stdout, r.stderr, o)
r, o, _ = step(PLAN_MINUTES, policy({"roles/x/role.yaml": "role: x\n"}), ROLE="x")
assert o["minutes"] == "60", "no budget: 45 minutes, plus 15"
r, o, _ = step(PLAN_MINUTES, repo, ROLE="../triage")
assert r.returncode == 1 and "is not a role name" in r.stdout and not o
print("ok  minutes from the role's budget, plus 15; a name that isn't a role's reads nothing")

r, o, tmp = step(PLAN, repo, ROLE="triage", OWNER="acme", POLICY_REPO="acme/policy")
assert r.returncode == 0, r.stdout + r.stderr
assert o["model"] == "claude-sonnet-5-5" and o["max_turns"] == "80" and json.loads(o["connectors"]) == ["board"]
assert o["allowed"] == "mcp__board__board_read,mcp__board__board_post,Bash(gh issue:*),Bash(git:*),Read,Grep,Glob,LS,WebFetch,Edit,Write,MultiEdit", o["allowed"]
assert json.loads(Path(tmp, "clones.json").read_text()) == [{"repo": "acme/api", "write": True}, {"repo": "acme/web", "write": False}]
mcp = json.loads(Path(tmp, "mcp.json").read_text())
assert mcp == {"mcpServers": {"board": {"type": "http", "url": "https://mcp.acme.example/mcp", "headers": {"X-API-Key": "${ROLE_MCP_BOARD_TOKEN}"}}}}, mcp
prompt = Path(tmp, "prompt.txt").read_text()
assert prompt.startswith("You are the triage for acme. The acme/policy repository is in your working directory")
assert "Your GitHub identity is acme-airlock[bot]" in prompt and "agent/triage/" in prompt and "lunareng" not in prompt.lower()
print("ok  the run's model, turns, tools, MCP config, clones and prompt come from the role's file")

r, o, _ = step(PLAN, policy({"roles/w/role.yaml": "role: w\nbrief: b\nwrites: {repos: [acme/api]}\n"}), ROLE="w", OWNER="acme", POLICY_REPO="acme/policy")
assert r.returncode == 1 and "has no run: block" in r.stdout
r, o, _ = step(PLAN, policy({"roles/w/role.yaml": ROLE.replace("clone: [acme/api, acme/web]", "clone: [acme/api, '../../etc']")}), ROLE="w", OWNER="acme", POLICY_REPO="acme/policy")
assert r.returncode == 1 and "not owner/repo" in r.stdout
print("ok  a role without run:, or a clone that isn't owner/repo, stops the run")

r, _, tmp = step(SESSIONS, repo, SESSIONS=json.dumps({"board": {"token": "s" * 40}}), CONNECTORS='["board"]')
assert r.returncode == 0 and Path(tmp, "env").read_text() == f"ROLE_MCP_BOARD_TOKEN={'s' * 40}\n" and "s" * 40 not in r.stdout
r, _, tmp = step(SESSIONS, repo, SESSIONS="{}", CONNECTORS='["board"]')
assert r.returncode == 1 and "no MCP session for board" in r.stdout
r, _, tmp = step(SESSIONS, repo, SESSIONS=json.dumps({"board": {"token": "a\nEVIL=1" + "s" * 30}}), CONNECTORS='["board"]')
assert r.returncode == 1 and (not Path(tmp, "env").exists() or "EVIL" not in Path(tmp, "env").read_text())
print("ok  each MCP session reaches Claude Code's env by the name mcp.json uses; a missing or odd one stops the run")

# The file's rules: every action pinned to a commit, no expression expanded inside a script, the run after the plan.
text = (ROOT / ".github" / "workflows" / "role.yml").read_text()
for job in WF["jobs"].values():
    for s in job["steps"]:
        if "uses" in s:
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", s["uses"]), s["uses"]
        assert "${{" not in s.get("run", ""), f"{s.get('name')}: an expression in a script is an injection; pass it in env"
assert WF["jobs"]["run"]["needs"] == "plan"
assert WF["permissions"] == {"contents": "read", "id-token": "write"}
print("ok  every action pinned, no expression in a script, read-only contents")

print("OK: 0 failing case(s)")
