#!/usr/bin/env python3
"""The weekly digest across every repo in a policy repo's repos/ (the digest action, digest/action.yml).

Reads GitHub only and writes one issue in the policy repo, labelled `digest`, titled "Digest: week of <Monday>"
(updated in place if the week's issue exists). The owner reads it in about 10 minutes and answers the proposals
yes or no, as a comment on that issue.

Sections, each at most 5 lines, empty ones skipped:
  1. Proposals   open `proposal` issues across repos, oldest first, each a yes/no with its link
  2. Shipped     PRs merged this week, with the level label the gate set and the branch (agent markers)
  3. Waiting     open PRs that are not drafts and labelled level:L1 or level:L2 (they wait on the owner)
  4. Declined    open issues labelled `declined` this week (agents mark what they chose not to build)
  5. Health      open ci-failure issues; role runs this week (role-*.yml in the policy repo): X of Y succeeded,
                 with the failing roles (cancelled and skipped runs don't count; workflow roles left out)
  6. Catches     lines dated this week in each config.yaml `catch_logs` file that name its `marker`
  7. Delivered?  open issues labelled `delivered?`: a merged PR named these tool-requests without Closes
  8. Overrides   PRs merged over a FAIL review verdict since last week's Monday (the router asked whoever merged
                 each for `Override: <one line>`), each with its reason, or "no reason given" after 7 days

The organization and the policy repo are the caller's (--owner, --policy-repo; GITHUB_REPOSITORY_OWNER and
GITHUB_REPOSITORY by default), and the policy files are its checkout (--policy-dir; the working directory).

  digest/digest.py [--dry-run] [--week-of YYYY-MM-DD] [--repos a,b] [--policy-dir .] [--owner o] [--policy-repo o/r]
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

MAX = 5

# The router's override rule writes this marker on the merged PR it asks for a reason, and the reason is a
# reply `Override: <line>`. Both are the router's contract (lunareng/airlock-actions router/), copied here so the
# digest does not import the router; a change to either changes both.
OVERRIDE_MARK = "<!-- router:override pr={pr} -->"
OVERRIDE_REASON_RE = re.compile(r"^\s*Override:\s*(\S.*?)\s*$", re.M | re.I)
REVIEW_CHECK = "claude-review"  # the required check whose FAIL verdict a merge can go over


class ConfigError(Exception):
    pass


def gh(path: str, **params):
    items, page = [], 1
    while True:
        q = "&".join(f"{k}={v}" for k, v in {"page": page, **params}.items())  # a caller's page wins (object responses don't paginate here)
        for attempt in range(3):
            out = subprocess.run(["gh", "api", f"{path}?{q}"], capture_output=True, text=True)
            if out.returncode == 0:
                break
            time.sleep(2 * (attempt + 1))
        else:
            return None  # a repo the token can't see: the digest says so instead of failing
        data = json.loads(out.stdout)
        if not isinstance(data, list):
            return data
        items += data
        if len(data) < int(params.get("per_page", 30)):
            return items
        page += 1


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def labels_of(x: dict) -> set[str]:
    return {l["name"] for l in x.get("labels", [])}


def level_of(pr: dict) -> str:
    return next((l[6:] for l in labels_of(pr) if l.startswith("level:")), "?")


def is_agent(branch: str, markers: dict) -> bool:
    return any(branch.startswith(p) for p in markers.get("branch_prefixes", []))


# ---------- the policy repo's settings ----------

def _strings(v, where: str) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise ConfigError(f"{where} must be a list of strings")
    return v


def catch_logs(config: dict) -> list[dict]:
    """config.yaml `catch_logs`: files in which a line `- YYYY-MM-DD ...` naming `marker` records a catch."""
    logs = config.get("catch_logs") or []
    if not isinstance(logs, list):
        raise ConfigError("catch_logs must be a list")
    for i, log in enumerate(logs):
        if not isinstance(log, dict) or not all(isinstance(log.get(k), str) and log.get(k) for k in ("repo", "path", "marker")):
            raise ConfigError(f"catch_logs[{i}] needs repo, path and marker, each a string")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", log["repo"]):
            raise ConfigError(f"catch_logs[{i}].repo must be owner/name")
    return logs


def settings(config: dict) -> dict:
    """config.yaml's `digest` block, with its defaults; a malformed block stops the run."""
    block = config.get("digest") or {}
    if not isinstance(block, dict):
        raise ConfigError("digest must be a mapping")
    unknown = set(block) - {"skip_repos", "router_logins"}
    if unknown:
        raise ConfigError(f"digest has unknown keys: {', '.join(sorted(unknown))}")
    return {"skip_repos": set(_strings(block.get("skip_repos"), "digest.skip_repos")),
            "router_logins": _strings(block.get("router_logins"), "digest.router_logins"),
            "catch_logs": catch_logs(config)}


