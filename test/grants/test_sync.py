#!/usr/bin/env python3
"""grants/sync/sync.py against a stand-in server on 127.0.0.1, an invented acme policy repo, and an RSA key made for
the test. Run: python3 test/grants/test_sync.py (needs pyyaml, git and openssl)."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SYNC = ROOT / "grants" / "sync" / "sync.py"
D = Path(tempfile.mkdtemp())
subprocess.run(["openssl", "genrsa", "-out", str(D / "key.pem"), "2048"], check=True, capture_output=True)
subprocess.run(["openssl", "rsa", "-in", str(D / "key.pem"), "-pubout", "-out", str(D / "pub.pem")], check=True, capture_output=True)
KEY = (D / "key.pem").read_text()

TRIAGE = "role: triage\nlevel: L3\nscopes: {board: [read, write]}\nwrites: {repos: [acme/api]}\nrun: {bash: [gh issue]}\n"
CLOCK = "role: clock\nlevel: L4\nkind: workflow\n"


def policy() -> Path:
    d = Path(tempfile.mkdtemp())
    run = lambda *a: subprocess.run(a, cwd=d, check=True, capture_output=True, text=True)  # noqa: E731
    run("git", "init", "-q", "-b", "main")
    for role, text in (("triage", TRIAGE), ("clock", CLOCK)):
        (d / "roles" / role).mkdir(parents=True)
        (d / "roles" / role / "role.yaml").write_text(text)
    run("git", "add", "-A")
    run("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "base")
    return d


class World:
    def __init__(self, put_status=200):
        self.calls, self.put_status = [], put_status


def serve(w: World):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def handle_any(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            w.calls.append((self.command, self.path, dict(self.headers), body))
            if self.command == "POST":
                code, out = 200, {"token": "s" * 40}
            elif self.command == "PUT":
                code, out = w.put_status, {"changed": True}
            else:
                code, out = 204, {}
            self.send_response(code)
            self.end_headers()
            self.wfile.write(json.dumps(out).encode())

        do_POST = do_PUT = do_DELETE = handle_any

    s = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def run(d: Path, *args, w: World | None = None, env=None):
    w = w or World()
    s = serve(w)
    r = subprocess.run([sys.executable, str(SYNC), "--api", f"http://127.0.0.1:{s.server_address[1]}", "--policy-dir", str(d), *args],
                       capture_output=True, text=True, env={**os.environ, "APP_ID": "123", "PRIVATE_KEY": KEY, **(env or {})})
    s.shutdown()
    return r, w


d = policy()
sha = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

r, w = run(d, "--sha", sha, "--dry-run")
lines = [json.loads(l) for l in r.stdout.splitlines()]
assert r.returncode == 0 and w.calls == [], "a dry run sends nothing"
assert {"role": "clock", "skipped": "no scopes in its role file: nothing to grant"} in lines
assert {"role": "triage", "body": {"sha": sha, "level": "L3", "scopes": {"board": ["read", "write"]}, "repos": ["acme/api"],
                                   "bash_prefixes": ["gh issue"], "floors": []}} in lines, lines
print("ok  a dry run prints each role's body, exactly the role file's keys, and skips a role without scopes")

r, w = run(d, "--sha", sha, "--roles", "triage")
assert r.returncode == 0, r.stdout + r.stderr
(m1, p1, h1, b1), (m2, p2, h2, b2), (m3, p3, h3, _) = w.calls
assert (m1, p1, m2, p2, m3, p3) == ("POST", "/api/public/agents/sessions", "PUT", "/api/agents/roles/triage", "DELETE", "/api/agents/sessions/self")
jwt = h1["Authorization"].removeprefix("Bearer ")
head, claims, sig = jwt.split(".")
pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
assert json.loads(base64.urlsafe_b64decode(pad(claims)))["iss"] == "123"
(D / "sig").write_bytes(base64.urlsafe_b64decode(pad(sig)))
v = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(D / "pub.pem"), "-signature", str(D / "sig")],
                   input=f"{head}.{claims}".encode(), capture_output=True)
assert v.returncode == 0, "the App JWT verifies with the App's public key"
assert h2["X-Agent-Session"] == "s" * 40 and h3["X-Agent-Session"] == "s" * 40 and b2["sha"] == sha
assert r.stdout.startswith("::add-mask::" + "s" * 40 + "\n"), "the session is masked before anything else is printed"
print("ok  signs in with an App JWT that verifies, PUTs the grant, ends the session")

r, w = run(d, "--sha", sha, "--roles", "triage", w=World(put_status=409))
assert r.returncode == 1 and w.calls[-1][0] == "DELETE", "a refused grant fails the run, and the session still ends"
print("ok  a refused PUT fails the run, and the session is ended anyway")

(d / "roles/triage/role.yaml").write_text(TRIAGE.replace("[read, write]", "[read]"))
subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "narrow"], check=True)
sha2 = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
r, w = run(d, "--sha", sha2, "--before", sha, "--dry-run")
assert [json.loads(l)["role"] for l in r.stdout.splitlines()] == ["triage"], "a push syncs only the roles it changed"
print("ok  a push syncs only the role files it changed")

for bad in ("../etc", "ghost", "Triage; rm -rf /"):
    r, w = run(d, "--sha", sha, "--roles", bad)
    assert r.returncode == 1 and w.calls == [], bad
r, w = run(d, "--sha", sha, "--roles", "triage", env={"APP_ID": ""})
assert r.returncode == 1 and w.calls == []
print("ok  a role name that isn't one here, or no App to sign in with, sends nothing")

print("OK: 0 failing case(s)")
