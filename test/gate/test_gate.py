#!/usr/bin/env python3
"""Tests for the gate (gate/gate.py, gate/rerun.py, gate/policy.py), on invented acme policies. Run: python3 test/gate/test_gate.py"""
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "gate"))
from gate import airlock_credit, effective_level, is_agent  # noqa: E402

api = yaml.safe_load((FIXTURES / "api.yaml").read_text())
cli = yaml.safe_load((FIXTURES / "lunar-cli.yaml").read_text())
infra = yaml.safe_load((FIXTURES / "infra.yaml").read_text())
salon = yaml.safe_load((FIXTURES / "lunar-salon.yaml").read_text())

cases = [
    # (policy, changed paths, expected level, note)
    (cli, ["src/main.rs", "README.md"], "L4", "lunar-cli ordinary change stays L4"),
    (cli, ["AUTONOMY.yaml"], "L2", "editing the policy file is L2 everywhere"),
    (api, ["apps/api/src/main/java/dev/coderanger/api/parlor/internal/ParlorService.java"], "L3", "a Parlor module change runs at the L3 default"),
    (api, ["apps/api/src/main/java/dev/coderanger/api/lunarmd/internal/charts/ChartEntity.java"], "L1", "EHI chart write is L1"),
    (api, ["apps/api/src/main/java/dev/coderanger/api/lunarmd/internal/appointments/Booking.java"], "L2", "LunarMD module, non-EHI, is L2"),
    (api, ["apps/api/src/main/java/dev/coderanger/api/lunarmd/internal/patients/Patient.java", "README.md"], "L1", "strictest path wins"),
    (api, ["packages/platform-sms/src/Sender.java"], "L2", "SMS package is L2"),
    (api, ["apps/api/src/main/resources/db/migration/V100__x.sql"], "L2", "migrations are L2 (the Flyway path, api#1075)"),
    (api, ["apps/api/src/main/java/dev/coderanger/api/capcom/internal/BroadcastService.java"], "L2", "Capcom sending is L2"),
    (api, ["apps/api/src/main/java/dev/coderanger/api/ops/internal/RevenueController.java"], "L3", "an ops aggregate route runs at the L3 default"),
    (infra, ["dns.tf"], "L1", "root-level file glob matches"),
    (infra, ["modules/static-site/main.tf"], "L2", "untouched infra path takes the default"),
    (salon, ["src/sim/orchestrator.ts"], "L4", "sim change is L4"),
    (salon, ["config/email-templates/welcome.html"], "L2", "floor keyword 'email' in a path caps at L2 even without a carve-out"),
]

failed = 0
for policy, paths, want, note in cases:
    got, reasons = effective_level(policy, paths)
    ok = got == want
    failed += not ok
    print(f"{'ok ' if ok else 'BAD'} {note}: {paths} -> {got}" + ("" if ok else f" (wanted {want}); {reasons}"))

# A wildcard before a trailing /** (autonomy#128): the prefix matches any of the path's leading directories.
from gate import _match  # noqa: E402
for glob, path, want in [
    ("src/**/send/**", "src/send/a.ts", True),
    ("src/**/send/**", "src/lib/send/a.ts", True),
    ("src/**/send/**", "src/a/b/send/c/d.ts", True),
    ("src/**/send/**", "src/sender/a.ts", False),
    ("src/**/send/**", "lib/send/a.ts", False),
    ("**/payments/**", "payments/a", True),
    ("**/payments/**", "x/payments/a", True),
    ("**/payments/**", "x/y/payments/z/a", True),
    ("**/payments/**", "x/prepayments/a", False),
    ("apps/api/src/main/resources/db/migration/**", "apps/api/src/main/resources/db/migration/V1__x.sql", True),
    ("docs/**", "docs", True),
    ("docs/**", "docsite/a.md", False),
    ("docs/**", "x/docs/a.md", False),
]:
    ok = _match(glob, path) == want
    failed += not ok
    print(f"{'ok ' if ok else 'BAD'} _match({glob!r}, {path!r}) is {want}")
