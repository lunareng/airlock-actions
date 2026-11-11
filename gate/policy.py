#!/usr/bin/env python3
"""The gate's read of the tenant's policy repo, through Airlock (POST /v1/read).

The job proves itself with its GitHub OIDC token, and Airlock answers a token that can read the tenant's policy repo
and nothing else, for this repository's own `.github/workflows/autonomy-gate.yml` only. With it this reads, from the
policy repo's default branch:
  config.yaml                the owner, the agent markers and the tenant's floors
  repos/<this repo>.yaml     this repository's policy there, the stricter of it and AUTONOMY.yaml scores the PR
  roles/*/role.yaml          every role's bot login, so a PR any role opens is an agent's
then hands the token back. It writes the gate's config (config.yaml with every role's login and Airlock's App login
under agent_markers.logins; `floors` passes through as written) and this repository's policy file. A `floors` the gate
can't read fails here, before anything is scored.

Airlock's App (lunareng/airlock#251). Every role on Airlock opens its pull requests as one App. Its login is Airlock's
to say, not the tenant's: the read's answer names it (`appLogin`), and it goes into the gate's config as an agent login
and as `airlock.app_login`, whatever config.yaml and the role files say (a role mid-switch to Airlock, or one whose
file can't be read, still pushes as the App). So a pull request the App opened is an agent's on every tenant, with
nothing written in config.yaml. `airlock.url` in the gate's config is the origin this read used. config.yaml may
still carry an `airlock:` block, which other actions read: an `app_login` there must be Airlock's own, and any other
fails the gate; a `url` there that isn't this origin is not used, and the log says so. An Airlock that answers no
`appLogin` fails the gate: it never falls back to scoring the App's pull request as a person's.

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

# The gate's own reading of config.yaml's floors, from this action's directory (never the checkout's).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gate import FloorError, floors_of  # noqa: E402

TOKEN = re.compile(r"[A-Za-z0-9_.~+/=-]{20,4096}")
# A GitHub App's bot login: the App's slug, then [bot].
APP_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\[bot\]")
REPO = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
ROLE_FILE = re.compile(r"roles/[A-Za-z0-9._-]{1,100}/role\.yaml")
MAX_ROLES = 200
API = os.environ.get("GITHUB_API_URL", "https://api.github.com")


class Stop(Exception):
    """A reason the gate can't read the policy, said once as the job's error."""


# A 5xx or no answer is retried, after 2 and then 4 seconds: one blip at GitHub or Airlock shouldn't fail a PR.
# So is a 403 or 429 that says it is a rate limit (a burst of gates reading role files, lunareng/airlock-actions#42).
# Anything else, a plain 403 included, is the answer.
TRIES, BACKOFF = 3, float(os.environ.get("AIRLOCK_GATE_BACKOFF", "2"))


def _rate_limited(e: urllib.error.HTTPError) -> bool:
    """GitHub's rate-limit answer: a 403 or 429 with retry-after, or with x-ratelimit-remaining 0."""
    h = e.headers
    return e.code in (403, 429) and h is not None and (
        h.get("retry-after") is not None or (h.get("x-ratelimit-remaining") or "").strip() == "0")


def _call(req: urllib.request.Request, what: str) -> bytes:
    for attempt in range(TRIES):
        last = attempt == TRIES - 1
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            limited = _rate_limited(e)
            if last and limited:
                raise Stop(f"{what}: HTTP {e.code}, rate limited after {TRIES} tries") from None
            if (e.code < 500 and not limited) or last:
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


def read_grant(airlock: str) -> tuple[str, dict]:
    """The read's token, masked, and Airlock's whole answer. The answer is checked by granted(), after the caller holds
    the token: whatever is wrong with the rest of it, the token is still handed back."""
    req = urllib.request.Request(f"{airlock}/v1/read", method="POST", headers={"Authorization": f"Bearer {oidc(airlock)}"})
    a = json.loads(_call(req, "Airlock"))
    if not isinstance(a, dict):
        raise Stop("Airlock's answer isn't one the gate can read")
    token = a.get("token") or ""
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise Stop(f"Airlock refused the policy read: {_line(a.get('reason'))} ({_line(a.get('message'))})")
    print(f"::add-mask::{token}", flush=True)
    return token, a


