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
        self.airlock = (200, {"token": TOKEN, "repository": "acme/policy", "expiresAt": "x", "appLogin": "acme-airlock[bot]"})
        self.files = {"config.yaml": CONFIG, "repos/api.yaml": POLICY, **ROLES}
        self.calls = []
        self.missing_first = 0  # answer 404 for config.yaml this many times first
        self.fail_first = 0     # answer 502 to /v1/read this many times first
        self.limited = {}       # path under contents/ -> [times left, code, headers]: a rate-limit answer first
        self.forbidden = set()  # paths under contents/ answered with a plain 403
        self.stamp = (404, {"message": "no stamp"})  # Airlock's answer to GET /v1/stamps/...


def serve(world: World):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def answer(self, code, body, raw=False, headers=None):
            data = body.encode() if raw else json.dumps(body).encode()
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            world.calls.append(("GET", self.path, self.headers.get("Authorization")))
            if self.path.startswith("/oidc"):
                return self.answer(200, {"value": "oidc-jwt"})
            if self.path.startswith("/v1/stamps/"):
                return self.answer(*world.stamp)
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self.answer(401, {"message": "Bad credentials"})
            if self.path == "/repos/acme/policy":
                return self.answer(200, {"default_branch": "main"})
            if self.path.startswith("/repos/acme/policy/git/trees/main"):
                return self.answer(200, {"truncated": False, "tree": [{"path": p, "type": "blob"} for p in world.files]})
            prefix = "/repos/acme/policy/contents/"
            if self.path.startswith(prefix):
                p = self.path[len(prefix):]
                if world.limited.get(p, [0])[0]:
                    world.limited[p][0] -= 1
                    _, code, headers = world.limited[p]
                    return self.answer(code, {"message": "You have exceeded a secondary rate limit."}, headers=headers)
                if p in world.forbidden:
                    return self.answer(403, {"message": "Resource not accessible by integration"})
                if p == "config.yaml" and world.missing_first:
                    world.missing_first -= 1
                    return self.answer(404, {"message": "Not Found"})
                return self.answer(200, world.files[p], raw=True) if p in world.files else self.answer(404, {"message": "Not Found"})
            self.answer(404, {"message": "Not Found"})

        def do_POST(self):
            world.calls.append(("POST", self.path, self.headers.get("Authorization")))
            if world.fail_first:
                world.fail_first -= 1
                return self.answer(502, {"message": "Bad Gateway"})
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

w = World()
w.missing_first = 1
w.fail_first = 1
r, cfg, policy = run(w)
assert r.returncode == 0 and cfg and policy == POLICY, r.stdout + r.stderr
assert sum(1 for c in w.calls if c[0] == "POST") == 2, "a 502 from Airlock is retried once"
assert sum(1 for c in w.calls if c[1].endswith("/contents/config.yaml")) == 2, "a 404 for config.yaml is retried once"
print("ok  a 502 from Airlock, or a 404 for config.yaml from a fresh token, is retried before the gate fails closed")

w = World()
w.files["config.yaml"] = "agent_markers: {}\n"
w.missing_first = 0
r, cfg, policy = run(w)
assert r.returncode == 1 and "no config.yaml naming a human_owner" in r.stdout, "a config.yaml without an owner still fails, at once"

# The tenant's floors reach the gate as config.yaml writes them, and floors the gate can't read stop it here.
w = World()
w.files["config.yaml"] = CONFIG + "floors:\n  actions: {send: L2}\n  paths: {firmware: L1, ehi: L1}\n"
r, cfg, policy = run(w)
assert r.returncode == 0 and cfg["floors"] == {"actions": {"send": "L2"}, "paths": {"firmware": "L1", "ehi": "L1"}}, (r.stdout, cfg)
assert list(cfg["floors"]["paths"]) == ["firmware", "ehi"], "in the tenant's order"
w = World()
w.files["config.yaml"] = CONFIG + "floors:\n  path: {firmware: L1}\n"
r, cfg, policy = run(w)
errors = [l for l in r.stdout.splitlines() if l.startswith("::error")]
assert r.returncode == 1 and cfg is None and errors == ["::error title=autonomy gate::acme/policy's config.yaml: floors has path; it takes only actions and paths; no policy, no gate"], errors
assert ("DELETE", "/installation/token", f"Bearer {TOKEN}") in w.calls, "and the read is still ended"
print("ok  config.yaml's floors pass through to the gate, and floors it can't read fail it")

