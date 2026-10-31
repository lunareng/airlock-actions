#!/usr/bin/env python3
"""The gate's read of the tenant's policy repo, through Airlock (POST /v1/read).

The job proves itself with its GitHub OIDC token, and Airlock answers a token that can read the tenant's policy repo
and nothing else, for this repository's own `.github/workflows/autonomy-gate.yml` only. With it this reads, from the
policy repo's default branch:
  config.yaml                the owner, the agent markers and Airlock's App login
  repos/<this repo>.yaml     this repository's policy there, the stricter of it and AUTONOMY.yaml scores the PR
  roles/*/role.yaml          every role's bot login, so a PR any role opens is an agent's
then hands the token back. It writes the gate's config (config.yaml with every role's login under
agent_markers.logins, and Airlock's App login once any role is on Airlock) and this repository's policy file.

  policy.py --airlock <origin> --repo-name <name> --config-out <file> --policy-out <file>
    Exit 0: both written (no policy-out when the policy repo has no file for this repository).
    Exit 1: Airlock refused or couldn't be reached, or the policy repo couldn't be read; the error says which.
    No token, no gate: a gate that can't read the policy fails the PR rather than scoring it by its own copy alone.

Env: ACTIONS_ID_TOKEN_REQUEST_URL and ACTIONS_ID_TOKEN_REQUEST_TOKEN (the job has id-token: write).
The token is masked before anything else and never printed.
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

TOKEN = re.compile(r"[A-Za-z0-9_.~+/=-]{20,4096}")
REPO = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
ROLE_FILE = re.compile(r"roles/[A-Za-z0-9._-]{1,100}/role\.yaml")
MAX_ROLES = 200
API = os.environ.get("GITHUB_API_URL", "https://api.github.com")


class Stop(Exception):
    """A reason the gate can't read the policy, said once as the job's error."""


# A 5xx or no answer is retried, after 2 and then 4 seconds: one blip at GitHub or Airlock shouldn't fail a PR.
# Anything else, a 4xx included, is the answer.
TRIES, BACKOFF = 3, float(os.environ.get("AIRLOCK_GATE_BACKOFF", "2"))


def _call(req: urllib.request.Request, what: str) -> bytes:
    for attempt in range(TRIES):
        last = attempt == TRIES - 1
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 or last:
                raise _refusal(e.code, e.read(), what) from None
        except (urllib.error.URLError, TimeoutError) as e:
            if last:
                raise Stop(f"{what}: unreachable ({type(e).__name__})") from None
        time.sleep(BACKOFF * 2 ** attempt)
    raise AssertionError("unreachable")


def _refusal(code: int, body: bytes, what: str) -> Stop:
    try:
        a = json.loads(body)
    except ValueError:
        return Stop(f"{what}: HTTP {code}")
    if isinstance(a, dict) and a.get("reason"):
        return Stop(f"Airlock refused the policy read: {_line(a.get('reason'))} ({_line(a.get('message'))})")
    return Stop(f"{what}: HTTP {code}")


def _line(v) -> str:
    """One printable line: text from a server can't break the log or start a workflow command."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(v or ""))[:300].replace("::", ": :")


def oidc(audience: str) -> str:
    url, bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"), os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not bearer:
        raise Stop("no OIDC token for this job (add permissions: id-token: write)")
    req = urllib.request.Request(f"{url}&audience={urllib.parse.quote(audience, safe='')}", headers={"Authorization": f"bearer {bearer}"})
    value = json.loads(_call(req, "GitHub's OIDC token")).get("value")
    if not isinstance(value, str) or not value:
        raise Stop("GitHub answered no OIDC token")
    return value


def read_grant(airlock: str) -> tuple[str, str]:
    req = urllib.request.Request(f"{airlock}/v1/read", method="POST", headers={"Authorization": f"Bearer {oidc(airlock)}"})
    a = json.loads(_call(req, "Airlock"))
    token, repo = a.get("token") or "", a.get("repository") or ""
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise Stop(f"Airlock refused the policy read: {_line(a.get('reason'))} ({_line(a.get('message'))})")
    print(f"::add-mask::{token}", flush=True)
    if not isinstance(repo, str) or not REPO.fullmatch(repo):
        raise Stop("Airlock's answer names no policy repository")
    return token, repo


