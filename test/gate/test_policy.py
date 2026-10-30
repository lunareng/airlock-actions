#!/usr/bin/env python3
"""gate/policy.py against one stand-in for GitHub's OIDC endpoint, Airlock and the GitHub API, on 127.0.0.1.
Run: python3 test/gate/test_policy.py"""
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "gate" / "policy.py"
TOKEN = "ghs_" + "x" * 36
CONFIG = "human_owner: '@owner'\nagent_markers:\n  logins: []\nairlock:\n  app_login: acme-airlock[bot]\n"
ROLES = {
    "roles/clerk/role.yaml": "role: clerk\nidentity:\n  key_store: airlock\n",
    "roles/station/role.yaml": "role: station\nidentity:\n  github_login: acme-station[bot]\n",
}
POLICY = "repo: acme/api\ndefault: L3\n"


class World:
    def __init__(self):
        self.airlock = (200, {"token": TOKEN, "repository": "acme/policy", "expiresAt": "x"})
        self.files = {"config.yaml": CONFIG, "repos/api.yaml": POLICY, **ROLES}
        self.calls = []


def serve(world: World):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def answer(self, code, body, raw=False):
            data = body.encode() if raw else json.dumps(body).encode()
            self.send_response(code)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            world.calls.append(("GET", self.path, self.headers.get("Authorization")))
            if self.path.startswith("/oidc"):
                return self.answer(200, {"value": "oidc-jwt"})
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self.answer(401, {"message": "Bad credentials"})
            if self.path == "/repos/acme/policy":
                return self.answer(200, {"default_branch": "main"})
            if self.path.startswith("/repos/acme/policy/git/trees/main"):
                return self.answer(200, {"truncated": False, "tree": [{"path": p, "type": "blob"} for p in world.files]})
            prefix = "/repos/acme/policy/contents/"
            if self.path.startswith(prefix):
                p = self.path[len(prefix):]
                return self.answer(200, world.files[p], raw=True) if p in world.files else self.answer(404, {"message": "Not Found"})
            self.answer(404, {"message": "Not Found"})

        def do_POST(self):
            world.calls.append(("POST", self.path, self.headers.get("Authorization")))
            code, body = world.airlock
            self.answer(code, body)

        def do_DELETE(self):
            world.calls.append(("DELETE", self.path, self.headers.get("Authorization")))
            self.answer(204, {})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def run(world: World, repo_name="api"):
    srv = serve(world)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "ACTIONS_ID_TOKEN_REQUEST_URL": f"{base}/oidc?x=1", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req", "GITHUB_API_URL": base}
        r = subprocess.run([sys.executable, str(SCRIPT), "--airlock", base, "--repo-name", repo_name,
                            "--config-out", f"{d}/config.yaml", "--policy-out", f"{d}/policy.yaml"], env=env, capture_output=True, text=True)
        cfg = yaml.safe_load(Path(d, "config.yaml").read_text()) if Path(d, "config.yaml").exists() else None
        policy = Path(d, "policy.yaml").read_text() if Path(d, "policy.yaml").exists() else None
    srv.shutdown()
    return r, cfg, policy


def printed(r):
    """What the job's log shows: everything but the mask command, which GitHub never prints."""
    return "\n".join(l for l in (r.stdout + r.stderr).splitlines() if not l.startswith("::add-mask::"))


w = World()
r, cfg, policy = run(w)
assert r.returncode == 0, r.stdout + r.stderr
assert cfg["human_owner"] == "@owner" and cfg["agent_markers"]["logins"] == ["acme-airlock[bot]", "acme-station[bot]"], cfg
assert policy == POLICY
assert r.stdout.startswith(f"::add-mask::{TOKEN}\n"), "the token is masked before anything else"
assert TOKEN not in printed(r), "and never printed"
assert ("DELETE", "/installation/token", f"Bearer {TOKEN}") in w.calls, "the read is ended"
assert ("POST", "/v1/read", "Bearer oidc-jwt") in w.calls, "Airlock gets the job's OIDC token"
print("ok  reads config, every role's login and this repo's file, then ends the read")

w = World()
w.files.pop("repos/api.yaml")
r, cfg, policy = run(w)
assert r.returncode == 0 and cfg and policy is None and "AUTONOMY.yaml alone" in r.stdout, r.stdout
print("ok  no file for this repo: the gate scores by AUTONOMY.yaml alone")

w = World()
w.airlock = (403, {"decision": "refused", "reason": "wrong_workflow", "message": "not autonomy-gate.yml\n::warning::x"})
r, cfg, policy = run(w)
assert r.returncode == 1 and cfg is None and policy is None
errors = [l for l in r.stdout.splitlines() if l.startswith("::")]
assert errors == ["::error title=autonomy gate::Airlock refused the policy read: wrong_workflow (not autonomy-gate.yml : :warning: :x); no policy, no gate"], errors
print("ok  Airlock's refusal fails the gate, says why, and can't start a second command")

w = World()
w.airlock = (200, {"token": "short"})
r, cfg, policy = run(w)
assert r.returncode == 1 and cfg is None and "::add-mask::" not in r.stdout
print("ok  an answer without a token fails the gate")

w = World()
w.files["config.yaml"] = "agent_markers: {}\n"
r, cfg, policy = run(w)
assert r.returncode == 1 and cfg is None and "no config.yaml naming a human_owner" in r.stdout
assert ("DELETE", "/installation/token", f"Bearer {TOKEN}") in w.calls, "a failed read is ended too"
print("ok  a policy repo without an owner fails the gate, and the read is still ended")

r, cfg, policy = run(World(), repo_name="../etc")
assert r.returncode == 1 and cfg is None
print("ok  a repository name that isn't a file name reads nothing")

print("OK: 0 failing case(s)")