# A burst of gates reading role files at once gets GitHub's rate-limit 403 (lunareng/airlock-actions#42). That answer,
# a 403 or 429 with retry-after or x-ratelimit-remaining 0, is retried with the same backoff; a plain 403 is the answer.
ROLE = "roles/station/role.yaml"
for code, headers in [(403, {"retry-after": "60"}), (403, {"x-ratelimit-remaining": "0"}), (429, {"retry-after": "1"})]:
    w = World()
    w.limited[ROLE] = [1, code, headers]
    r, cfg, policy = run(w)
    assert r.returncode == 0 and cfg and policy == POLICY, (code, headers, r.stdout + r.stderr)
    assert sum(1 for c in w.calls if c[1].endswith("/contents/" + ROLE)) == 2, "read again once"
    assert "acme-station[bot]" in cfg["agent_markers"]["logins"], "and the retried file is in the gate's config"
w = World()
w.limited[ROLE] = [3, 403, {"retry-after": "60"}]
r, cfg, policy = run(w)
errors = [l for l in r.stdout.splitlines() if l.startswith("::error")]
assert r.returncode == 1 and cfg is None and policy is None, r.stdout
assert errors == [f"::error title=autonomy gate::the policy repo (repos/acme/policy/contents/{ROLE}): HTTP 403, rate limited after 3 tries; no policy, no gate"], errors
assert sum(1 for c in w.calls if c[1].endswith("/contents/" + ROLE)) == 3, "three tries, then the gate fails closed"
assert ("DELETE", "/installation/token", f"Bearer {TOKEN}") in w.calls, "and the read is still ended"
w = World()
w.forbidden.add(ROLE)
r, cfg, policy = run(w)
errors = [l for l in r.stdout.splitlines() if l.startswith("::error")]
assert r.returncode == 1 and cfg is None and policy is None, r.stdout
assert errors == [f"::error title=autonomy gate::the policy repo (repos/acme/policy/contents/{ROLE}): HTTP 403; no policy, no gate"], errors
assert sum(1 for c in w.calls if c[1].endswith("/contents/" + ROLE)) == 1, "a plain 403 is not retried"
print("ok  a rate-limit 403 or 429 is retried before the gate fails closed; a plain 403 fails it at once")

# lunareng/airlock#251: every pull request Airlock's App opens is an agent's, on every tenant, with nothing in
# config.yaml that Airlock's docs don't ask for. Airlock names its own App in the read (appLogin), and the gate takes it
# from there. End to end: policy.py writes the gate's config from a config.yaml with no `airlock:` block, and gate.py
# scores a pull request the App opened with it.
APP = "lunar-astronauts[bot]"
BARE = "human_owner: '@owner'\n"   # what the quickstart writes: no agent_markers, no airlock block
GATE = ROOT / "gate" / "gate.py"


def app_world(config=BARE, roles=None, **answer):
    w = World()
    w.airlock = (200, {"token": TOKEN, "repository": "acme/policy", "expiresAt": "x", "appLogin": APP, **answer})
    w.files = {"config.yaml": config, "repos/api.yaml": POLICY, **(ROLES if roles is None else roles)}
    return w


