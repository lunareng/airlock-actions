#!/usr/bin/env python3
"""The review composite action (review/action.yml, with review/security/): its verdict step run as written, against a stand-in
`gh` that answers from canned reviews and comments through the real jq, and the rules its file must keep.
Run: python3 test/review/test_review.py (needs pyyaml, jq and git)."""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
REVIEW = yaml.safe_load((ROOT / "review" / "action.yml").read_text())
SECURITY = yaml.safe_load((ROOT / "review" / "security" / "action.yml").read_text())
VERDICT = next(s for s in REVIEW["runs"]["steps"] if s.get("name") == "verdict")["run"]
SHA = "a" * 40

# A stand-in gh: `gh api [--paginate] <path> --jq <expr>` answers the canned JSON for the path's kind through jq.
GH = """#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
if args[:2] == ["pr", "comment"]:
    with open(os.environ["POSTED"], "a") as f:
        f.write(sys.stdin.read() + "\\n---\\n")
    sys.exit(0)
path = next(a for a in args if a.startswith("repos/"))
expr = args[args.index("--jq") + 1]
if "/collaborators/" in path:
    login = path.split("/collaborators/")[1].split("/")[0]
    if login == "unreadable":
        sys.exit(1)
    rs = [r for r in json.load(open(os.environ["CANNED"]))["reviews"] if r["user"]["login"] == login]
    writer = rs and rs[-1].get("author_association") in ("OWNER", "MEMBER", "COLLABORATOR")
    permission = rs[-1].get("permission", "write" if writer else "read") if rs else "none"
    sys.stdout.write(subprocess.run(["jq", "-r", expr], input=json.dumps({"permission": permission}), capture_output=True, text=True, check=True).stdout)
    sys.exit(0)
kind = "reviews" if "/reviews" in path else "comments"
data = json.load(open(os.environ["CANNED"]))[kind]
pages = [data[i:i + 100] for i in range(0, len(data), 100)] or [[]]
for page in pages:
    sys.stdout.write(subprocess.run(["jq", "-r", expr], input=json.dumps(page), capture_output=True, text=True, check=True).stdout)
"""


