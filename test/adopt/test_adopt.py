#!/usr/bin/env python3
"""adopt/adopt.py against the invented acme policy repo (test/generate/fixtures/policy) and a stand-in gh
(test/adopt/fake_gh.py) whose GitHub is a directory of bare repositories. Run: python3 test/adopt/test_adopt.py
(needs pyyaml and git).

With REPRODUCE=<a policy checkout> and REPRODUCE_TEMPLATES=<a directory of that policy's current autonomy-gate.yml and
claude-review.yml>, it also checks the templates render to those files byte for byte. Nothing here names one."""
import json
import traceback
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
ADOPT = ROOT / "adopt" / "adopt.py"
FIXTURE = ROOT / "test" / "generate" / "fixtures" / "policy"
FAKE = Path(__file__).resolve().parent / "fake_gh.py"
SHA = "0123456789abcdef0123456789abcdef01234567"
failures = 0

sys.path.insert(0, str(ROOT / "adopt"))
import adopt  # noqa: E402


def case(name: str):
    def wrap(fn):
        global failures
        try:
            fn()
            print(f"ok  {name}")
        except AssertionError as e:
            failures += 1
            line = traceback.extract_tb(e.__traceback__)[-1].lineno
            print(f"FAIL {name} (line {line}): {e}")
        return fn
    return wrap


