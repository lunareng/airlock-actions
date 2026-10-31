#!/usr/bin/env python3
"""Tests for the ratchet (ratchet/ratchet.py), on an invented acme policy repo. Run: python3 test/ratchet/test_ratchet.py"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
POLICY = Path(__file__).resolve().parent / "fixtures" / "policy"
sys.path.insert(0, str(ROOT / "ratchet"))
import ratchet  # noqa: E402
from ratchet import (ConfigError, decide, demote_prefixes, demoted_policy, demoted_role, demotion_pr_open, evidence, judge_catch_body,  # noqa: E402
                     judge_checks, judge_plan, judge_title, last_report, last_workflow_run, proposal_body, settings)

now = datetime(2026, 10, 20, tzinfo=timezone.utc)
L = lambda *n: [{"name": x} for x in n]  # noqa: E731
day = lambda d: (now - timedelta(days=d)).isoformat()  # noqa: E731
log = lambda text, marker="Wile": [{"repo": "acme/handbook", "path": "ops/LEARNINGS.md", "marker": marker, "text": text}]  # noqa: E731

ev = evidence([
    {"created_at": day(40), "labels": L("catch"), "html_url": "c1"},
    {"created_at": day(3), "labels": L("type:bug"), "html_url": "b"},
], None, now)
assert ev["catches"] == 1 and ev["clean_weeks"] == 5 and not ev["recent_incidents"], ev

ev_inc = evidence([{"created_at": day(2), "labels": L("catch", "severity:high"), "html_url": "c2"}], None, now)
assert ev_inc["clean_weeks"] == 0 and len(ev_inc["recent_incidents"]) == 1

ev_learn = evidence([], log("- 2026-10-19 · Wile · the thumbnail says USER vs ROLE\n- 2026-10-01 · judge · x\n- 2026-10-02 · Wiley · not a whole word"), now)
assert ev_learn["catches"] == 1 and ev_learn["last_catch"].date().isoformat() == "2026-10-19", ev_learn
assert evidence([], log("- 2026-10-19 · Wile · x", marker="Road"), now)["catches"] == 0, "only the log's own marker"
assert evidence([], log(None), now)["catches"] == 0, "an unreadable log adds nothing"
both = evidence([], log("- 2026-10-19 · Wile · x") + [{"repo": "acme/handbook", "path": "ops/OTHER.md", "marker": "Road", "text": "* 2026-10-18 Road"}], now)
assert both["catches"] == 2, "every log kept in the repo counts"

ev_none = evidence([], None, now)
assert ev_none["clean_weeks"] is None
ev_new = evidence([], None, now, since=now - timedelta(days=8))
assert ev_new["clean_weeks"] == 1, "the clock starts when the policy landed"
ev_capped = evidence([{"created_at": day(40), "labels": L("catch"), "html_url": "c1"}], None, now, since=now - timedelta(days=8))
assert ev_capped["clean_weeks"] == 1, "a policy younger than the last catch sets the clock"

l3 = {"default": "L3"}
d = decide("acme/x", l3, ev, set(), 4)
assert d["promote"] == "Promote acme/x to L4" and d["demote"] is None, d
assert decide("acme/x", l3, ev, {"Promote acme/x to L4"}, 4)["promote"] is None, "no duplicate proposal"
assert decide("acme/x", l3, evidence([{"created_at": day(10), "labels": L("catch"), "html_url": "c"}], None, now), set(), 4)["promote"] is None, "1 clean week is not enough"
assert decide("acme/x", l3, ev_none, set(), 4)["promote"] is None, "no clock, no promotion"
assert decide("acme/x", l3, ev_new, set(), 4)["promote"] is None, "1 week since the policy landed is not 4"
assert decide("acme/x", {"default": "L4"}, ev, set(), 4)["promote"] is None, "L4 has nowhere to go"
dm = decide("acme/x", {"default": "L4"}, ev_inc, set(), 4)
assert dm["demote"] == "L3" and "incident" in dm["why"], dm
assert decide("acme/x", {"default": "L2"}, ev_inc, set(), 4)["demote"] is None, "L2 is not demoted further"
assert decide("acme/x", l3, ev, set(), 6)["promote"] is None, "the tenant's weeks set the bar"

body = proposal_body("acme/x", l3, ev, "L4")
assert "a `judge`" in body and "a `goals` file" in body and "Yes or no" in body
assert "Not yet" not in proposal_body("acme/x", {**l3, "judge": {"metric": "m"}, "goals": "GOALS.md"}, ev, "L4")
assert "so the owner merges" in body and "after 4 clean weeks" in body, "neutral by default"
named = proposal_body("acme/x", l3, ev, "L4", "Wile", 6)
assert "so Wile merges the PR that follows" in named and "after 6 clean weeks" in named, named

src = "schema: 1\nrepo: acme/x\ndefault: L4\nactions:\n  send: L2\n# trailing comment\n"
out = demoted_policy(src, "L3", "2026-10-20", "incident: c2")
assert out.startswith("schema: 1\nrepo: acme/x\ndefault: L3\nactions:\n  send: L2\n# trailing comment\n"), out
doc = yaml.safe_load(out)
assert doc["ratchet"] == {"demoted_on": "2026-10-20", "evidence": "incident: c2"}, doc
twice = demoted_policy(out, "L2", "2026-10-27", "again")
assert yaml.safe_load(twice)["ratchet"]["demoted_on"] == "2026-10-27" and twice.count("ratchet:") == 1, twice

# judge_ran: a judge that ran recently does nothing, 7+ days opens one catch, 14+ days demotes.
ci = {"repo": "acme/x", "workflow": "ci.yml", "branch": "main", "max_age_days": 7}
rep = {"repo": "acme/x", "issue_label": "sim-coverage", "max_age_days": 7}
landed = now - timedelta(days=60)
at = lambda d: now - timedelta(days=d)  # noqa: E731
T = judge_title("acme/x")
p3 = judge_plan("acme/x", "L4", [(ci, at(3))], landed, now, set(), False)
assert p3["catch"] is None and p3["demote"] is None and p3["age_days"] == 3, p3
p9 = judge_plan("acme/x", "L4", [(ci, at(9))], landed, now, set(), False)
assert p9["catch"] == T == "Judge not running: acme/x" and p9["demote"] is None, p9
assert "acme/x ci.yml: last ran " + at(9).date().isoformat() in p9["why"], p9
p15 = judge_plan("acme/x", "L4", [(ci, at(15))], landed, now, {T}, False)
assert p15["demote"] == "L3" and p15["catch"] is None, "15 days with its catch open: one tightening PR"
assert judge_plan("acme/x", "L4", [(ci, at(15))], landed, now, set(), False)["catch"] == T, "15 days with no catch open: the catch too"
again = judge_plan("acme/x", "L4", [(ci, at(15))], landed, now, {T}, True)
assert again["catch"] is None and again["demote"] is None, "a second run writes nothing"
assert judge_plan("acme/x", "L4", [(ci, at(9))], landed, now, {T}, False)["catch"] is None, "a second run opens no second catch"
assert judge_plan("acme/x", "L4", [(ci, at(7))], landed, now, set(), False)["catch"] == T, "7 days is missing"
assert judge_plan("acme/x", "L4", [(ci, at(14))], landed, now, {T}, False)["demote"] == "L3", "14 days drops it"
assert judge_plan("acme/x", "L4", [(ci, at(15))], landed, now, {T}, False, demote_days=21)["demote"] is None, "the tenant's judge_demote_days set the line"
# Never ran: counted from the day the check landed, so a new check is not late on its first Monday.
assert judge_plan("acme/x", "L4", [(ci, None)], now - timedelta(days=2), now, set(), False)["catch"] is None
never = judge_plan("acme/x", "L4", [(ci, None)], now - timedelta(days=20), now, set(), False)
assert never["catch"] == T and never["demote"] == "L3" and "last ran never" in never["why"], never
# Several sources: every one has to run; the latest one sets the age.
two = judge_plan("role w", "L4", [(ci, at(1)), ({**ci, "workflow": "judge.yml"}, at(10))], landed, now, set(), False)
assert two["catch"] == "Judge not running: role w" and two["age_days"] == 10 and "judge.yml" in two["why"] and "ci.yml" not in two["why"], two
assert judge_plan("acme/x", "L4", [(rep, at(2))], landed, now, set(), False)["catch"] is None, "a report judge ran"
assert judge_plan("acme/x", "L3", [(ci, at(30))], landed, now, set(), False) == {"catch": None, "demote": None, "age_days": None, "why": "L3, no judge check"}, "only L4 is checked"

# What counts as a run: completed with success or failure; skipped and cancelled don't.
runs = [{"created_at": day(1), "status": "completed", "conclusion": "skipped"},
        {"created_at": day(2), "status": "completed", "conclusion": "cancelled"},
        {"created_at": day(3), "status": "in_progress", "conclusion": None},
        {"created_at": day(5), "status": "completed", "conclusion": "failure"},
        {"created_at": day(8), "status": "completed", "conclusion": "success"}]
assert last_workflow_run(runs) == at(5), "a failing judge is still a working judge"
assert last_workflow_run(runs[:3]) is None and last_workflow_run([]) is None
# A report: only the reporter's own comments (the account that opened the issue) count.
issues = [{"number": 80, "user": {"login": "sim-reporter"}}, {"number": 81, "pull_request": {}, "user": {"login": "sim-reporter"}}]
comments = {80: [{"created_at": day(1), "user": {"login": "acme-sim-engineer[bot]"}}, {"created_at": day(4), "user": {"login": "sim-reporter"}}]}
assert last_report(issues, comments) == at(4)
assert last_report(issues, {80: comments[80][:1]}) is None and last_report([], {}) is None

assert judge_checks({"judge": {"check": ci}}) == [ci] and judge_checks({"judge": {"check": [ci, rep]}}) == [ci, rep] and judge_checks({"judge": {}}) == []
cb = judge_catch_body("acme/x", p9, [(ci, at(9))])
assert "judge_ran rule" in cb and "promotion clock" in cb and "L4 to L3" in cb and at(9).date().isoformat() in cb, cb
assert "At 14 days" in cb and "the owning role or the owner" in cb, cb
cb21 = judge_catch_body("acme/x", p9, [(ci, at(9))], "Wile", 21)
assert "At 21 days" in cb21 and "the owning role or Wile" in cb21, cb21

role_src = "schema: 1\nrole: w\n# a comment\nlevel: L4   # was here\nbrief: b.md\n"
rd = demoted_role(role_src, "L3", "2026-11-02", p15["why"])
assert rd.startswith("schema: 1\nrole: w\n# a comment\nlevel: L3   # ratchet 2026-11-02: judge not running") and rd.endswith("\nbrief: b.md\n"), rd
assert yaml.safe_load(rd)["level"] == "L3"

# A judge-missing catch is a catch: it resets the repo's promotion clock.
ev_judge = evidence([{"created_at": day(3), "labels": L("catch", "judge-missing"), "html_url": "j"}], None, now, since=now - timedelta(days=60))
assert ev_judge["clean_weeks"] == 0 and not ev_judge["recent_incidents"], ev_judge
pr = lambda ref: {"head": {"ref": ref}}  # noqa: E731
legacy = ["chore/policy-demote-"]
assert demote_prefixes("ratchet", "api-", legacy) == ("agent/ratchet/demote-api-", "chore/policy-demote-api-")
assert demotion_pr_open([pr("agent/ratchet/demote-api-2026-11-02")], demote_prefixes("ratchet", "api-", legacy)), "its branch on Airlock"
assert demotion_pr_open([pr("chore/policy-demote-api-2026-10-26")], demote_prefixes("ratchet", "api-", legacy)), "one opened on a legacy prefix: no duplicate"
assert not demotion_pr_open([pr("chore/policy-demote-api-2026-10-26")], demote_prefixes("ratchet", "api-")), "no legacy prefix configured: only the role's branch"
assert not demotion_pr_open([pr("agent/ratchet/demote-parlor-2026-11-02")], demote_prefixes("ratchet", "api-", legacy)), "another subject's"
assert not demotion_pr_open([pr("agent/api-engineer/demote-api-x")], demote_prefixes("ratchet", "api-", legacy)), "another role's branch"
assert demotion_pr_open([pr("chore/policy-demote-role-groomer-2026-10-26")], demote_prefixes("ratchet", "role-groomer-", legacy)), "roles too"
assert demotion_pr_open([pr("agent/levels/demote-api-2026-11-02")], demote_prefixes("levels", "api-")), "the role is the caller's"
print("OK: ratchet decisions")

# The tenant's settings: config.yaml's ratchet block over the defaults, and catch_logs; anything malformed stops the run.
config = yaml.safe_load((POLICY / "config.yaml").read_text())
cfg = settings(config)
assert cfg == {"skip_repos": {"archive"}, "weeks": 4, "judge_demote_days": 14, "owner_name": "Wile", "legacy_demote_prefixes": ["chore/policy-demote-"],
               "catch_logs": [{"repo": "acme/handbook", "path": "ops/LEARNINGS.md", "marker": "Wile"}]}, cfg
assert settings({}) == {"skip_repos": set(), "weeks": 4, "judge_demote_days": 14, "owner_name": "the owner", "legacy_demote_prefixes": [], "catch_logs": []}
for bad in ({"ratchet": ["x"]}, {"ratchet": {"week": 4}}, {"ratchet": {"weeks": 0}}, {"ratchet": {"weeks": "4"}}, {"ratchet": {"weeks": True}},
            {"ratchet": {"judge_demote_days": -1}}, {"ratchet": {"skip_repos": "archive"}}, {"ratchet": {"owner_name": " "}},
            {"ratchet": {"legacy_demote_prefixes": [""]}}, {"catch_logs": [{"repo": "acme/handbook", "marker": "Wile"}]}, {"catch_logs": [{"repo": "handbook", "path": "p", "marker": "m"}]}):
    try:
        settings(bad)
    except ConfigError:
        continue
    raise AssertionError(f"accepted {bad}")
print("OK: ratchet settings")

# A whole run, live, on a stand-in GitHub over a git checkout of the fixture policy repo: every write it makes,
# and none outside the caller's org.
tmp = Path(tempfile.mkdtemp())
try:
    repo_dir = tmp / "policy"
    shutil.copytree(POLICY, repo_dir)
    env = {**os.environ, "GIT_AUTHOR_DATE": "2026-08-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-08-01T00:00:00Z", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "."], ["git", "commit", "-q", "-m", "policy"]):
        subprocess.run(cmd, cwd=repo_dir, env=env, check=True)
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    B = lambda s: {"content": base64.b64encode(s.encode()).decode(), "sha": "blob"}  # noqa: E731
    answers = {
        "repos/acme/policy/issues": [],  # no open proposals, no judge-missing issues
        "repos/acme/policy/pulls": [],
        "repos/acme/rockets/issues": [{"created_at": (now - timedelta(days=2)).isoformat(), "labels": L("incident"), "html_url": "https://github.com/acme/rockets/issues/9"}],
        "repos/acme/anvils/issues": [],  # clean since the policy landed: a proposal
        "repos/acme/handbook/issues": [],  # a catch in its log 1 week ago: no proposal
        "repos/acme/handbook/contents/ops/LEARNINGS.md": B("- 2026-10-19 · Wile · the guide's diagram was upside down"),
        "repos/acme/fuses/issues": None,  # unreadable: nothing written for it
        "repos/acme/rockets": {"default_branch": "main"}, "repos/acme/policy": {"default_branch": "main"},
        "repos/acme/rockets/git/ref/heads/main": {"object": {"sha": "r1"}}, "repos/acme/policy/git/ref/heads/main": {"object": {"sha": "p1"}},
        "repos/acme/rockets/contents/AUTONOMY.yaml": B("schema: 1\nrepo: acme/rockets\ndefault: L4\n"),
        "repos/acme/policy/contents/repos/rockets.yaml": B("x"),
        "repos/acme/policy/contents/roles/groomer/role.yaml": B("x"),
    }
    reads = []

    def stand_in(path, **params):
        reads.append(path)
        return answers.get(path, [])

    writes = []
    real_run = subprocess.run

    def fake_run(cmd, *args, **kw):
        if cmd[:2] == ["gh", "api"] and "-X" in cmd:
            writes.append((cmd[3], cmd[4], json.loads(kw.get("input") or "{}")))
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"number": len(writes)}), "")
        if cmd[:2] == ["gh", "api"]:  # a judge's workflow runs
            path = cmd[2].split("?")[0]
            runs = {"repos/acme/policy/actions/workflows/judge-groomer.yml/runs": [{"created_at": (now - timedelta(days=20)).isoformat(), "status": "completed", "conclusion": "success"}],
                    "repos/acme/rockets/actions/workflows/ci.yml/runs": [{"created_at": (now - timedelta(days=1)).isoformat(), "status": "completed", "conclusion": "failure"}]}
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"workflow_runs": runs[path]}), "")
        if cmd[:3] == ["gh", "pr", "merge"]:
            writes.append(("MERGE", cmd[cmd.index("--repo") + 1], cmd[3]))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, *args, **kw)

    ratchet.gh = stand_in
    ratchet.subprocess.run = fake_run
    try:
        ctx = ratchet.Ctx(repo_dir, "acme", "acme/policy", "ratchet", cfg, dry_run=False)
        rep = ratchet.run(ctx, cfg["weeks"], now)
    finally:
        ratchet.subprocess.run = real_run
    assert set(rep["repos"]) == {"anvils", "fuses", "handbook", "rockets"}, "the policy repo's own file and skip_repos are never moved"
    assert not any("archive" in p for p in reads), "a skipped repo is never read"
    assert rep["repos"]["fuses"] == "unreadable"
    assert rep["proposed"] == ["Promote acme/anvils to L4"], rep
    assert rep["repos"]["handbook"]["catches"] == 1 and rep["repos"]["handbook"]["clean_weeks"] == 1, rep["repos"]["handbook"]
    assert rep["demoted"] == ["acme/rockets: L4 -> L3", "role groomer: L4 -> L3 (judge not running for 20 day(s): acme/policy judge-groomer.yml: last ran 2026-10-06)"], rep["demoted"]
    assert rep["judge_catches"] == ["Judge not running: role groomer"], rep
    targets = [(w[0], w[1]) for w in writes]
    assert all(t.startswith(("repos/acme/", "acme/")) for _, t in targets), targets
    assert targets == [
        ("POST", "repos/acme/policy/issues"),  # the proposal
        ("POST", "repos/acme/policy/git/refs"), ("PUT", "repos/acme/policy/contents/repos/rockets.yaml"), ("POST", "repos/acme/policy/pulls"), ("MERGE", "acme/policy"),
        ("POST", "repos/acme/rockets/git/refs"), ("PUT", "repos/acme/rockets/contents/AUTONOMY.yaml"), ("POST", "repos/acme/rockets/pulls"), ("MERGE", "acme/rockets"),
        ("POST", "repos/acme/policy/labels"), ("POST", "repos/acme/policy/issues"),  # the judge catch
        ("POST", "repos/acme/policy/git/refs"), ("PUT", "repos/acme/policy/contents/roles/groomer/role.yaml"), ("POST", "repos/acme/policy/pulls"), ("MERGE", "acme/policy"),
    ], targets
    proposal = writes[0][2]
    assert proposal["labels"] == ["proposal", "ratchet"] and "so Wile merges" in proposal["body"], proposal
    assert writes[1][2]["ref"] == "refs/heads/agent/ratchet/demote-rockets-2026-10-26"
    put = yaml.safe_load(base64.b64decode(writes[2][2]["content"]).decode())
    assert put["default"] == "L3" and put["ratchet"] == {"demoted_on": "2026-10-26", "evidence": "incident in the last 7 days: https://github.com/acme/rockets/issues/9"}, put
    assert yaml.safe_load(base64.b64decode(writes[6][2]["content"]).decode())["default"] == "L3", "the repo's AUTONOMY.yaml too"
    assert writes[11][2]["ref"] == "refs/heads/agent/ratchet/demote-role-groomer-2026-10-26"
    assert "Restoration is Wile's" in writes[13][2]["body"]
    every = json.dumps([w[2] for w in writes])
    assert "lunareng" not in every and "Matthew" not in every and "autonomy#" not in every

    # A dry run reads the same and writes nothing.
    writes.clear()
    ratchet.subprocess.run = fake_run
    try:
        dry = ratchet.run(ratchet.Ctx(repo_dir, "acme", "acme/policy", "ratchet", cfg, dry_run=True), cfg["weeks"], now)
    finally:
        ratchet.subprocess.run = real_run
    assert writes == [] and dry == rep, (writes, dry)

    # Open proposals it can't read: no proposal is opened (each would be a duplicate), and the rest still runs.
    writes.clear()
    ratchet.gh = lambda path, **params: None if params.get("labels") == "proposal" else stand_in(path, **params)
    ratchet.subprocess.run = fake_run
    try:
        blind = ratchet.run(ratchet.Ctx(repo_dir, "acme", "acme/policy", "ratchet", cfg, dry_run=False), cfg["weeks"], now)
    finally:
        ratchet.subprocess.run = real_run
        ratchet.gh = stand_in
    assert blind["proposed"] == [] and blind["repos"]["anvils"]["decision"].endswith("; no proposal: the open proposals are unreadable"), blind
    assert ("POST", "repos/acme/policy/issues") in [(w[0], w[1]) for w in writes], "the judge catch still opens"
    assert not any(w[2].get("labels") == ["proposal", "ratchet"] for w in writes if isinstance(w[2], dict)), writes
    assert blind["demoted"] == rep["demoted"], "demotions still run"
finally:
    shutil.rmtree(tmp)
print("OK: ratchet run")

# The run fails closed: no org, a policy repo outside it, a bad role, or a directory that isn't a policy repo.
reads.clear()
assert ratchet.main(["--dry-run", "--policy-dir", str(POLICY), "--policy-repo", "acme/policy", "--owner", ""]) == 2
assert ratchet.main(["--dry-run", "--policy-dir", str(POLICY), "--policy-repo", "other/policy", "--owner", "acme"]) == 2
assert ratchet.main(["--dry-run", "--policy-dir", str(POLICY), "--policy-repo", "acme/policy", "--owner", "acme", "--role", "../x"]) == 2
assert ratchet.main(["--dry-run", "--policy-dir", str(POLICY / "repos"), "--policy-repo", "acme/policy", "--owner", "acme"]) == 2
assert reads == [], reads
print("OK: ratchet fails closed")