def repo(workflow_change: bool) -> str:
    d = tempfile.mkdtemp()
    run = lambda *a: subprocess.run(a, cwd=d, check=True, capture_output=True, text=True)  # noqa: E731
    run("git", "init", "-q", "-b", "main")
    Path(d, "a.py").write_text("x\n")
    run("git", "add", "-A")
    run("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "base")
    run("git", "update-ref", "refs/remotes/origin/main", "HEAD")
    if workflow_change:
        Path(d, ".github/workflows").mkdir(parents=True)
        Path(d, ".github/workflows/x.yml").write_text("on: push\n")
    else:
        Path(d, "a.py").write_text("y\n")
    run("git", "add", "-A")
    run("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "the PR")
    return d


def verdict(author: str, reviews=(), comments=(), workflow_change=False, agents="*[bot]"):
    d = repo(workflow_change)
    bin_dir = tempfile.mkdtemp()
    Path(bin_dir, "gh").write_text(GH)
    os.chmod(Path(bin_dir, "gh"), 0o755)
    canned = Path(bin_dir, "canned.json")
    canned.write_text(json.dumps({"reviews": list(reviews), "comments": list(comments)}))
    env = {**os.environ, "PATH": f"{bin_dir}:{Path(sys.executable).parent}:{os.environ['PATH']}", "CANNED": str(canned),
           "GH_TOKEN": "x", "ONLY_DOCS": "false", "BASE_REF": "main", "AUTHOR": author, "REPO": "acme/api", "PR": "7",
           "SHA": SHA, "AGENTS": agents, "POSTED": str(Path(bin_dir, "posted")), "HERE": str(ROOT / "review")}
    r = subprocess.run(["bash", "-e", "-c", VERDICT], cwd=d, env=env, capture_output=True, text=True)
    r.posted = Path(bin_dir, "posted").read_text() if Path(bin_dir, "posted").exists() else ""
    return r


claude = lambda body: {"user": {"login": "claude[bot]"}, "body": body}  # noqa: E731
approval = lambda who, sha=SHA, state="APPROVED", assoc="MEMBER", at="2026-11-01T00:00:00Z": {"user": {"login": who}, "commit_id": sha, "state": state, "id": 1, "author_association": assoc, "submitted_at": at}  # noqa: E731

r = verdict("acme-airlock[bot]", comments=[claude(f"looks fine\n\nVerdict: PASS ({SHA})\n")])
assert r.returncode == 0 and "PASS" in r.stdout, r.stdout + r.stderr
r = verdict("acme-airlock[bot]", comments=[claude(f"Verdict: PASS ({SHA})"), claude(f"later\nVerdict: FAIL ({SHA})")])
assert r.returncode == 1 and "verdict on this head is FAIL" in r.stdout, r.stdout
r = verdict("acme-airlock[bot]", comments=[claude(f"Verdict: PASS ({'b' * 40})")])
assert r.returncode == 1 and "no review verdict on this head" in r.stdout, "a verdict on an earlier head says nothing"
r = verdict("acme-airlock[bot]", comments=[{"user": {"login": "acme-airlock[bot]"}, "body": f"Verdict: PASS ({SHA})"}])
assert r.returncode == 1, "only the reviewer's own comments count"
r = verdict("acme-airlock[bot]", comments=[claude(f"quoting: Verdict: PASS ({SHA})\nVerdict: FAIL ({SHA})")])
assert r.returncode == 1, "the last line decides"
print("ok  an agent's PR: PASS on this head passes; FAIL, an older head's PASS, someone else's PASS or a quoted PASS fail")

r = verdict("acme-airlock[bot]", comments=[claude(f"Verdict: FAIL ({SHA})")], reviews=[approval("owner")])
assert r.returncode == 0 and "a person approved this commit" in r.stdout, r.stdout + r.stderr
r = verdict("acme-airlock[bot]", reviews=[approval("other[bot]")])
assert r.returncode == 1, "a bot's approval isn't a person's"
r = verdict("acme-airlock[bot]", reviews=[approval("owner", sha="b" * 40)])
assert r.returncode == 1, "an approval of another commit says nothing"
r = verdict("acme-airlock[bot]", reviews=[approval("x[bot]")] * 150 + [approval("owner")])
assert r.returncode == 0, "the approval on page two counts"
print("ok  a person's approval on this commit passes an agent's PR, past the first page; a bot's or an old commit's doesn't")

r = verdict("someone", comments=[claude(f"Verdict: FAIL ({SHA})")])
assert r.returncode == 0 and "::warning::" in r.stdout, r.stdout
r = verdict("dependabot[bot]", comments=[], agents="acme-*[bot]")
assert r.returncode == 0 and "::warning::" in r.stdout, "outside agent-logins, a bot's PR gets a warning"
r = verdict("acme-airlock[bot]", comments=[], agents="acme-*[bot]")
assert r.returncode == 1, "inside agent-logins, it binds"
print("ok  a person's PR, or a login outside agent-logins, gets the verdict as a warning")

r = verdict("acme-airlock[bot]", workflow_change=True)
assert r.returncode == 1 and "needs a person's approving review" in r.stdout
r = verdict("acme-airlock[bot]", workflow_change=True, reviews=[approval("owner")])
assert r.returncode == 0
r = verdict("someone", workflow_change=True)
assert r.returncode == 0 and "the person merging it is its reviewer" in r.stdout
print("ok  a workflow change: an agent's needs a person's approval on this commit; a person's own passes")

for action in (REVIEW, SECURITY):
    for s in action["runs"]["steps"]:
        if "uses" in s:
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", s["uses"]), s["uses"]
        assert "${{" not in s.get("run", ""), f"{s.get('name') or s.get('id')}: an expression in a script is an injection; pass it in env"
print("ok  every action pinned, no expression in a script")

# The retry (lunareng/autonomy#204): the prompt is built once, by step `prompt`, and is the old inline prompt exactly;
# step `posted` says whether the review posted a verdict for this head; a second attempt runs only when it didn't.
OLD = yaml.safe_load((Path(__file__).resolve().parent / "fixtures" / "action-before-retry.yml").read_text())
old_prompt = next(st for st in OLD["runs"]["steps"] if "claude-code-action" in str(st.get("uses")))["with"]["prompt"]
vals = {"${{ inputs.human-owner }}": "the human owner", "${{ github.event.pull_request.number }}": "7",
        "${{ github.repository }}": "acme/api", "${{ github.event.pull_request.head.sha }}": SHA}
for k, v in vals.items():
    old_prompt = old_prompt.replace(k, v)
STEPS = {st.get("id"): st for st in REVIEW["runs"]["steps"]}
with tempfile.TemporaryDirectory() as d:
    out = Path(d, "out")
    r = subprocess.run(["bash", "-e", "-c", STEPS["prompt"]["run"]], capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(out),
                            "HUMAN": "the human owner", "NUMBER": "7", "REPO": "acme/api", "SHA": SHA})
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    eof = text.split("<<", 1)[1].split("\n", 1)[0]
    built = text.split("\n", 1)[1].rsplit("\n" + eof, 1)[0]
import difflib; assert built == old_prompt.rstrip("\n"), "\n".join(list(difflib.unified_diff(old_prompt.rstrip("\n").split("\n"), built.split("\n"), lineterm=""))[:12])
assert STEPS["review"]["with"]["prompt"] == STEPS["review-again"]["with"]["prompt"] == "${{ steps.prompt.outputs.text }}"
assert "steps.posted.outputs.found == 'false'" in STEPS["review-again"]["if"]
print("ok  the prompt is built once and is the old prompt exactly; the second attempt runs only without a verdict")


def posted(comments):
    bin_dir = tempfile.mkdtemp()
    Path(bin_dir, "gh").write_text(GH)
    os.chmod(Path(bin_dir, "gh"), 0o755)
    canned = Path(bin_dir, "canned.json")
    canned.write_text(json.dumps({"reviews": [], "comments": comments}))
    out = Path(bin_dir, "out")
    subprocess.run(["bash", "-e", "-o", "pipefail", "-c", STEPS["posted"]["run"]], check=True, capture_output=True, text=True,
                   env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CANNED": str(canned), "GITHUB_OUTPUT": str(out),
                        "GH_TOKEN": "x", "REPO": "acme/api", "PR": "7", "SHA": SHA})
    return out.read_text().strip()


