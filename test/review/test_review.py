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
path = next(a for a in args if a.startswith("repos/"))
expr = args[args.index("--jq") + 1]
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
           "SHA": SHA, "AGENTS": agents}
    return subprocess.run(["bash", "-e", "-c", VERDICT], cwd=d, env=env, capture_output=True, text=True)


claude = lambda body: {"user": {"login": "claude[bot]"}, "body": body}  # noqa: E731
approval = lambda who, sha=SHA, state="APPROVED": {"user": {"login": who}, "commit_id": sha, "state": state, "id": 1}  # noqa: E731

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

print("OK: 0 failing case(s)")
