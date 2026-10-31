#!/usr/bin/env python3
"""Tests for the router's decisions, on the invented acme tenant's policy repo (fixtures/policy).
Run: python test/router/test_router.py (needs pyyaml)"""
import sys
from pathlib import Path

import re

import yaml

ROOT = Path(__file__).resolve().parents[2]
FIX = Path(__file__).resolve().parent / "fixtures" / "policy"
sys.path.insert(0, str(ROOT / "router"))
import router as _tenant  # noqa: E402

RCFG = yaml.safe_load((FIX / "router.yaml").read_text())
_tenant.configure(FIX, "acme/policy", "router", RCFG)
SIMS = RCFG["product_bugs"]["sim_repos"]
TOOL_LABEL = RCFG["tool_request_label"]
from router import ci_plan, ci_title, dispatch_plan, latest_by_workflow, owner_role, product_bug_plan, tool_request_plan  # noqa: E402

L = lambda *names: [{"name": n} for n in names]  # noqa: E731
sim_issue = {"number": 421, "created_at": "2026-10-06T16:55:59Z", "labels": L("type:bug", "product:shop"),
             "body": "## Summary\nShop Sim found it on QA. See https://github.com/acme/sim-shop/issues/44"}
incident = {"body": "<!-- sim-incident:signature=experience%3Acollect_payment%3ABILLING_TERMINAL_READER_BUSY -->\nFiled by sim-shop"}

p = product_bug_plan(sim_issue, incident, SIMS)
assert p and "## Signal" in p["append"] and "sim-shop#44" in p["append"], p
assert "severity:medium" in p["labels"] and "routed" in p["labels"] and "product:shop" in p["labels"], p["labels"]
assert "experience:collect_payment" in p["append"]

inv = product_bug_plan({**sim_issue, "labels": L("product-bug")}, {"body": "<!-- sim-incident:signature=invariant:ledger_balanced -->"}, SIMS)
assert "severity:high" in inv["labels"] and "product:shop" in inv["labels"], inv["labels"]

assert product_bug_plan({**sim_issue, "body": sim_issue["body"] + "\n\n## Signal\nalready"}, incident, SIMS) is None, "already routed"
assert product_bug_plan({"number": 1, "labels": L("type:bug"), "body": "nothing to do with a sim"}, None, SIMS) is None, "not a sim bug"
clinic = product_bug_plan({"number": 2, "labels": [], "body": "see https://github.com/acme/sim-clinic/issues/9"}, None, SIMS)
assert "product:clinic" in clinic["labels"], clinic["labels"]
# The sims are the tenant's: a link to a repo that isn't one of its sim_repos, or to another organization's, isn't a sim bug.
assert product_bug_plan({"number": 3, "labels": [], "body": "see https://github.com/acme/api/issues/9"}, None, SIMS) is None
assert product_bug_plan({"number": 4, "labels": [], "body": "see https://github.com/other/sim-shop/issues/9"}, None, SIMS) is None
assert product_bug_plan({"number": 5, "labels": [], "body": "see https://github.com/acme/sim-shop/issues/9"}, None, {}) is None, "no sims configured"
assert "product:shop" not in product_bug_plan({"number": 6, "labels": L("product-bug"), "body": "x"}, None, {})["labels"], "product-bug alone: no sim, no product"

t = tool_request_plan({"number": 1017, "labels": L("tool-request"), "user": {"login": "acme-owner"}, "html_url": "u", "body": "Needed per msg_05e1 and msg_eddc."}, TOOL_LABEL)
assert "`msg_05e1`" in t["append"] and "type:feature" in t["labels"] and "product:core" in t["labels"], t
assert t["append"].endswith("_Routed by acme/policy (lunareng/airlock-actions router)._"), "the footer names the tenant's policy repo"
bare = tool_request_plan({"number": 1, "labels": L("tool-request"), "user": {"login": "x"}, "html_url": "u", "body": ""})
assert not any(l.startswith("product:") for l in bare["labels"]), "no tool_request_label: no product label"

assert tool_request_plan({"labels": L("tool-request"), "body": "## Signal\nx"}, TOOL_LABEL) is None
assert tool_request_plan({"labels": L("type:bug"), "body": ""}, TOOL_LABEL) is None

runs = [
    {"name": "ci", "status": "completed", "conclusion": "failure", "updated_at": "2026-10-20T10:00:00Z", "html_url": "r1", "head_sha": "abcdef0123"},
    {"name": "ci", "status": "completed", "conclusion": "success", "updated_at": "2026-10-20T09:00:00Z", "html_url": "r0", "head_sha": "0000000000"},
    {"name": "deploy", "status": "completed", "conclusion": "success", "updated_at": "2026-10-20T10:00:00Z", "html_url": "r2", "head_sha": "1111111111"},
    {"name": "lint", "status": "in_progress", "conclusion": None, "updated_at": "2026-10-20T11:00:00Z", "html_url": "r3", "head_sha": "2222222222"},
]
latest = latest_by_workflow(runs)
assert set(latest) == {"ci", "deploy"} and latest["ci"]["conclusion"] == "failure", latest
plan = ci_plan(latest, [{"number": 5, "title": ci_title("deploy"), "labels": L("ci-failure")}], "product:core")
assert [o["title"] for o in plan["open"]] == [ci_title("ci")], plan
assert plan["open"][0]["signal_at"] == "2026-10-20T10:00:00Z" and "## Signal" in plan["open"][0]["body"]
assert [c["number"] for c in plan["close"]] == [5], plan
again = ci_plan(latest, [{"number": 6, "title": ci_title("ci"), "labels": L("ci-failure")}], "product:core")
assert again["open"] == [] and again["close"] == [], "idempotent: the ci issue already exists"
noclose = ci_plan(latest, [{"number": 7, "title": ci_title("deploy"), "labels": L("type:bug")}], "product:core")
assert noclose["close"] == [], "never closes an issue it didn't label ci-failure"
assert "doesn't need the human owner." in plan["open"][0]["body"], "names no person"
print("OK: router decisions")

REPOS = FIX / "repos"
assert owner_role("acme/cli", REPOS) == "cli-engineer"
assert owner_role("acme/station", REPOS) == "station-engineer"
assert owner_role("acme/sim-shop", REPOS) == "sim-engineer", "the engineer, not sim-triage, owns the sims' requests"
assert owner_role("acme/studio", REPOS) == "animator"
assert owner_role("acme/api", REPOS) == "api-engineer", "api's requests dispatch the api-engineer (2026-10-21)"
assert owner_role("acme/k8s", REPOS) == "ops-engineer"
assert owner_role("acme/marketing", REPOS) == "web-engineer"
assert owner_role("acme/policy", REPOS) == "policy-engineer", "the control repo's requests dispatch the policy-engineer (autonomy#106)"
assert owner_role("acme/legacy", REPOS) is None, "a repo with no engineer role holds the request for a person"
assert dispatch_plan("acme/legacy", {"html_url": "u"}, REPOS) is None
d = dispatch_plan("acme/cli", {"html_url": "u"}, REPOS)
assert d == {"workflow": "role-cli-engineer.yml", "inputs": {"signal": "u"}, "role": "cli-engineer"}, d
print("ok  tool-request dispatch")

from router import review_plan  # noqa: E402

