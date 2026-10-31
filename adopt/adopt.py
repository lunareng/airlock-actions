#!/usr/bin/env python3
"""Bring an organization's repositories under its policy repo (lunareng/airlock-actions/adopt). A person runs it, with gh
signed in, from a checkout of this repository at the commit the policy pins:

  adopt.py rollout  --org ORG --policy DIR --signal TEXT [REPO ...]
      One PR per repository with the gate and review workflows and the CLAUDE.md section, where they differ.
      Default: every repos/*.yaml except config.yaml adopt.skip. Needs push and PR rights on each repository.
  adopt.py new-repo --org ORG --policy DIR NAME
      After repos/NAME.yaml has merged: create the repository (private, main, rebase-only), open its first PR
      (AUTONOMY.yaml, CODEOWNERS, the workflows, the CLAUDE.md section, seeds/NAME/), then protect main.
      Needs an org owner.
  adopt.py protect  --org ORG --policy DIR REPO [--dry-run]
      Apply the repository's generated branch protection to main. Needs repository admin.
  adopt.py render   --policy DIR --out DIR
      Write the two workflows as rendered for this policy, to look at.

Everything tenant-specific comes from the policy checkout's config.yaml (adopt/README.md). Fails closed: a missing
setting, a stale generated file, or a policy checkout off main stops it before anything is written.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "generate"))
import generate  # noqa: E402  (this repository's generator: the same settings, paths and rules)

TEMPLATES = {"autonomy-gate.yml": HERE / "templates" / "autonomy-gate.yml", "claude-review.yml": HERE / "templates" / "claude-review.yml"}
PLACEHOLDER = re.compile(r"<airlock:(\w+)>")
BEGIN, END = "<!-- autonomy:begin", "<!-- autonomy:end -->"
NEW_CLAUDE = "# CLAUDE.md\n\nGuidance for Claude Code in this repository.\n"
FIRST_BRANCH = "chore/autonomy-policy"
LOGINS = re.compile(r'^[A-Za-z0-9*\[\]_.,\- ]+$')
PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")


# One error for a setting or step that can't be trusted, the generator's own included: it stops the run and says why.
AdoptError = generate.PolicyError


class Adopt:
    """A policy checkout's settings for adopting repositories: generate's, plus config.yaml `adopt`."""

    def __init__(self, policy: Path, repo: str | None = None):
        self.root = policy.resolve()
        self.gen = generate.Settings(self.root, generate.policy_repo(self.root, repo))
        a = self.gen.cfg.get("adopt") or {}
        if not isinstance(a, dict):
            raise AdoptError("config.yaml adopt: not a mapping")
        self.runner = str(a.get("runner", "ubuntu-latest"))
        if not generate.LABEL.match(self.runner):
            raise AdoptError("config.yaml adopt.runner: one runner label")
        self.runner_note = f" # {generate.one_line(a['runner_note'], 'adopt.runner_note')}" if a.get("runner_note") is not None else ""
        self.agent_logins = str(a.get("agent_logins", "*[bot]"))
        if not LOGINS.match(self.agent_logins):
            raise AdoptError("config.yaml adopt.agent_logins: logins and * only")
        self.human_owner_name = str(a.get("human_owner_name", "the human owner"))
        if not PERSON.match(self.human_owner_name):
            raise AdoptError("config.yaml adopt.human_owner_name: a plain name")
        section = Path(generate.one_line(a.get("claude_section", "seeds/CLAUDE-section.md"), "adopt.claude_section"))
        if section.is_absolute() or ".." in section.parts:
            raise AdoptError("config.yaml adopt.claude_section: a path inside the policy repo")
        self.section_path = section
        self.skip = set(a.get("skip") or [])
        self.branch = generate.one_line(a.get("branch", "airlock/adopt"), "adopt.branch")

    def values(self) -> dict:
        return {"runner": self.runner, "runner_note": self.runner_note, "ref": self.gen.ref,
                "ref_note": f" # {self.gen.ref_note}" if self.gen.ref_note else "",
                "agent_logins": self.agent_logins, "human_owner_name": self.human_owner_name}

    def workflows(self) -> dict[str, str]:
        v = self.values()
        out = {}
        for name, path in TEMPLATES.items():
            def fill(m, name=name):
                if m.group(1) not in v:
                    raise AdoptError(f"adopt/templates/{name}: unknown placeholder {m.group(0)}")
                return v[m.group(1)]
            out[name] = PLACEHOLDER.sub(fill, path.read_text())
        return out

    def section(self) -> str:
        p = self.root / self.section_path
        if not p.is_file():
            raise AdoptError(f"no {self.section_path} in the policy checkout (config.yaml adopt.claude_section)")
        text = p.read_text().rstrip("\n")
        if not text.startswith(BEGIN) or not text.endswith(END) or text.count(BEGIN) != 1:
            raise AdoptError(f"{self.section_path}: must start with '{BEGIN} ...' and end with '{END}'")
        return text

    def generated(self) -> dict[Path, str]:
        """What the policy produces, and proof the checkout has it committed: adopting from stale files is refused."""
        files = generate.render(self.root, self.gen)
        extra = generate.leftovers(self.root, self.gen, files)
        stale = [p for p, body in files.items() if not p.is_file() or p.read_text() != body] + extra
        if stale:
            raise AdoptError(f"{len(stale)} generated file(s) are stale, e.g. {stale[0].relative_to(self.root)}: run generate --write and merge it first")
        return files

    def codeowners(self, name: str) -> str:
        return self.generated()[self.gen.paths["codeowners"] / f"{name}.CODEOWNERS"]

    def protection(self, name: str) -> str:
        p = self.gen.paths["branch_protection"] / f"{name}.json"
        files = self.generated()
        if p not in files:
            raise AdoptError(f"no repos/{name}.yaml in the policy")
        return files[p]


def splice(claude_md: str | None, section: str) -> str:
    """CLAUDE.md with the section in place of its old one, or appended; the rest of the file as it was."""
    s = claude_md if claude_md is not None else NEW_CLAUDE
    if BEGIN in s:
        return re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), lambda m: section, s, count=1, flags=re.S)
    return s.rstrip("\n") + "\n\n" + section + "\n"


def sh(*args: str, cwd: Path | None = None, check: bool = True, stdin: str | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, input=stdin)
    if check and r.returncode != 0:
        raise AdoptError(f"{' '.join(args[:3])} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def ok(*args: str) -> bool:
    return sh(*args, check=False).returncode == 0


def write_files(d: Path, ad: Adopt) -> None:
    (d / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    for name, body in ad.workflows().items():
        (d / ".github" / "workflows" / name).write_text(body)
    claude = d / "CLAUDE.md"
    claude.write_text(splice(claude.read_text() if claude.exists() else None, ad.section()))


def prettier(d: Path, repo: str) -> None:
    """A repository whose CI runs a formatter rejects the files as copied, so format them with its own prettier."""
    pkg = d / "package.json"
    if not pkg.is_file() or '"prettier"' not in pkg.read_text():
        return
    if not ok("npm", "ci", "--prefix", str(d), "--ignore-scripts", "--no-audit", "--no-fund", "--silent") or sh(
            "npx", "--no-install", "prettier", "--write", "CLAUDE.md", ".github/workflows/autonomy-gate.yml",
            ".github/workflows/claude-review.yml", cwd=d, check=False).returncode != 0:
        print(f"{repo}: npm ci or prettier failed; formatting skipped (its format check will say)", file=sys.stderr)


def open_pr(org: str, name: str, branch: str, title: str, body: str) -> str:
    n = sh("gh", "api", f"repos/{org}/{name}/pulls?head={org}:{branch}&state=open", "-q", ".[0].number").stdout.strip()
    if n and n != "null":
        return n
    return sh("gh", "api", "-X", "POST", f"repos/{org}/{name}/pulls", "-f", f"title={title}", "-f", f"head={branch}",
              "-f", "base=main", "-f", f"body={body}", "-q", ".number").stdout.strip()


def clone(org: str, name: str, d: Path, tries: int = 1, wait: int = 10) -> None:
    for attempt in range(tries):
        if ok("gh", "repo", "clone", f"{org}/{name}", str(d), "--", "-q"):
            return
        shutil.rmtree(d, ignore_errors=True)
        if attempt + 1 < tries:
            time.sleep(wait)
    raise AdoptError(f"{org}/{name}: clone failed")


def commit(d: Path, branch: str, message: str) -> None:
    sh("git", "checkout", "-q", "-B", branch, cwd=d)
    sh("git", "add", "-A", cwd=d)
    sh("git", "commit", "-q", "-m", message, cwd=d)


def rollout(ad: Adopt, org: str, signal: str, repos: list[str]) -> int:
    if not signal.strip():
        raise AdoptError("--signal: what asked for this rollout (an issue, or a person's words and the date); it heads every PR")
    if not repos:
        repos = [p.stem for p in sorted((ad.root / "repos").glob("*.yaml")) if p.stem not in ad.skip]
    ad.workflows(), ad.section()  # every setting checked before the first clone
    title = f"Adopt {ad.gen.repo}'s autonomy files: the gate, Claude review, the CLAUDE.md section"
    body = (f"## Signal\n{signal.strip()}\n\n## What changes\nThis repository's copies of {ad.gen.repo}'s autonomy files, "
            "written by lunareng/airlock-actions `adopt/adopt.py rollout`: `.github/workflows/autonomy-gate.yml` (every PR's "
            "level, and the owner's approval on an agent's L1/L2 PR), `.github/workflows/claude-review.yml` (Claude's "
            "`claude-review` verdict and the advisory `claude-security-review`), and the autonomy section of `CLAUDE.md` "
            f"(from the policy repo's `{ad.section_path}`). No product code changes.\n")
    failed = 0
    work = Path(tempfile.mkdtemp())
    try:
        for name in repos:
            d = work / name
            try:
                clone(org, name, d)
                write_files(d, ad)
                prettier(d, name)
                if not sh("git", "status", "--porcelain", cwd=d).stdout.strip():
                    print(f"{name}: up to date")
                    continue
                commit(d, ad.branch, f"{title}\n\nFrom {ad.gen.repo} with lunareng/airlock-actions adopt/adopt.py rollout.")
                sh("git", "push", "-q", "-f", "origin", ad.branch, cwd=d)
                print(f"{name}: PR #{open_pr(org, name, ad.branch, title, body)}")
            except AdoptError as e:
                failed += 1
                print(f"{name}: {e}", file=sys.stderr)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


def new_repo(ad: Adopt, org: str, name: str, wait: int = 5) -> int:
    policy = ad.root / "repos" / f"{name}.yaml"
    if not policy.is_file():
        raise AdoptError(f"no repos/{name}.yaml: merge the repository's policy first")
    if not re.search(r"^owner_role:", policy.read_text(), re.M):
        raise AdoptError(f"{name}: no owner_role, not a proposed repository")
    branch = sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=ad.root, check=False).stdout.strip()
    if branch != "main":
        raise AdoptError("run from the policy checkout's main (the policy that merged)")
    codeowners, protection = ad.codeowners(name), ad.protection(name)
    workflows, section = ad.workflows(), ad.section()

    if not ok("gh", "api", f"repos/{org}/{name}"):
        print(f"creating {org}/{name}")
        settings = {"name": name, "private": True, "has_wiki": False, "has_projects": False, "allow_merge_commit": False,
                    "allow_squash_merge": False, "allow_rebase_merge": True, "allow_auto_merge": True,
                    "delete_branch_on_merge": True, "auto_init": True}
        sh("gh", "api", "-X", "POST", f"orgs/{org}/repos", "--input", "-", stdin=json.dumps(settings))
    # GitHub writes the auto_init commit a few seconds after the repository exists: wait for main before cloning, or
    # the first PR's branch becomes the default.
    for attempt in range(30):
        if ok("gh", "api", f"repos/{org}/{name}/branches/main"):
            break
        if attempt == 29:
            raise AdoptError(f"{name}: main never appeared")
        time.sleep(wait)

    pr = ""
    # Populated once: not when main has its policy, nor while the first PR's branch is waiting to merge.
    if not ok("gh", "api", f"repos/{org}/{name}/contents/AUTONOMY.yaml?ref=main") and not ok(
            "gh", "api", f"repos/{org}/{name}/branches/{FIRST_BRANCH}"):
        work = Path(tempfile.mkdtemp())
        try:
            d = work / name
            clone(org, name, d, tries=5, wait=wait * 2)
            shutil.copyfile(policy, d / "AUTONOMY.yaml")
            (d / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
            (d / ".github" / "CODEOWNERS").write_text(codeowners)
            for wf, body in workflows.items():
                (d / ".github" / "workflows" / wf).write_text(body)
            # Seeds first, then the section, so a seeded CLAUDE.md keeps its own text and gets the current section.
            seeds = ad.root / "seeds" / name
            if seeds.is_dir():
                shutil.copytree(seeds, d, dirs_exist_ok=True)
            claude = d / "CLAUDE.md"
            claude.write_text(splice(claude.read_text() if claude.exists() else None, section))
            commit(d, FIRST_BRANCH, f"Create {name} under its autonomy policy ({ad.gen.repo} repos/{name}.yaml)")
            sh("git", "push", "-q", "origin", FIRST_BRANCH, cwd=d)
            body = (f"## Signal\n{ad.gen.repo} `repos/{name}.yaml` (merged), created with lunareng/airlock-actions "
                    f"`adopt/adopt.py new-repo`.\n\n## What changes\nThe repository's first commit: `AUTONOMY.yaml`, CODEOWNERS, "
                    "the autonomy gate and Claude review workflows, the autonomy section of CLAUDE.md, and seeds from "
                    f"{ad.gen.repo} `seeds/{name}/` if any.\n")
            pr = open_pr(org, name, FIRST_BRANCH, f"Create {name} under its autonomy policy", body)
            print(f"populated {org}/{name}: PR #{pr}")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    sh("gh", "api", "-X", "PUT", f"repos/{org}/{name}/branches/main/protection", "--input", "-", stdin=protection)
    sh("gh", "api", "-X", "PATCH", f"repos/{org}/{name}", "-F", "allow_auto_merge=true", "-F", "delete_branch_on_merge=true")
    print(f"{org}/{name} is protected")
    pr = pr or sh("gh", "api", f"repos/{org}/{name}/pulls?state=open&head={org}:{FIRST_BRANCH}", "-q", ".[0].number").stdout.strip()
    if pr and pr != "null":
        print(f"next: merge https://github.com/{org}/{name}/pull/{pr} when its checks are green")
    return 0


def protect(ad: Adopt, org: str, name: str, dry_run: bool) -> int:
    body = ad.protection(name)
    if dry_run:
        print(f"PUT /repos/{org}/{name}/branches/main/protection")
        print(body, end="")
        return 0
    sh("gh", "api", "-X", "PUT", f"repos/{org}/{name}/branches/main/protection", "--input", "-", stdin=body)
    print(f"applied: {org}/{name}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    org_name = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
    for cmd in ("rollout", "new-repo", "protect", "render"):
        p = sub.add_parser(cmd)
        p.add_argument("--policy", required=True, help="the policy repo checkout")
        p.add_argument("--repo", help="the policy repo's full name (default: GITHUB_REPOSITORY, then its origin)")
        if cmd != "render":
            p.add_argument("--org", required=True, help="the organization whose repositories these are")
        if cmd == "rollout":
            p.add_argument("--signal", required=True, help="what asked for this rollout; it heads every PR")
            p.add_argument("repos", nargs="*")
        elif cmd == "render":
            p.add_argument("--out", required=True)
        else:
            p.add_argument("name")
        if cmd == "protect":
            p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        if getattr(args, "org", None) is not None and not org_name.match(args.org):
            raise AdoptError(f"--org {args.org!r} is not an organization name")
        for n in [*getattr(args, "repos", []), *([args.name] if hasattr(args, "name") else [])]:
            if not generate.NAME.match(n):
                raise AdoptError(f"{n!r} is not a repository name")
        ad = Adopt(Path(args.policy), args.repo)
        if args.cmd == "rollout":
            return rollout(ad, args.org, args.signal, args.repos)
        if args.cmd == "new-repo":
            return new_repo(ad, args.org, args.name)
        if args.cmd == "protect":
            return protect(ad, args.org, args.name, args.dry_run)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for name, body in ad.workflows().items():
            (out / name).write_text(body)
        print(f"wrote {len(TEMPLATES)} workflows to {out}")
        return 0
    except AdoptError as e:
        print(f"adopt: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
