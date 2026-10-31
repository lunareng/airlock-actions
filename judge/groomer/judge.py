#!/usr/bin/env python3
"""The groomer's judge: numbers a backlog-grooming role is held to and cannot write.

Reads GitHub only, through `gh api` (GH_TOKEN must read every repository it measures). Prints a JSON report and
exits 1 when the pass condition fails:
  pick_conversion >= --min-conversion (0.5) and picks_without_signal <= --max-without-signal (0)
over the trailing --weeks (4), a pick counting as converted when a merged PR mentioning it landed within
--window-days (14) of its labelling. With no picks at all it passes (nothing was asked of the role).

The repositories are the role file's `writes.repos` (roles/<role>/role.yaml in the policy repo checkout), or
--repos. Nothing here names an organization: a repository's own owner scopes the search for its PRs.

  judge.py [--policy .] [--role groomer] [--repos acme/api,acme/web] [--pick-label triage:this-week]
           [--weeks 4] [--window-days 14] [--min-conversion 0.5] [--max-without-signal 0]

Exit codes: 0 pass, 1 fail, 2 the judge could not measure (a role file it can't read, a GitHub read that kept
failing): never a pass.
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
ROLE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEMAND_FIELDS = ("mrr_at_risk", "pipeline_blocked", "askers", "moat", "looked_up")


class Unmeasured(Exception):
    """The judge could not read what it needs: it reports that and exits 2, never a pass."""


def gh(path: str, until=None, **params):
    """GET with manual paging (page=N), since Link headers point at repositories/{id} URLs some proxies refuse. Every
    value is URL-encoded: `gh api` reads the endpoint as a URL, so a raw `#` would cut the query off as a fragment and
    a raw `+` would reach GitHub as a space. With `until`, paging stops after the first page holding an item it fits."""
    items, page = [], 1
    while True:
        q = urllib.parse.urlencode({**params, "page": page})
        for attempt in range(3):
            out = subprocess.run(["gh", "api", f"{path}?{q}"], capture_output=True, text=True)
            if out.returncode == 0:
                break
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
        else:
            raise Unmeasured(f"gh api {path}: {out.stderr.strip()[:300]}")
        try:
            data = json.loads(out.stdout)
        except json.JSONDecodeError:
            raise Unmeasured(f"gh api {path}: an answer that isn't JSON")
        if isinstance(data, dict):
            return data
        if not isinstance(data, list):
            raise Unmeasured(f'gh api {path}: an answer that is neither a list nor an object')
        items += data
        if len(data) < int(params.get("per_page", 30)) or (until and any(until(i) for i in data)):
            return items
        page += 1


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def label_names(issue) -> set[str]:
    return {l["name"] for l in issue.get("labels", [])}


def pick_events(repo: str, since: datetime, pick_label: str):
    """(issue, labeled_at) for every pick-label labelling since `since`."""
    picks = []
    issues = gh(f"repos/{repo}/issues", state="all", since=since.strftime("%Y-%m-%dT%H:%M:%SZ"), per_page=100)
    for issue in issues:
        if "pull_request" in issue:
            continue
        for ev in gh(f"repos/{repo}/issues/{issue['number']}/events", per_page=100):
            if ev.get("event") == "labeled" and ev.get("label", {}).get("name") == pick_label and ts(ev["created_at"]) >= since:
                picks.append((issue, ts(ev["created_at"])))
                break
    return picks


_merged: dict[str, list] = {}


def merged_prs(repo: str, since: datetime) -> list:
    """Merged PRs in `repo` updated since `since` (cached per repo)."""
    if repo not in _merged:
        # Newest update first, so paging stops at the first page that reaches back past `since`.
        prs = gh(f"repos/{repo}/pulls", until=lambda pr: ts(pr["updated_at"]) < since,
                 state="closed", sort="updated", direction="desc", per_page=100)
        _merged[repo] = [pr for pr in prs if pr.get("merged_at") and ts(pr["updated_at"]) >= since]
    return _merged[repo]


def mentions(text: str, repo: str, number: int) -> bool:
    """Whether a PR's text names issue `number` of `repo` (owner/name): `#N`, `name#N`, `owner/name#N`, or the issue's
    URL. Another owner's `other/name#N`, or another repository's `web#N`, is not it."""
    owner, short = (re.escape(p) for p in repo.split("/"))
    return bool(re.search(
        rf"(?<![\w/.-])(?:{owner}/)?{short}#{number}\b"
        rf"|(?<![\w/#]){'#'}{number}\b"
        rf"|github\.com/{owner}/{short}/issues/{number}\b", text or "", re.I))


def merged_pr_for(repo: str, number: int, after: datetime, before: datetime, since: datetime) -> bool:
    """A merged PR mentioning this issue, merged inside the window. The issue's own repo is scanned directly; the
    rest of its owner's repositories through search, when the token allows it (CI does; some proxies don't)."""
    owner, short = repo.split("/")
    for pr in merged_prs(repo, since):
        if after <= ts(pr["merged_at"]) <= before and mentions((pr.get("title") or "") + "\n" + (pr.get("body") or ""), repo, number):
            return True
    # user: is any account, an organization or a person (org: is a 422 for a person's repositories).
    q = f'user:{owner} is:pr is:merged merged:{after.date()}..{before.date()} "{short}#{number}"'
    try:
        res = gh("search/issues", q=q, per_page=5)
    except Unmeasured:
        return False  # not converted: a search the token can't make never makes a pick look done
    return any(mentions((it.get("title") or "") + "\n" + (it.get("body") or ""), repo, number) for it in res.get("items", []))


def has_signal(body: str) -> bool:
    m = re.search(r"^## Signal\s*\n(.*?)(?=^## |\Z)", body or "", re.S | re.M)
    return bool(m and re.search(r"https?://\S+|\b[\w.-]+#\d+\b|(?<!\w)#\d+\b", m.group(1)))


def repos_of(policy: Path, role: str) -> list[str]:
    if not ROLE.match(role):
        raise Unmeasured(f"role {role!r} is not a role name")
    path = policy / "roles" / role / "role.yaml"
    try:
        doc = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise Unmeasured(f"roles/{role}/role.yaml: {e.__class__.__name__}")
    repos = ((doc or {}).get("writes") or {}).get("repos") if isinstance(doc, dict) else None
    if not isinstance(repos, list) or not repos:
        raise Unmeasured(f"roles/{role}/role.yaml has no writes.repos to measure")
    return repos


def measure(a) -> dict:
    repos = [r for r in re.split(r"[,\s]+", a.repos) if r] if a.repos else repos_of(Path(a.policy), a.role)
    bad = [r for r in repos if not isinstance(r, str) or not REPO.match(r)]
    if bad:
        raise Unmeasured(f"not owner/name: {', '.join(map(str, bad))}")
    fields = [f for f in re.split(r"[,\s]+", a.demand_fields) if f]
    if "looked_up" not in fields:
        fields.append("looked_up")  # the lookup date is how a demand issue's value is known to be fresh
    now = ts(a.now) if a.now else datetime.now(timezone.utc)
    since = now - timedelta(weeks=a.weeks)

    converted = total = no_signal = unranked = stale_demand = 0
    details = []
    for repo in repos:
        for issue, labeled_at in pick_events(repo, since, a.pick_label):
            total += 1
            deadline = min(labeled_at + timedelta(days=a.window_days), now)
            done = merged_pr_for(repo, issue["number"], labeled_at, deadline, since)
            converted += done
            signal = has_signal(issue.get("body"))
            no_signal += not signal
            details.append({"issue": f"{repo}#{issue['number']}", "picked": labeled_at.date().isoformat(), "merged_pr": done, "signal": signal})
        for issue in gh(f"repos/{repo}/issues", state="open", per_page=100):
            if "pull_request" in issue:
                continue
            labels = label_names(issue)
            if ts(issue["created_at"]) < now - timedelta(days=7) and not any(l.startswith(a.priority_prefix) for l in labels):
                unranked += 1
            if a.demand_label in labels:
                body = issue.get("body") or ""
                found = {f: re.search(rf"^{re.escape(f)}:\s*(.+)$", body, re.M) for f in fields}
                looked = found["looked_up"]
                try:
                    fresh = looked and ts(looked.group(1).strip() + "T00:00:00+00:00") >= now - timedelta(days=a.demand_fresh_days)
                except ValueError:
                    fresh = False  # a lookup date that isn't a date is no lookup
                if not all(found.values()) or not fresh:
                    stale_demand += 1

    report = {
        "as_of": now.date().isoformat(),
        "weeks": a.weeks,
        "repos": repos,
        "picks": total,
        "pick_conversion": round(converted / total, 2) if total else None,
        "picks_without_signal": no_signal,
        "unranked_over_7d": unranked,
        "stale_demand": stale_demand,
        "details": details,
    }
    report["pass"] = total == 0 or (report["pick_conversion"] >= a.min_conversion and no_signal <= a.max_without_signal)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", default=".", help="the policy repo checkout (default: the working directory)")
    ap.add_argument("--role", default="groomer", help="whose role file names the repositories (writes.repos)")
    ap.add_argument("--repos", default="", help="owner/name list, comma or space separated, in place of the role file's")
    ap.add_argument("--pick-label", default="triage:this-week")
    ap.add_argument("--priority-prefix", default="priority:")
    ap.add_argument("--demand-label", default="type:demand")
    ap.add_argument("--demand-fields", default=" ".join(DEMAND_FIELDS))
    ap.add_argument("--demand-fresh-days", type=int, default=14)
    ap.add_argument("--weeks", type=int, default=4)
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument("--min-conversion", type=float, default=0.5)
    ap.add_argument("--max-without-signal", type=int, default=0)
    ap.add_argument("--now", default="", help=argparse.SUPPRESS)  # tests: measure as of a fixed time
    a = ap.parse_args(argv)
    try:
        report = measure(a)
    except Unmeasured as e:
        print(json.dumps({"pass": False, "unmeasured": str(e)}, indent=2))
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