LOGINS = {"acme-cli-engineer[bot]", "acme-router[bot]"}
pr = {"user": {"login": "acme-cli-engineer[bot]"}, "html_url": "https://github.com/acme/cli/pull/9"}
head = "2026-10-21T20:00:00Z"
cr = {"id": 11, "user": {"login": "acme-owner"}, "state": "CHANGES_REQUESTED", "submitted_at": "2026-10-21T21:00:00Z"}
old_cr = {"id": 10, "user": {"login": "acme-owner"}, "state": "CHANGES_REQUESTED", "submitted_at": "2026-10-21T19:00:00Z"}
approve = {"id": 12, "user": {"login": "acme-owner"}, "state": "APPROVED", "submitted_at": "2026-10-21T21:30:00Z"}
inline = {"id": 21, "user": {"login": "acme-owner"}, "created_at": "2026-10-21T21:10:00Z"}
bot_inline = {"id": 22, "user": {"login": "acme-cli-engineer[bot]"}, "created_at": "2026-10-21T21:20:00Z"}
mention = {"id": 31, "user": {"login": "acme-owner"}, "created_at": "2026-10-21T21:40:00Z", "body": "@acme-cli-engineer please also handle the empty case"}
chat = {"id": 32, "user": {"login": "acme-owner"}, "created_at": "2026-10-21T21:41:00Z", "body": "nice"}
mark = {"id": 33, "user": {"login": "acme-router[bot]"}, "created_at": "2026-10-21T21:42:00Z", "body": "Review seen <!-- router:review-dispatch id=31 -->"}

p = review_plan(pr, head, [cr], [], [], LOGINS)
assert p and p["id"] == 11 and p["kind"] == "changes requested", p
assert review_plan(pr, head, [old_cr], [], [], LOGINS) is None, "a review older than the head commit was already answered by the push"
assert review_plan(pr, head, [approve], [], [], LOGINS) is None, "an approval wakes nobody; the role merges at its next run"
p = review_plan(pr, head, [cr], [inline], [], LOGINS)
assert p and p["id"] == 21 and p["kind"] == "review comment", "the newest item is the marker"
assert review_plan(pr, head, [], [bot_inline], [], LOGINS) is None, "the agent's own replies never count"
p = review_plan(pr, head, [], [], [mention], LOGINS)
assert p and p["id"] == 31 and p["kind"] == "mention"
assert review_plan(pr, head, [], [], [chat], LOGINS) is None, "a conversation comment counts only when it addresses the agent"
assert review_plan(pr, head, [], [], [mention, mark], LOGINS) is None, "dispatched once per newest comment"
assert review_plan({**pr, "user": {"login": "acme-owner"}}, head, [cr], [], [], LOGINS) is None, "the owner's own PRs are not an agent's to rework"
print("ok  review dispatch")

from router import digest_answer_plan, DIGEST_MARK
mk = lambda i, who, body, at: {"id": i, "user": {"login": who}, "body": body, "created_at": at, "html_url": f"u{i}"}
c1 = mk(1, "acme-owner", "1 yes, 2 no", "2026-10-30T14:00:00Z")
assert digest_answer_plan([c1], "acme-owner")["id"] == 1, "the owner's answer dispatches"
assert digest_answer_plan([mk(2, "acme-api-engineer[bot]", "1 yes", "2026-10-30T14:00:00Z")], "acme-owner") is None, "an agent's 'answer' never dispatches"
assert digest_answer_plan([mk(3, "acme-owner", "looks good, thanks", "2026-10-30T14:00:00Z")], "acme-owner") is None, "a comment with no yes/no is not an answer"
mark = mk(4, "acme-router[bot]", "seen " + DIGEST_MARK.format(id=1), "2026-10-30T14:05:00Z")
assert digest_answer_plan([c1, mark], "acme-owner") is None, "a dispatched answer is not dispatched again"
c5 = mk(5, "acme-owner", "3: no", "2026-10-30T15:00:00Z")
assert digest_answer_plan([c1, mark, c5], "acme-owner")["id"] == 5, "a later answer dispatches"
assert digest_answer_plan([mk(6, "acme-owner", "all yes", "2026-10-30T14:00:00Z")], "acme-owner")["id"] == 6
print("ok  digest answers")

from datetime import datetime, timezone  # noqa: E402
from router import DELIVERED_MARK, delivered_plan, delivered_since, signal_refs  # noqa: E402

refs = signal_refs("## Signal\nCloses #12 (part of #10). Part of acme/api#1187; see api#1154, https://github.com/acme/k8s/issues/691,\n"
                   "Fixes https://github.com/acme/k8s/issues/692 and #12 again.\n\n## What\nCloses #999", "acme/cli")
assert refs == {("acme/cli", 12): "close", ("acme/cli", 10): "part", ("acme/api", 1187): "part", ("acme/api", 1154): "named",
                ("acme/k8s", 691): "named", ("acme/k8s", 692): "close"}, refs
assert signal_refs("Closes #3, no Signal section", "acme/api") == {}, "only links under ## Signal count"
req = {"number": 76, "state": "open", "labels": L("tool-request"), "repository_url": "https://api.github.com/repos/acme/cli"}
mpr = lambda body: {"number": 78, "html_url": "https://github.com/acme/cli/pull/78", "merged_at": "2026-10-21T19:00:00Z", "body": body}  # noqa: E731
assert delivered_plan(mpr("## Signal\nCloses #76"), "acme/cli", req, [], False, "cli-engineer") is None, "Closes: GitHub closes it, nothing to do"
assert delivered_plan(mpr("## Signal\nFixes acme/cli#76"), "acme/cli", req, [], False, "cli-engineer") is None
part = delivered_plan(mpr("## Signal\nPart of acme/cli#76"), "acme/cli", req, [], False, "cli-engineer")
assert part["comment"].startswith("Part delivered by https://github.com/acme/cli/pull/78") and "label" not in part, "Part of: one comment, stays open"
named = delivered_plan(mpr("## Signal\ntool-request #76"), "acme/cli", req, [], False, "cli-engineer")
assert named["label"] == "delivered?" and "the `cli-engineer` role closes it" in named["comment"], "no keyword: one comment and the label"
mark_c = {"body": "x " + DELIVERED_MARK.format(pr="acme/cli#78")}
assert delivered_plan(mpr("## Signal\ntool-request #76"), "acme/cli", req, [mark_c], False, "cli-engineer") is None, "a second run writes nothing"
assert delivered_plan(mpr("## Signal\nPart of #76"), "acme/cli", req, [mark_c], False, "cli-engineer") is None
assert delivered_plan(mpr("## Signal\ntool-request #76"), "acme/cli", req, [], True, "cli-engineer") is None, "an open PR also names it: left alone"
assert delivered_plan(mpr("## Signal\n#76"), "acme/cli", {**req, "state": "closed"}, [], False, "cli-engineer") is None, "never reopens or touches a closed request"
assert delivered_plan(mpr("## Signal\n#76"), "acme/cli", {**req, "labels": L("type:bug")}, [], False, "cli-engineer") is None, "only tool-requests"
assert delivered_plan(mpr("## Signal\nacme/api#76"), "acme/cli", req, [], False, "cli-engineer") is None, "same number, other repo: not this request"
BACKFILL = RCFG["delivered"]["backfill"]
assert delivered_since(datetime(2026, 10, 26, tzinfo=timezone.utc), BACKFILL).date().isoformat() == "2026-10-19", "the first week looks back to 2026-10-19"
assert delivered_since(datetime(2026, 11, 3, 12, tzinfo=timezone.utc), BACKFILL) == datetime(2026, 11, 1, 12, tzinfo=timezone.utc), "then 48 hours"
assert delivered_since(datetime(2026, 10, 26, tzinfo=timezone.utc)) == datetime(2026, 10, 24, tzinfo=timezone.utc), "no backfill: 48 hours"
assert "`Closes acme/<repo>#N`" in named["comment"], "the tenant's organization, never another's"
assert signal_refs("## Signal\nCloses https://github.com/other/cli/issues/76 and other/cli#77", "acme/cli") == {}, "another organization's issue is never one of the tenant's"
print("ok  delivered")
from router import RUN_FAILED_MARK, run_failed_plan, run_failed_title  # noqa: E402