capcom = yaml.safe_load((FIXTURES / "lunar-capcom.yaml").read_text())
houston = yaml.safe_load((FIXTURES / "lunar-houston.yaml").read_text())
for name, policy in (("lunar-capcom", capcom), ("lunar-houston", houston)):
    got, reasons = effective_level(policy, ["src/lib/send/broadcast.ts"])
    ok = got == "L2" and any("src/**/send/**" in r for r in reasons)
    failed += not ok
    print(f"{'ok ' if ok else 'BAD'} {name}: src/lib/send/broadcast.ts is L2 by src/**/send/** -> {got}" + ("" if ok else f"; {reasons}"))

tighter = {**cli, "default": "L3"}
got, _ = effective_level(tighter, ["AUTONOMY.yaml"], base_policy=cli)
assert got == "L3", f"a tightening-only policy edit computes from the new default, got {got}"
got, _ = effective_level(cli, ["AUTONOMY.yaml"], base_policy=tighter)
assert got == "L2", f"a loosening policy edit is L2, got {got}"
got, _ = effective_level({**api, "paths": api["paths"][1:]}, ["AUTONOMY.yaml"], base_policy=api)
assert got == "L2", f"removing a carve-out is a loosening, got {got}"
print("ok  policy edits: tightening computes, loosening is L2")

markers = {"logins": ["lunar-builder[bot]"]}
assert is_agent("feat/x", "lunar-builder[bot]", markers)
assert not is_agent("claude/fix-thing", "pyrabbit", markers), "a branch name never makes Matthew's PR an agent's"
assert not is_agent("agent/policy-change", "pyrabbit", markers)
assert not is_agent("feat/x", "dependabot[bot]", markers), "only the roles' logins are agents"
assert is_agent("chore/autonomy-policy", "lunar-builder[bot]", markers), "no branch makes an agent's PR a person's"
legacy = {**markers, "owner_branches": ["chore/autonomy-policy"]}
assert is_agent("chore/autonomy-policy", "lunar-builder[bot]", legacy), "an old config's owner_branches exempts nothing"
assert is_agent("chore/autonomy-demote-x", "lunar-builder[bot]", markers)
# A deleted role App's open PRs show `ghost` as their author: they stay an agent's, never a person's (autonomy#139).
assert is_agent("agent/api-engineer/x", "ghost", markers), "a deleted App's PR is not a person's"
assert is_agent("agent/api-engineer/x", None, markers) and is_agent("agent/api-engineer/x", "", markers), "no author fails closed"
assert not is_agent("feat/x", "ghostwriter", markers), "only the exact deleted-account login"
print("ok  agent markers")

# Airlock (lunareng/airlock build step 3): every role on Airlock opens PRs as one App, so its PR needs
# Airlock's stamp crediting a role on the head commit; any other author is credited by its login as before.
air = {"app_login": "lunar-astronauts[bot]", "url": "https://airlock.example.com"}
assert airlock_credit("lunar-astronauts[bot]", air, {"role": "clerk", "reason": None}) == (True, "Airlock: the clerk role pushed this commit (stamp agent: clerk)")
ok, why = airlock_credit("lunar-astronauts[bot]", air, {"role": None, "reason": "the branch \"fix\" doesn't name a role (agent/<role>/...)"})
assert not ok and "doesn't name a role" in why, "an unproven stamp fails the PR"
assert airlock_credit("lunar-astronauts[bot]", air, None)[0] is False, "no stamp (Airlock unreachable, no delivery) fails the PR"
assert airlock_credit("lunar-astronauts[bot]", air, {"role": "Clerk; rm -rf"})[0] is False, "a role that isn't a role name credits nothing"
assert airlock_credit("lunar-astronauts[bot]", air, {"role": ["clerk"]})[0] is False
assert airlock_credit("lunar-astronauts[bot]", air, {"role": "clerk\n"})[0] is False, "a trailing newline is not a role name"
for odd in ("x", 1, [], None):
    assert airlock_credit("lunar-astronauts[bot]", air, odd)[0] is False, f"a stamp that isn't an object credits nothing: {odd!r}"
assert "\n" not in airlock_credit("lunar-astronauts[bot]", air, {"role": None, "reason": "a\nb\x1b[31m"})[1], "a reason can't break the summary"
assert airlock_credit("lunar-clerk[bot]", air, None) == (True, None), "a role's own App is credited by its login"
assert airlock_credit("pyrabbit", air, None) == (True, None), "a person's PR needs no stamp"
assert airlock_credit("lunar-astronauts[bot]", {}, None) == (True, None), "no airlock config: nothing to check"
print("ok  airlock credit")

