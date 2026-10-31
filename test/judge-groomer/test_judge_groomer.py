#!/usr/bin/env python3
"""judge/groomer against an invented acme organization: a fake `gh` on PATH answers from recorded GitHub answers
(fixtures/github.json) and logs every call, and the policy repo is fixtures/policy. Nothing reaches GitHub.
Run: python3 test/judge-groomer/test_judge_groomer.py (needs pyyaml)."""
import copy
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
HERE = Path(__file__).resolve().parent
JUDGE = ROOT / "judge" / "groomer" / "judge.py"
ACTION = ROOT / "judge" / "groomer" / "action.yml"
POLICY = HERE / "fixtures" / "policy"
ANSWERS = json.loads((HERE / "fixtures" / "github.json").read_text())
NOW = "2026-10-30T12:00:00Z"

FAKE_GH = HERE / "gh"  # a fake `gh api`, answering from the recorded answers and logging every call


def judge(*args, answers=None, policy=POLICY, now=NOW):
    """Runs the judge as the action does (python -I), with the fake gh first on PATH. Returns (exit, report, calls)."""
    with tempfile.TemporaryDirectory() as d:
        gh = Path(d) / "gh"
        gh.write_text(FAKE_GH.read_text().replace("/usr/bin/env python3", sys.executable, 1))
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        (Path(d) / "answers.json").write_text(json.dumps(answers or ANSWERS))
        log = Path(d) / "log"
        log.write_text("")
        env = dict(os.environ, PATH=f"{d}:{os.environ['PATH']}", GH_LOG=str(log), GH_ANSWERS=str(Path(d) / "answers.json"))
        r = subprocess.run([sys.executable, "-I", str(JUDGE), "--policy", str(policy), "--now", now, *args],
                           capture_output=True, text=True, env=env)
        calls = [json.loads(l) for l in log.read_text().splitlines()]
    try:
        report = json.loads(r.stdout)
    except json.JSONDecodeError:
        report = {"stdout": r.stdout, "stderr": r.stderr}
    return r.returncode, report, calls


def edit(fn):
    """A copy of the recorded answers with one change."""
    a = copy.deepcopy(ANSWERS)
    fn(a["rules"])
    return a


def rule(rules, path, **params):
    return next(r for r in rules if r["path"] == path and all(r.get("params", {}).get(k) == v for k, v in params.items()))


def with_signal(rules):
    """api#13 gets a linked signal, so every converted pick has one."""
    issue = next(i for i in rule(rules, "repos/acme/api/issues", state="all")["answer"] if i["number"] == 13)
    issue["body"] = issue["body"].replace("someone said so", "https://github.com/acme/policy/issues/4")


def policy_with(role_text: str) -> str:
    d = tempfile.mkdtemp()
    (Path(d) / "roles" / "groomer").mkdir(parents=True)
    (Path(d) / "roles" / "groomer" / "role.yaml").write_text(role_text)
    return d