rr = lambda i, c, at, who="acme-clock[bot]": {"id": i, "status": "completed", "conclusion": c, "updated_at": at,  # noqa: E731
                                                "html_url": f"https://github.com/acme/policy/actions/runs/{i}", "triggering_actor": {"login": who}, "display_title": "role groomer"}
JOBS = [{"name": "run", "conclusion": "failure", "html_url": "j1", "steps": [{"name": "tools (gh, claude)", "conclusion": "success"}, {"name": "run the brief", "conclusion": "failure"}]}]
ARTS = [{"name": "groomer-run", "expired": False}]
ok0 = rr(100, "success", "2026-10-24T10:00:00Z")
f1 = rr(101, "failure", "2026-10-25T10:00:00Z")
p = run_failed_plan("groomer", [ok0, f1], None, [], JOBS, ARTS, "acme-owner")
o = p["open"]
assert o["title"] == run_failed_title("groomer") == "Role run: groomer failing" and o["labels"] == ["run-failure", "type:bug", "severity:high"], o
assert "## Signal" in o["body"] and "runs/101" in o["body"] and "step `run the brief`" in o["body"] and "`groomer-run`" in o["body"], o["body"]
assert "the clock" in o["body"] and RUN_FAILED_MARK.format(id=101) in o["body"] and "@acme-owner" not in o["body"], "a first failure opens one issue, no mention"
issue = {"number": 9, "body": o["body"], "user": {"login": "acme-router[bot]"}}
assert run_failed_plan("groomer", [ok0, f1], issue, [], JOBS, ARTS, "acme-owner") is None, "idempotent: that run is already in the issue"
f2 = rr(102, "timed_out", "2026-10-25T12:00:00Z", who="acme-router[bot]")
c = run_failed_plan("groomer", [ok0, f1, f2], issue, [], JOBS, ARTS, "acme-owner")
assert c["comment"]["number"] == 9 and RUN_FAILED_MARK.format(id=102) in c["comment"]["body"], "a second failure comments, never a second issue"
assert "timed_out" in c["comment"]["body"] and "the router" in c["comment"]["body"] and "@acme-owner" not in c["comment"]["body"], "timed_out counts"
c2 = {"body": c["comment"]["body"]}
assert run_failed_plan("groomer", [ok0, f1, f2], issue, [c2], JOBS, ARTS, "acme-owner") is None, "one comment per failed run"
cx = rr(103, "cancelled", "2026-10-25T13:00:00Z")
assert run_failed_plan("groomer", [ok0, f1, f2, cx], issue, [c2], JOBS, ARTS, "acme-owner") is None, "cancelled is ignored"
f3 = rr(104, "failure", "2026-10-25T14:00:00Z", who="acme-owner")
c3 = run_failed_plan("groomer", [ok0, f1, f2, cx, f3], issue, [c2], JOBS, ARTS, "acme-owner")["comment"]["body"]
assert c3.startswith("@acme-owner: 3 failed runs in a row") and "a person (`acme-owner`" in c3, "the third failure in a row mentions"
f4 = rr(105, "failure", "2026-10-25T15:00:00Z")
c4 = run_failed_plan("groomer", [ok0, f1, f2, cx, f3, f4], issue, [c2, {"body": c3}], JOBS, ARTS, "acme-owner")["comment"]["body"]
assert "@acme-owner" not in c4 and "in a row: 4" in c4, "the owner is mentioned once per issue"
ok1 = rr(106, "success", "2026-10-25T16:00:00Z")
cl = run_failed_plan("groomer", [ok0, f1, f2, f3, f4, ok1], issue, [c2], [], [], "acme-owner")
assert set(cl) == {"close"} and cl["close"]["number"] == 9 and "runs/106" in cl["close"]["comment"], "a success closes"
assert run_failed_plan("groomer", [ok1], {**issue, "user": {"login": "acme-owner"}}, [], [], [], "acme-owner") is None, "never closes an issue it didn't open"
# On Airlock (autonomy#170) the router opens its issues as the shared App: those are its own too.
shared_issue = {**issue, "user": {"login": "acme-airlock[bot]"}}
assert set(run_failed_plan("groomer", [ok0, f1, f2, f3, f4, ok1], shared_issue, [c2], [], [], "acme-owner")) == {"close"}, "an issue the router opened on Airlock closes on success"
f5 = rr(108, "failure", "2026-10-25T18:00:00Z", who="acme-airlock[bot]")
assert "the router" in run_failed_plan("groomer", [ok0, f5], None, [], JOBS, [], "acme-owner")["open"]["body"], "a run the router started on Airlock reads as the router's"
assert run_failed_plan("groomer", [ok0, ok1], None, [], [], [], "acme-owner") is None, "green and no issue: nothing"
assert run_failed_plan("groomer", [cx, rr(107, "skipped", "2026-10-25T17:00:00Z")], None, [], [], [], "acme-owner") is None, "only cancelled/skipped: nothing"
cold = run_failed_plan("groomer", [f1, f2, f3], None, [], JOBS, [], "acme-owner")["open"]["body"]
assert "@acme-owner: 3 failed runs in a row" in cold and "Artifacts: none" in cold, "a role already 3 down when first seen mentions in the new issue"
sig = run_failed_plan("groomer", [{**f1, "display_title": "https://github.com/acme/api/issues/1"}], None, [], JOBS, [], "acme-owner")["open"]["body"]
assert "with signal https://github.com/acme/api/issues/1" in sig, "a run titled with its signal names it"
nostep = [{"name": "run", "conclusion": "failure", "html_url": "j", "steps": [{"name": "a", "conclusion": "success"}]}]
assert "no failing step" in run_failed_plan("groomer", [f1], None, [], nostep, [], "acme-owner")["open"]["body"]
print("ok  run failed")
from router import OVERRIDE_MARK, override_line, override_plan, review_required_repos, review_verdict  # noqa: E402

HEAD, OLD = "c39b012aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "7aad6e3bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
cm = lambda i, who, body, at: {"id": i, "user": {"login": who}, "body": body, "created_at": at}  # noqa: E731
fail_head = cm(1, "claude[bot]", f"Intent: unrelated change in x.py:3\n\nVerdict: FAIL ({HEAD})\n", "2026-10-25T10:00:00Z")
pass_head = cm(2, "claude[bot]", f"All checks hold.\nVerdict: PASS ({HEAD})", "2026-10-25T11:00:00Z")
fail_old = cm(3, "claude[bot]", f"Verdict: FAIL ({OLD})", "2026-10-25T09:00:00Z")
mpr2 = {"number": 7, "title": "t", "html_url": "https://github.com/acme/mobile/pull/7", "merged_at": "2026-10-25T12:00:00Z",
        "head": {"sha": HEAD}, "user": {"login": "acme-mobile-engineer[bot]"}, "merged_by": {"login": "acme-owner"}}