class World:
    """A policy checkout on main, a fake GitHub, and an env whose gh is the stand-in."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.policy = self.tmp / "policy"
        shutil.copytree(FIXTURE, self.policy)
        self.remotes = self.tmp / "remotes"
        self.remotes.mkdir()
        self.log = self.tmp / "gh.log"
        self.log.write_text("")
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        (bin_ / "gh").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        (bin_ / "gh").chmod(0o755)
        # npm and npx stand-ins: a repository using prettier gets node_modules/ in its clone, which must never be committed.
        (bin_ / "npm").write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do if [ "$1" = --prefix ]; then mkdir -p "$2/node_modules"; '
                                  'echo x > "$2/node_modules/junk"; fi; shift; done\n')
        (bin_ / "npx").write_text("#!/bin/sh\nexit 0\n")
        (bin_ / "npm").chmod(0o755)
        (bin_ / "npx").chmod(0o755)
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Acme Tester\n\temail = tester@acme.example\n[init]\n\tdefaultBranch = main\n[protocol \"file\"]\n\tallow = always\n")
        self.env = {**{k: v for k, v in os.environ.items() if k != "GITHUB_REPOSITORY"}, "PATH": f"{bin_}:{os.environ['PATH']}",
                    "FAKE_GH_REMOTES": str(self.remotes), "FAKE_GH_LOG": str(self.log), "GIT_CONFIG_GLOBAL": str(gitconfig),
                    "GIT_CONFIG_NOSYSTEM": "1"}
        self.git("init", "-q", "-b", "main", cwd=self.policy)
        self.git("add", "-A", cwd=self.policy)
        self.git("commit", "-q", "-m", "policy", cwd=self.policy)
        self.git("remote", "add", "origin", "https://github.com/acme/policy.git", cwd=self.policy)

    def git(self, *a, cwd: Path):
        return subprocess.run(["git", *a], cwd=cwd, env=getattr(self, "env", None), capture_output=True, text=True, check=True).stdout

    def repo(self, name: str, files: dict[str, str]) -> None:
        dst = self.remotes / "acme" / f"{name}.git"
        dst.parent.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "--bare", "-b", "main", str(dst), cwd=self.tmp)
        work = self.tmp / f"seed-{name}"
        self.git("clone", "-q", str(dst), str(work), cwd=self.tmp)
        for p, text in files.items():
            (work / p).parent.mkdir(parents=True, exist_ok=True)
            (work / p).write_text(text)
        self.git("add", "-A", cwd=work)
        self.git("commit", "-q", "-m", "seed", cwd=work)
        self.git("push", "-q", "origin", "HEAD:main", cwd=work)

    def show(self, name: str, ref: str, path: str) -> str:
        return subprocess.run(["git", "--git-dir", str(self.remotes / "acme" / f"{name}.git"), "show", f"{ref}:{path}"],
                              capture_output=True, text=True, check=True).stdout

    def has_branch(self, name: str, ref: str) -> bool:
        return subprocess.run(["git", "--git-dir", str(self.remotes / "acme" / f"{name}.git"), "rev-parse", "--verify", "-q", ref],
                              capture_output=True).returncode == 0

    def run(self, *args: str):
        return subprocess.run([sys.executable, "-I", str(ADOPT), *args], env=self.env, capture_output=True, text=True)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def pulls(self) -> list[dict]:
        p = self.remotes / "state.json"
        return json.loads(p.read_text())["pulls"] if p.exists() else []

    def rendered(self) -> dict[str, str]:
        return adopt.Adopt(self.policy, "acme/policy").workflows()


def set_config(policy: Path, edit) -> None:
    p = policy / "config.yaml"
    cfg = yaml.safe_load(p.read_text())
    edit(cfg)
    p.write_text(yaml.safe_dump(cfg, sort_keys=False))


SECTION = (FIXTURE / "seeds/CLAUDE-section.md").read_text().rstrip("\n")


@case("render: the workflows carry the policy's pin, runners, agent logins and owner name, and nothing left unfilled")
def _():
    w = adopt.Adopt(FIXTURE, "acme/policy").workflows()
    gate, review = yaml.safe_load(w["autonomy-gate.yml"]), yaml.safe_load(w["claude-review.yml"])
    assert all("<airlock:" not in t for t in w.values())
    assert gate["jobs"]["gate"]["name"] == "autonomy-gate" and gate["jobs"]["gate"]["runs-on"] == "acme-ci"
    assert gate["jobs"]["gate"]["steps"][1]["uses"] == f"lunareng/airlock-actions/gate@{SHA}"
    assert "      - uses: lunareng/airlock-actions/gate@" + SHA + " # main at acme's pin\n" in w["autonomy-gate.yml"]
    assert "    runs-on: acme-ci # acme's own pool\n" in w["autonomy-gate.yml"]
    r, s = review["jobs"]["review"], review["jobs"]["security"]
    assert (r["name"], s["name"]) == ("claude-review", "claude-security-review")
    assert r["runs-on"] == s["runs-on"] == "acme-ci"
    assert r["steps"][0]["uses"] == f"lunareng/airlock-actions/review@{SHA}" and s["steps"][0]["uses"] == f"lunareng/airlock-actions/review/security@{SHA}"
    assert r["steps"][0]["with"] == {"claude-code-oauth-token": "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}", "agent-logins": "acme-*[bot]", "human-owner": "Dana"}
    assert "agents' PRs, acme-*[bot], and" in w["claude-review.yml"]


@case("render without an adopt block: hosted runners, the review action's own defaults, no comments after pins")
def _():
    d = Path(tempfile.mkdtemp()) / "p"
    shutil.copytree(FIXTURE, d)
    set_config(d, lambda c: (c.pop("adopt"), c["airlock_actions"].pop("note")))
    shutil.copy(FIXTURE / "seeds/CLAUDE-section.md", d / "seeds/CLAUDE-section.md")
    w = adopt.Adopt(d, "acme/policy").workflows()
    review = yaml.safe_load(w["claude-review.yml"])
    assert review["jobs"]["review"]["runs-on"] == "ubuntu-latest"
    assert review["jobs"]["review"]["steps"][0]["with"]["agent-logins"] == "*[bot]"
    assert review["jobs"]["review"]["steps"][0]["with"]["human-owner"] == "the human owner"
    assert f"review@{SHA}\n" in w["claude-review.yml"] and " # " not in w["autonomy-gate.yml"].split("runs-on: ", 1)[1].split("\n", 1)[0]


@case("fails closed on a setting that could break the workflow or the section")
def _():
    bad = [
        ("adopt.runner", lambda c: c["adopt"].update(runner="a b")),
        ("adopt.agent_logins", lambda c: c["adopt"].update(agent_logins='x" injected: "y')),
        ("adopt.human_owner_name", lambda c: c["adopt"].update(human_owner_name="Dana: ${{ x }}")),
        ("adopt.runner_note", lambda c: c["adopt"].update(runner_note="two\nlines")),
        ("adopt.claude_section", lambda c: c["adopt"].update(claude_section="../outside.md")),
        ("airlock_actions.ref", lambda c: c["airlock_actions"].update(ref="main")),
        ("adopt.human_owner_name", lambda c: c["adopt"].update(human_owner_name="No")),
        ("adopt.human_owner_name", lambda c: c["adopt"].update(human_owner_name="123")),
        ("adopt.skip", lambda c: c["adopt"].update(skip="policy")),
        ("adopt.branch", lambda c: c["adopt"].update(branch="main")),
        ("adopt.branch", lambda c: c["adopt"].update(branch="a..b")),
        ("adopt.agent_logins", lambda c: c["adopt"].update(agent_logins="acme-*[bot]\n")),
    ]
    for key, edit in bad:
        d = Path(tempfile.mkdtemp()) / "p"
        shutil.copytree(FIXTURE, d)
        set_config(d, edit)
        try:
            adopt.Adopt(d, "acme/policy").workflows()
        except adopt.AdoptError as e:
            assert key in str(e), (key, str(e))
        else:
            raise AssertionError(f"{key}: accepted")
    d = Path(tempfile.mkdtemp()) / "p"
    shutil.copytree(FIXTURE, d)
    for text in ("## No markers\n", f"{adopt.BEGIN} -->\none\n{adopt.END}\ntwo\n{adopt.END}\n"):
        (d / "seeds/CLAUDE-section.md").write_text(text)
        try:
            adopt.Adopt(d, "acme/policy").section()
            raise AssertionError(f"accepted: {text!r}")
        except adopt.AdoptError as e:
            assert "must start with" in str(e)


@case("splice: replaces the old section and keeps the rest; appends where there is none; starts a new CLAUDE.md")
def _():
    old = "# CLAUDE.md\n\nOurs.\n\n<!-- autonomy:begin (old) -->\nold text\n<!-- autonomy:end -->\n\n## After\nkept\n"
    assert adopt.splice(old, SECTION) == f"# CLAUDE.md\n\nOurs.\n\n{SECTION}\n\n## After\nkept\n"
    assert adopt.splice("# CLAUDE.md\n\nOurs.\n\n\n", SECTION) == f"# CLAUDE.md\n\nOurs.\n\n{SECTION}\n"
    assert adopt.splice(None, SECTION) == f"{adopt.NEW_CLAUDE}\n{SECTION}\n"
    assert adopt.splice(adopt.splice(old, SECTION), SECTION) == adopt.splice(old, SECTION), "idempotent"


@case("rollout: a PR per repository that differs, none where it matches, skipped and failing repositories reported")
def _():
    w = World()
    files = w.rendered()
    w.repo("api", {"README.md": "api\n", ".github/workflows/autonomy-gate.yml": "old\n",
                   "CLAUDE.md": "# CLAUDE.md\n\nThe api.\n\n<!-- autonomy:begin (old) -->\nold\n<!-- autonomy:end -->\n"})
    w.repo("web", {".github/workflows/autonomy-gate.yml": files["autonomy-gate.yml"], ".github/workflows/claude-review.yml": files["claude-review.yml"],
                   "CLAUDE.md": adopt.splice(None, SECTION)})
    w.repo("docs", {"README.md": "docs\n", "package.json": '{"devDependencies": {"prettier": "3.0.0"}}\n'})
    w.repo("policy", {"README.md": "the policy\n"})
    r = w.run("rollout", "--org", "acme", "--policy", str(w.policy), "--signal", "acme/policy#9 (Dana, 2026-10-30)")
    assert r.returncode == 1, "blog has no repository yet: the run reports it and fails"
    assert "api: PR #1" in r.stdout and "docs: PR #2" in r.stdout and "web: up to date" in r.stdout, r.stdout
    assert "blog: acme/blog: clone failed" in r.stderr, r.stderr
    cloned = [c["args"][2] for c in w.calls() if c["args"][:2] == ["repo", "clone"]]
    assert sorted(cloned) == ["acme/api", "acme/blog", "acme/docs", "acme/web"], ("adopt.skip keeps the policy repo out", cloned)
    for name in ("api", "docs"):
        assert w.show(name, "airlock/adopt", ".github/workflows/autonomy-gate.yml") == files["autonomy-gate.yml"]
        assert w.show(name, "airlock/adopt", ".github/workflows/claude-review.yml") == files["claude-review.yml"]
    assert w.show("api", "airlock/adopt", "CLAUDE.md") == f"# CLAUDE.md\n\nThe api.\n\n{SECTION}\n"
    assert w.show("docs", "airlock/adopt", "CLAUDE.md") == adopt.splice(None, SECTION)
    assert w.show("api", "airlock/adopt", "README.md") == "api\n" and not w.has_branch("web", "airlock/adopt")
    tree = subprocess.run(["git", "--git-dir", str(w.remotes / "acme/docs.git"), "ls-tree", "-r", "--name-only", "airlock/adopt"],
                          capture_output=True, text=True, check=True).stdout.split()
    assert "node_modules/junk" not in tree and sorted(tree) == sorted(["README.md", "package.json", *adopt.ADOPTED]), tree
    head = w.git("--git-dir", str(w.remotes / "acme/api.git"), "rev-parse", "airlock/adopt", cwd=w.tmp)
    pr = w.pulls()[0]
    assert pr["head"] == "airlock/adopt" and pr["base"] == "main" and pr["body"].startswith("## Signal\nacme/policy#9 (Dana, 2026-10-30)\n\n## What changes\n")
    assert "acme/policy" in pr["title"] and "seeds/CLAUDE-section.md" in pr["body"]

    r = w.run("rollout", "--org", "acme", "--policy", str(w.policy), "--signal", "again", "api")
    assert r.returncode == 0 and "api: PR #1" in r.stdout, (r.stdout, r.stderr)
    assert len(w.pulls()) == 2, "the open PR is reused, not opened twice"
    assert w.git("--git-dir", str(w.remotes / "acme/api.git"), "rev-parse", "airlock/adopt", cwd=w.tmp) == head, \
        "the open PR already holds this tree: no new commit, no re-run of its checks, no dismissed approval"


@case("rollout: a CLAUDE.md with a begin marker and no end is an error for that repository, never 'up to date'")
def _():
    w = World()
    w.repo("api", {"CLAUDE.md": "# CLAUDE.md\n\n<!-- autonomy:begin (old) -->\nno end\n"})
    r = w.run("rollout", "--org", "acme", "--policy", str(w.policy), "--signal", "x", "api")
    assert r.returncode == 1 and "without one matching" in r.stderr and "up to date" not in r.stdout, (r.stdout, r.stderr)
    assert not w.has_branch("api", "airlock/adopt")


@case("rollout refuses without a signal, or with a stale policy setting, before touching GitHub")
def _():
    w = World()
    r = w.run("rollout", "--org", "acme", "--policy", str(w.policy), "--signal", "  ")
    assert r.returncode == 1 and "--signal" in r.stderr and w.calls() == [], r.stderr
    set_config(w.policy, lambda c: c["adopt"].update(runner="two words"))
    r = w.run("rollout", "--org", "acme", "--policy", str(w.policy), "--signal", "x")
    assert r.returncode == 1 and "adopt.runner" in r.stderr and w.calls() == [], r.stderr
    r = w.run("rollout", "--org", "acme/evil", "--policy", str(w.policy), "--signal", "x")
    assert r.returncode == 1 and "--org" in r.stderr
    w = World()
    r = w.run("rollout", "--org", "acne", "--policy", str(w.policy), "--signal", "x", "api")
    assert r.returncode == 1 and "repos/api.yaml says repo: 'acme/api', not acne/api" in r.stderr and w.calls() == [], r.stderr
    r = w.run("rollout", "--org", "acme", "--policy", str(w.policy), "--signal", "x", "shop")
    assert r.returncode == 1 and "no repos/shop.yaml" in r.stderr and w.calls() == [], r.stderr
    w.git("checkout", "-q", "-b", "proposal", cwd=w.policy)
    r = w.run("rollout", "--org", "acme", "--policy", str(w.policy), "--signal", "x")
    assert r.returncode == 1 and "main" in r.stderr and w.calls() == [], r.stderr
    w.git("checkout", "-q", "main", cwd=w.policy)
    (w.policy / "seeds/CLAUDE-section.md").write_text("<!-- autonomy:begin -->\nunmerged\n<!-- autonomy:end -->\n")
    r = w.run("rollout", "--org", "acme", "--policy", str(w.policy), "--signal", "x")
    assert r.returncode == 1 and "uncommitted" in r.stderr and w.calls() == [], r.stderr


@case("new-repo: creates the repository, opens its first PR with the policy's files and seeds, then protects main")
def _():
    w = World()
    r = w.run("new-repo", "--org", "acme", "--policy", str(w.policy), "--repo", "acme/policy", "blog")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "creating acme/blog" in r.stdout and "populated acme/blog: PR #1" in r.stdout and "acme/blog is protected" in r.stdout, r.stdout
    assert "next: merge https://github.com/acme/blog/pull/1" in r.stdout, r.stdout
    create = next(c for c in w.calls() if c["args"][:4] == ["api", "-X", "POST", "orgs/acme/repos"])
    assert json.loads(create["stdin"])["private"] is True and json.loads(create["stdin"])["allow_merge_commit"] is False
    head = "chore/autonomy-policy"
    assert w.show("blog", head, "AUTONOMY.yaml") == (FIXTURE / "repos/blog.yaml").read_text()
    assert w.show("blog", head, ".github/CODEOWNERS") == (FIXTURE / "generated/codeowners/blog.CODEOWNERS").read_text()
    assert w.show("blog", head, ".github/workflows/claude-review.yml") == w.rendered()["claude-review.yml"]
    assert w.show("blog", head, "GOALS.md") == (FIXTURE / "seeds/blog/GOALS.md").read_text()
    assert w.show("blog", head, "CLAUDE.md") == f"# CLAUDE.md\n\nThe blog: posts under posts/, drafts under drafts/.\n\n{SECTION}\n"
    assert w.show("blog", head, "README.md") == "# blog\n", "the auto_init commit is kept"
    put = [c for c in w.calls() if c["args"][:4] == ["api", "-X", "PUT", "repos/acme/blog/branches/main/protection"]]
    assert len(put) == 1, put
    assert put[0]["stdin"] == (FIXTURE / "generated/branch-protection/blog.json").read_text()
    body = w.pulls()[0]["body"]
    assert body.startswith("## Signal\nacme/policy `repos/blog.yaml` (merged)") and "## What changes" in body

    n = len(w.calls())
    r = w.run("new-repo", "--org", "acme", "--policy", str(w.policy), "--repo", "acme/policy", "blog")
    assert r.returncode == 0 and "creating" not in r.stdout and "populated" not in r.stdout, r.stdout
    assert "next: merge https://github.com/acme/blog/pull/1" in r.stdout and len(w.pulls()) == 1, (r.stdout, w.pulls())
    assert any(c["args"][:3] == ["api", "-X", "PUT"] for c in w.calls()[n:]), "protection is applied every time"


@case("new-repo refuses a repository with no owner_role, a checkout off main, or stale generated files, before GitHub")
def _():
    w = World()
    r = w.run("new-repo", "--org", "acme", "--policy", str(w.policy), "--repo", "acme/policy", "docs")
    assert r.returncode == 1 and "no owner_role" in r.stderr, r.stderr
    r = w.run("new-repo", "--org", "acme", "--policy", str(w.policy), "--repo", "acme/policy", "shop")
    assert r.returncode == 1 and "no repos/shop.yaml" in r.stderr, r.stderr
    w.git("checkout", "-q", "-b", "proposal", cwd=w.policy)
    r = w.run("new-repo", "--org", "acme", "--policy", str(w.policy), "--repo", "acme/policy", "blog")
    assert r.returncode == 1 and "main" in r.stderr, r.stderr
    w.git("checkout", "-q", "main", cwd=w.policy)
    (w.policy / "generated/codeowners/blog.CODEOWNERS").write_text("* @someone\n")
    w.git("commit", "-q", "-am", "stale", cwd=w.policy)
    r = w.run("new-repo", "--org", "acme", "--policy", str(w.policy), "--repo", "acme/policy", "blog")
    assert r.returncode == 1 and "stale" in r.stderr, r.stderr
    assert w.calls() == []


@case("protect: the generated body, as committed; --dry-run sends nothing; a stale or unknown repo is refused")
def _():
    w = World()
    w.repo("web", {"README.md": "web\n"})
    r = w.run("protect", "--org", "acme", "--policy", str(w.policy), "--repo", "acme/policy", "--dry-run", "web")
    body = (FIXTURE / "generated/branch-protection/web.json").read_text()
    assert r.returncode == 0 and r.stdout == f"PUT /repos/acme/web/branches/main/protection\n{body}" and w.calls() == [], r.stdout
    r = w.run("protect", "--org", "acme", "--policy", str(w.policy), "--repo", "acme/policy", "web")
    assert r.returncode == 0 and "applied: acme/web" in r.stdout
    assert w.calls()[0]["stdin"] == body
    r = w.run("protect", "--org", "acme", "--policy", str(w.policy), "--repo", "acme/policy", "shop")
    assert r.returncode == 1 and "no repos/shop.yaml" in r.stderr
    (w.policy / "generated/branch-protection/web.json").write_text("{}\n")
    w.git("commit", "-q", "-am", "stale", cwd=w.policy)
    r = w.run("protect", "--org", "acme", "--policy", str(w.policy), "--repo", "acme/policy", "web")
    assert r.returncode == 1 and "stale" in r.stderr and len(w.calls()) == 1, r.stderr


@case("render writes the two workflows for a person to look at")
def _():
    w = World()
    out = w.tmp / "out"
    r = w.run("render", "--policy", str(w.policy), "--repo", "acme/policy", "--out", str(out))
    assert r.returncode == 0 and sorted(p.name for p in out.iterdir()) == ["autonomy-gate.yml", "claude-review.yml"]
    assert (out / "claude-review.yml").read_text() == w.rendered()["claude-review.yml"]


@case("the templates name no tenant: every value comes from a placeholder")
def _():
    for p in (ROOT / "adopt/templates").iterdir():
        text = p.read_text().replace("lunareng/airlock-actions", "")
        for word in ("lunareng", "Lunar", "lunar-", "Matthew", "acme"):
            assert word not in text, (p.name, word)


if os.environ.get("REPRODUCE"):
    @case("the templates reproduce the given policy's current workflows byte for byte")
    def _():
        w = adopt.Adopt(Path(os.environ["REPRODUCE"]), os.environ.get("REPRODUCE_REPO")).workflows()
        for name, body in w.items():
            want = (Path(os.environ["REPRODUCE_TEMPLATES"]) / name).read_text()
            assert body == want, f"{name} differs"
            print(f"    {name}: identical ({len(body.encode())} bytes)")

print(f"{'FAIL' if failures else 'OK'}: {failures} failing case(s)")
sys.exit(1 if failures else 0)
