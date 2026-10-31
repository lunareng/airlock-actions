#!/usr/bin/env python3
"""Tests for the digest (digest/digest.py), on an invented acme policy repo. Run: python3 test/digest/test_digest.py"""
import base64
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
POLICY = Path(__file__).resolve().parent / "fixtures" / "policy"
sys.path.insert(0, str(ROOT / "digest"))
import digest  # noqa: E402
from digest import (OVERRIDE_MARK, ConfigError, catch_lines, override_line, policy_repos, render, review_required_repos,  # noqa: E402
                    role_runs_line, router_logins, settings, workflow_roles)

config = yaml.safe_load((POLICY / "config.yaml").read_text())
cfg = settings(config)
monday = datetime(2026, 10, 26, tzinfo=timezone.utc)
run = lambda wf, c="success", at="2026-10-28T10:00:00Z": {"path": f".github/workflows/{wf}", "status": "completed", "conclusion": c, "created_at": at}  # noqa: E731

# The workflow roles are read from the role files (kind: workflow); agent roles aren't among them.
wf = workflow_roles(POLICY / "roles")
assert {"router", "digest", "ratchet"} <= wf and "groomer" not in wf and "api-engineer" not in wf, wf

# The 30-minute router (and digest, ratchet) don't count in the agent-role line.
runs = [run("role-router.yml")] * 48 + [run("role-ratchet.yml", "failure"), run("role-groomer.yml"), run("role-clerk.yml", "failure")]
assert role_runs_line(runs, monday, wf) == "Role runs this week: 1 of 2 succeeded (failed: clerk)", role_runs_line(runs, monday, wf)
assert role_runs_line([run("role-router.yml")], monday, wf) is None, "only workflow-role runs: no line"
assert role_runs_line([run("role-groomer.yml", at="2026-10-20T10:00:00Z")], monday, wf) is None, "last week's runs don't count"
assert role_runs_line([run("ci.yml", "failure"), run("role-groomer.yml")], monday, wf) == "Role runs this week: 1 of 1 succeeded", "only role-*.yml"
assert role_runs_line(runs, monday, frozenset())[:37] == "Role runs this week: 49 of 51 succeed", "skip is explicit when given"
print("OK: digest role runs")

# The tenant's settings: config.yaml's digest block and catch_logs, with defaults; anything malformed stops the run.
assert cfg == {"skip_repos": {"archive"}, "router_logins": ["acme-router[bot]"],
               "catch_logs": [{"repo": "acme/handbook", "path": "ops/LEARNINGS.md", "marker": "Wile"}]}, cfg
assert settings({}) == {"skip_repos": set(), "router_logins": [], "catch_logs": []}, "no block: nothing skipped, no catch log"
for bad in ({"digest": ["x"]}, {"digest": {"skip_repo": ["x"]}}, {"digest": {"skip_repos": "archive"}},
            {"catch_logs": [{"repo": "acme/handbook", "path": "ops/LEARNINGS.md"}]}, {"catch_logs": [{"repo": "handbook", "path": "p", "marker": "m"}]},
            {"catch_logs": {"repo": "acme/handbook"}}, ["not", "a", "mapping"], {"airlock": "acme-agents[bot]"}, {"agent_markers": ["agent/"]},
            {"agent_markers": {"branch_prefixes": "agent/"}}, {"required_checks": ["claude-review"]}, {"required_checks": {"default": "claude-review"}}):
    try:
        settings(bad)
    except ConfigError:
        continue
    raise AssertionError(f"accepted {bad}")
assert settings(config, "acme")["catch_logs"] == cfg["catch_logs"], "a catch log in the caller's org"
try:
    settings({"catch_logs": [{"repo": "other/handbook", "path": "ops/LEARNINGS.md", "marker": "Wile"}]}, "acme")
    raise AssertionError("accepted a catch log outside the caller's org")
except ConfigError:
    pass
assert policy_repos(POLICY, "acme", cfg["skip_repos"]) == ["acme/anvils", "acme/handbook", "acme/policy", "acme/rockets"], "every repos/ file but the skipped, in the caller's org"
print("OK: digest settings")

# The repos where a merge can go over a FAIL verdict: claude-review is required there (the default, unless a repo has its own entry).
watched = review_required_repos(config, POLICY / "repos", "acme")
assert "acme/rockets" in watched and "acme/anvils" in watched, watched
assert "acme/policy" not in watched and "acme/archive" not in watched, "repos that don't require claude-review are skipped"
logins = router_logins(config, cfg)
assert logins == {"acme-router[bot]", "acme-agents[bot]"}, logins
assert router_logins({}, settings({})) == frozenset(), "no router login configured: no marker counts"