o = override_plan(mpr2, [fail_head])
assert o["comment"].startswith(f"@acme-owner: Merged over a FAIL verdict on {HEAD}.") and "Reply `Override: <one line>`" in o["comment"], o
assert OVERRIDE_MARK.format(pr=7) in o["comment"], "FAIL on the merged head comments once, with the marker"
assert override_plan(mpr2, [fail_head, {"body": o["comment"], "user": {"login": "acme-router[bot]"}}]) is None, "a second run writes nothing"
assert override_plan(mpr2, [pass_head]) is None, "PASS does nothing"
assert override_plan(mpr2, [fail_old, pass_head]) is None, "FAIL on an earlier head, then PASS on the merged head: nothing"
assert override_plan(mpr2, [fail_head, pass_head]) is None, "the newest verdict for the head wins"
assert override_plan(mpr2, [fail_old]) is None, "no verdict on the merged head: nothing"
assert override_plan({**mpr2, "user": {"login": "acme-deployer[bot]"}}, [fail_head]) is None, "a deployer PR does nothing"
assert override_plan(mpr2, [cm(4, "acme-mobile-engineer[bot]", f"Verdict: FAIL ({HEAD})", "2026-10-25T10:00:00Z")]) is None, "only claude[bot]'s verdict counts"
quoted = cm(5, "claude[bot]", f"Verdict: FAIL ({HEAD})\nquoted above; my verdict:\nVerdict: PASS ({HEAD})", "2026-10-25T10:00:00Z")
assert review_verdict([quoted], HEAD) == "PASS" and review_verdict([fail_old], HEAD) is None, "the last non-empty line, matched whole"
assert override_plan({**mpr2, "merged_at": None}, [fail_head]) is None, "only merged PRs"
assert override_plan({**mpr2, "merged_by": None}, [fail_head])["comment"].startswith("Merged over"), "no merger known: no mention"
asked = cm(6, "acme-router[bot]", o["comment"], "2026-10-25T12:30:00Z")
now = datetime(2026, 10, 30, tzinfo=timezone.utc)
assert "reason asked, none yet" in override_line("mobile", mpr2, [fail_head, asked], now)
assert "no reason given" in override_line("mobile", mpr2, [fail_head, asked], datetime(2026, 11, 2, tzinfo=timezone.utc)), "7 days, no reason"
why = cm(7, "acme-owner", "Override: the FAIL was the stale-body note; fixed in #8", "2026-10-26T09:00:00Z")
line = override_line("mobile", mpr2, [fail_head, asked, why], now)
assert line.startswith("mobile#7 t · merged 2026-10-25 · the FAIL was the stale-body note; fixed in #8"), line
spoof = cm(8, "acme-mobile-engineer[bot]", "Override: all fine", "2026-10-26T10:00:00Z")
assert "reason asked" in override_line("mobile", mpr2, [fail_head, asked, spoof], now), "only the person asked gives the reason"
assert override_line("mobile", mpr2, [fail_head], now) is None, "not asked: not an override line"
fake = cm(9, "acme-owner", o["comment"], "2026-10-25T12:30:00Z")
assert override_line("mobile", mpr2, [fail_head, fake], now) is None, "only the router's marker counts"
watched = review_required_repos()
assert "acme/mobile" in watched and "acme/api" in watched, watched
assert "acme/policy" not in watched and "acme/legacy" not in watched, "repos that don't require claude-review are skipped"
print("ok  override")

# resume: a request the role took and deferred for its PR budget starts it again once one of its PRs closes.
# The replay is autonomy#130 on 2026-10-26: dispatched 15:31, deferred 15:34 (both slots held by #120 and #121),
# the owner's comment 17:46, #121 and #120 closed 17:48 and 17:55. Nothing started the role until Wednesday.
from router import DISPATCH_MARK, RESUME_MARK, GitHub, router_logins, budget_for, is_roles_pr, last_start, resume_plan, role_logins, role_open_count, role_repos, waits_for_slot  # noqa: E402

ROLE, LOGIN = "policy-engineer", "acme-policy-engineer[bot]"
ROUTER_LOGIN = "acme-router[bot]"  # the router role's own App: roles/router/role.yaml identity.github_login
assert router_logins() == {ROUTER_LOGIN, "acme-airlock[bot]"}, router_logins()
LOGINS = role_logins(ROLE)
# On Airlock (autonomy#139, wave 2) the role writes as its own App until that App retires, and as the shared one.
assert LOGINS == {LOGIN, "acme-airlock[bot]"}, LOGINS
KEY = ("acme/policy", 130)
req = {"number": 130, "state": "open", "labels": L("tool-request", "routed"), "html_url": "https://github.com/acme/policy/issues/130"}
dispatched = {"user": {"login": ROUTER_LOGIN}, "created_at": "2026-10-26T15:31:24Z", "body": "Dispatched to the `policy-engineer` role (acme/policy `role-policy-engineer.yml`, signal = this issue)."}
waits = {"user": {"login": LOGIN}, "created_at": "2026-10-26T15:34:23Z", "body": "<!-- policy-engineer:waits pr=120,121 -->\nTaken, and it waits for a PR slot."}
approve = {"user": {"login": "acme-owner"}, "created_at": "2026-10-26T17:46:43Z", "body": "I approve this looks great. Very much needed."}
thread = [dispatched, waits, approve]
mine = lambda n, branch, closed=None, body="": {"number": n, "user": {"login": LOGIN}, "head": {"ref": f"agent/policy-engineer/{branch}"}, "closed_at": closed, "body": body}  # noqa: E731
pr120 = mine(120, "review-gh-first", "2026-10-26T17:55:08Z", "## Signal\nCloses acme/policy#104")
pr121 = mine(121, "override", "2026-10-26T17:48:34Z", "## Signal\nCloses acme/policy#109")
plan = lambda comments=thread, open_count=0, closed=(pr120, pr121), pending=False, budget=2: resume_plan(  # noqa: E731
    ROLE, LOGINS, LOGIN, req, list(comments), open_count, budget, list(closed), pending, KEY)

assert plan() == {"signal": req["html_url"], "freed": ["#120", "#121"]}, plan()
assert last_start(thread, ROLE).isoformat().startswith("2026-10-26T15:31:24"), "the router's dispatch is the start"
assert waits_for_slot(thread, ROLE, LOGINS, last_start(thread, ROLE)), "the owner's comment doesn't change what the role waits for"
marked = {**dispatched, "body": "Dispatched to the `policy-engineer` role. " + DISPATCH_MARK.format(role=ROLE)}
assert last_start([marked], ROLE) is not None, "the dispatch marker counts too"
# On Airlock (autonomy#170) the router writes as the shared App; its markers there count, a person's don't.
assert last_start([{**marked, "user": {"login": "acme-airlock[bot]"}}], ROLE) is not None, "the router's marker from the shared App counts"
assert last_start([{**marked, "user": {"login": "acme-owner"}}], ROLE) is None, "a person pasting the marker isn't the router"

assert plan(open_count=2) is None, "at its budget (counted across its repos by the caller)"
assert plan(closed=[mine(99, "old", "2026-10-26T15:00:00Z")]) is None, "nothing of the role's closed since the start"
assert plan(pending=True) is None, "an open PR names the request"
question = {"user": {"login": LOGIN}, "created_at": "2026-10-26T15:40:00Z", "body": "This needs the owner's call on the scope first: which roles?"}
assert plan(comments=[dispatched, waits, question]) is None, "a question isn't a wait"
assert plan(comments=[dispatched])["freed"] == ["#120", "#121"], "a role that said nothing since the start is resumed"
assert plan(comments=[waits]) is None, "never started by the router: not this rule's"
resumed = {"user": {"login": ROUTER_LOGIN}, "created_at": "2026-10-26T18:00:40Z", "body": "A PR slot is free. " + RESUME_MARK.format(role=ROLE)}
assert plan(comments=thread + [resumed]) is None, "once per freed slot"
pr133 = mine(133, "gate-glob", "2026-10-26T20:38:54Z", "## Signal\nCloses acme/policy#128")
assert plan(comments=thread + [resumed], closed=[pr120, pr121, pr133])["freed"] == ["#133"], "a later close frees a slot again"
assert resume_plan(ROLE, LOGINS, LOGIN, {**req, "state": "closed"}, thread, 0, 2, [pr120], False, KEY) is None
assert resume_plan(ROLE, LOGINS, LOGIN, {**req, "labels": L("routed")}, thread, 0, 2, [pr120], False, KEY) is None

