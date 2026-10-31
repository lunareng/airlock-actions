#!/usr/bin/env python3
"""Push each changed role's grant to the server that enforces it (an api or MCP server holding agent grants).

A role's file says what its sessions may do (`scopes`), where it writes (`writes.repos`), what it may run (`run.bash`)
and its floors. A server that enforces those on its own routes keeps a copy, its grants. This sends the server exactly
what the role file says at this commit, so the copy follows main. The server should re-read the same file at that
commit and refuse a body that differs.

The protocol (the server implements it):
  POST   <api>/api/public/agents/sessions   Authorization: Bearer <GitHub App JWT>   {"runtime","run_ref","launched_by"}
         -> {"token": <session>}   the App is the grant-syncing identity the server trusts
  PUT    <api>/api/agents/roles/<role>      X-Agent-Session: <session>   {"sha","level","scopes","repos","bash_prefixes","floors"}
  DELETE <api>/api/agents/sessions/self     X-Agent-Session: <session>   (ends the session)

A role whose file has no `scopes` is skipped: there is nothing to grant.

  sync.py --api <origin> --sha <commit> [--before <commit>] [--roles "a b"] [--dry-run]
    Roles: --roles, else every role a push changed (--before..--sha), else every role. Env APP_ID and PRIVATE_KEY sign
    in (not needed with --dry-run), plus RUN_REF and LAUNCHED_BY for the session. Exit 1 if any PUT fails.
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROLE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def body(root: Path, role: str, sha: str) -> dict:
    doc = yaml.safe_load((root / "roles" / role / "role.yaml").read_text())
    return {
        "sha": sha,
        "level": doc["level"],
        "scopes": doc.get("scopes") or {},
        "repos": doc.get("writes", {}).get("repos", []),
        "bash_prefixes": doc.get("run", {}).get("bash", []),
        "floors": doc.get("floors", []),
    }


def has_scopes(root: Path, role: str) -> bool:
    doc = yaml.safe_load((root / "roles" / role / "role.yaml").read_text())
    return bool(doc.get("scopes"))


def changed_roles(root: Path, before: str, sha: str) -> list[str]:
    if not before or set(before) == {"0"}:
        return sorted(p.parent.name for p in (root / "roles").glob("*/role.yaml"))
    out = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "-z", before, sha, "--", "roles/*/role.yaml"],
                         check=True, capture_output=True).stdout.decode()
    return sorted({p.split("/")[1] for p in out.split("\0") if p})


def app_jwt(app_id: str, private_key: str) -> str:
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    now = int(time.time())
    head = b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}, separators=(",", ":")).encode())
    with tempfile.TemporaryDirectory() as d:
        key = Path(d, "key.pem")
        key.write_text(private_key)
        key.chmod(0o600)
        sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key)], input=f"{head}.{claims}".encode(),
                             check=True, capture_output=True).stdout
    return f"{head}.{claims}.{b64(sig)}"


def call(method: str, url: str, headers: dict, payload: dict | None = None) -> tuple[int, str]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True)
    ap.add_argument("--sha", required=True)
    ap.add_argument("--before", default="")
    ap.add_argument("--roles", default="")
    ap.add_argument("--policy-dir", default=os.environ.get("GITHUB_WORKSPACE") or ".")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    root = Path(a.policy_dir)
    api = a.api.rstrip("/")
    if not re.fullmatch(r"https://[a-z0-9.-]+(:\d+)?|http://127\.0\.0\.1(:\d+)?", api):
        print("::error title=airlock grants sync::api-url must be an https origin")
        return 1
    roles = a.roles.split() if a.roles.strip() else changed_roles(root, a.before, a.sha)
    bad = [r for r in roles if not ROLE.fullmatch(r) or not (root / "roles" / r / "role.yaml").is_file()]
    if bad:
        print(f"::error title=airlock grants sync::not a role with a file here: {' '.join(bad)}")
        return 1
    todo = []
    for role in roles:
        if has_scopes(root, role):
            todo.append(role)
        else:
            print(json.dumps({"role": role, "skipped": "no scopes in its role file: nothing to grant"}))
    if a.dry_run:
        for role in todo:
            print(json.dumps({"role": role, "body": body(root, role, a.sha)}))
        return 0
    if not todo:
        return 0
    app_id, key = os.environ.get("APP_ID", ""), os.environ.get("PRIVATE_KEY", "")
    if not app_id or not key:
        print("::error title=airlock grants sync::no app-id or private-key to sign in with")
        return 1
    status, text = call("POST", f"{api}/api/public/agents/sessions", {"Authorization": f"Bearer {app_jwt(app_id, key)}"},
                        {"runtime": "actions", "run_ref": os.environ.get("RUN_REF", ""), "launched_by": os.environ.get("LAUNCHED_BY", "")})
    try:
        session = json.loads(text)["token"] if status < 300 else ""
    except (ValueError, KeyError, TypeError):
        session = ""
    if not session:
        print(f"::error title=airlock grants sync::the server refused the session (HTTP {status})")
        return 1
    print(f"::add-mask::{session}", flush=True)
    failed = 0
    try:
        for role in todo:
            status, text = call("PUT", f"{api}/api/agents/roles/{role}", {"X-Agent-Session": session}, body(root, role, a.sha))
            print(json.dumps({"role": role, "status": status, "response": text[:500]}))
            failed += status >= 300
    finally:
        call("DELETE", f"{api}/api/agents/sessions/self", {"X-Agent-Session": session})
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