def granted(answer: dict) -> tuple[str, str]:
    """(the policy repository, Airlock's App login) from the read's answer. Both are Airlock's to say."""
    repo, app = answer.get("repository") or "", answer.get("appLogin")
    if not isinstance(repo, str) or not REPO.fullmatch(repo):
        raise Stop("Airlock's answer names no policy repository")
    if not isinstance(app, str) or not APP_LOGIN.fullmatch(app):
        # Never a reason to go on without it: with no App login, the App's pull request would score as a person's.
        raise Stop("Airlock's answer doesn't name its App (appLogin), so the gate can't tell which pull requests "
                   "Airlock's App opened; this gate needs an Airlock that answers it (lunareng/airlock#251)")
    return repo, app


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


def airlock_block(config: dict, app_login: str, origin: str) -> tuple[dict, str | None]:
    """(the gate's `airlock` block, a note for the log). The App's login and the stamp's origin are Airlock's: the login
    the read answered, the origin the read used. config.yaml's own block is held to them (lunareng/airlock#251): an
    app_login that isn't Airlock's is a mistake the gate fails closed on, since the tenant meant some other login to be
    the App's; a url that isn't this origin is not used, and the note says so. Its other keys pass through."""
    block = config.get("airlock")
    if block is None:
        block = {}
    if not isinstance(block, dict):
        raise Stop("config.yaml's airlock is not a mapping; take it out (the gate learns Airlock's App from Airlock itself)")
    named = block.get("app_login")
    if named is not None and named != app_login:
        raise Stop(f"config.yaml's airlock.app_login is {_line(named)!r}, and Airlock's App is {app_login!r}; "
                   "correct it or take it out (the gate learns Airlock's App from Airlock itself)")
    note = None
    url = block.get("url")
    if url is not None and (not isinstance(url, str) or url.rstrip("/") != origin):
        note = (f"config.yaml's airlock.url is {_line(url)!r}, which is not the Airlock this gate reads from ({origin}); "
                f"the stamp is read from {origin}")
    return {**block, "app_login": app_login, "url": origin}, note


def gate_config(config: dict, roles: list[dict], app_login: str, origin: str) -> tuple[dict, str | None]:
    """(the gate's config, a note for the log): config.yaml with every role's bot login and Airlock's App login as
    agent markers, and the `airlock` block Airlock's own answer gives (airlock_block). The App's login is added
    whether or not a role file says it is on Airlock: what a pull request's author is doesn't depend on a file the
    tenant writes."""
    cfg = dict(config)
    markers = cfg.get("agent_markers")
    if markers is None:
        markers = {}
    if not isinstance(markers, dict) or not isinstance(markers.get("logins") or [], list):
        raise Stop("config.yaml's agent_markers is not a mapping with a list of logins")
    markers = dict(markers)
    logins = {l for l in markers.get("logins") or [] if isinstance(l, str)}
    logins |= {r["identity"]["github_login"] for r in roles
               if isinstance(r.get("identity"), dict) and isinstance(r["identity"].get("github_login"), str)}
    cfg["airlock"], note = airlock_block(cfg, app_login, origin)
    logins.add(app_login)
    markers["logins"] = sorted(logins)
    cfg["agent_markers"] = markers
    return cfg, note


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
    try:
        floors_of(config)
    except FloorError as e:
        raise Stop(f"{repo}'s config.yaml: {e}") from None
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
        token, answer = read_grant(airlock)
        repo, app_login = granted(answer)
        config, roles, policy = read_policy(token, repo, a.repo_name)
        cfg, note = gate_config(config, roles, app_login, airlock)
    except Stop as e:
        print(f"::error title=autonomy gate::{_line(e)}; no policy, no gate")
        return 1
    finally:
        if token:
            end(token)
    if note:
        print(f"::warning title=autonomy gate::{_line(note)}")
    with open(a.config_out, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    if policy is not None:
        with open(a.policy_out, "w") as f:
            f.write(policy)
    print(f"read {repo}: config.yaml, {len(roles)} role file(s), "
          + (f"repos/{a.repo_name}.yaml" if policy is not None else f"no repos/{a.repo_name}.yaml (AUTONOMY.yaml alone scores the PR)"))
    print(f"Airlock's App is {app_login}: a pull request it opened is an agent's, and needs Airlock's stamp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