# Review findings on #136 (claude[bot], 2026-10-26):
# 1. Budgets differ by role: support-agent and sales-agent allow 1 open PR.
CFG = {"default": 2, "support-agent": 1, "sales-agent": 1}
assert budget_for("support-agent", CFG) == 1 and budget_for("sales-agent", CFG) == 1 and budget_for(ROLE, CFG) == 2 and budget_for(ROLE, 2) == 2
assert plan(open_count=1, budget=1) is None, "support-agent with one PR open is at its budget"
# 2. The budget counts across every repo the role writes (ops-engineer: k8s and infra): two open in infra and
#    none in k8s is a full budget for a k8s request, and a close in infra frees a slot for it.
OPS = role_repos("ops-engineer")
assert {"acme/k8s", "acme/infra"} <= set(OPS), OPS
ops_pr = lambda n, closed=None, repo="acme/infra": {"number": n, "user": {"login": "acme-ops-engineer[bot]"}, "head": {"ref": f"agent/ops-engineer/x{n}"}, "closed_at": closed, "repo": repo, "body": ""}  # noqa: E731
assert role_open_count("ops-engineer", "acme-ops-engineer[bot]", OPS, {"acme/infra": [ops_pr(90), ops_pr(91)], "acme/k8s": []}) == 2
assert role_open_count("ops-engineer", "acme-ops-engineer[bot]", OPS, {"acme/infra": [ops_pr(90), {**ops_pr(92), "user": {"login": "acme-owner"}, "head": {"ref": "fix/x"}}]}) == 1, "the owner's PRs don't count"
k8s_req = {"number": 700, "state": "open", "labels": L("tool-request", "routed"), "html_url": "https://github.com/acme/k8s/issues/700"}
k8s_start = {**dispatched, "body": "Dispatched to the `ops-engineer` role. " + DISPATCH_MARK.format(role="ops-engineer")}
r = resume_plan("ops-engineer", {"acme-ops-engineer[bot]"}, "acme-ops-engineer[bot]", k8s_req, [k8s_start], 1, 2,
                [ops_pr(90, "2026-10-26T18:00:00Z")], False, ("acme/k8s", 700))
assert r and r["freed"] == ["acme/infra#90"], r
# 4. A role on Airlock comments under the App every Airlock role shares, so only its marker makes a comment
#    there its own (autonomy#162 review): its marked question stops the resume; another Airlock role's
#    unmarked comment under the same login doesn't.
CLERK = role_logins("clerk")
assert "acme-airlock[bot]" in CLERK, CLERK
cdispatch = {**dispatched, "body": "Dispatched to the `clerk` role. " + DISPATCH_MARK.format(role="clerk")}
cquestion = {"user": {"login": "acme-airlock[bot]"}, "created_at": "2026-10-26T15:40:00Z", "body": "Which digest does this answer belong to?\n<!-- clerk:said -->"}
assert not waits_for_slot([cdispatch, cquestion], "clerk", CLERK, last_start([cdispatch], "clerk")), "the role's marked question is the role's"
other = {"user": {"login": "acme-airlock[bot]"}, "created_at": "2026-10-26T15:41:00Z", "body": "Ranked P2.\n<!-- groomer:said -->"}
assert waits_for_slot([cdispatch, other], "clerk", CLERK, last_start([cdispatch], "clerk")), "another Airlock role's comment isn't the clerk's"
assert waits_for_slot([cdispatch, {**other, "body": "Ranked P2."}], "clerk", CLERK, last_start([cdispatch], "clerk")), "an unmarked shared-login comment says nothing"
assert not waits_for_slot([cdispatch, other, {**cquestion, "created_at": "2026-10-26T15:42:00Z"}], "clerk", CLERK, last_start([cdispatch], "clerk")), "the role's own newest comment still decides"
# A role's PR under the shared App: its agent/<role>/ branch, the shared author and a same-repo head, all three.
S = "acme-airlock[bot]"
spr = lambda author, ref, head_repo="acme/policy": {"user": {"login": author}, "head": {"ref": ref, "repo": {"full_name": head_repo}}, "base": {"repo": {"full_name": "acme/policy"}}}  # noqa: E731
assert is_roles_pr(spr(S, "agent/clerk/answers"), "clerk", "acme-clerk[bot]", S), "shared author, own branch, same repo"
assert not is_roles_pr(spr("stranger", "agent/clerk/answers", "stranger/autonomy"), "clerk", "acme-clerk[bot]", S), "a fork named like the role's branch"
assert not is_roles_pr(spr(S, "agent/clerk/answers", "stranger/autonomy"), "clerk", "acme-clerk[bot]", S), "a fork head, even under the shared login"
assert not is_roles_pr(spr("acme-owner", "agent/clerk/answers"), "clerk", "acme-clerk[bot]", S), "a person's PR on the role's branch name"
assert not is_roles_pr(spr(S, "agent/groomer/x"), "clerk", "acme-clerk[bot]", S), "another Airlock role's branch"
assert is_roles_pr(spr("acme-clerk[bot]", "fix/old"), "clerk", "acme-clerk[bot]", S), "the role's old App, any branch (before Airlock)"
# 5. Work turned down isn't restarted: a PR naming the request closed (unmerged) since the start, in the
#    request's repo or another of the role's (a PR in airlock may name autonomy#130).
rejected = mine(135, "airlock-revoke", "2026-10-26T19:00:00Z", "## Signal\nCloses acme/policy#130")
assert plan(closed=[pr120, pr121, rejected]) is None, "a PR for this request closed since the start: a person's call"
elsewhere = {**mine(21, "revoke", "2026-10-26T19:00:00Z", "## Signal\nPart of acme/policy#130"), "repo": "acme/service"}
assert plan(closed=[pr120, pr121, elsewhere]) is None, "a PR in another repo naming the request counts too"
bare = {**mine(22, "x", "2026-10-26T19:00:00Z", "## Signal\nPart of #130"), "repo": "acme/service"}
assert plan(closed=[pr120, pr121, bare])["freed"] == ["#120", "#121", "acme/service#22"], "a bare #130 in airlock is airlock#130, not this request"
# 6. autonomy#151, #139's real history: dispatched 00:01 and waits 00:05 on 2026-10-27; #140 (groomer onto
#    Airlock) merged as `Part of #139` and airlock#32 merged. A merged `Part of` frees its slot and doesn't hold
#    the request, so the role resumes and both closes are its freed slots.
from router import holds_request  # noqa: E402
KEY139 = ("acme/policy", 139)
req139 = {**req, "number": 139, "html_url": "https://github.com/acme/policy/issues/139"}
d139 = {**dispatched, "created_at": "2026-10-27T00:01:25Z"}
w139 = {**waits, "created_at": "2026-10-27T00:05:18Z", "body": "Wave 1, role 1 (groomer) is #140. <!-- policy-engineer:waits -->"}
merged = lambda p, at: {**p, "merged_at": at}  # noqa: E731
pr140 = merged(mine(140, "groomer-airlock", "2026-10-27T12:46:18Z", "## Signal\nPart of acme/policy#139 (Airlock step 5: every role onto Airlock, one role per PR)."), "2026-10-27T12:46:18Z")
air32 = {**merged(mine(32, "mcp-sessions", "2026-10-27T00:40:19Z", "## Signal\nCloses acme/service#31"), "2026-10-27T00:40:19Z"), "repo": "acme/service"}
p139 = lambda closed: resume_plan(ROLE, LOGINS, LOGIN, req139, [d139, w139], 0, 2, closed, False, KEY139)  # noqa: E731
assert p139([pr140, air32]) == {"signal": req139["html_url"], "freed": ["#140", "acme/service#32"]}, p139([pr140, air32])
assert not holds_request(pr140, KEY139), "merged as Part of: frees the slot, doesn't hold the request"
turned_down = mine(141, "sim-triage-airlock", "2026-10-27T15:00:00Z", "## Signal\nPart of #139")
assert p139([turned_down, air32]) is None, "a PR naming the request closed without merging: a person's call (unchanged)"
delivered = merged(mine(142, "last-wave", "2026-10-27T15:00:00Z", "## Signal\nCloses acme/policy#139"), "2026-10-27T15:00:00Z")
assert p139([delivered, air32]) is None, "merged with Closes: delivered"
assert p139([merged({**delivered, "body": "## Signal\nFixes #139"}, delivered["merged_at"]), air32]) is None, "Fixes counts as Closes"
assert p139([pr140, delivered, air32]) is None, "a Closes merge holds it even beside a Part of"
assert p139([merged({**pr140, "body": "## Signal\nPart of #138"}, pr140["merged_at"]), air32])["freed"] == ["#140", "acme/service#32"], "a PR for another request just frees a slot"
# 3. Closed PRs are one page: GitHub.get stops after max_pages even when every page is full.
import json as _json  # noqa: E402
import subprocess as _sp  # noqa: E402
_calls = []
class _Out:
    returncode, stderr = 0, ""
    stdout = _json.dumps([{"number": i} for i in range(50)])