assert posted([claude(f"fine\nVerdict: PASS ({SHA})")]) == "found=true"
assert posted([claude(f"Verdict: FAIL ({SHA})")]) == "found=true"
assert posted([]) == "found=false"
assert posted([claude(f"Verdict: PASS ({'b' * 40})")]) == "found=false", "a verdict on another head doesn't count"
assert posted([{"user": {"login": "someone"}, "body": f"Verdict: PASS ({SHA})"}]) == "found=false"
print("ok  posted finds the reviewer's verdict for this head, and only that")

# No verdict says why on the PR, once per commit (airlock#115 and autonomy#205 went green with no comment at all).
MARK = f"<!-- claude-review: no verdict on {SHA} -->"
r = verdict("matthew", workflow_change=True)
assert r.returncode == 0 and "changes a file under `.github/workflows/`" in r.posted and MARK in r.posted, r.posted + r.stderr
assert "person who merges it is its reviewer" in r.posted
r = verdict("matthew", workflow_change=True, comments=[{"user": {"login": "github-actions[bot]"}, "body": f"earlier\n\n{MARK}"}])
assert r.returncode == 0 and r.posted == "", "the note is posted once per commit"
r = verdict("acme-airlock[bot]", workflow_change=True)
assert r.returncode == 1 and "approves this commit" in r.posted, r.posted
r = verdict("acme-airlock[bot]", workflow_change=True, reviews=[approval("matthew")])
assert r.returncode == 0 and r.posted == "", "an approved workflow change needs no note"
r = verdict("matthew")
assert r.returncode == 0 and "ran twice on this commit" in r.posted and MARK in r.posted, r.posted
r = verdict("matthew", comments=[claude(f"Verdict: FAIL ({SHA})")])
assert r.returncode == 0 and r.posted == "", "a FAIL is a verdict: no note"
r = verdict("matthew", comments=[claude(f"Verdict: PASS ({SHA})")])
assert r.posted == ""
r = verdict("acme-airlock[bot]", comments=[claude(f"Verdict: PASS ({SHA})")])
assert r.returncode == 0 and r.posted == ""
STEPS = {st.get("id"): st for st in REVIEW["runs"]["steps"]}
for sid in ("prompt", "review", "posted", "review-again"):
    assert "steps.docs.outputs.workflows != 'true'" in STEPS[sid]["if"], f"{sid} must not run on a workflow PR"
print("ok  no verdict is explained on the PR once per commit; a workflow PR skips both review attempts")