def router_logins(config: dict, cfg: dict) -> frozenset[str]:
    """Whose override marker counts: the logins in digest.router_logins, and the App every Airlock role shares
    (config.yaml airlock.app_login), which the router writes as once it runs on Airlock."""
    return frozenset(set(cfg["router_logins"]) | {(config.get("airlock") or {}).get("app_login")} - {None})


def review_required_repos(config: dict, repos_dir: Path, owner: str) -> list[str]:
    """Every repo whose required checks (config.yaml required_checks, the default unless the repo has its own
    entry) include claude-review: the repos where a merge can go over a FAIL verdict."""
    checks = config.get("required_checks") or {}
    return [f"{owner}/{p.stem}" for p in sorted(repos_dir.glob("*.yaml")) if REVIEW_CHECK in checks.get(p.stem, checks.get("default") or [])]


def override_line(short: str, pr: dict, comments: list[dict], now: datetime, logins: frozenset[str]) -> str | None:
    """The Overrides line for a merged PR: None unless the router asked for a reason on it. The reason is the
    newest `Override: <line>` after the router's comment from the person it mentioned (anyone who is not a bot
    when it mentioned nobody); "no reason given" once 7 days have passed since the merge."""
    mark = OVERRIDE_MARK.format(pr=pr["number"])
    asked = next((c for c in comments if (c.get("user") or {}).get("login") in logins and mark in (c.get("body") or "")), None)
    if not asked:
        return None
    m = re.match(r"@(\S+?):", asked.get("body") or "")
    reason = None
    for c in sorted(comments, key=lambda c: c.get("created_at", "")):
        who = (c.get("user") or {}).get("login", "")
        if c.get("created_at", "") <= asked.get("created_at", "") or (who != m.group(1) if m else who.endswith("[bot]")):
            continue
        found = OVERRIDE_REASON_RE.search(c.get("body") or "")
        if found:
            reason = found.group(1)
    merged = ts(pr["merged_at"])
    why = reason or ("no reason given" if now - merged >= timedelta(days=7) else "reason asked, none yet")
    return f"{short}#{pr['number']} {pr.get('title', '')} · merged {merged.date().isoformat()} · {why} ({pr.get('html_url', '')})"


# The workflow roles (kind: workflow) run as role-<name>.yml too; a 30-minute router alone would be ~336 runs a
# week, so the agent-role line leaves them out. Read from the role files, so the next workflow role is left out
# without a change here.
def workflow_roles(roles_dir: Path) -> frozenset[str]:
    out = set()
    for f in roles_dir.glob("*/role.yaml"):
        doc = yaml.safe_load(f.read_text()) or {}
        if doc.get("kind") == "workflow":
            out.add(doc.get("role") or f.parent.name)
    return frozenset(out)


def role_runs_line(runs: list[dict], monday: datetime, skip: frozenset[str]) -> str | None:
    """'Role runs this week: X of Y succeeded' from the policy repo's workflow runs, failing roles named."""
    counted = [r for r in runs if re.fullmatch(r"\.github/workflows/role-[\w-]+\.ya?ml", r.get("path") or "")
               and Path(r["path"]).stem.removeprefix("role-") not in skip
               and r.get("status") == "completed" and r.get("conclusion") in ("success", "failure", "timed_out")
               and ts(r["created_at"]) >= monday]
    if not counted:
        return None
    ok = sum(r["conclusion"] == "success" for r in counted)
    failing = sorted({Path(r["path"]).stem.removeprefix("role-") for r in counted if r["conclusion"] != "success"})
    return f"Role runs this week: {ok} of {len(counted)} succeeded" + (f" (failed: {', '.join(failing)})" if failing else "")


