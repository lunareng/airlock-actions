#!/usr/bin/env python3
"""validate/check.py against an invented acme policy repo and a stand-in Airlock on 127.0.0.1 serving small test
schemas (the action fetches the real ones at run time; nothing here copies them). Run: python3 test/validate/test_validate.py"""
import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CHECK = ROOT / "validate" / "check.py"
LEVEL = {"enum": ["L1", "L2", "L3", "L4"]}
SCHEMAS = {
    "autonomy.schema.json": {"type": "object", "required": ["repo", "default"], "properties": {
        "repo": {"type": "string"}, "default": LEVEL, "actions": {"type": "object"}, "paths": {"type": "array"},
        "policy": {"type": "object"}, "judge": {"type": "object"}, "schema": {"type": "integer"}}, "additionalProperties": False},
    "role.schema.json": {"type": "object", "required": ["role", "level"], "properties": {
        "role": {"type": "string"}, "level": LEVEL, "brief": {"type": "string"}, "writes": {"type": "object"},
        "run": {"type": "object"}, "judge": {"type": "object"}, "schema": {"type": "integer"}}, "additionalProperties": False},
}
REPO = "schema: 1\nrepo: acme/api\ndefault: L3\nactions:\n  send: L2\npaths:\n  - glob: src/billing/**\n    level: L2\n"
GOOD = "schema: 1\nrole: triage\nlevel: L3\nbrief: roles/triage/BRIEF.md\nwrites:\n  repos: [acme/api]\n"
BAD = "schema: 1\nrole: scribe\nlevel: L5\nbrief: roles/scribe/BRIEF.md\n"


def stand_in(status=200):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            name = self.path.rsplit("/", 1)[-1]
            if status != 200 or name not in SCHEMAS:
                self.send_response(status if status != 200 else 404); self.end_headers(); return
            self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(SCHEMAS[name]).encode())

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def repo(files: dict) -> str:
    d = tempfile.mkdtemp()
    for p, text in files.items():
        Path(d, p).parent.mkdir(parents=True, exist_ok=True)
        Path(d, p).write_text(text)
    return d


def check(d: str, *extra, status=200):
    srv = stand_in(status)
    r = subprocess.run([sys.executable, str(CHECK), "--airlock", f"http://127.0.0.1:{srv.server_address[1]}", "--owner", "acme", *extra],
                       cwd=d, capture_output=True, text=True)
    srv.shutdown()
    return r, [l for l in r.stdout.splitlines() if l.startswith("::error")]


BASE = {"repos/api.yaml": REPO, "roles/triage/role.yaml": GOOD, "roles/triage/BRIEF.md": "x", "roles/scribe/BRIEF.md": "x"}

r, errors = check(repo(BASE))
assert r.returncode == 0 and errors == [] and "OK: 0 problem(s)" in r.stdout, r.stdout + r.stderr
print("ok  a valid policy repo passes")

r, errors = check(repo({**BASE, "roles/scribe/role.yaml": BAD}))
assert r.returncode == 1 and len(errors) == 1, errors
assert errors[0].startswith("::error file=roles/scribe/role.yaml,line=3::level: 'L5' is not one of"), errors
print("ok  one valid and one invalid role file: exactly one annotation, on the invalid file's line")

r, errors = check(repo({**BASE, "repos/api.yaml": REPO.replace("send: L2", "send: L3").replace("level: L2", "level: L4") + "policy:\n  loosen: auto\n"}))
msgs = "\n".join(errors)
assert r.returncode == 1 and "actions.send is L3; the charter's floor is L2" in msgs and "looser than the default L3" in msgs and "policy.loosen must be review" in msgs, msgs
print("ok  above a floor, a carve-out looser than the default, and loosen other than review all fail")

r, errors = check(repo({**BASE, "repos/api.yaml": REPO.replace("acme/api", "globex/api")}))
assert len(errors) == 1 and "repo must be acme/api" in errors[0], errors
r, errors = check(repo({**BASE, "roles/triage/role.yaml": GOOD.replace("[acme/api]", "[acme/web]") + "run:\n  clone: [globex/x]\n"}))
msgs = "\n".join(errors)
assert "writes to acme/web, which has no repos/*.yaml policy" in msgs and "clones globex/x, which isn't one of acme's" in msgs, msgs
r, errors = check(repo({**BASE, "roles/triage/role.yaml": GOOD.replace("role: triage", "role: other")}))
assert len(errors) == 1 and "must match its folder 'triage'" in errors[0], errors
r, errors = check(repo({**BASE, "roles/triage/role.yaml": GOOD.replace("roles/triage/BRIEF.md", "roles/triage/MISSING.md")}))
assert len(errors) == 1 and "brief roles/triage/MISSING.md does not exist" in errors[0], errors
print("ok  another org's repo, an unknown or foreign repository, a role named off its folder, a missing brief")

r, errors = check(repo({**BASE, "roles/triage/role.yaml": "role: [triage\n"}))
assert r.returncode == 1 and len(errors) == 1 and "not YAML" in errors[0] and "file=roles/triage/role.yaml" in errors[0], errors
print("ok  a file that isn't YAML is a problem of its own")

r, errors = check(repo(BASE), status=503)
assert r.returncode == 1 and "Airlock: couldn't check the policy (HTTP 503" in r.stdout + r.stderr
print("ok  Airlock answering outside 2xx fails closed")

d = repo(BASE)
run = lambda *a: subprocess.run(a, cwd=d, check=True, capture_output=True, text=True)  # noqa: E731
run("git", "init", "-q", "-b", "main")
run("git", "add", "-A")
run("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "base")
Path(d, "repos/api.yaml").write_text(REPO.replace("default: L3", "default: L4\njudge:\n  check: {workflow: ci.yml}").replace("paths:\n  - glob: src/billing/**\n    level: L2\n", ""))
r, errors = check(d, "--base", "HEAD")
msgs = "\n".join(errors)
assert "default loosened L3 -> L4" in msgs and "carve-out src/billing/** removed" in msgs, msgs
r, errors = check(d)
assert r.returncode == 0, ("without --base, the same loosening is no problem: it waits for the owner, not the schema", errors)
print("ok  with --base, a loosened level or a removed carve-out is a problem")

r, errors = check(repo({**BASE, "roles/scribe/role.yaml": BAD.replace("scribe\nlevel", "scribe\n,evil: x\nlevel")}))
assert all("\n" not in e for e in errors)
print("ok  every annotation is one line")

print("OK: 0 failing case(s)")