# A re-run of a head with its answer reads it: nobody pays for a second review to turn a row green.
def already(comments=(), reviews=(), author="acme-airlock[bot]", trigger="matthew"):
    bin_dir = tempfile.mkdtemp()
    Path(bin_dir, "gh").write_text(GH)
    os.chmod(Path(bin_dir, "gh"), 0o755)
    canned = Path(bin_dir, "canned.json")
    canned.write_text(json.dumps({"reviews": list(reviews), "comments": list(comments)}))
    out = Path(bin_dir, "out")
    subprocess.run(["bash", "-e", "-o", "pipefail", "-c", STEPS["already"]["run"]], check=True, capture_output=True, text=True,
                   env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CANNED": str(canned), "GITHUB_OUTPUT": str(out),
                        "GH_TOKEN": "x", "REPO": "acme/api", "PR": "7", "SHA": SHA, "AUTHOR": author, "AGENTS": "*[bot]", "TRIGGER": trigger, "HERE": str(ROOT / "review")})
    return out.read_text().strip()


assert already([claude(f"fine\nVerdict: PASS ({SHA})")]) == "found=true"
assert already([claude(f"Verdict: FAIL ({SHA})")]) == "found=false", "a person's re-run reviews a FAIL again: a fixed PR body is new work"
assert already([claude(f"Verdict: FAIL ({SHA})")], trigger="github-actions[bot]") == "found=true", "a bot's re-run reads it: a dismissed approval rolls no dice"
assert already(reviews=[approval("matthew")]) == "found=true", "a person's approval of an agent's head is its answer"
assert already(reviews=[approval("matthew")], author="matthew") == "found=false", "a person's own PR is still reviewed"
assert already([claude(f"Verdict: PASS ({SHA})")], author="matthew") == "found=true"
assert already() == "found=false"
assert already([claude(f"Verdict: PASS ({'b' * 40})")]) == "found=false", "a new head is new work"
assert already(reviews=[approval("other[bot]")]) == "found=false", "a bot's approval isn't a person's"
for sid in ("prompt", "review", "posted", "review-again"):
    assert "steps.already.outputs.found != 'true'" in STEPS[sid]["if"], f"{sid} must not run on a head that has its answer"
# Only a writer's latest word counts: anyone can review a public repository.
assert already(reviews=[approval("stranger", assoc="NONE")]) == "found=false"
assert already(reviews=[approval("stranger", assoc="CONTRIBUTOR")]) == "found=false"
assert already(reviews=[approval("owner"), approval("owner", state="CHANGES_REQUESTED", at="2026-11-02T00:00:00Z")]) == "found=false", "a later change request takes it back"
assert already(reviews=[approval("owner"), approval("second", state="CHANGES_REQUESTED")]) == "found=false", "another writer's change request stands"
assert already(reviews=[approval("owner", state="CHANGES_REQUESTED"), approval("owner", at="2026-11-02T00:00:00Z")]) == "found=true"
# The repository's own answer decides: a private org member reads as a contributor to the workflow's token
# (lunareng/airlock-actions#46: the owner's approval was refused).
private = {**approval("owner", assoc="CONTRIBUTOR"), "permission": "admin"}
r = verdict("acme-airlock[bot]", reviews=[private])
assert r.returncode == 0, "a private member with write access approves"
assert already(reviews=[private]) == "found=true"
r = verdict("acme-airlock[bot]", reviews=[{**approval("owner", assoc="MEMBER"), "permission": "read"}])
assert r.returncode == 1, "a member with read access doesn't"
# A permission it can't read decides nothing, and hides no change requested.
r = verdict("acme-airlock[bot]", reviews=[approval("owner"), approval("unreadable", state="CHANGES_REQUESTED")])
assert r.returncode == 1, "a lookup that fails counts no approval"
# Each person's latest across every page: an approval on page one taken back on page two is no approval.
filler = [approval(f"reader{i}", assoc="NONE", state="COMMENTED", at="2026-11-01T00:00:00Z") for i in range(100)]
r = verdict("acme-airlock[bot]", reviews=[approval("owner"), *filler, approval("owner", state="DISMISSED", at="2026-11-03T00:00:00Z")])
assert r.returncode == 1, "a later page's DISMISSED takes back an earlier page's APPROVED"
r = verdict("acme-airlock[bot]", reviews=[approval("stranger", assoc="NONE")])
assert r.returncode == 1, "a stranger's approval passes no agent's PR"
r = verdict("acme-airlock[bot]", reviews=[approval("owner"), approval("second", state="CHANGES_REQUESTED")])
assert r.returncode == 1, "nor does an approval with a change still requested"
print("ok  a head that passed, or an agent's head a person approved, is read, not reviewed again")