def main() -> int:
    checks = {}
    sys.path.insert(0, str(JUDGE.parent))
    import judge as j  # noqa: E402  the pure helpers

    # The recorded week: api#12 and web#7 converted in their own repo, api#13 through the org search, api#16's PR
    # merged after its 14 days; api#14 was picked before the window and api#15 is a pull request.
    rc, rep, calls = judge()
    by = {d["issue"]: d for d in rep.get("details", [])}
    checks.update({
        "the repositories are the role file's writes.repos": rep.get("repos") == ["acme/api", "acme/web"],
        "four picks in the trailing 4 weeks (one picked earlier, a pull request skipped)": rep.get("picks") == 4
            and set(by) == {"acme/api#12", "acme/api#13", "acme/api#16", "acme/web#7"},
        "a pick merged in its own repo converts": by.get("acme/api#12", {}).get("merged_pr") is True
            and by.get("acme/web#7", {}).get("merged_pr") is True,
        "a pick merged elsewhere in its owner's repositories converts, through search": by.get("acme/api#13", {}).get("merged_pr") is True,
        "a PR merged after the window doesn't convert its pick": by.get("acme/api#16", {}).get("merged_pr") is False,
        "pick conversion is converted over picks": rep.get("pick_conversion") == 0.75,
        "a Signal section with no link is no signal": rep.get("picks_without_signal") == 1 and by["acme/api#13"]["signal"] is False,
        "open issues over 7 days with no priority label are unranked": rep.get("unranked_over_7d") == 1,
        "demand with a missing field or an old lookup is stale": rep.get("stale_demand") == 2,
        "one pick without a signal fails the judge, exit 1": rc == 1 and rep.get("pass") is False,
        "the search is scoped to the repository's own owner": any(c[1].startswith("search/issues?") and "q=org:acme+" in c[1] for c in calls)
            and not any("lunareng" in c[1] for c in calls),
        "it only reads: every call is a plain gh api GET": all(c[0] == "api" and len(c) == 2 for c in calls),
    })

    rc, rep, _ = judge(answers=edit(with_signal))
    checks["every pick with a signal and 0.75 converted passes, exit 0"] = rc == 0 and rep.get("pass") is True and rep.get("picks_without_signal") == 0

    def unconvert(rules):
        with_signal(rules)
        rule(rules, "repos/acme/web/pulls", state="closed")["answer"] = []
        next(r for r in rules if r.get("q_contains"))["answer"] = {"items": []}
    rc, rep, _ = judge(answers=edit(unconvert))
    checks["1 of 4 converted is under 0.5 and fails, exit 1"] = rc == 1 and rep.get("pick_conversion") == 0.25 and rep.get("pass") is False
    rc, rep, _ = judge("--min-conversion", "0.25", answers=edit(unconvert))
    checks["the pass condition's threshold is the caller's"] = rc == 0 and rep.get("pass") is True
    rc, rep, _ = judge("--max-without-signal", "1")
    checks["the most picks without a signal is the caller's"] = rc == 0 and rep.get("pass") is True

    def no_picks(rules):
        for r in rules:
            if r["path"].endswith("/events"):
                r["answer"] = []
    rc, rep, _ = judge(answers=edit(no_picks))
    checks["no picks passes, with no conversion to report"] = rc == 0 and rep.get("picks") == 0 and rep.get("pick_conversion") is None

    rc, rep, calls = judge("--repos", "acme/web")
    checks["--repos replaces the role file's list"] = rep.get("repos") == ["acme/web"] and not any("acme/api" in c[1] for c in calls)

    rc, rep, _ = judge("--pick-label", "next-up")
    checks["the pick label is the caller's"] = rc == 0 and rep.get("picks") == 0

    def retry_label(rules):
        for r in rules:
            if r["path"].endswith("/events"):
                for ev in r["answer"]:
                    ev["label"]["name"] = ev["label"]["name"].replace("triage:this-week", "next-up")
    rc, rep, _ = judge("--pick-label", "next-up", answers=edit(retry_label))
    checks["another tenant's pick label measures the same picks"] = rep.get("picks") == 4 and rep.get("pick_conversion") == 0.75

    def broken(rules):
        rules.remove(rule(rules, "repos/acme/web/pulls", state="closed"))
    rc, rep, _ = judge(answers=edit(broken))
    checks["a GitHub read that keeps failing is unmeasured, exit 2, never a pass"] = rc == 2 and rep.get("pass") is False and "acme/web/pulls" in rep.get("unmeasured", "")

    rc, rep, calls = judge(policy=tempfile.mkdtemp())
    checks["no role file is unmeasured, exit 2, and reads nothing"] = rc == 2 and rep.get("pass") is False and calls == []
    rc, rep, calls = judge(policy=policy_with("schema: 1\nrole: groomer\nlevel: L4\nwrites:\n  repos: []\n"))
    checks["a role file with no writes.repos is unmeasured, exit 2"] = rc == 2 and "writes.repos" in rep.get("unmeasured", "") and calls == []
    rc, rep, calls = judge(policy=policy_with("writes: {repos: ['acme/api', '../../etc']}\n"))
    checks["a repository that isn't owner/name is unmeasured, exit 2, and reads nothing"] = rc == 2 and calls == []
    rc, rep, calls = judge("--role", "../x")
    checks["a role that isn't a role name is unmeasured, exit 2"] = rc == 2 and calls == []

    checks.update({
        "mentions: #N and repo#N, not another owner's or repo's": j.mentions("Closes #12", "api", 12) and j.mentions("Part of api#12", "api", 12)
            and not j.mentions("other/api#12", "api", 12) and not j.mentions("web#12", "api", 12) and not j.mentions("#123", "api", 12),
        "has_signal: a link, an issue reference or #N under ## Signal": j.has_signal("## Signal\nhttps://x.example/1\n")
            and j.has_signal("## Signal\nacme/api#3\n") and j.has_signal("## Signal\n#3\n## Done means\nx")
            and not j.has_signal("## Signal\nsomeone said so\n## Done means\nhttps://x.example/1") and not j.has_signal("no section #3"),
    })

    # The action: tenant-neutral, every expression through env, never inside a script, every action pinned.
    doc = yaml.safe_load(ACTION.read_text())
    steps = doc["runs"]["steps"]
    checks.update({
        "no organization is named in the judge or its action": not re.search(r"lunareng|coderanger", JUDGE.read_text() + ACTION.read_text(), re.I),
        "no ${{ }} inside a run: script": all("${{" not in s.get("run", "") for s in steps),
        "every uses: is pinned to a full commit sha": all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", s["uses"]) for s in steps if "uses" in s),
        "the judge runs as python -I": "python -I \"$HERE/judge.py\"" in next(s["run"] for s in steps if s.get("id") == "judge"),
        "the token reaches the judge only as GH_TOKEN": next(s for s in steps if s.get("id") == "judge")["env"]["GH_TOKEN"] == "${{ inputs.token }}",
        "every judge flag has an input": all(f"--{k}" in next(s["run"] for s in steps if s.get("id") == "judge")
            for k in doc["inputs"] if k not in ("token", "artifact", "policy-path")),
    })

    fails = 0
    for name, ok in checks.items():
        print(("ok  " if ok else "FAIL") + "  " + name)
        fails += not ok
    print(f"{'OK' if not fails else 'FAIL'}: {len(checks)} cases, {fails} failing")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