_real = _sp.run
_sp.run = lambda args, **kw: (_calls.append(args[2]), _Out())[1]
try:
    got = GitHub(dry_run=True).get("repos/acme/api/pulls", max_pages=1, state="closed", per_page=50)
finally:
    _sp.run = _real
assert len(got) == 50 and len(_calls) == 1, (len(got), _calls)
# A role on Airlock opens PRs under the shared App: its agent/<role>/ branch says whose they are.
shared = {"number": 7, "user": {"login": "acme-airlock[bot]"}, "head": {"ref": "agent/clerk/answers"}}
assert is_roles_pr(shared, "clerk", "acme-clerk[bot]") and not is_roles_pr(shared, ROLE, LOGIN)
assert not is_roles_pr({"number": 8, "user": {"login": "acme-owner"}, "head": {"ref": "airlock/x"}}, ROLE, LOGIN), "the owner's PRs aren't a role's"
print("OK: resume")

# A repo the router's App isn't installed on is skipped, not fatal: one 404 must not end the pass before the
# reviews, resume, digest-answer, ci_main, delivered and run_failed rules run (autonomy#137's review).
from router import config_repos, without_repos  # noqa: E402
_cfg = {"product_bugs": {"repos": ["acme/api"], "sim_repos": {"sim-shop": "product:shop"}}, "tool_requests": ["acme/api", "acme/service"],
        "reviews": ["acme/service", "acme/k8s"], "ci_main": {"acme/api": "product:core", "acme/service": "x"},
        "run_failed": ["acme/policy"], "resume": True}
assert config_repos(_cfg) == ["acme/api", "acme/k8s", "acme/policy", "acme/service"]
_left = without_repos(_cfg, {"acme/service"})
assert _left["tool_requests"] == ["acme/api"] and _left["reviews"] == ["acme/k8s"], _left
assert _left["ci_main"] == {"acme/api": "product:core"} and _left["resume"] is True, _left
assert without_repos(_cfg, {"acme/api"})["product_bugs"] == {"repos": [], "sim_repos": {"sim-shop": "product:shop"}}
assert _cfg["tool_requests"] == ["acme/api", "acme/service"], "the loaded config itself is left alone"
class _Probe:
    def __init__(self, code, err):
        self.returncode, self.stderr, self.stdout = code, err, "{}"
for _code, _err, _want in [(0, "", True), (1, "gh: Not Found (HTTP 404)", False)]:
    _sp.run = lambda args, **kw: _Probe(_code, _err)
    try:
        assert GitHub(dry_run=True).can_read("acme/service") is _want, (_code, _err)
    finally:
        _sp.run = _real
import router as _router  # noqa: E402
_sleep, _router.time.sleep = _router.time.sleep, lambda s: None
_sp.run = lambda args, **kw: _Probe(1, "gh: Bad credentials (HTTP 401)")
try:
    GitHub(dry_run=True).can_read("acme/service")
    raise AssertionError("a 401 is an outage, not a missing install: it must stop the pass")
except SystemExit:
    pass
finally:
    _sp.run = _real
    _router.time.sleep = _sleep
# The resume rule counts a role's PRs across its writes.repos, which aren't in the config's lists: the skip
# reaches them too (policy-engineer writes airlock), and the probe covers every role's repos.
from router import role_repos  # noqa: E402
assert "acme/service" in role_repos("policy-engineer")
assert role_repos("policy-engineer", skip=frozenset({"acme/service"})) == [r for r in role_repos("policy-engineer") if r != "acme/service"]
import yaml as _yaml  # noqa: E402
from router import config_problems  # noqa: E402
# These were checks on the policy repo's own files; the router now makes them before every pass (config_problems).
assert config_problems(RCFG) == [], config_problems(RCFG)
_unprobed = {**RCFG, "tool_requests": [r for r in RCFG["tool_requests"] if r != "acme/station"],
             "reviews": [r for r in RCFG["reviews"] if r != "acme/station"]}
assert any("roles/router/role.yaml: acme/station is in no router.yaml list" in p for p in config_problems(_unprobed)), \
    "a role's repo the config never probes: a missing install there would end the pass"
print("OK: unreadable repos")

# gate_rerun (autonomy#160): a commit on main that changes what the gate scores by re-runs the gate on each open PR it
# touches whose newest gate run started before it, so a tightening reaches a PR that is already green.
from datetime import datetime as _dt, timezone as _tz  # noqa: E402
from router import gate_policy_changes, gate_rerun_plan, gate_rerun_scope, parse_policy_log  # noqa: E402
_repos = ["acme/api", "acme/cli", "acme/policy"]
_reach = {"cli-engineer": {"acme/cli"}, "api-engineer": {"acme/api", "acme/policy"}}
assert gate_rerun_scope("repos/cli.yaml", _repos, _reach) == {"acme/cli"}
assert gate_rerun_scope("repos/mobile.yaml", _repos, _reach) == set(), "a repo the rule can't re-run is not swept"
assert gate_rerun_scope("repos/cli/AUTONOMY.yaml", _repos, _reach) == set(), "only a policy file itself"
assert gate_rerun_scope("config.yaml", _repos, _reach) == set(_repos)
assert gate_rerun_scope("config.yaml", _repos, _reach) == set(_repos)
assert gate_rerun_scope("roles/cli-engineer/role.yaml", _repos, _reach) == {"acme/cli"}
assert gate_rerun_scope("roles/api-engineer/BRIEF.md", _repos, _reach) == {"acme/api", "acme/policy"}
assert gate_rerun_scope("roles/gone-role/role.yaml", _repos, _reach) == set(_repos), "a removed role reaches every repo"
assert gate_rerun_scope("ops/router/router.py", _repos, _reach) == set() and gate_rerun_scope("README.md", _repos, _reach) == set()
assert gate_rerun_scope("ops/generated/roles/x/allowlist.json", _repos, _reach) == set()
_log = parse_policy_log("\0" + "2026-10-30T12:00:00+00:00\n\nrepos/cli.yaml\nREADME.md\n"
                        "\0" + "2026-10-29T09:00:00+00:00\n\nconfig.yaml\n" "\0" + "2026-10-28T09:00:00+00:00\n\nrouter/router.py\n")