# The verdict, end to end through the CLI: an Airlock PR fails at L3 without a credited role, passes with one,
# and passes without one once the owner approved the commit.
import subprocess, tempfile, os  # noqa: E401,E402
with tempfile.TemporaryDirectory() as d:
    run = lambda *a, **kw: subprocess.run(a, cwd=d, check=True, capture_output=True, text=True, **kw)  # noqa: E731
    run("git", "init", "-q", "-b", "main")
    Path(d, "AUTONOMY.yaml").write_text("default: L3\n")
    run("git", "add", "-A")
    run("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "base")
    Path(d, "README.md").write_text("x\n")
    run("git", "add", "-A")
    run("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "change")
    cfg = Path(d, "config.yaml")
    cfg.write_text(yaml.safe_dump({"agent_markers": {"logins": ["lunar-astronauts[bot]"]}, "airlock": air}))
    stamp = Path(d, "stamp.json")
    def verdict(extra, stamp_body=None):
        if stamp_body is None:
            stamp.unlink(missing_ok=True)
        else:
            stamp.write_text(json.dumps(stamp_body))
        r = subprocess.run([sys.executable, str(ROOT / "gate" / "gate.py"), "--base", "HEAD~1", "--author", "lunar-astronauts[bot]",
                            "--config", str(cfg), "--stamp-file", str(stamp), "--json", *extra], cwd=d, capture_output=True, text=True)
        return r.returncode, json.loads(r.stdout)
    code, out = verdict([], {"role": "clerk", "reason": None})
    assert code == 0 and out["credited"] and out["level"] == "L3", out
    code, out = verdict([], {"role": None, "reason": "the clerk role held no Airlock token for api when this was pushed"})
    assert code == 1 and not out["credited"], out
    code, out = verdict([])
    assert code == 1 and not out["credited"], "no stamp file at all fails"
    code, out = verdict(["--approved"])
    assert code == 0 and out["allowed"], "the owner's approval on this commit passes it"
print("ok  airlock verdict through the CLI")

# L1's second key (autonomy#146, Matthew 2026-10-27): approval is not enough at L1. The PR needs a Proof owed section
# naming each command and its expected output, the owner's proof for every item in a comment, and auto-merge off.
from gate import proof_given, proof_owed_items, second_key  # noqa: E402
OWED = ("## Signal\nCloses #1\n\n## Proof owed\n"
        "- [ ] `kubectl diff -k infrastructure/x --context prod`: expected output is one Deployment changing its image\n"
        "- [ ] `gh workflow run router.yml --ref b`, expected: the report lists 0 writes\n\n## Rollback\nRevert.\n")
PROOF = "## Proof\n1. ```\ndeployment.apps/x configured\n```\n2. The report lists 0 writes (run 1)\n"
assert proof_owed_items("## Signal\nx\n") is None
assert len(proof_owed_items(OWED)) == 2, proof_owed_items(OWED)
assert proof_owed_items("## Proof owed\n```\n## not a heading\n- not an item\n```\n- `a` expected b\n") == ["`a` expected b"]
assert proof_given([PROOF], 2) == {1, 2}
assert proof_given(["## Proof\n1. done\n2.\n"], 2) == {1}, "an entry with nothing after it proves nothing"
assert proof_given(["1. ok\n2. ok\n"], 2) == set(), "without a `## Proof` heading a comment proves nothing"
assert proof_given(["## Proof owed\n1. ok\n2. ok\n"], 2) == set(), "quoting the Proof owed section is not proof"
assert proof_given(["## Proof\n### 1\nok\n### 2\n```\nout\n```\n"], 2) == {1, 2}
assert proof_given(["## Proof\nthe diff showed one change\n"], 1) == {1}, "one item owed: an unnumbered proof answers it"
assert proof_given(["## Proof\nthe diff showed one change\n"], 2) == set()
assert proof_given(["## Proof\n1. a\n", "## Proof\n2. b\n"], 2) == {1, 2}, "proof may come in more than one comment"
ok, why = second_key({"body": "## Signal\nx\n", "auto_merge": False, "proofs": []})
assert ok and any("no proof owed" in r for r in why), "L1 owing no proof passes on the owner's approval (2026-10-30)"
ok, why = second_key({"body": "## Signal\nx\n\n## Proof owed\n\nNone: nothing here needs a person beyond the review.\n", "auto_merge": False, "proofs": []})
assert ok, "a Proof owed section with no items owes nothing"
ok, why = second_key({"body": "## Signal\nx\n", "auto_merge": True, "proofs": []})
assert not ok and any("auto-merge is on" in r for r in why), "owing no proof, auto-merge still fails: the body can be edited after the approval"
ok, why = second_key({"body": "## Proof owed\n- run the diff\n", "auto_merge": False, "proofs": ["## Proof\nfine\n"]})
assert not ok and any("item 1 needs an exact command" in r for r in why), "an item without a command and its expected output fails"
ok, why = second_key({"body": OWED, "auto_merge": False, "proofs": []})
assert not ok and any("no proof yet for Proof owed item(s) 1, 2" in r for r in why), "Proof owed but no proof comment fails"
ok, why = second_key({"body": OWED.replace("- [ ]", "- [x]"), "auto_merge": False, "proofs": []})
assert not ok, "ticking the body is not proof: the proof goes in a comment"
ok, why = second_key({"body": OWED, "auto_merge": False, "proofs": ["## Proof\n1. configured\n"]})
assert not ok and any("item(s) 2;" in r for r in why), "proof for some items is not proof for all"
ok, why = second_key({"body": OWED, "auto_merge": True, "proofs": [PROOF]})
assert not ok and any("auto-merge is on" in r for r in why), "auto-merge on fails, even with every proof"
ok, why = second_key({"body": OWED, "auto_merge": False, "proofs": [PROOF]})
assert ok, why
assert second_key(None)[0] is False and second_key("x")[0] is False, "no PR data fails closed"
print("ok  L1 second key")

# The same end to end through the CLI: an agent's PR at L1 and at L2, with and without the owner's approval.
with tempfile.TemporaryDirectory() as d:
    run = lambda *a, **kw: subprocess.run(a, cwd=d, check=True, capture_output=True, text=True, **kw)  # noqa: E731
    run("git", "init", "-q", "-b", "main")
    Path(d, "AUTONOMY.yaml").write_text("default: L3\npaths:\n  - glob: \"cluster/**\"\n    level: L1\n  - glob: \"db/**\"\n    level: L2\n")
    run("git", "add", "-A")
    run("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "base")
    cfg = Path(d, "config.yaml")
    cfg.write_text(yaml.safe_dump({"agent_markers": {"logins": ["lunar-ops-engineer[bot]"]}}))
    prf = Path(d, "pr.json")
    def gate_on(path, extra, pr=None):
        Path(d, path).parent.mkdir(parents=True, exist_ok=True)
        Path(d, path).write_text("x\n")
        run("git", "add", "-A")
        run("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", path)
        if pr is None:
            prf.unlink(missing_ok=True)
        else:
            prf.write_text(json.dumps(pr))
        r = subprocess.run([sys.executable, str(ROOT / "gate" / "gate.py"), "--base", "HEAD~1", "--author", "lunar-ops-engineer[bot]",
                            "--config", str(cfg), "--pr-file", str(prf), "--json", *extra], cwd=d, capture_output=True, text=True)
        return r.returncode, json.loads(r.stdout)
    good = {"body": OWED, "auto_merge": False, "proofs": [PROOF]}
    code, out = gate_on("cluster/a.yaml", ["--approved"], {**good, "body": "## Signal\nx\n", "proofs": []})
    assert code == 0 and out["level"] == "L1" and out["allowed"], f"L1, approved, owing no proof: the approval is enough (2026-10-30); {out}"
    code, out = gate_on("cluster/a2.yaml", [], {**good, "body": "## Signal\nx\n", "proofs": []})
    assert code == 1 and out["level"] == "L1", "L1 owing no proof still needs the owner's approval of its head commit"
    code, out = gate_on("cluster/b.yaml", ["--approved"], {**good, "proofs": []})
    assert code == 1, "L1, approved, Proof owed but no proof comment: fails"
    code, out = gate_on("cluster/c.yaml", ["--approved"], {**good, "auto_merge": True})
    assert code == 1, "L1, approved, proof on the PR, auto-merge on: fails"
    code, out = gate_on("cluster/d.yaml", ["--approved"])
    assert code == 1, "L1, approved, no PR data: fails"
    code, out = gate_on("cluster/e.yaml", [], good)
    assert code == 1, "L1, proof on the PR but no approval on the head: fails"
    code, out = gate_on("cluster/f.yaml", ["--approved"], good)
    assert code == 0 and out["allowed"], f"L1, proof comment present, approved on the head: passes; {out}"
    code, out = gate_on("db/a.sql", [], good)
    assert code == 1 and out["level"] == "L2", "L2 without approval still fails"
    code, out = gate_on("db/b.sql", ["--approved"])
    assert code == 0 and out["level"] == "L2", "L2 unchanged: the owner's approval passes it, with no PR data, Proof owed or proof"
    code, out = gate_on("db/c.sql", ["--approved"], {"body": "", "auto_merge": True, "proofs": []})
    assert code == 0, "L2 unchanged: auto-merge on an approved L2 PR is still fine"
    code, out = gate_on("src/a.ts", [], {"body": "", "auto_merge": True, "proofs": []})
    assert code == 0 and out["level"] == "L3", "L3 unchanged"
print("ok  L1 second key through the CLI; L2 and L3 unchanged")

# A review re-runs the push-time job when its verdict differs, so the PR keeps one row per check (autonomy#157).
from rerun import job_named, latest_run, rerun_plan  # noqa: E402
runs = [
    {"id": 1, "event": "pull_request", "created_at": "2026-10-28T00:47:00Z", "status": "completed"},
    {"id": 3, "event": "pull_request_review", "created_at": "2026-10-29T13:28:00Z", "status": "in_progress"},
    {"id": 2, "event": "pull_request", "created_at": "2026-10-28T09:00:00Z", "status": "completed"},
]
assert latest_run(runs)["id"] == 2, "the newest pull_request run, never a review run"
assert latest_run([runs[1]]) is None and latest_run([]) is None
jobs = [{"id": 10, "name": "claude-review", "conclusion": "failure"}, {"id": 11, "name": "claude-security-review", "conclusion": "success"}]
assert job_named(jobs, "claude-review")["id"] == 10 and job_named(jobs, "autonomy-gate") is None
done = {"id": 2, "status": "completed"}
red, green = {"id": 9, "conclusion": "failure"}, {"id": 9, "conclusion": "success"}
assert rerun_plan(True, done, red)[0] == "rerun", "an approval re-runs the push-time failure that should now pass"
assert rerun_plan(False, done, green)[0] == "rerun", "a dismissal re-runs the push-time pass that should now fail"
assert rerun_plan(True, done, green)[0] == "none", "agreeing rows: nothing to re-run"
assert rerun_plan(False, done, red)[0] == "none", "a COMMENTED review on an unapproved PR re-runs nothing"
assert rerun_plan(True, done, {"id": 9, "conclusion": "cancelled"})[0] == "rerun", "a cancelled push-time job is not a pass"
assert rerun_plan(True, done, {"id": 9, "conclusion": "skipped"})[0] == "none", "a skipped job (a draft) stays skipped"
assert rerun_plan(True, None, None)[0] == "none", "no pull_request run: this run's own verdict is the only one"
assert rerun_plan(True, done, None)[0] == "none"
assert rerun_plan(True, {"id": 2, "status": "in_progress"}, None)[0] == "wait", "a push-time run still going is waited for"
print("ok  review re-runs the push-time job only when the verdicts differ")

# The review-time gate keeps its own verdict as its result: the re-run step comes before the verdict, and neither it
# nor the gate's own steps may continue on error, so a COMMENTED review can never turn the row green.
action = yaml.safe_load((ROOT / "gate" / "action.yml").read_text())
names = [st.get("name") for st in action["runs"]["steps"]]
assert names.index("re-run the push-time check") < names.index("verdict") == len(names) - 1, names
assert not any("continue-on-error" in st for st in action["runs"]["steps"]), "no gate step continues on error"
assert names.index("airlock (a read of the policy repo; no policy, no gate)") < names.index("compute level"), names
print("ok  the gate action re-runs before its verdict and keeps it")

# lunareng/autonomy's main is the source of truth and AUTONOMY.yaml its copy (autonomy#160): the stricter scores the PR,
# so a path tightened on main binds an open PR at its next gate run, and a stale or loosened copy never scores it looser.
from gate import with_main_policy  # noqa: E402
copy = {"default": "L3", "paths": [{"glob": "src/**", "level": "L3"}]}
main_tight = {"default": "L3", "paths": [{"glob": "src/**", "level": "L2"}]}
lvl, why = with_main_policy(*effective_level(copy, ["src/a.ts"]), main_tight, ["src/a.ts"])
assert lvl == "L2" and any("stricter than AUTONOMY.yaml's L3" in r for r in why) and "main: src/a.ts: L2 (src/**)" in why, why
lvl, why = with_main_policy(*effective_level(main_tight, ["src/a.ts"]), copy, ["src/a.ts"])
assert lvl == "L2", "a copy stricter than main keeps its level: main never loosens a PR"
lvl, why = with_main_policy(*effective_level(copy, ["docs/a.md"]), main_tight, ["docs/a.md"])
assert lvl == "L3" and not any(r.startswith("main:") for r in why), "a path main didn't tighten scores as before"
lvl, why = with_main_policy(*effective_level(copy, ["src/a.ts"]), None, ["src/a.ts"])
assert lvl == "L3" and "AUTONOMY.yaml alone scores it" in why[-1], "no file on main: the copy alone, said so"
looser_main = {"default": "L4", "paths": []}
lvl, _ = with_main_policy(*effective_level(looser_main, ["AUTONOMY.yaml"], base_policy=copy), looser_main, ["AUTONOMY.yaml"])
assert lvl == "L2", "a PR loosening AUTONOMY.yaml is still L2 against the base's copy, even with main already looser"
tightened = {"default": "L2", "paths": copy["paths"]}
lvl, _ = with_main_policy(*effective_level(tightened, ["AUTONOMY.yaml"], base_policy=copy), tightened, ["AUTONOMY.yaml"])
assert lvl == "L2", "rolling a tightening out to the copy computes from the new level, not a forced L2 from main"
lvl, _ = with_main_policy(*effective_level({**tightened, "default": "L3"}, ["AUTONOMY.yaml"], base_policy=copy), copy, ["AUTONOMY.yaml"])
assert lvl == "L3", "a tightening-only copy edit merges at the level the rest of the diff computes, as decided 2026-10-20"
print("ok  the stricter of AUTONOMY.yaml and lunareng/autonomy main")

# The same through the CLI: a green L3 PR fails once main tightens its path to L2, until the owner approves its head.
with tempfile.TemporaryDirectory() as d:
    run = lambda *a, **kw: subprocess.run(a, cwd=d, check=True, capture_output=True, text=True, **kw)  # noqa: E731
    run("git", "init", "-q", "-b", "main")
    Path(d, "AUTONOMY.yaml").write_text(yaml.safe_dump(copy))
    run("git", "add", "-A")
    run("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "base")
    Path(d, "src").mkdir()
    Path(d, "src", "a.ts").write_text("x\n")
    run("git", "add", "-A")
    run("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "change")
    cfg = Path(d, "config.yaml")
    cfg.write_text(yaml.safe_dump({"agent_markers": {"logins": ["lunar-cli-engineer[bot]"]}}))
    main_file = Path(d, "main-policy.yaml")
    def scored(extra):
        r = subprocess.run([sys.executable, str(ROOT / "gate" / "gate.py"), "--base", "HEAD~1", "--author", "lunar-cli-engineer[bot]",
                            "--config", str(cfg), "--json", *extra], cwd=d, capture_output=True, text=True)
        return r.returncode, json.loads(r.stdout)
    main_file.write_text(yaml.safe_dump(copy))
    code, out = scored(["--main-policy", str(main_file)])
    assert code == 0 and out["level"] == "L3", f"green at L3 while main agrees: {out}"
    main_file.write_text(yaml.safe_dump(main_tight))
    code, out = scored(["--main-policy", str(main_file)])
    assert code == 1 and out["level"] == "L2", f"main tightened src/** to L2: the same PR fails on its re-run: {out}"
    code, out = scored(["--main-policy", str(main_file), "--approved"])
    assert code == 0 and out["level"] == "L2", "and passes once the owner approves its head commit"
    code, out = scored(["--main-policy", str(Path(d, "missing.yaml"))])
    assert code == 0 and out["level"] == "L3" and "AUTONOMY.yaml alone" in out["reasons"][-1], out
    code, out = scored([])
    assert code == 0 and out["level"] == "L3" and not any("lunareng/autonomy main" in r for r in out["reasons"]), "no flag: as before"
print("ok  a tightening on main fails a green PR through the CLI")

# A failing gate says why on the Checks tab (Matthew 2026-10-30): one `::error` line, what blocks it first, then
# what set the level, cut short on a large diff, and no reason can end the command or start another.
from gate import annotation, verdict_line  # noqa: E402
g = {"level": "L2", "agent": True, "credited": True, "allowed": False, "changed": 9,
     "reasons": [f"src/{n}.py: L2 (src/**)" for n in range(8)] + ["owner review: none"], "level_reasons": 8}
a = annotation(g)
assert a.startswith("::error title=autonomy gate%3A L2%2C agent PR%2C not yet mergeable::"), a
assert "\n" not in a and "%0A" in a, "one line: GitHub ends a workflow command at a newline"
body = a.split("::", 2)[2].replace("%0A", "\n")
assert body.index("owner review: none") < body.index("Why L2:"), "what blocks it comes before what set the level"
assert "src/4.py" in body and "src/5.py" not in body and "and 3 more in the job summary" in body, body
assert body.startswith("agent PR at L1/L2. A human takes it from here"), body
evil = dict(g, reasons=["x%0A::warning::y\n::error::z"], level_reasons=1)
assert "\n" not in annotation(evil) and "%0A::warning" not in annotation(evil), "a reason can't inject a command"
assert verdict_line(dict(g, allowed=True)) == "Verdict: may merge at this level."
assert verdict_line(dict(g, level="L1", reasons=["the human owner approved this commit, but at L1 a person must also act"])).startswith("Verdict: L1, so a person presses merge")
assert verdict_line(dict(g, credited=False)).startswith("Verdict: Airlock credits no role")
assert annotation({k: v for k, v in g.items() if k != "level_reasons"}), "a result from an older gate.py still annotates"
print("ok  a failing gate's annotation says why")

# The PR's checkout is the gate job's working directory, so nothing the gate runs may import from it: every Python
# call is isolated (-I), and a module a PR plants there never runs (security review, 2026-10-30).
import re as _re  # noqa: E402
action_text = (ROOT / 'gate/action.yml').read_text()
assert not _re.search(r"\bpython (?!-I)", action_text), "every python in the action runs isolated (-I)"
with tempfile.TemporaryDirectory() as d:
    Path(d, "json.py").write_text("open('planted', 'w').write('ran')\n")
    subprocess.run([sys.executable, "-c", "import json"], cwd=d, check=True)
    assert Path(d, "planted").exists(), "without -I, a planted json.py runs"
    Path(d, "planted").unlink()
    subprocess.run([sys.executable, "-I", "-c", "import json"], cwd=d, check=True)
    assert not Path(d, "planted").exists(), "-I keeps it from running"
print("ok  a module the PR plants in its checkout never runs in the gate")

# The owner's latest review is read across every page: a CHANGES_REQUESTED on page 2 cancels an APPROVED on page 1.
assert '--paginate "repos/$REPO/pulls/$PR/reviews?per_page=100"' in action_text and "tail -n 1" in action_text
print("ok  the owner's latest review is read across every page")

# A file moved off a strict path is scored at that path: rename detection would show only where it landed.
with tempfile.TemporaryDirectory() as d:
    run = lambda *a: subprocess.run(a, cwd=d, check=True, capture_output=True, text=True)  # noqa: E731
    run("git", "init", "-q", "-b", "main")
    Path(d, "AUTONOMY.yaml").write_text("default: L3\npaths:\n  - glob: secrets/**\n    level: L1\n")
    Path(d, "secrets").mkdir()
    Path(d, "secrets", "key.txt").write_text("a fairly long file so git sees the move as a rename\n" * 5)
    run("git", "add", "-A")
    run("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "base")
    Path(d, "cfg.yaml").write_text("agent_markers: {logins: []}\n")
    run("git", "mv", "secrets/key.txt", "docs-key.txt")
    run("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "move")
    r = subprocess.run([sys.executable, str(ROOT / 'gate/gate.py'), "--base", "HEAD~1", "--json", "--config", str(Path(d, "cfg.yaml"))], cwd=d, capture_output=True, text=True)
    out = json.loads(r.stdout)
    assert out["level"] == "L1" and any("secrets/key.txt" in x for x in out["reasons"]), out
print("ok  a file moved off a strict path scores that path")

print(f"{'FAIL' if failed else 'OK'}: {failed} failing case(s)")
sys.exit(1 if failed else 0)