def role_runs(policy_repo: str, monday: datetime) -> list[dict] | None:
    runs = []
    for page in range(1, 11):
        data = gh(f"repos/{policy_repo}/actions/runs", event="workflow_dispatch", created=f">={monday.date().isoformat()}", per_page=100, page=page)
        if data is None:
            return None
        batch = data.get("workflow_runs", [])
        runs += batch
        if len(batch) < 100:
            return runs
    return runs


def catch_lines(text: str, marker: str, monday: datetime, label: str) -> list[str]:
    """Lines `- YYYY-MM-DD ...` dated this week that name `marker` (a whole word) as the catcher."""
    out = []
    word = re.compile(rf"\b{re.escape(marker)}\b")
    for line in text.splitlines():
        m = re.match(r"^\s*[-*]\s*(\d{4}-\d{2}-\d{2})\b(.*)", line)
        if m and ts(m.group(1) + "T00:00:00Z") >= monday and word.search(m.group(2)):
            out.append(f"{label} {m.group(1)}:{m.group(2).strip()[:140]}")
    return out


def section(title: str, lines: list[str], more: str = "more") -> str:
    if not lines:
        return ""
    body = "\n".join(f"- {l}" for l in lines[:MAX])
    if len(lines) > MAX:
        body += f"\n- …and {len(lines) - MAX} {more}"
    return f"**{title}**\n{body}\n\n"


def gather(repos: list[str], monday: datetime, markers: dict, *, policy_repo: str, watched: set[str], logins: frozenset[str],
           skip_roles: frozenset[str], logs: list[dict], now: datetime | None = None) -> dict:
    d = {"proposals": [], "shipped": [], "waiting": [], "declined": [], "health": [], "catches": [], "delivered": [], "overrides": [], "unreadable": []}
    now = now or datetime.now(timezone.utc)
    for repo in repos:
        short = repo.split("/")[1]
        issues = gh(f"repos/{repo}/issues", state="open", per_page=100)
        if issues is None:
            d["unreadable"].append(short)
            continue
        for i in issues:
            if "pull_request" in i:
                continue
            labs = labels_of(i)
            if "proposal" in labs:
                d["proposals"].append((ts(i["created_at"]), f"{short}#{i['number']} {i['title']} ({i['html_url']})"))
            if "declined" in labs and ts(i["updated_at"]) >= monday:
                d["declined"].append(f"{short}#{i['number']} {i['title']} ({i['html_url']})")
            if "delivered?" in labs:
                d["delivered"].append(f"{short}#{i['number']} {i['title']} ({i['html_url']})")
            if "ci-failure" in labs:
                d["health"].append(f"{short}: {i['title']} ({i['html_url']})")
        prs = gh(f"repos/{repo}/pulls", state="all", sort="updated", direction="desc", per_page=50) or []
        for pr in prs:
            branch = pr["head"]["ref"]
            who = "agent" if is_agent(branch, markers) else "human"
            if pr.get("merged_at") and ts(pr["merged_at"]) >= monday:
                d["shipped"].append(f"{short}#{pr['number']} {pr['title']} · L{level_of(pr)[-1]} · {who} ({pr['html_url']})")
            elif pr["state"] == "open" and level_of(pr) in ("L1", "L2") and not pr.get("draft"):
                d["waiting"].append(f"{short}#{pr['number']} {pr['title']} · {level_of(pr)} ({pr['html_url']})")
            # Overrides: last week's too, so one asked late in a week shows its reason or "no reason given".
            if repo in watched and pr.get("merged_at") and ts(pr["merged_at"]) >= monday - timedelta(days=7):
                line = override_line(short, pr, gh(f"repos/{repo}/issues/{pr['number']}/comments", per_page=100) or [], now, logins)
                if line:
                    d["overrides"].append(line)
    d["proposals"] = [line for _, line in sorted(d["proposals"])]
    runs = role_runs(policy_repo, monday)
    line = role_runs_line(runs, monday, skip_roles) if runs is not None else "Role runs this week: unreadable (token scope)"
    if line:
        d["health"].insert(0, line)
    # Catches: each catch log's lines dated this week that name its marker as the catcher.
    for log in logs:
        f = gh(f"repos/{log['repo']}/contents/{log['path']}")
        if f and f.get("content"):
            text = base64.b64decode(f["content"]).decode()
            d["catches"] += catch_lines(text, log["marker"], monday, log["repo"].split("/")[1])
    return d