# A person's review waits for the push-time run, then makes its row agree, whatever this run's verdict was.
NAMED = {st.get("name"): st for st in REVIEW["runs"]["steps"]}
ORDER = [st.get("name") or st.get("id") for st in REVIEW["runs"]["steps"]]
assert ORDER.index("wait for the push-time review") < ORDER.index("verdict")
assert "re-run the push-time check" not in ORDER, "actions: write stays off the job that runs a model over a PR"
wait = NAMED["wait for the push-time review"]
RERUN = yaml.safe_load((ROOT / "review" / "rerun" / "action.yml").read_text())
assert "command -v gh" in RERUN["runs"]["steps"][0]["run"], "the lunareng-ci image ships no gh"
assert "actions: write" in RERUN["description"] and "${{" not in RERUN["inputs"]["passed"]["description"]
again = RERUN["runs"]["steps"][1]
TEMPLATE = yaml.safe_load((ROOT / "adopt" / "templates" / "claude-review.yml").read_text())
assert TEMPLATE["permissions"]["actions"] == "read" and TEMPLATE["jobs"]["rows"]["permissions"] == {"actions": "write", "pull-requests": "read"}
assert TEMPLATE["jobs"]["rows"]["name"] != "claude-review" and "review/rerun@" in TEMPLATE["jobs"]["rows"]["steps"][0]["uses"]
assert "needs.review.result == 'failure'" in TEMPLATE["jobs"]["rows"]["if"], "a cancelled review says nothing about the row"
assert "event_name" not in TEMPLATE["jobs"]["rows"]["if"], "both directions: a push can clear a stale review row too"
assert wait["if"] == "github.event_name == 'pull_request_review'" and "--wait-only" in wait["run"]
assert again["env"]["PASSED"] == "${{ inputs.passed }}"
assert "--job claude-review" in again["run"] and "${{" not in again["run"]
assert again["run"].index(".head.sha") < again["run"].index("rerun.py"), "an old head's re-run must not cancel the new head's review"
assert "actions: read" in REVIEW["description"]
assert next(st for st in REVIEW["runs"]["steps"] if "actions/checkout" in st.get("uses", ""))["with"]["persist-credentials"] is False
assert "never give this job actions: write" in REVIEW["description"]
print("ok  a person's review waits for the push-time run and makes its row agree")

# The diffs start at the merge base, so what main gained after this checkout's merge commit isn't this PR's change.
with tempfile.TemporaryDirectory() as d:
    def git(*a):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *a], cwd=d, check=True, capture_output=True, text=True).stdout
    git("init", "-q", "-b", "main")
    Path(d, "a.txt").write_text("a\n"); git("add", "-A"); git("commit", "-qm", "base")
    git("checkout", "-qb", "pr"); Path(d, "b.txt").write_text("b\n"); git("add", "-A"); git("commit", "-qm", "pr")
    git("checkout", "-q", "main"); git("checkout", "-qb", "merged"); git("merge", "-q", "--no-ff", "-m", "merge", "pr")
    git("checkout", "-q", "main"); Path(d, ".github/workflows").mkdir(parents=True); Path(d, ".github/workflows/x.yml").write_text("x\n"); git("add", "-A"); git("commit", "-qm", "main moves")
    git("update-ref", "refs/remotes/origin/main", "main"); git("checkout", "-q", "merged")
    step = STEPS["docs"]["run"]
    assert '"origin/$BASE_REF...HEAD"' in step and '"origin/$BASE_REF" ' not in step, step
    out = Path(d, "out")
    subprocess.run(["bash", "-e", "-c", step], cwd=d, check=True, capture_output=True, text=True, env={**os.environ, "AUTHOR": "acme-airlock[bot]", "BASE_REF": "main", "GITHUB_OUTPUT": str(out)})
    assert "workflows=false" in out.read_text(), "main's newer workflow isn't this PR's"
assert '"origin/$BASE_REF...HEAD" -- .github/workflows/' in VERDICT
print("ok  a workflow main gained after the merge commit isn't this PR's change")

print("OK: 0 failing case(s)")