# Overrides: the router's marker, from the router's own login, and the reason from the person it asked.
HEAD = "c39b012aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
cm = lambda i, who, body, at: {"id": i, "user": {"login": who}, "body": body, "created_at": at}  # noqa: E731
fail_head = cm(1, "claude[bot]", f"Intent: unrelated change in x.py:3\n\nVerdict: FAIL ({HEAD})\n", "2026-10-25T10:00:00Z")
mpr2 = {"number": 7, "title": "t", "html_url": "https://github.com/acme/rockets/pull/7", "merged_at": "2026-10-25T12:00:00Z",
        "head": {"sha": HEAD}, "user": {"login": "acme-rockets-engineer[bot]"}, "merged_by": {"login": "wile"}}
ask = f"@wile: Merged over a FAIL verdict on {HEAD}. Reply `Override: <one line>` so the digest records why.\n{OVERRIDE_MARK.format(pr=7)}"
asked = cm(6, "acme-router[bot]", ask, "2026-10-25T12:30:00Z")
now = datetime(2026, 10, 30, tzinfo=timezone.utc)
assert "reason asked, none yet" in override_line("rockets", mpr2, [fail_head, asked], now, logins)
assert "no reason given" in override_line("rockets", mpr2, [fail_head, asked], datetime(2026, 11, 2, tzinfo=timezone.utc), logins), "7 days, no reason"
why = cm(7, "wile", "Override: the FAIL was the stale-body note; fixed in #8", "2026-10-26T09:00:00Z")
line = override_line("rockets", mpr2, [fail_head, asked, why], now, logins)
assert line.startswith("rockets#7 t · merged 2026-10-25 · the FAIL was the stale-body note; fixed in #8"), line
spoof = cm(8, "acme-rockets-engineer[bot]", "Override: all fine", "2026-10-26T10:00:00Z")
assert "reason asked" in override_line("rockets", mpr2, [fail_head, asked, spoof], now, logins), "only the person asked gives the reason"
assert override_line("rockets", mpr2, [fail_head], now, logins) is None, "not asked: not an override line"
fake = cm(9, "wile", ask, "2026-10-25T12:30:00Z")
assert override_line("rockets", mpr2, [fail_head, fake], now, logins) is None, "only the router's marker counts"
on_airlock = cm(6, "acme-agents[bot]", ask, "2026-10-25T12:30:00Z")
assert "reason asked" in override_line("rockets", mpr2, [fail_head, on_airlock], now, logins), "the router on Airlock's shared App counts too"
anon = cm(6, "acme-router[bot]", ask.replace("@wile: ", ""), "2026-10-25T12:30:00Z")
anyone = cm(10, "roadrunner", "Override: beep", "2026-10-26T09:00:00Z")
assert "· beep (" in override_line("rockets", mpr2, [anon, spoof, anyone], now, logins), "no one mentioned: any person's reason, never a bot's"
print("OK: digest overrides")

# Catches: lines dated this week that name the log's marker as a whole word.
log = "- 2026-10-27 · Wile · the thumbnail says USER vs ROLE\n- 2026-10-28 · Wiley · not this one\n- 2026-10-19 · Wile · last week\n- 2026-13-40 · Wile · no such day\n* 2026-10-29 Wile caught the date"
assert catch_lines(log, "Wile", monday, "handbook") == ["handbook 2026-10-27:· Wile · the thumbnail says USER vs ROLE", "handbook 2026-10-29:Wile caught the date"]
print("OK: digest catches")

# The whole digest, from a stand-in for GitHub: every section, in the caller's org and policy repo only.
B = lambda s: {"content": base64.b64encode(s.encode()).decode()}  # noqa: E731
I = lambda n, t, labels, at="2026-10-27T09:00:00Z", repo="rockets": {"number": n, "title": t, "labels": [{"name": l} for l in labels], "created_at": at, "updated_at": at, "html_url": f"https://github.com/acme/{repo}/issues/{n}"}  # noqa: E731,E741
P = lambda n, t, ref, state="closed", merged=None, labels=(), draft=False: {"number": n, "title": t, "head": {"ref": ref, "sha": HEAD}, "state": state, "merged_at": merged, "draft": draft, "labels": [{"name": l} for l in labels], "html_url": f"https://github.com/acme/rockets/pull/{n}"}  # noqa: E731
answers = {
    "repos/acme/rockets/issues": [I(3, "Raise rockets to L4", ["proposal"], "2026-10-20T09:00:00Z"), I(4, "CI red on main", ["ci-failure"]),
                                  I(5, "Not building the anvil API", ["declined"]), I(6, "Add a launch command", ["tool-request", "delivered?"]),
                                  {"number": 9, "pull_request": {}, "labels": [{"name": "proposal"}]}],
    "repos/acme/rockets/pulls": [P(11, "Faster fuse", "agent/rockets-engineer/fuse", merged="2026-10-28T10:00:00Z", labels=["level:L3"]),
                                 P(12, "Rename crate", "wile/rename", merged="2026-10-27T10:00:00Z", labels=["level:L4"]),
                                 P(13, "Rotate key", "agent/rockets-engineer/key", state="open", labels=["level:L1"]),
                                 P(14, "Draft", "agent/x/y", state="open", labels=["level:L2"], draft=True),
                                 {**mpr2, "number": 7, "state": "closed", "labels": [{"name": "level:L3"}], "head": {"ref": "agent/rockets-engineer/z", "sha": HEAD}, "title": "t"}],
    "repos/acme/rockets/issues/7/comments": [fail_head, asked, why],
    "repos/acme/anvils/issues": None,
    "repos/acme/handbook/issues": [I(2, "Publish the guide?", ["proposal"], "2026-10-22T09:00:00Z", repo="handbook")],
    "repos/acme/handbook/contents/ops/LEARNINGS.md": B(log),
    "repos/acme/policy/issues": [],
    "repos/acme/policy/actions/runs": {"workflow_runs": [run("role-groomer.yml"), run("role-clerk.yml", "failure"), run("role-router.yml")]},
}
asked_for = []