def render(d: dict, monday: date, today: date) -> str:
    n_p, n_s, n_d = len(d["proposals"]), len(d["shipped"]), len(d["declined"])
    first = f"Answer {n_p} proposal{'s' if n_p != 1 else ''}: reply like '1 yes, 2 no'." if n_p else "Nothing needs you this week."
    out = f"{first}\n\n"
    out += section("Proposals", [f"{k + 1}. {l}" for k, l in enumerate(d["proposals"])], "more proposals wait")
    out += section("Shipped", d["shipped"], "more merged")
    out += section("Waiting on you", d["waiting"])
    out += section("Declined", d["declined"])
    health = d["health"][:]
    if d["unreadable"]:
        health.append("digest could not read: " + ", ".join(d["unreadable"]) + " (token scope)")
    out += section("Health", health)
    out += section("Catches", d["catches"])
    out += section("Delivered? (a merged PR named these tool-requests without Closes)", d["delivered"])
    out += section("Overrides (merged over a FAIL review verdict)", d["overrides"], "more overrides")
    out += f"Week of {monday.isoformat()} (to {today.isoformat()}): {n_s} shipped, {n_p} proposals, {n_d} declined.\n\n_Generated by Airlock's digest action._"
    return out


def policy_repos(policy_dir: Path, owner: str, skip: set[str]) -> list[str]:
    return [f"{owner}/{p.stem}" for p in sorted((policy_dir / "repos").glob("*.yaml")) if p.stem not in skip]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--week-of")
    ap.add_argument("--repos")
    ap.add_argument("--policy-dir", default=os.environ.get("GITHUB_WORKSPACE") or ".")
    ap.add_argument("--owner", default=os.environ.get("GITHUB_REPOSITORY_OWNER"))
    ap.add_argument("--policy-repo", default=os.environ.get("GITHUB_REPOSITORY"))
    a = ap.parse_args(argv)
    root = Path(a.policy_dir)
    # Fail closed: no organization, no policy repo, or no config.yaml, and nothing is read or written.
    if not a.owner or not a.policy_repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", a.policy_repo) or a.policy_repo.split("/")[0] != a.owner:
        print("digest: --owner and --policy-repo (owner/name, in that owner) are required", file=sys.stderr)
        return 2
    if not (root / "config.yaml").is_file() or not (root / "repos").is_dir():
        print(f"digest: {root} is not a policy repo checkout (config.yaml and repos/)", file=sys.stderr)
        return 2
    config = yaml.safe_load((root / "config.yaml").read_text()) or {}
    try:
        cfg = settings(config)
    except ConfigError as e:
        print(f"digest: config.yaml: {e}", file=sys.stderr)
        return 2
    today = date.fromisoformat(a.week_of) if a.week_of else datetime.now(timezone.utc).date()
    monday_d = today - timedelta(days=today.weekday())
    monday = datetime(monday_d.year, monday_d.month, monday_d.day, tzinfo=timezone.utc)
    markers = config.get("agent_markers", {})
    repos = a.repos.split(",") if a.repos else policy_repos(root, a.owner, cfg["skip_repos"])
    d = gather(repos, monday, markers, policy_repo=a.policy_repo, watched=set(review_required_repos(config, root / "repos", a.owner)),
               logins=router_logins(config, cfg), skip_roles=workflow_roles(root / "roles"), logs=cfg["catch_logs"])
    body = render(d, monday_d, today)
    title = f"Digest: week of {monday_d.isoformat()}"
    if a.dry_run:
        print(title + "\n\n" + body)
        return 0
    existing = [i for i in (gh(f"repos/{a.policy_repo}/issues", state="open", labels="digest", per_page=20) or []) if i["title"] == title]
    if existing:
        subprocess.run(["gh", "api", "-X", "PATCH", f"repos/{a.policy_repo}/issues/{existing[0]['number']}", "--input", "-"], input=json.dumps({"body": body}), check=True, capture_output=True, text=True)
        print(existing[0]["html_url"])
    else:
        out = subprocess.run(["gh", "api", "-X", "POST", f"repos/{a.policy_repo}/issues", "--input", "-"], input=json.dumps({"title": title, "body": body, "labels": ["digest"]}), check=True, capture_output=True, text=True)
        print(json.loads(out.stdout)["html_url"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