def github(token: str, path: str, raw: bool = False):
    req = urllib.request.Request(f"{API}/{path}", headers={
        "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28",
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json"})
    body = _call(req, f"the policy repo ({path.split('?')[0]})")
    return body.decode() if raw else json.loads(body)


def maybe_file(token: str, repo: str, path: str) -> str | None:
    try:
        return github(token, f"repos/{repo}/contents/{path}", raw=True)
    except Stop as e:
        if "HTTP 404" in str(e):
            return None
        raise


def gate_config(config: dict, roles: list[dict]) -> dict:
    """config.yaml with every role's bot login as an agent marker, and Airlock's App login once a role is on it."""
    cfg = dict(config)
    markers = dict(cfg.get("agent_markers") or {})
    logins = set(markers.get("logins") or [])
    logins |= {r["identity"]["github_login"] for r in roles
               if isinstance(r.get("identity"), dict) and isinstance(r["identity"].get("github_login"), str)}
    app_login = (cfg.get("airlock") or {}).get("app_login")
    if app_login and any(isinstance(r.get("identity"), dict) and r["identity"].get("key_store") == "airlock" for r in roles):
        logins.add(app_login)
    markers["logins"] = sorted(logins)
    cfg["agent_markers"] = markers
    return cfg


def read_policy(token: str, repo: str, name: str) -> tuple[dict, list[dict], str | None]:
    # A freshly minted token has been seen to get a 404 for a file that exists (lunareng/lunareng.com#41's gate, the
    # same commit green two minutes later). config.yaml is never absent from a policy repo, so a 404 for it is retried.
    text = None
    for attempt in range(TRIES):
        text = maybe_file(token, repo, "config.yaml")
        if text is not None or attempt == TRIES - 1:
            break
        time.sleep(BACKOFF * 2 ** attempt)
    config = yaml.safe_load(text or "") or {}
    if not isinstance(config, dict) or not config.get("human_owner"):
        raise Stop(f"{repo} has no config.yaml naming a human_owner")
    default = github(token, f"repos/{repo}")["default_branch"]
    tree = github(token, f"repos/{repo}/git/trees/{urllib.parse.quote(default, safe='')}?recursive=1")
    paths = sorted(e["path"] for e in tree.get("tree", []) if e.get("type") == "blob" and ROLE_FILE.fullmatch(e.get("path", "")))
    if tree.get("truncated") or len(paths) > MAX_ROLES:
        raise Stop(f"{repo}'s tree is too large to read every role file")
    roles = []
    for p in paths:
        doc = yaml.safe_load(github(token, f"repos/{repo}/contents/{p}", raw=True))
        if isinstance(doc, dict):
            roles.append(doc)
    return config, roles, maybe_file(token, repo, f"repos/{name}.yaml")


def end(token: str) -> None:
    req = urllib.request.Request(f"{API}/installation/token", method="DELETE",
                                 headers={"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        _call(req, "ending the policy read")
        print("policy read ended")
    except Stop as e:
        print(f"policy read not ended ({e}); it expires within the hour")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--airlock", required=True)
    ap.add_argument("--repo-name", required=True)
    ap.add_argument("--config-out", required=True)
    ap.add_argument("--policy-out", required=True)
    a = ap.parse_args()
    airlock = a.airlock.rstrip("/")
    if not re.fullmatch(r"https://[a-z0-9.-]+(:\d+)?|http://127\.0\.0\.1(:\d+)?", airlock):
        print("::error title=autonomy gate::airlock-url must be an https origin, like https://airlock.lunareng.com")
        return 1
    if not NAME.fullmatch(a.repo_name):
        print("::error title=autonomy gate::this repository's name isn't one a policy file can have")
        return 1
    token = None
    try:
        token, repo = read_grant(airlock)
        config, roles, policy = read_policy(token, repo, a.repo_name)
    except Stop as e:
        print(f"::error title=autonomy gate::{_line(e)}; no policy, no gate")
        return 1
    finally:
        if token:
            end(token)
    with open(a.config_out, "w") as f:
        yaml.safe_dump(gate_config(config, roles), f, sort_keys=False)
    if policy is not None:
        with open(a.policy_out, "w") as f:
            f.write(policy)
    print(f"read {repo}: config.yaml, {len(roles)} role file(s), "
          + (f"repos/{a.repo_name}.yaml" if policy is not None else f"no repos/{a.repo_name}.yaml (AUTONOMY.yaml alone scores the PR)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