def stand_in(path, **params):
    asked_for.append(path)
    return answers.get(path, [])


digest.gh = stand_in
repos = policy_repos(POLICY, "acme", cfg["skip_repos"])
d = digest.gather(repos, monday, config["agent_markers"], policy_repo="acme/policy", watched=set(watched), logins=logins, skip_roles=wf,
                  logs=cfg["catch_logs"], now=now)
assert all(p.startswith(("repos/acme/policy/", "repos/acme/rockets/", "repos/acme/anvils/", "repos/acme/handbook/")) for p in asked_for), asked_for
assert not any("archive" in p for p in asked_for), "a skipped repo is never read"
body = render(d, date(2026, 10, 26), date(2026, 10, 30))
expected = """Answer 2 proposals: reply like '1 yes, 2 no'.

**Proposals**
- 1. rockets#3 Raise rockets to L4 (https://github.com/acme/rockets/issues/3)
- 2. handbook#2 Publish the guide? (https://github.com/acme/handbook/issues/2)

**Shipped**
- rockets#11 Faster fuse · L3 · agent (https://github.com/acme/rockets/pull/11)
- rockets#12 Rename crate · L4 · human (https://github.com/acme/rockets/pull/12)

**Waiting on you**
- rockets#13 Rotate key · L1 (https://github.com/acme/rockets/pull/13)

**Declined**
- rockets#5 Not building the anvil API (https://github.com/acme/rockets/issues/5)

**Health**
- Role runs this week: 1 of 2 succeeded (failed: clerk)
- rockets: CI red on main (https://github.com/acme/rockets/issues/4)
- digest could not read: anvils (token scope)

**Catches**
- handbook 2026-10-27:· Wile · the thumbnail says USER vs ROLE
- handbook 2026-10-29:Wile caught the date

**Delivered? (a merged PR named these tool-requests without Closes)**
- rockets#6 Add a launch command (https://github.com/acme/rockets/issues/6)

**Overrides (merged over a FAIL review verdict)**
- rockets#7 t · merged 2026-10-25 · the FAIL was the stale-body note; fixed in #8 (https://github.com/acme/rockets/pull/7)

Week of 2026-10-26 (to 2026-10-30): 2 shipped, 2 proposals, 1 declined.

_Generated by Airlock's digest action._"""
assert body == expected, body
assert "lunareng" not in body and "Matthew" not in body
print("OK: digest end to end")

# The run fails closed: no org, a policy repo outside it, or a directory that isn't a policy repo, and nothing is read.
asked_for.clear()
assert digest.main(["--dry-run", "--policy-dir", str(POLICY), "--policy-repo", "acme/policy", "--owner", ""]) == 2
assert digest.main(["--dry-run", "--policy-dir", str(POLICY), "--policy-repo", "other/policy", "--owner", "acme"]) == 2
assert digest.main(["--dry-run", "--policy-dir", str(POLICY / "repos"), "--policy-repo", "acme/policy", "--owner", "acme"]) == 2
for repos_arg in ("rockets", "other/rockets", "acme/rockets,other/anvils"):
    assert digest.main(["--dry-run", "--repos", repos_arg, "--policy-dir", str(POLICY), "--policy-repo", "acme/policy", "--owner", "acme"]) == 2, repos_arg
assert asked_for == [], asked_for

# A live run whose lookup of the week's open digest issues fails writes nothing, rather than a second digest.
writes = []
real_run = digest.subprocess.run
digest.gh = lambda path, **params: None if params.get("labels") == "digest" else stand_in(path, **params)
digest.subprocess.run = lambda cmd, *a, **kw: writes.append(cmd) or real_run(["true"])
try:
    assert digest.main(["--week-of", "2026-10-30", "--policy-dir", str(POLICY), "--policy-repo", "acme/policy", "--owner", "acme"]) == 1
finally:
    digest.subprocess.run = real_run
assert writes == [], writes
print("OK: digest fails closed")