def scored(cfg, path, *extra, author=APP, stamp=None):
    """gate.py's verdict for a one-file pull request by `author`, under the gate config policy.py wrote."""
    with tempfile.TemporaryDirectory() as d:
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *a], cwd=d, check=True, capture_output=True)  # noqa: E731
        git("init", "-q", "-b", "main")
        Path(d, "AUTONOMY.yaml").write_text("default: L3\npaths:\n  - {glob: 'infra/**', level: L1, why: Infrastructure.}\n  - {glob: 'src/pay/**', level: L2, why: Money.}\n")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        Path(d, path).parent.mkdir(parents=True, exist_ok=True)
        Path(d, path).write_text("x\n")
        git("add", "-A")
        git("commit", "-q", "-m", "change")
        Path(d, "gate-config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        if stamp is not None:
            Path(d, "stamp.json").write_text(json.dumps(stamp))
        r = subprocess.run([sys.executable, str(GATE), "--base", "HEAD~1", "--author", author, "--branch", "agent/builder/x",
                            "--config", f"{d}/gate-config.yaml", "--stamp-file", f"{d}/stamp.json", "--json", *extra], cwd=d, capture_output=True, text=True)
        return r.returncode, json.loads(r.stdout)


r, cfg, policy = run(app_world())
assert r.returncode == 0 and cfg, r.stdout + r.stderr
assert APP in cfg["agent_markers"]["logins"], f"Airlock's App is an agent login with no airlock block: {cfg}"
assert cfg["airlock"]["app_login"] == APP, cfg
code, out = scored(cfg, "infra/main.tf", stamp={"role": "builder", "reason": None})
assert code == 1 and out["level"] == "L1" and out["agent"] and not out["allowed"], f"an L1 pull request the App opened passed as a person's: {out}"
code, out = scored(cfg, "src/pay/charge.py", stamp={"role": "builder", "reason": None})
assert code == 1 and out["level"] == "L2" and out["agent"] and not out["allowed"], f"an L2 pull request the App opened passed as a person's: {out}"
code, out = scored(cfg, "src/pay/charge.py", "--approved", stamp={"role": "builder", "reason": None})
assert code == 0 and out["allowed"], f"the owner's approval of the head commit passes it at L2: {out}"
code, out = scored(cfg, "README.md", stamp={"role": "builder", "reason": None})
assert code == 0 and out["level"] == "L3" and out["agent"] and out["credited"] and out["allowed"], f"at L3 with a stamp naming a role it passes: {out}"
code, out = scored(cfg, "README.md")
assert code == 1 and out["level"] == "L3" and not out["credited"] and not out["allowed"], f"at L3 with no stamp it fails: {out}"
code, out = scored(cfg, "README.md", stamp={"role": None, "reason": "the branch doesn't name a role"})
assert code == 1 and not out["credited"], f"a stamp that credits no role fails it too: {out}"
code, out = scored(cfg, "README.md", "--approved")
assert code == 0 and out["allowed"], f"with no stamp, the owner's approval of the head commit passes it: {out}"
code, out = scored(cfg, "infra/main.tf", author="ada")
assert code == 0 and not out["agent"], f"a person's pull request is still a person's: {out}"
print("ok  a pull request Airlock's App opened is an agent's with no airlock block: L1 and L2 fail, L3 needs its stamp (lunareng/airlock#251)")

# The App's login is Airlock's to say, whatever the role files say: no role on Airlock yet (one mid-switch still pushes
# as the App), a role file that isn't a mapping, or no role files at all.
for label, roles in {"no role is on Airlock": {"roles/station/role.yaml": "role: station\nidentity:\n  github_login: acme-station[bot]\n"},
                     "a role file the gate can't read as a mapping": {"roles/clerk/role.yaml": "- not\n- a mapping\n"},
                     "no role files": {}}.items():
    r, cfg, policy = run(app_world(roles=roles))
    assert r.returncode == 0 and APP in cfg["agent_markers"]["logins"] and cfg["airlock"]["app_login"] == APP, (label, r.stdout, cfg)
    code, out = scored(cfg, "infra/main.tf", stamp={"role": "builder", "reason": None})
    assert code == 1 and out["agent"], (label, out)
print("ok  Airlock's App is an agent login whether or not a role file says a role is on Airlock")

# An Airlock that answers no appLogin (an older one) fails the gate closed, and says which issue: the gate never falls
# back to scoring the App's pull request as a person's. So does an appLogin that isn't an App's bot login.
for label, answer in {"no appLogin": None, "an empty one": "", "a person's login": "ada", "not text": 7, "a list": [APP],
                      "one that would start a workflow command": "x[bot]\n::warning::y", "a login with a space": "lunar astronauts[bot]"}.items():
    w = app_world()
    if answer is None:
        w.airlock[1].pop("appLogin")
    else:
        w.airlock[1]["appLogin"] = answer
    r, cfg, policy = run(w)
    errors = [l for l in r.stdout.splitlines() if l.startswith("::") and not l.startswith("::add-mask::")]
    assert r.returncode == 1 and cfg is None and policy is None, (label, r.stdout)
    assert errors == ["::error title=autonomy gate::Airlock's answer doesn't name its App (appLogin), so the gate can't tell which pull requests "
                      "Airlock's App opened; this gate needs an Airlock that answers it (lunareng/airlock#251); no policy, no gate"], (label, errors)
    assert ("DELETE", "/installation/token", f"Bearer {TOKEN}") in w.calls, f"{label}: the read is still ended"
    assert not any(c[1].startswith("/repos/") for c in w.calls), f"{label}: and the policy repo is never read"
w = app_world()
w.airlock = (200, [{"token": TOKEN}])
r, cfg, policy = run(w)
assert r.returncode == 1 and cfg is None and "isn't one the gate can read" in r.stdout, r.stdout
w = app_world(repository="not a repository")
r, cfg, policy = run(w)
assert r.returncode == 1 and cfg is None and "names no policy repository" in r.stdout, r.stdout
assert ("DELETE", "/installation/token", f"Bearer {TOKEN}") in w.calls, "a read whose answer names no repository is ended too"
print("ok  an Airlock that answers no appLogin fails the gate closed, naming lunareng/airlock#251, and the read is ended")

# config.yaml may still carry an `airlock:` block (other actions read it). Its app_login is kept only when it is
# Airlock's own; a different one is a mistake the gate fails closed on, and says so.
r, cfg, policy = run(app_world(config=BARE + f"airlock:\n  app_login: '{APP}'\n  url: https://airlock.example.com\n  stamp_wait_seconds: 30\n"))
assert r.returncode == 0 and cfg["airlock"]["app_login"] == APP and cfg["airlock"]["stamp_wait_seconds"] == 30, (r.stdout, cfg)
assert cfg["agent_markers"]["logins"].count(APP) == 1, cfg
for label, block in {"another App's login": "airlock:\n  app_login: 'acme-agents[bot]'\n",
                     "a person's login": "airlock:\n  app_login: ada\n",
                     "Airlock's in another case": "airlock:\n  app_login: 'Lunar-Astronauts[bot]'\n",
                     "not text": "airlock:\n  app_login: [x]\n",
                     "an empty one": "airlock:\n  app_login: ''\n"}.items():
    w = app_world(config=BARE + block)
    r, cfg, policy = run(w)
    errors = [l for l in r.stdout.splitlines() if l.startswith("::error")]
    assert r.returncode == 1 and cfg is None and policy is None, (label, r.stdout)
    assert len(errors) == 1 and "config.yaml's airlock.app_login is " in errors[0] and f"and Airlock's App is '{APP}'" in errors[0] \
        and errors[0].endswith("the gate learns Airlock's App from Airlock itself); no policy, no gate"), (label, errors)
    assert ("DELETE", "/installation/token", f"Bearer {TOKEN}") in w.calls, f"{label}: the read is still ended"
w = app_world(config=BARE + "airlock: 'lunar-astronauts[bot]'\n")
r, cfg, policy = run(w)
assert r.returncode == 1 and cfg is None and "config.yaml's airlock is not a mapping" in r.stdout, r.stdout
w = app_world(config=BARE + "airlock:\n  app_login: \"x\\n::warning::y\"\n")
r, cfg, policy = run(w)
assert r.returncode == 1 and len([l for l in r.stdout.splitlines() if l.startswith("::") and not l.startswith("::add-mask::")]) == 1, "a login in config.yaml can't start a second command"
w = app_world(config=BARE + "agent_markers: [x]\n")
r, cfg, policy = run(w)
assert r.returncode == 1 and cfg is None and "agent_markers is not a mapping" in r.stdout, r.stdout
print("ok  config.yaml's airlock.app_login is kept only when it is Airlock's; any other fails the gate closed")

# The stamp is read from the Airlock the gate read the policy from. config.yaml's airlock.url is used only when it is
# that origin; another is left unused, and the log says so.
w = app_world()
r, cfg, policy = run(w)
origin = cfg["airlock"]["url"]
assert origin.startswith("http://127.0.0.1:") and not origin.endswith("/"), "the gate's config names the origin the read used"
assert "::warning" not in r.stdout, r.stdout
r, cfg, policy = run(app_world(config=BARE + "airlock:\n  url: https://elsewhere.example.com\n"))
warnings = [l for l in r.stdout.splitlines() if l.startswith("::warning")]
assert r.returncode == 0 and cfg["airlock"]["url"].startswith("http://127.0.0.1:"), (r.stdout, cfg)
assert len(warnings) == 1 and "config.yaml's airlock.url is 'https://elsewhere.example.com', which is not the Airlock this gate reads from" in warnings[0], warnings
r, cfg, policy = run(app_world(config=BARE + "airlock:\n  url: [x]\n"))
assert r.returncode == 0 and cfg["airlock"]["url"].startswith("http://127.0.0.1:") and "::warning" in r.stdout, (r.stdout, cfg)
print("ok  the gate's config names the Airlock the read used; another airlock.url in config.yaml is not used")

# The step that reads the stamp (gate/action.yml), run as the runner runs it: the App's login comes from the gate's
# config, which policy.py wrote from Airlock's answer, and the stamp from this action's own airlock-url, never from an
# origin config.yaml names.
ACTION = yaml.safe_load((ROOT / "gate" / "action.yml").read_text())
STAMP_STEP = next(st for st in ACTION["runs"]["steps"] if (st.get("name") or "").startswith("which role pushed the head"))
assert STAMP_STEP["env"]["AIRLOCK"] == "${{ inputs.airlock-url }}", "the stamp is read from the action's own Airlock"
assert ACTION["inputs"]["airlock-url"]["default"] == "https://airlock.lunareng.com"
assert '"url"' not in STAMP_STEP["run"] and ".get(\"url\"" not in STAMP_STEP["run"], "and never from a url in the gate's config"
HEAD_SHA = "a" * 40


def stamp_step(world, cfg, author=APP, slash=""):
    """Run the step's script with a stand-in for Airlock; (exit code, the stamp file's JSON or None, the stamp calls)."""
    srv = serve(world)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with tempfile.TemporaryDirectory() as d:
        Path(d, "bin").mkdir()
        os.symlink(sys.executable, Path(d, "bin", "python"))
        Path(d, "gate-config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        env = {**os.environ, "PATH": f"{d}/bin:{os.environ['PATH']}", "RUNNER_TEMP": d, "AIRLOCK": base + slash, "AUTHOR": author,
               "OWNER_ID": "9000001", "REPO_ID": "9200001", "HEAD": HEAD_SHA}
        r = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", STAMP_STEP["run"]], env=env, capture_output=True, text=True, timeout=60)
        f = Path(d, "airlock-stamp.json")
        stamp = json.loads(f.read_text()) if f.exists() else None
    srv.shutdown()
    return r, stamp, [c[1] for c in world.calls if c[1].startswith("/v1/stamps/")]


r, cfg, policy = run(app_world())   # config.yaml has no airlock block
cfg["airlock"]["stamp_wait_seconds"] = 0
w = World()
w.stamp = (200, {"role": "builder", "reason": None, "startedBy": []})
r, stamp, calls = stamp_step(w, cfg)
assert r.returncode == 0 and stamp == {"role": "builder", "reason": None, "startedBy": []}, (r.stdout, r.stderr, stamp)
assert calls == [f"/v1/stamps/9000001/9200001/{HEAD_SHA}"], calls
code, out = scored(cfg, "README.md", stamp=stamp)
assert code == 0 and out["credited"] and out["agent"], f"at L3, the App's pull request with a stamp naming a role passes: {out}"
w = World()
r, stamp, calls = stamp_step(w, cfg)
assert r.returncode == 0 and stamp == {"role": None, "reason": f"Airlock has no stamp for {HEAD_SHA}"} and len(calls) == 1, (r.stdout, r.stderr, stamp)
code, out = scored(cfg, "README.md", stamp=stamp)
assert code == 1 and not out["credited"], f"with no stamp it fails at L3: {out}"
code, out = scored(cfg, "infra/main.tf", stamp=stamp)
assert code == 1 and out["level"] == "L1", f"and at L1: {out}"
# A url in the gate's config that isn't this Airlock is not where the stamp comes from; a trailing slash on the input is fine.
w = World()
w.stamp = (200, {"role": "builder", "reason": None})
r, stamp, calls = stamp_step(w, {**cfg, "airlock": {**cfg["airlock"], "url": "http://127.0.0.1:1"}}, slash="/")
assert r.returncode == 0 and stamp == {"role": "builder", "reason": None} and calls == [f"/v1/stamps/9000001/9200001/{HEAD_SHA}"], (r.stdout, r.stderr, stamp, calls)
# A stamp_wait_seconds that isn't a number of seconds is the default, never a broken step.
r, stamp, calls = stamp_step(w, {**cfg, "airlock": {**cfg["airlock"], "stamp_wait_seconds": "soon; rm -rf /"}})
assert r.returncode == 0 and stamp == {"role": "builder", "reason": None}, (r.stdout, r.stderr)
# Anyone else's pull request asks Airlock nothing.
w = World()
for author in ("ada", "acme-station[bot]", "lunar-astronauts", ""):
    r, stamp, calls = stamp_step(w, cfg, author=author)
    assert r.returncode == 0 and stamp is None and calls == [], (author, r.stdout, r.stderr, calls)
print("ok  the stamp is read from the action's own Airlock for the App's pull requests, and for nobody else's")

# airlock-actions#35: the gate's config names the repository read and its config files, so a PR in the policy repo that
# changes one scores L2 or stricter. Those keys are the read's own: config.yaml can't set them.
r, cfg, policy = run(app_world(config=BARE + "policy_repository: acme/api\npolicy_config_files: [nothing.yaml]\n"))
assert r.returncode == 0 and cfg["policy_repository"] == "acme/policy" and cfg["policy_config_files"] == ["config.yaml"], cfg
for named, files in [("airlock/config.yaml", ["config.yaml", "airlock/config.yaml"]), ("config.yaml", ["config.yaml"]),
                     ("../config.yaml", ["config.yaml"]), ("/etc/config.yaml", ["config.yaml"]), ("a//b.yaml", ["config.yaml"]),
                     ("a\nb.yaml", ["config.yaml"]), (7, ["config.yaml"]), (None, ["config.yaml"])]:
    r, cfg, policy = run(app_world(policyConfigFile=named))
    assert r.returncode == 0 and cfg["policy_config_files"] == files, (named, cfg)
print("ok  the gate's config names the policy repo and its config files; a policyConfigFile outside the repo is config.yaml alone")

print("OK: 0 failing case(s)")