assert [len(f) for _, f in _log] == [2, 1, 1] and _log[0][1] == ["repos/cli.yaml", "README.md"], _log
_changed = gate_policy_changes(_log, _repos, _reach)
_at = lambda s: _dt.fromisoformat(s).replace(tzinfo=_tz.utc)  # noqa: E731
assert _changed == {"acme/cli": _at("2026-10-30T12:00:00"), "acme/api": _at("2026-10-29T09:00:00"),
                    "acme/policy": _at("2026-10-29T09:00:00")}, _changed
assert gate_policy_changes(parse_policy_log(""), _repos, _reach) == {}, "no commits in the window: nothing to sweep"
_pr = {"number": 7, "head": {"sha": "abc1234def"}}
def _run(i, event="pull_request", started="2026-10-30T11:00:00Z", status="completed", sha="abc1234def", path=".github/workflows/autonomy-gate.yml"):
    return {"id": i, "event": event, "run_started_at": started, "created_at": started, "status": status, "head_sha": sha, "path": path}
_when = _at("2026-10-30T12:00:00")
assert gate_rerun_plan(_pr, [_run(1), _run(2)], _when) == {"rerun": 2}, "the latest pull_request gate run is re-run"
assert gate_rerun_plan(_pr, [_run(2, started="2026-10-30T12:05:00Z")], _when) is None, "started after the change: already current"
assert gate_rerun_plan(_pr, [_run(1), _run(3, event="pull_request_review", started="2026-10-30T12:05:00Z")], _when) is None, \
    "a review's gate run after the change already scored the head against today's main"
assert gate_rerun_plan(_pr, [_run(1, event="pull_request_review")], _when) == {"skip": "no autonomy-gate (pull_request) run on abc1234"}
assert gate_rerun_plan(_pr, [_run(1, sha="0000000")], _when) == {"skip": "no autonomy-gate (pull_request) run on abc1234"}, "a run on an older head says nothing"
assert gate_rerun_plan(_pr, [], _when)["skip"], "no gate run at all: listed"
assert gate_rerun_plan(_pr, [_run(1, path=".github/workflows/ci.yml")], _when)["skip"], "only the gate workflow's runs"
assert gate_rerun_plan(_pr, [_run(1, path=".github/workflows/autonomy-gate.yml@refs/pull/7/merge")], _when) == {"rerun": 1}
assert gate_rerun_plan(_pr, [_run(1, status="in_progress")], _when) is None, "a run still going can't be re-run: the next pass looks again"
# The re-run's own start time is the marker: once it started, the next pass writes nothing.
assert gate_rerun_plan(_pr, [{**_run(2), "run_started_at": "2026-10-30T12:10:00Z"}], _when) is None
# Each repo the rule re-runs in needs Actions write, which the router's token has only on its writes.repos.
_router_file = _yaml.safe_load((FIX / "roles" / "router" / "role.yaml").read_text())
assert _router_file["writes"]["dispatch"] is True, "a re-run needs the Actions write that dispatch grants"
assert set(RCFG["gate_rerun"]["repos"]) <= set(_router_file["writes"]["repos"])
_gr = lambda repos: {**RCFG, "gate_rerun": {**RCFG["gate_rerun"], "repos": repos}, "reviews": [*RCFG["reviews"], *repos]}  # noqa: E731
assert any("are not in roles/router/role.yaml writes.repos" in p for p in config_problems(_gr(["acme/marketing"]))), "outside the router's writes.repos"
_saved = _tenant.role_file
_tenant.role_file = lambda role, roles_dir=None: {**_saved(role, roles_dir), "writes": {**_saved(role, roles_dir)["writes"], "dispatch": False}} if role == "router" else _saved(role, roles_dir)
try:
    assert any("writes.dispatch: true" in p for p in config_problems(RCFG)), "no dispatch, no Actions write"
finally:
    _tenant.role_file = _saved
_nofile = {**_gr(["acme/gone"])}
assert any("gate_rerun repo acme/gone has no repos/gone.yaml" in p for p in config_problems(_nofile)), "every gate_rerun repo has a policy file"
_left = without_repos({**_cfg, "gate_rerun": {"repos": ["acme/api", "acme/service"], "max_per_pass": 3}}, {"acme/service"})
assert _left["gate_rerun"] == {"repos": ["acme/api"], "max_per_pass": 3}, _left
assert "acme/infra" in config_repos({"gate_rerun": {"repos": ["acme/infra"]}}), "gate_rerun repos are probed too"
# A refused re-run is reported, not fatal: the pass goes on and fails at the end.
_sp.run = lambda args, **kw: _Probe(1, "gh: Resource not accessible by integration (HTTP 403)")
try:
    assert GitHub(dry_run=False).attempt("POST", "repos/acme/api/actions/runs/1/rerun", {}) == "gh: Resource not accessible by integration (HTTP 403)"
    assert GitHub(dry_run=True).attempt("POST", "repos/acme/api/actions/runs/1/rerun", {}) is None, "a dry run writes nothing"
finally:
    _sp.run = _real
print("OK: gate_rerun")


# A Signal link to an issue that doesn't exist is skipped, not the end of the pass: get_or_none answers None on a 404,
# and fails as get() does on anything else.
import subprocess as _sp  # noqa: E402
from unittest import mock as _mock  # noqa: E402
import router as _router  # noqa: E402
_gh = _router.GitHub(dry_run=True)
if True:
    with _mock.patch.object(_sp, "run", return_value=_sp.CompletedProcess([], 1, "", "gh: Not Found (HTTP 404)")):
        assert _gh.get_or_none("repos/acme/service/issues/606") is None
    with _mock.patch.object(_sp, "run", return_value=_sp.CompletedProcess([], 0, '{"number": 7, "state": "open"}', "")):
        assert _gh.get_or_none("repos/acme/service/issues/7") == {"number": 7, "state": "open"}
    print("OK: a missing Signal link is skipped")


# The config check, the tenant's own boundaries (config_problems): another organization's repo, an old-shaped key, a
# digest-answer rule with no role, or a key the router doesn't know (a typo would otherwise switch a rule off silently).
_p = lambda **kw: config_problems({**RCFG, **kw})  # noqa: E731
assert any("other/api is not a repository of acme" in p for p in _p(reviews=[*RCFG["reviews"], "other/api"])), "one tenant's router never reads another's repos"
assert any("not a list" in p for p in _p(product_bugs=["acme/api"])), "the old list shape of product_bugs"
assert any("digest_answers names the role" in p for p in _p(digest_answers=True)), "digest_answers needs its role"
assert any("unknown key `tool_request`" in p for p in _p(tool_request=["acme/api"])), "a typo is a problem, not a rule turned off"
assert any("`tool_requests` has no value" in p for p in _p(tool_requests=None)), "a key with no value is a problem, not a TypeError"
assert any("`reviews` is a list" in p for p in _p(reviews="acme/api")), "a string where a list goes"
assert any("`ci_main` is a mapping" in p for p in _p(ci_main=["acme/api"]))
assert not any("not a repository of" in p for p in _p(reviews=[*RCFG["reviews"], "ACME/api"])), "organization names are case-insensitive"
import contextlib as _ctx, io as _io, os as _os, tempfile as _tmp  # noqa: E401,E402
for _bad in ("", "acme", "acme/policy/x", "../x"):
    try:
        _tenant.configure(FIX, _bad)
        raise AssertionError(f"configure accepted {_bad!r}")
    except SystemExit:
        pass
_tenant.configure(FIX, "acme/policy", "router", RCFG)
# main() fails closed: no router.yaml, or a config problem, routes nothing and calls GitHub not once.
_calls.clear()
_sp.run = lambda args, **kw: (_calls.append(args), _Probe(0, ""))[1]
try:
    with _tmp.TemporaryDirectory() as _d:
        for _argv, _want in [(["--policy-dir", _d, "--policy-repo", "acme/policy"], "no router config"),
                             (["--policy-dir", str(FIX), "--policy-repo", "acme/policy", "--config", str(Path(_d) / "r.yaml")], "problem(s)")]:
            Path(_d, "r.yaml").write_text("reviews: [other/api]\n")
            sys.argv = ["router.py", "--dry-run", *_argv]
            try:
                with _ctx.redirect_stderr(_io.StringIO()):
                    _tenant.main()
                raise AssertionError("main() ran on a bad config")
            except SystemExit as e:
                assert _want in str(e), e
finally:
    _sp.run = _real
_tenant.configure(FIX, "acme/policy", "router", RCFG)
assert _calls == [], f"a refused config called GitHub: {_calls}"
print("OK: the router's config check")

# A whole dry-run pass on the acme tenant against a stand-in GitHub: every rule runs, every write it would make is in
# acme's repos or its policy repo, the text it writes names no person, and nothing is written (dry run).
import json as _j  # noqa: E402
_TOOL_REQ = {"number": 5, "title": "t", "state": "open", "labels": [{"name": "tool-request"}], "user": {"login": "acme-api-engineer[bot]"},
             "html_url": "https://github.com/acme/cli/issues/5", "body": "## Needed by\nacme/api", "created_at": "2026-10-30T10:00:00Z"}
_SIM_BUG = {"number": 9, "title": "b", "state": "open", "labels": [], "user": {"login": "x"}, "created_at": "2026-10-30T10:00:00Z",
            "html_url": "https://github.com/acme/api/issues/9", "body": "see https://github.com/acme/sim-shop/issues/3"}
_CI_RUN = {"name": "ci", "status": "completed", "conclusion": "failure", "updated_at": "2026-10-30T10:00:00Z",
           "html_url": "https://github.com/acme/web/actions/runs/1", "head_sha": "abcdef0123"}
_DIGEST = {"number": 40, "title": "digest", "state": "open", "labels": [{"name": "digest"}]}
_ANSWER = {"id": 77, "user": {"login": "acme-owner"}, "body": "1 yes", "created_at": "2026-10-30T10:00:00Z",
           "html_url": "https://github.com/acme/policy/issues/40#c77"}


def _fake_gh(args, **kw):
    if args[0] != "gh":
        return _Probe(0, "")  # the gate_rerun rule's log of the policy repo: no commits
    _calls.append(args)
    path = args[2] if len(args) > 2 and not args[2].startswith("-") else ""
    base, _, q = path.partition("?")
    if base.startswith("search/"):
        out = {"items": [], "total_count": 0}
    elif base.endswith("/actions/workflows"):
        out = {"workflows": []}
    elif base.endswith("/runs"):
        out = {"workflow_runs": [_CI_RUN] if base == "repos/acme/web/actions/runs" else [], "total_count": 0}
    elif base == "repos/acme/cli/issues" and "labels=tool-request" in q:
        out = [_TOOL_REQ]
    elif base == "repos/acme/api/issues" and "labels" not in q:
        out = [_SIM_BUG]
    elif base == "repos/acme/sim-shop/issues/3":
        out = {"number": 3, "body": "<!-- sim-incident:signature=invariant:ledger -->"}
    elif base == "repos/acme/policy/issues" and "labels=digest" in q:
        out = [_DIGEST]
    elif base == "repos/acme/policy/issues/40/comments":
        out = [_ANSWER]
    elif base.count("/") == 2 and base.startswith("repos/"):
        out = {"full_name": base[6:]}
    else:
        out = []
    p = _Probe(0, "")
    p.stdout = _j.dumps(out)
    return p


_calls.clear()
_sp.run = _fake_gh
_out = _io.StringIO()
try:
    sys.argv = ["router.py", "--dry-run", "--policy-dir", str(FIX), "--policy-repo", "acme/policy"]
    with _ctx.redirect_stdout(_out):
        _code = _tenant.main()
finally:
    _sp.run = _real
_tenant.configure(FIX, "acme/policy", "router", RCFG)
_rep = _j.loads(_out.getvalue())
assert _code == 0, _rep
assert not any("-X" in a for a in _calls), "a dry run calls no write"
_writes = _rep["writes"]
assert "acme/cli#5 tool_request" in _rep["routed"] and "acme/cli#5 -> cli-engineer" in _rep["dispatched"], _rep
assert "acme/api#9 product_bug" in _rep["routed"] and "policy#40 digest answers -> clerk" in _rep["dispatched"], _rep
assert any(w.startswith("POST repos/acme/policy/actions/workflows/role-cli-engineer.yml/dispatches") for w in _writes), _writes
assert any(w.startswith("POST repos/acme/policy/actions/workflows/role-clerk.yml/dispatches") for w in _writes), _writes
assert any(w.startswith("POST repos/acme/web/issues ") for w in _writes), "the failing CI run on main gets its issue"
assert any(w.startswith("PATCH repos/acme/api/issues/9") and "Sim incident sim-shop#3" in w for w in _writes), _writes
assert all(w.split()[1].startswith("repos/acme/") for w in _writes), _writes
assert all(o == "acme" for w in _writes for o in re.findall(r"github\.com/([\w.-]+)/", w)), "every link it writes is in acme"
assert all(a[2].startswith(("repos/acme/", "search/")) for a in _calls), "reads stay in acme"
print("OK: a dry-run pass on acme")

# The action's own rules (CLAUDE.md, the org): every action pinned to a full commit sha, no expression inside a run
# script, Python isolated, a token required, and dry-run exactly true or false, true by default.
_action = yaml.safe_load((ROOT / "router" / "action.yml").read_text())
for _st in _action["runs"]["steps"]:
    if "uses" in _st:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", _st["uses"]), _st["uses"]
    if "run" in _st:
        assert "${{" not in _st["run"], _st.get("name")
        assert all("python -I" in l for l in _st["run"].splitlines() if re.search(r"\bpython3? ", l)), _st.get("name")
_route = next(s for s in _action["runs"]["steps"] if s.get("id") == "route")["run"]
assert "true) mode=(--dry-run)" in _route and "false) mode=()" in _route and "*) echo" in _route, "dry-run is true or false"
assert '[ -z "$GH_TOKEN" ]' in _route, "no token, no pass"
assert _action["inputs"]["dry-run"]["default"] == "true", "a caller that forgets the input writes nothing"
assert _action["inputs"]["token"]["required"] is True
print("OK: the action")
