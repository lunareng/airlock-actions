#!/usr/bin/env python3
"""The signal router (charter step 5, first slice): signals become issues an agent can work from cold.

One pass for one tenant: the organization is the policy repo's owner (GITHUB_REPOSITORY, or --policy-repo), the policy
repo is the checkout (GITHUB_WORKSPACE, or --policy-dir), and where each rule applies is the policy repo's router.yaml
(README.md beside this file documents every key). Nothing here names a tenant. The router role's workflow runs it
(role-<role>.yml, on the token Airlock mints from roles/<role>/role.yaml). It writes nothing but issues, labels and
comments, and it is idempotent: every rule checks GitHub before it writes.

Rules (router.yaml says where each applies):
  product_bug   An issue the sim triage filed from a sim incident (label `product-bug`, or a body that links an
                issue in one of router.yaml's product_bugs.sim_repos) with no `## Signal` section gets one appended:
                the incident link and a severity derived from the incident signature. Labels: type:bug,
                product:<the sim's label in sim_repos>, severity:<derived> when missing. Priority stays the groomer's.
  tool_request  A `tool-request` issue with no `## Signal` gets one naming the requesting agent (the issue's
                author and any board post id in the body). Labels: type:feature, and router.yaml's
                tool_request_label (a product label) when the issue has none.
  review        A review on an agent's open PR (a `CHANGES_REQUESTED` review, or a review comment or issue
                comment from a person, newer than the PR's head commit) starts the owning role with the PR as
                its signal, once per newest comment (a marker comment records which). Without it a review
                waited for the role's next scheduled run (2026-10-21). Approvals don't dispatch:
                the role merges green L3/L4 PRs at its next run, and the human owner merges L2.
  resume        A `tool-request` the router started a role on, which the role took but couldn't start for its PR
                budget, starts the role again once it has a free slot. All of: no open PR names the request;
                no PR naming it was closed unmerged (work the owner turned down isn't restarted) or merged with
                `Closes`/`Fixes`/`Resolves` since the router last started the role on it (one merged as `Part of`
                only frees its slot, autonomy#151); the role has fewer open PRs than its budget (`pr_budget`, counted across
                every repo its `writes.repos` lists); one of its PRs closed since that start; and since then the
                role either said nothing on the request or its newest comment there carries its
                `<!-- <role>:waits -->` marker (a role that asked a question is left alone; a role on Airlock
                comments under the App every Airlock role shares, so only its `<!-- <role>:` marker makes a
                comment there its own). Oldest request
                first, one start per role per pass, never while a run of that role is queued or running; a
                marker comment records each start. Without it a deferred request waited for the role's next
                scheduled run, days away (autonomy#130 waited from 15:31 to Wednesday; 2026-10-26).
  digest_answer The human owner's comment (config.yaml human_owner) on an open `digest` issue in the policy repo
                that answers proposals ("1 yes, 2 no") starts router.yaml's digest_answers.role with that comment as
                its signal, once per comment (a marker comment records which). That role writes each answer into
                a decisions log.
  ci_main       A failing workflow on a repo's main (latest completed run) gets one open issue per workflow,
                `CI: <workflow> failing on main`, labelled ci-failure + type:bug. When the latest run of that
                workflow on main is green again, the router closes its own issue with a comment. Decided
                2026-10-20: these are the only issues the router closes.
  delivered     A PR merged in the last 48 hours (router.yaml delivered.backfill: back to a date until another) in a tool_requests repo that
                links an open `tool-request` under `## Signal`: `Closes`/`Fixes`/`Resolves` means GitHub closes it,
                nothing to do; `Part of` gets one comment, "Part delivered by <PR>"; no keyword gets one comment
                asking the owning role to close it or say what is left, and the label `delivered?`. A request an
                open PR also names is left alone. It never closes and never reopens (decided 2026-10-25, autonomy#108).
                2026-10-20: these and the run-failure issues below are the only issues the router closes.
  run_failed    The latest completed run of each role workflow (.github/workflows/role-*.yml) that failed or timed
                out gets one open issue per role, `Role run: <role> failing`, labelled run-failure + type:bug +
                severity:high, naming the failing job and step, what started the run, and its artifacts. Another
                failure while it is open adds one comment (marker per run id); the third in a row mentions the
                human owner, once. The role's next success closes the router's own issue with a comment
                (decided 2026-10-25, autonomy#107). cancelled and skipped runs are ignored: the per-role
                concurrency group cancels queued runs by design.
  override      A PR merged in the last 48 hours, in a repo that requires `claude-review` (config.yaml
                required_checks), whose verdict on the merged head is FAIL (claude[bot]'s comment for that head,
                last line `Verdict: FAIL (<sha>)`, read as the review action's verdict step reads it) gets one comment
                mentioning whoever merged it, asking for `Override: <one line>`; the digest lists the reasons.
                Skips the deployer's PRs (router.yaml deployer_login) and PRs with no verdict on the merged head. It never blocks, reverts or
                relabels a PR, and never judges the reason (autonomy#109).
  gate_rerun    A commit on the policy repo's main in the last `window_days` that changes what the gate scores by
                (repos/<repo>.yaml: that repo; roles/<role>/: the repos the role writes or owns; config.yaml:
                every repo) re-runs the `autonomy-gate` check of each open PR there whose newest gate run on its head
                started before that commit, so a tightening reaches a PR that is already green (autonomy#160). It
                re-runs the PR's latest pull_request run, at most `max_per_pass` a pass (the rest wait for the next);
                a PR with no gate run on its head is listed; a refused re-run is listed with its error and fails
                the pass once every rule has run. A re-run's start time is its marker: the next pass writes nothing.
                The policy repo's gate-rerun workflow starts a pass on every such push, so it needn't wait for the clock.

Report: a JSON summary, including routed_within_1h (share of signals that had their issue within an hour of
the signal), the router's own judge number.

  router.py [--dry-run] [--policy-dir <checkout>] [--policy-repo <owner/name>] [--config <policy>/router.yaml] [--role router]

Before a pass it checks router.yaml against the policy repo (config_problems) and routes nothing if any check fails.
"""
import argparse
import functools
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SIGNAL_RE = re.compile(r"^## Signal\s*$", re.M)
SIGNATURE_RE = re.compile(r"<!--\s*sim-incident:signature=([^\s]+?)\s*-->")
BOARD_POST_RE = re.compile(r"\bmsg_[0-9a-f]{4,}\b")
SEVERITY_BY_PREFIX = {"invariant": "high", "expectation_unmet": "high", "tool_error": "medium", "experience": "medium"}
REVIEW_MARK = "<!-- router:review-dispatch id={id} -->"
REVIEW_MARK_RE = re.compile(r"<!-- router:review-dispatch id=(\d+) -->")
DIGEST_MARK = "<!-- router:digest-answer id={id} -->"
DIGEST_MARK_RE = re.compile(r"<!-- router:digest-answer id=(\d+) -->")
DELIVERED_MARK = "<!-- router:delivered pr={pr} -->"
RESUME_MARK = "<!-- router:resume role={role} -->"
DISPATCH_MARK = "<!-- router:dispatch role={role} -->"
WAITS_MARK = "<!-- {role}:waits"  # a role's comment on a request it took but can't start for its PR budget
SECTION_END_RE = re.compile(r"^## ", re.M)
RUN_FAILED_MARK = "<!-- router:run-failed run={id} -->"
RUN_FAILED_MARK_RE = re.compile(r"<!-- router:run-failed run=(\d+) -->")
OVERRIDE_MARK = "<!-- router:override pr={pr} -->"
OVERRIDE_REASON_RE = re.compile(r"^\s*Override:\s*(\S.*?)\s*$", re.M | re.I)
REVIEWER_LOGIN = "claude[bot]"  # the review action's verdicts (lunareng/airlock-actions review) come from Claude's App
ANSWER_RE = re.compile(r"\b(\d+|all)\s*[:.)-]?\s*(yes|no|y|n)\b", re.I)
GATE_WORKFLOW = ".github/workflows/autonomy-gate.yml"
POLICY_FILE_RE = re.compile(r"repos/([A-Za-z0-9._-]+)\.yaml")
ROLE_PATH_RE = re.compile(r"roles/([a-z][a-z0-9-]*)/")

# ---------- the tenant ----------
# Who this pass is for. configure() sets every one of these from the policy repo; nothing here names a tenant.
TOP = Path(os.environ.get("GITHUB_WORKSPACE") or ".").resolve()  # the policy repo's checkout
POLICY_REPO = os.environ.get("GITHUB_REPOSITORY", "")             # owner/name of the policy repo
ORG = POLICY_REPO.split("/", 1)[0]                                 # the tenant's GitHub organization
ROUTER_ROLE = "router"                                             # the role whose workflow runs the router
REPOS, ROLES, CONFIG = TOP / "repos", TOP / "roles", TOP / "config.yaml"
ROOT_CONFIG = CONFIG
FOOTER = ""
ROUTER_LOGIN: str | None = None    # the router role's own App (its role.yaml identity.github_login), if it has one
CLOCK_LOGIN: str | None = None     # router.yaml clock_login: the App whose dispatches are scheduled runs
DEPLOYER_LOGIN: str | None = None  # router.yaml deployer_login: the deploy bot whose PRs the override rule skips
REPO_RE = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")


def configure(top: Path | str, policy_repo: str, role: str = "router", cfg: dict | None = None) -> None:
    """Point the router at one tenant: its policy checkout `top`, its policy repo `owner/name`, the router's role
    name, and router.yaml's logins. Fails closed on a policy repo that isn't owner/name."""
    global TOP, POLICY_REPO, ORG, ROUTER_ROLE, REPOS, ROLES, CONFIG, ROOT_CONFIG, FOOTER, ROUTER_LOGIN, CLOCK_LOGIN, DEPLOYER_LOGIN
    if not REPO_RE.fullmatch(policy_repo or ""):
        raise SystemExit(f"the policy repo must be owner/name (GITHUB_REPOSITORY or --policy-repo), not {policy_repo!r}")
    cfg = cfg or {}
    TOP = Path(top).resolve()
    POLICY_REPO, ORG, ROUTER_ROLE = policy_repo, policy_repo.split("/", 1)[0], role
    REPOS, ROLES, CONFIG = TOP / "repos", TOP / "roles", TOP / "config.yaml"
    ROOT_CONFIG = CONFIG
    FOOTER = f"\n\n_Routed by {POLICY_REPO} (lunareng/airlock-actions router)._"
    ROUTER_LOGIN = (role_file(role).get("identity") or {}).get("github_login")
    CLOCK_LOGIN = cfg.get("clock_login") or None
    DEPLOYER_LOGIN = cfg.get("deployer_login") or None
    router_logins.cache_clear()


def owner_login(config: Path | None = None) -> str:
    """The human owner's GitHub login (config.yaml human_owner, '@<login>')."""
    return str((yaml.safe_load((config or CONFIG).read_text()) or {}).get("human_owner", "")).lstrip("@")


def digest_answer_plan(comments: list[dict], owner: str) -> dict | None:
    """The newest comment by `owner` on a digest issue that answers proposals and has no dispatch yet.

    An answer is a number (or "all") followed by yes or no. Comments by anyone else, including agents and
    the router itself, never dispatch. Only the newest unanswered comment is dispatched; the role it starts reads the
    whole thread, so earlier ones it missed are in its view too."""
    done = {int(m) for c in comments for m in DIGEST_MARK_RE.findall(c.get("body") or "")}
    for c in sorted(comments, key=lambda c: c.get("created_at", ""), reverse=True):
        if (c.get("user") or {}).get("login") != owner:
            continue
        if c["id"] in done:
            return None
        if ANSWER_RE.search(c.get("body") or ""):
            return {"id": c["id"], "signal": c.get("html_url", ""), "at": c["created_at"]}
    return None


def agent_logins(config: Path | None = None, roles_dir: Path | None = None) -> set[str]:
    """Every login an agent opens PRs under: config.yaml's agent_markers.logins, every role's identity.github_login,
    and Airlock's App login once any role is on Airlock (the same set lunareng/airlock-actions' gate builds). The
    PRs the review rule watches."""
    cfg = yaml.safe_load((config or CONFIG).read_text()) or {}
    out = set((cfg.get("agent_markers") or {}).get("logins") or [])
    on_airlock = False
    for f in sorted((roles_dir or ROLES).glob("*/role.yaml")):
        ident = (yaml.safe_load(f.read_text()) or {}).get("identity") or {}
        if ident.get("github_login"):
            out.add(ident["github_login"])
        on_airlock |= ident.get("key_store") == "airlock"
    app = (cfg.get("airlock") or {}).get("app_login")
    if on_airlock and app:
        out.add(app)
    return out


def review_plan(pr: dict, head_at: str, reviews: list[dict], comments: list[dict], issue_comments: list[dict], logins: set[str]) -> dict | None:
    """Decide whether a person's review on an agent's PR needs the role woken, and which comment it is.
    `comments` are review (inline) comments, `issue_comments` the PR's conversation. A person is anyone
    whose login is not a role's; a bot's own replies and the router's never count. The newest qualifying
    item's id is the marker; a marker already in the conversation for that id means it was dispatched."""
    author = (pr.get("user") or {}).get("login", "")
    if author not in logins:
        return None
    since = ts(head_at)
    dispatched = {int(m) for c in issue_comments for m in REVIEW_MARK_RE.findall(c.get("body") or "")}
    items = []
    for r in reviews:
        who = (r.get("user") or {}).get("login", "")
        if who in logins or who.endswith("[bot]") or not r.get("submitted_at"):
            continue
        if r.get("state") == "CHANGES_REQUESTED" and ts(r["submitted_at"]) > since:
            items.append((ts(r["submitted_at"]), r["id"], "changes requested"))
    for c in comments:
        who = (c.get("user") or {}).get("login", "")
        if who in logins or who.endswith("[bot]"):
            continue
        if ts(c["created_at"]) > since:
            items.append((ts(c["created_at"]), c["id"], "review comment"))
    for c in issue_comments:
        who = (c.get("user") or {}).get("login", "")
        if who in logins or who.endswith("[bot]") or REVIEW_MARK_RE.search(c.get("body") or ""):
            continue
        if ts(c["created_at"]) > since and "@" + author.removesuffix("[bot]") in (c.get("body") or ""):
            items.append((ts(c["created_at"]), c["id"], "mention"))
    if not items:
        return None
    when, newest, kind = max(items)
    if newest in dispatched:
        return None
    return {"id": newest, "kind": kind, "signal": pr.get("html_url", ""), "at": when.isoformat(timespec="seconds")}


def owner_role(repo: str, repos_dir: Path | None = None) -> str | None:
    """The agent role that works `repo` (repos/<name>.yaml owner_role). A tool-request there is its signal,
    so the router starts its workflow with the issue as the signal instead of leaving it for the next
    scheduled run (2026-10-21: a Claude Code session that needs a change in another repo files the request,
    and the system does the rest). No owner: the request waits for a person."""
    f = (repos_dir or REPOS) / (repo.split("/", 1)[1] + ".yaml")
    if not f.exists():
        return None
    return (yaml.safe_load(f.read_text()) or {}).get("owner_role")


def dispatch_plan(repo: str, issue: dict, repos_dir: Path | None = None) -> dict | None:
    role = owner_role(repo, repos_dir)
    if not role:
        return None
    return {"workflow": f"role-{role}.yml", "inputs": {"signal": issue.get("html_url", "")}, "role": role}


def role_file(role: str, roles_dir: Path | None = None) -> dict:
    f = (roles_dir or ROLES) / role / "role.yaml"
    return (yaml.safe_load(f.read_text()) or {}) if f.exists() else {}


def role_logins(role: str, roles_dir: Path | None = None, config: Path | None = None) -> set[str]:
    """The logins a role writes under: its own App (identity.github_login), and for a role on Airlock
    (identity.key_store: airlock) the App every Airlock role shares (config.yaml airlock.app_login)."""
    ident = role_file(role, roles_dir).get("identity") or {}
    out = {ident["github_login"]} if ident.get("github_login") else set()
    if ident.get("key_store") == "airlock":
        shared = airlock_login(config)
        if shared:
            out.add(shared)
    return out


def role_repos(role: str, roles_dir: Path | None = None, skip: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """Every repo the role writes to (writes.repos): its PR budget counts across all of them. `skip` is this
    pass's repos the App can't read (a role may write a repo the router's App isn't installed on yet)."""
    return [r for r in (role_file(role, roles_dir).get("writes") or {}).get("repos") or [] if r not in skip]


def budget_for(role: str, cfg_budget) -> int:
    """`pr_budget` is a number, or {default: N, <role>: M} for roles whose brief allows a different count."""
    if isinstance(cfg_budget, dict):
        return int(cfg_budget.get(role, cfg_budget.get("default", 2)))
    return int(cfg_budget or 2)


def role_open_count(role: str, own_login: str | None, repos: list[str], prs_by_repo: dict[str, list[dict]]) -> int:
    """The role's open PRs across every repo it writes to: its budget is per role, not per repo."""
    return sum(is_roles_pr(p, role, own_login) for r in repos for p in prs_by_repo.get(r, []))


def airlock_login(config: Path | None = None) -> str | None:
    """The login every role on Airlock shares (config.yaml airlock.app_login)."""
    return ((yaml.safe_load((config or CONFIG).read_text()) or {}).get("airlock") or {}).get("app_login")


@functools.lru_cache(maxsize=1)
def router_logins() -> frozenset[str]:
    """The router's own logins: its role's own App (roles/<role>/role.yaml identity.github_login), and the App every
    Airlock role shares (config.yaml airlock.app_login): its older comments and issues may be under either."""
    return frozenset({(role_file(ROUTER_ROLE).get("identity") or {}).get("github_login"), airlock_login()} - {None})


def is_roles_pr(pr: dict, role: str, own_login: str | None, shared: str | None = None) -> bool:
    """A role's PR: opened under its own App login, or opened by the App every Airlock role shares on the role's
    `agent/<role>/` branch in the repo itself. A branch name alone is a marker anyone can pick, a fork's PR
    included, so it never counts without the shared author and a same-repo head (autonomy#162 review)."""
    author = (pr.get("user") or {}).get("login")
    if own_login and author == own_login:
        return True
    head = pr.get("head") or {}
    if not (head.get("ref") or "").startswith(f"agent/{role}/"):
        return False
    shared = shared if shared is not None else airlock_login()
    head_repo = (head.get("repo") or {}).get("full_name")
    base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
    same_repo = not (head_repo and base_repo) or head_repo == base_repo
    return bool(shared and author == shared and same_repo)


def last_start(comments: list[dict], role: str) -> datetime | None:
    """When the router last started `role` on this request: its dispatch comment (marker, or the text it
    wrote before the marker existed) or a resume comment."""
    marks = (DISPATCH_MARK.format(role=role), RESUME_MARK.format(role=role), f"Dispatched to the `{role}` role")
    times = [ts(c["created_at"]) for c in comments
             if (c.get("user") or {}).get("login") in router_logins() and any(m in (c.get("body") or "") for m in marks)]
    return max(times) if times else None


def waits_for_slot(comments: list[dict], role: str, logins: set[str], since: datetime, shared: str | None = None) -> bool:
    """Since `since`, the role said nothing on the request, or its newest comment there carries its waits
    marker. A role comment is one under its own App login, or one carrying a `<!-- <role>:` marker; anyone
    else's comment (the owner's "approved", say) doesn't change what the role is waiting for. The login every
    Airlock role shares says nothing by itself: another Airlock role's comment there isn't this role's
    (autonomy#162 review), so a comment under it counts only with the role's marker."""
    tag = f"<!-- {role}:"
    shared = shared if shared is not None else airlock_login()
    own = logins - {shared}
    mine = [c for c in comments if ts(c["created_at"]) > since
            and ((c.get("user") or {}).get("login") in own or tag in (c.get("body") or ""))]
    if not mine:
        return True
    newest = max(mine, key=lambda c: ts(c["created_at"]))
    return WAITS_MARK.format(role=role) in (newest.get("body") or "")


def holds_request(pr: dict, request_key: tuple[str, int]) -> bool:
    """A closed PR naming the request under `## Signal` holds it from the resume rule when it was closed without
    merging (turned down: a person's call) or merged with `Closes`/`Fixes`/`Resolves` (delivered). One that merged
    as `Part of` (or naming it without a keyword) only frees its slot: the request keeps resuming until it closes
    (autonomy#151: #140 merged `Part of #139` and #139 never resumed)."""
    kind = signal_refs(pr.get("body"), pr.get("repo") or request_key[0]).get(request_key)
    if kind is None:
        return False
    return not pr.get("merged_at") or kind == "close"


def resume_plan(role: str, logins: set[str], own_login: str | None, issue: dict, comments: list[dict],
                open_count: int, budget: int, closed_prs: list[dict], pending: bool, request_key: tuple[str, int]) -> dict | None:
    """Whether to start `role` again on a request it deferred for its PR budget (the resume rule). `open_count`
    is the role's open PRs across its repos; `closed_prs` are recently closed PRs across those repos, each
    with its "repo" (so a PR in airlock that names autonomy#N is seen, and a close in any of them frees a slot).
    Returns the signal and the PRs whose closing freed the slot, or None."""
    if "pull_request" in issue or issue.get("state") != "open" or "tool-request" not in labels_of(issue) or pending:
        return None
    if open_count >= budget:
        return None
    since = last_start(comments, role)
    if since is None or not waits_for_slot(comments, role, logins, since):
        return None
    after = [p for p in closed_prs if p.get("closed_at") and ts(p["closed_at"]) > since]
    if any(holds_request(p, request_key) for p in after):
        return None  # a PR for this request was turned down, or delivered it, since the start: a person's call
    here = request_key[0]
    freed = [f"#{p['number']}" if (p.get("repo") or here) == here else f"{p['repo']}#{p['number']}"
             for p in sorted(after, key=lambda p: ((p.get("repo") or here) != here, p.get("repo") or here, p["number"]))
             if is_roles_pr(p, role, own_login)]
    if not freed:
        return None
    return {"signal": issue.get("html_url", ""), "freed": freed}


# ---------- GitHub ----------

class GitHub:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.writes: list[str] = []

    def get_or_none(self, path: str):
        """One object, or None when GitHub answers 404 (the thing doesn't exist). Anything else fails as get() does."""
        out = subprocess.run(["gh", "api", path], capture_output=True, text=True)
        if out.returncode != 0 and "HTTP 404" in out.stderr:
            return None
        return self.get(path)

    def get(self, path: str, max_pages: int | None = None, **params):
        items, page = [], 1
        while True:
            q = "&".join(f"{k}={v}" for k, v in {**params, "page": page}.items())
            for attempt in range(3):
                out = subprocess.run(["gh", "api", f"{path}?{q}"], capture_output=True, text=True)
                if out.returncode == 0:
                    break
                time.sleep(2 * (attempt + 1))
            else:
                raise SystemExit(f"gh api {path}: {out.stderr.strip()}")
            data = json.loads(out.stdout)
            if not isinstance(data, list):
                return data
            items += data
            if len(data) < int(params.get("per_page", 30)) or (max_pages and page >= max_pages):
                return items
            page += 1

    def can_read(self, repo: str) -> bool:
        """Whether the router's App can read the repo. False only on a 404 (the App isn't installed there);
        any other failure still stops the pass, so an outage never reads as "skip everything"."""
        for attempt in range(3):
            out = subprocess.run(["gh", "api", f"repos/{repo}"], capture_output=True, text=True)
            if out.returncode == 0:
                return True
            if "HTTP 404" in out.stderr:
                return False
            time.sleep(2 * (attempt + 1))
        raise SystemExit(f"gh api repos/{repo}: {out.stderr.strip()}")

    def write(self, method: str, path: str, body: dict) -> dict:
        self.writes.append(f"{method} {path} {json.dumps(body)[:120]}")
        if self.dry_run:
            return {}
        out = subprocess.run(["gh", "api", "-X", method, path, "--input", "-"], input=json.dumps(body), capture_output=True, text=True)
        if out.returncode != 0:
            raise SystemExit(f"gh api {method} {path}: {out.stderr.strip()}")
        return json.loads(out.stdout) if out.stdout.strip() else {}

    def attempt(self, method: str, path: str, body: dict) -> str | None:
        """A write whose refusal is reported, not fatal: None when it was made, else gh's error."""
        self.writes.append(f"{method} {path} {json.dumps(body)[:120]}")
        if self.dry_run:
            return None
        out = subprocess.run(["gh", "api", "-X", method, path, "--input", "-"], input=json.dumps(body), capture_output=True, text=True)
        return None if out.returncode == 0 else (out.stderr.strip().splitlines() or ["no error text"])[-1]


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def product_bugs(cfg: dict) -> dict:
    """router.yaml product_bugs: {repos: [...], sim_repos: {<sim repo name>: <product label>}}."""
    pb = cfg.get("product_bugs") or {}
    return pb if isinstance(pb, dict) else {}


def config_repos(cfg: dict) -> list[str]:
    """Every repo a rule reads issues or PRs in, from the config's own lists."""
    repos = [*product_bugs(cfg).get("repos", []), *cfg.get("tool_requests", []), *cfg.get("reviews", []),
             *(cfg.get("ci_main") or {}).keys(), *cfg.get("run_failed", []), *(cfg.get("gate_rerun") or {}).get("repos", [])]
    return sorted(set(repos))


def without_repos(cfg: dict, skip: set[str]) -> dict:
    """The config with the repos the App can't read taken out of every rule, so one repo added before the App
    was installed there costs only that repo's rules, not the whole pass."""
    out = dict(cfg)
    for key in ("tool_requests", "reviews", "run_failed"):
        if key in out:
            out[key] = [r for r in out[key] if r not in skip]
    if product_bugs(out).get("repos"):
        out["product_bugs"] = {**out["product_bugs"], "repos": [r for r in out["product_bugs"]["repos"] if r not in skip]}
    if out.get("ci_main"):
        out["ci_main"] = {r: v for r, v in out["ci_main"].items() if r not in skip}
    if (out.get("gate_rerun") or {}).get("repos"):
        out["gate_rerun"] = {**out["gate_rerun"], "repos": [r for r in out["gate_rerun"]["repos"] if r not in skip]}
    return out


def config_problems(cfg: dict) -> list[str]:
    """What's wrong with router.yaml against the policy repo, each as one line; a pass routes nothing while any is
    listed (these were test_router.py's checks on the policy repo's own files)."""
    out = []
    known = {"product_bugs", "tool_requests", "tool_request_label", "reviews", "delivered", "resume", "pr_budget", "digest_answers",
             "override", "deployer_login", "clock_login", "gate_rerun", "run_failed", "ci_main"}
    out += [f"router.yaml: unknown key `{k}`" for k in sorted(set(cfg) - known)]
    if "product_bugs" in cfg and not isinstance(cfg["product_bugs"], dict):
        out.append("router.yaml: product_bugs is {repos: [...], sim_repos: {<sim repo>: <product label>}}, not a list")
    probed = config_repos(cfg)
    out += [f"router.yaml: {r} is not a repository of {ORG}" for r in probed if not (REPO_RE.fullmatch(str(r)) and str(r).split("/", 1)[0] == ORG)]
    for f in sorted(ROLES.glob("*/role.yaml")):
        writes = set(((yaml.safe_load(f.read_text()) or {}).get("writes") or {}).get("repos") or [])
        missing = sorted(writes - set(probed))
        if missing:
            out.append(f"{f.relative_to(TOP)}: {', '.join(missing)} is in no router.yaml list, so it is never probed and a missing install there ends the pass")
    da = cfg.get("digest_answers")
    if da and not (isinstance(da, dict) and da.get("role")):
        out.append("router.yaml: digest_answers names the role its answers start: {role: <role>}")
    gr = (cfg.get("gate_rerun") or {}).get("repos") or []
    if gr:
        router = role_file(ROUTER_ROLE)
        if (router.get("writes") or {}).get("dispatch") is not True:
            out.append(f"roles/{ROUTER_ROLE}/role.yaml: gate_rerun re-runs need the Actions write that writes.dispatch: true grants")
        extra = sorted(set(gr) - set((router.get("writes") or {}).get("repos") or []))
        if extra:
            out.append(f"router.yaml: gate_rerun repos {', '.join(extra)} are not in roles/{ROUTER_ROLE}/role.yaml writes.repos, so its token can't re-run there")
        out += [f"router.yaml: gate_rerun repo {r} has no repos/{r.split('/', 1)[-1]}.yaml" for r in gr
                if not (REPOS / f"{r.split('/', 1)[-1]}.yaml").is_file()]
    return out


def labels_of(issue: dict) -> set[str]:
    return {l["name"] for l in issue.get("labels", [])}


def has_signal(body: str | None) -> bool:
    return bool(SIGNAL_RE.search(body or ""))


# ---------- pure decisions (tested in test_router.py) ----------

@functools.lru_cache(maxsize=None)
def sim_link_re(org: str, sims: tuple[str, ...]) -> re.Pattern:
    """A link to an issue in one of the tenant's sim repos (router.yaml product_bugs.sim_repos)."""
    names = "|".join(re.escape(s) for s in sims) or r"(?!)"
    return re.compile(rf"https://github\.com/{re.escape(org)}/({names})/issues/(\d+)")


def sim_link(body: str, sim_repos: dict[str, str], org: str | None = None):
    return sim_link_re(org or ORG, tuple(sim_repos)).search(body or "")


def product_bug_plan(issue: dict, incident: dict | None, sim_repos: dict[str, str], org: str | None = None) -> dict | None:
    """What to do with a candidate product-bug issue. None = nothing (already routed, or not a sim bug).
    `sim_repos` is router.yaml's product_bugs.sim_repos: each sim repo's name and the product label its bugs carry."""
    body = issue.get("body") or ""
    labels = labels_of(issue)
    link = sim_link(body, sim_repos, org)
    if has_signal(body) or ("product-bug" not in labels and not link):
        return None
    sim, number = (link.group(1), int(link.group(2))) if link else (None, None)
    signature = SIGNATURE_RE.search((incident or {}).get("body") or "") if incident else None
    sig = signature.group(1) if signature else ""
    prefix = sig.split("%3A")[0].split(":")[0] if sig else ""
    severity = SEVERITY_BY_PREFIX.get(prefix, "medium")
    lines = ["## Signal"]
    if link:
        lines.append(f"Sim incident {sim}#{number} ({link.group(0)})")
    else:
        lines.append("Filed by sim triage from a sim incident (label product-bug); the incident link is in the body above.")
    if sig:
        lines.append(f"Signature: `{sig.replace('%3A', ':')}`")
    add = {"type:bug"}
    if sim and not any(l.startswith("product:") for l in labels):
        add.add(sim_repos[sim])
    if not any(l.startswith("severity:") for l in labels):
        add.add(f"severity:{severity}")
    return {"append": "\n\n" + "\n".join(lines) + FOOTER, "labels": sorted(labels | add | {"routed"})}


def tool_request_plan(issue: dict, product_label: str | None = None) -> dict | None:
    """`product_label` is router.yaml's tool_request_label: the product label a request with none gets."""
    body = issue.get("body") or ""
    labels = labels_of(issue)
    if "tool-request" not in labels or has_signal(body):
        return None
    posts = sorted(set(BOARD_POST_RE.findall(body)))
    author = (issue.get("user") or {}).get("login", "unknown")
    lines = ["## Signal", f"tool-request from `{author}` ({issue.get('html_url', '')})"]
    if posts:
        lines.append("Board posts: " + ", ".join(f"`{p}`" for p in posts))
    add = {"type:feature"} if not any(l.startswith("type:") for l in labels) else set()
    if product_label and not any(l.startswith("product:") for l in labels):
        add.add(product_label)
    return {"append": "\n\n" + "\n".join(lines) + FOOTER, "labels": sorted(labels | add | {"routed"})}


def ci_title(workflow: str) -> str:
    return f"CI: {workflow} failing on main"


def ci_plan(latest_runs: dict[str, dict], open_issues: list[dict], product_label: str) -> dict:
    """latest_runs: workflow name -> its latest completed run on main. Returns {"open": [...], "close": [...]}."""
    by_title = {i["title"]: i for i in open_issues if "ci-failure" in labels_of(i)}
    plan = {"open": [], "close": []}
    for wf, run in latest_runs.items():
        title = ci_title(wf)
        if run["conclusion"] == "failure" and title not in by_title:
            plan["open"].append({
                "title": title,
                "labels": ["ci-failure", "type:bug", product_label, "severity:high"],
                "body": (
                    f"## Outcome\n`{wf}` is green on main.\n\n## Signal\nFailing run: {run['html_url']} (head `{run['head_sha'][:7]}`, {run['updated_at']})\n\n"
                    f"## Evidence\nThe run's logs. The router files this once per workflow and closes it when the latest run of `{wf}` on main succeeds.\n\n"
                    f"## Done means\n- [ ] The latest `{wf}` run on main is green (the router closes this issue then).\n\n"
                    f"## Out of scope\nAnything the failing step doesn't need. Never weaken the check to make it pass.\n\n"
                    f"## Session rules\n- Keep going when a step doesn't need the human owner.\n- Stop before anything the repo's AUTONOMY.yaml puts at L1 or L2.\n"
                    f"- Finish by opening a PR whose body names this issue as its signal.{FOOTER}"
                ),
                "signal_at": run["updated_at"],
            })
        elif run["conclusion"] == "success" and title in by_title:
            plan["close"].append({"number": by_title[title]["number"], "comment": f"Closed by the router: the latest `{wf}` run on main succeeded ({run['html_url']})."})
    return plan


@functools.lru_cache(maxsize=None)
def ref_re(org: str) -> re.Pattern:
    """An issue link in `org`: its URL, `<org>/<repo>#N`, `<repo>#N` or a bare `#N`, with any closing keyword before it."""
    o = re.escape(org)
    return re.compile(r"(?:\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?|part of)\b\s*:?\s*)?"
                      rf"(?:https://github\.com/({o}/[\w.-]+)/issues/(\d+)|(?<![\w/])(?:{o}/)?([A-Za-z][\w.-]*)?#(\d+))", re.I)


def signal_refs(body: str | None, repo: str) -> dict[tuple[str, int], str]:
    """Issues linked under `## Signal`: (repo, number) -> "close" (Closes/Fixes/Resolves), "part" (Part of) or
    "named". A bare #N is in `repo`; `<name>#N` is in `repo`'s organization, the only one a link can name. The
    strongest keyword wins when one issue is linked twice."""
    org = repo.split("/", 1)[0]
    body = body or ""
    m = SIGNAL_RE.search(body)
    if not m:
        return {}
    rest = body[m.end():]
    end = SECTION_END_RE.search(rest)
    rank = {"named": 0, "part": 1, "close": 2}
    refs: dict[tuple[str, int], str] = {}
    for kw, url_repo, url_n, short_repo, short_n in ref_re(org).findall(rest[:end.start()] if end else rest):
        key = (url_repo or (f"{org}/{short_repo}" if short_repo else repo), int(url_n or short_n))
        kind = "named" if not kw else "part" if kw.lower() == "part of" else "close"
        if rank[kind] > rank[refs.get(key, "named")] or key not in refs:
            refs[key] = kind
    return refs


def delivered_plan(pr: dict, pr_repo: str, request: dict, comments: list[dict], pending: bool, role: str | None) -> dict | None:
    """A merged PR and one issue its `## Signal` links. Returns a comment (and for no keyword, the `delivered?`
    label) or None. Closes means GitHub closes it: nothing to do. It never closes and never reopens; a request
    that an open PR also names is left alone; one comment per merged PR (marker)."""
    if "pull_request" in request or request.get("state") != "open" or "tool-request" not in labels_of(request) or pending:
        return None
    key = (request["repository_url"].split("/repos/", 1)[1], request["number"]) if request.get("repository_url") else None
    kind = signal_refs(pr.get("body"), pr_repo).get(key)
    if kind in (None, "close"):
        return None
    mark = DELIVERED_MARK.format(pr=f"{pr_repo}#{pr['number']}")
    if any(mark in (c.get("body") or "") for c in comments):
        return None
    if kind == "part":
        return {"comment": f"Part delivered by {pr['html_url']} (merged {pr['merged_at'][:10]}). This request stays open for the rest.\n{mark}{FOOTER}"}
    who = f"the `{role}` role" if role else "whoever works it"
    org = pr_repo.split("/", 1)[0]
    return {"comment": (f"{pr['html_url']} (merged {pr['merged_at'][:10]}) names this request under `## Signal` without `Closes` or `Part of`. "
                        f"If it delivered the request, {who} closes it here with a link; if not, say here what is left. "
                        f"From now on a PR writes `Closes {org}/<repo>#N` or `Part of {org}/<repo>#N` for a request it answers.\n{mark}{FOOTER}"),
            "label": "delivered?"}


def delivered_since(now: datetime, backfill: list[str] | tuple[str, str] | None = None) -> datetime:
    """PRs merged after this are checked: the last 48 hours, or, with router.yaml's delivered.backfill [from, until]
    (dates), back to `from` while it is before `until` (a rule's first week, say)."""
    if backfill:
        start, until = (ts(str(d) + "T00:00:00Z") for d in backfill)
        if now < until:
            return start
    return now - timedelta(hours=48)


def latest_by_workflow(runs: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for r in runs:
        if r.get("status") != "completed" or r.get("conclusion") not in ("success", "failure"):
            continue
        name = r["name"]
        if name not in latest or ts(r["updated_at"]) > ts(latest[name]["updated_at"]):
            latest[name] = r
    return latest


def run_failed_title(role: str) -> str:
    return f"Role run: {role} failing"


def started_by(run: dict) -> str:
    """What started a role run: the clock, the router (with the signal when the run's title carries it), or a person."""
    who = (run.get("triggering_actor") or run.get("actor") or {}).get("login", "")
    title = run.get("display_title") or ""
    signal = f" with signal {title}" if title.startswith("https://") else ""
    if CLOCK_LOGIN and who == CLOCK_LOGIN:
        return "the clock (scheduled run)" + signal
    if who in router_logins():
        return "the router (a tool-request or review signal)" + signal
    return f"a person (`{who or 'unknown'}`, workflow_dispatch)" + signal


def failed_step(jobs: list[dict]) -> str:
    """The first failing job and step from the jobs API, as markdown."""
    for j in jobs:
        if j.get("conclusion") not in ("failure", "timed_out"):
            continue
        step = next((s["name"] for s in j.get("steps") or [] if s.get("conclusion") in ("failure", "timed_out")), None)
        where = f"job `{j['name']}`, step `{step}`" if step else f"job `{j['name']}` ({j['conclusion']}, no failing step: likely the job timeout)"
        return f"{where} ({j.get('html_url', '')})"
    return "no failing job reported by the jobs API"


def run_failed_plan(role: str, runs: list[dict], issue: dict | None, comments: list[dict], jobs: list[dict], artifacts: list[dict], owner: str) -> dict | None:
    """What to do about one role's runs. runs: its completed runs (any order); issue: its open run-failure issue
    (or None); comments: that issue's comments. Returns {"open": {...}} / {"comment": {...}} / {"close": {...}} or None.
    cancelled and skipped runs are ignored (the per-role concurrency group cancels queued runs by design);
    failure and timed_out count. Three failing in a row mention the owner once per issue."""
    counted = sorted((r for r in runs if r.get("status") == "completed" and r.get("conclusion") in ("success", "failure", "timed_out")),
                     key=lambda r: ts(r["updated_at"]), reverse=True)
    if not counted:
        return None
    latest = counted[0]
    if latest["conclusion"] == "success":
        if not issue or (issue.get("user") or {}).get("login") not in router_logins():
            return None  # closes only the issues it opened itself
        return {"close": {"number": issue["number"], "comment": f"Closed by the router: `{role}`'s latest run succeeded ({latest['html_url']}).{FOOTER}"}}
    streak = next((k for k, r in enumerate(counted) if r["conclusion"] == "success"), len(counted))
    mark = RUN_FAILED_MARK.format(id=latest["id"])
    texts = [(issue or {}).get("body") or ""] + [c.get("body") or "" for c in comments]
    if issue and any(mark in t for t in texts):
        return None
    mentioned = any(f"@{owner}" in t for t in texts if RUN_FAILED_MARK_RE.search(t))
    ping = f"@{owner}: {streak} failed runs in a row; `{role}` is effectively off.\n\n" if streak >= 3 and owner and not mentioned else ""
    arts = ", ".join(f"`{a['name']}`" for a in artifacts if not a.get("expired")) or "none"
    facts = (f"Failing run: {latest['html_url']} ({latest['conclusion']}, {latest['updated_at']})\n"
             f"Failed at: {failed_step(jobs)}\n"
             f"Started by: {started_by(latest)}\n"
             f"Artifacts: {arts}\n"
             f"Failed runs in a row: {streak}")
    if issue:
        return {"comment": {"number": issue["number"], "body": f"{ping}Failed again.\n\n{facts}\n{mark}{FOOTER}"}}
    wf = f"role-{role}.yml"
    return {"open": {
        "title": run_failed_title(role),
        "labels": ["run-failure", "type:bug", "severity:high"],
        "body": (
            f"## Outcome\n`{role}`'s runs succeed again.\n\n## Signal\n{ping}{facts}\n\n"
            f"## Evidence\nThe run's logs and its artifacts (`run.json`, `result.txt` when the brief step ran). The router files this once per role, "
            f"comments on each further failure, and closes it when `{wf}`'s next run succeeds.\n\n"
            f"## Done means\n- [ ] The next `{wf}` run succeeds (the router closes this issue then).\n\n"
            f"## Out of scope\nRe-running the role or loosening its policy to make the run pass.\n\n"
            f"## Session rules\n- Keep going when a step doesn't need the human owner.\n- Stop before anything this repo's AUTONOMY.yaml puts at L1 or L2.\n"
            f"- Finish by opening a PR whose body names this issue as its signal.\n{mark}{FOOTER}"
        ),
        "signal_at": latest["updated_at"],
    }}


def review_required_repos(root_config: Path | None = None, repos_dir: Path | None = None, org: str | None = None) -> list[str]:
    """Every repo whose required checks (config.yaml required_checks, the default unless the repo has its own
    entry) include `claude-review`: the repos where a merge can go over a FAIL verdict."""
    checks = (yaml.safe_load((root_config or ROOT_CONFIG).read_text()) or {}).get("required_checks") or {}
    return [f"{org or ORG}/{p.stem}" for p in sorted((repos_dir or REPOS).glob("*.yaml")) if "claude-review" in checks.get(p.stem, checks.get("default") or [])]


def review_verdict(comments: list[dict], sha: str) -> str | None:
    """PASS, FAIL or None for head `sha`, read as the review action's verdict step reads it: only
    claude[bot]'s comments, each one's last non-empty line matched whole, the newest such line for this head."""
    verdict = None
    for c in sorted(comments, key=lambda c: c.get("created_at", "")):
        if (c.get("user") or {}).get("login") != REVIEWER_LOGIN:
            continue
        lines = [l.strip() for l in (c.get("body") or "").splitlines() if l.strip()]
        for v in ("PASS", "FAIL"):
            if lines and lines[-1] == f"Verdict: {v} ({sha})":
                verdict = v
    return verdict


def override_plan(pr: dict, comments: list[dict]) -> dict | None:
    """A merged PR and its conversation. FAIL on the merged head gets one comment asking whoever merged it for
    a reason (marker per PR). The deployer's PRs, and PRs with PASS or no verdict on that head, get nothing.
    It never blocks, reverts or relabels, and never judges the reason."""
    if not pr.get("merged_at") or (DEPLOYER_LOGIN and (pr.get("user") or {}).get("login") == DEPLOYER_LOGIN):
        return None
    sha = pr["head"]["sha"]
    if review_verdict(comments, sha) != "FAIL":
        return None
    mark = OVERRIDE_MARK.format(pr=pr["number"])
    if any(mark in (c.get("body") or "") for c in comments):
        return None
    merger = (pr.get("merged_by") or {}).get("login")
    who = f"@{merger}: " if merger else ""
    return {"comment": f"{who}Merged over a FAIL verdict on {sha}. Reply `Override: <one line>` so the digest records why.\n{mark}{FOOTER}"}


def override_line(short: str, pr: dict, comments: list[dict], now: datetime) -> str | None:
    """The digest's Overrides line for a merged PR: None unless the router asked for a reason on it. The reason
    is the newest `Override: <line>` after the router's comment from the person it mentioned (anyone who is
    not a bot when it mentioned nobody); "no reason given" once 7 days have passed since the merge."""
    mark = OVERRIDE_MARK.format(pr=pr["number"])
    asked = next((c for c in comments if (c.get("user") or {}).get("login") in router_logins() and mark in (c.get("body") or "")), None)
    if not asked:
        return None
    m = re.match(r"@(\S+?):", asked.get("body") or "")
    reason = None
    for c in sorted(comments, key=lambda c: c.get("created_at", "")):
        who = (c.get("user") or {}).get("login", "")
        if c.get("created_at", "") <= asked.get("created_at", "") or (who != m.group(1) if m else who.endswith("[bot]")):
            continue
        found = OVERRIDE_REASON_RE.search(c.get("body") or "")
        if found:
            reason = found.group(1)
    merged = ts(pr["merged_at"])
    why = reason or ("no reason given" if now - merged >= timedelta(days=7) else "reason asked, none yet")
    return f"{short}#{pr['number']} {pr.get('title', '')} · merged {merged.date().isoformat()} · {why} ({pr.get('html_url', '')})"


# ---------- the run ----------

def gate_rerun_scope(path: str, repos: list[str], reach: dict[str, set[str]]) -> set[str]:
    """The repos (of `repos`) whose gate verdicts a change to `path` on the policy repo's main can move. `reach` is
    each role's writes.repos and owned repos; a role with no file there (one just removed) reaches every repo."""
    if path == "config.yaml":
        return set(repos)
    m = POLICY_FILE_RE.fullmatch(path)
    if m:
        return {r for r in repos if r == f"{ORG}/{m.group(1)}"}
    m = ROLE_PATH_RE.match(path)
    if m:
        return set(repos) & reach.get(m.group(1), set(repos))
    return set()


def parse_policy_log(text: str) -> list[tuple[datetime, list[str]]]:
    """`git log --format=%x00%cI --name-only` as [(committed at, files)], newest first."""
    out = []
    for chunk in text.split("\0")[1:]:
        lines = [l.strip() for l in chunk.splitlines() if l.strip()]
        if lines:
            out.append((ts(lines[0]), lines[1:]))
    return out


def gate_policy_changes(log: list[tuple[datetime, list[str]]], repos: list[str], reach: dict[str, set[str]]) -> dict[str, datetime]:
    """{repo: when the newest commit that can move its gate verdicts landed on main}."""
    out: dict[str, datetime] = {}
    for at, files in log:
        for path in files:
            for r in gate_rerun_scope(path, repos, reach):
                if r not in out or at > out[r]:
                    out[r] = at
    return out


def gate_rerun_plan(pr: dict, runs: list[dict], changed_at: datetime) -> dict | None:
    """Whether one open PR's gate needs a re-run since the policy commit at `changed_at`: {"rerun": run id},
    {"skip": why}, or None. A gate run on the head that started after the commit already read today's main (or is
    reading it); a run still going can't be re-run yet, so the next pass looks again."""
    head = (pr.get("head") or {}).get("sha")
    gate = [r for r in runs if str(r.get("path", "")).split("@")[0] == GATE_WORKFLOW and r.get("head_sha") == head]
    if any(ts(r.get("run_started_at") or r["created_at"]) >= changed_at for r in gate):
        return None
    on_pr = sorted((r for r in gate if r.get("event") == "pull_request"), key=lambda r: r["id"])
    if not on_pr:
        return {"skip": f"no autonomy-gate (pull_request) run on {str(head)[:7]}"}
    if on_pr[-1].get("status") != "completed":
        return None
    return {"rerun": on_pr[-1]["id"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--policy-dir", default=os.environ.get("GITHUB_WORKSPACE") or ".", help="the policy repo's checkout")
    ap.add_argument("--policy-repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/name of the policy repo")
    ap.add_argument("--config", default="", help="router.yaml (default: router.yaml at the top of the policy repo)")
    ap.add_argument("--role", default="router", help="the router's role name (roles/<role>/role.yaml)")
    a = ap.parse_args()
    cfg_path = Path(a.config) if a.config else Path(a.policy_dir) / "router.yaml"
    if not cfg_path.is_file():
        raise SystemExit(f"no router config at {cfg_path}: the policy repo keeps it as router.yaml")
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    if not isinstance(cfg, dict):
        raise SystemExit(f"{cfg_path}: not a mapping")
    configure(a.policy_dir, a.policy_repo, a.role, cfg)
    problems = config_problems(cfg)
    if problems:
        for p in problems:
            print(f"::error title=signal router::{p}", file=sys.stderr)
        raise SystemExit(f"{len(problems)} problem(s) in the router's config; routed nothing")
    gh = GitHub(a.dry_run)
    now = datetime.now(timezone.utc)
    report = {"as_of": now.isoformat(timespec="seconds"), "routed": [], "dispatched": [], "closed": [], "latency_hours": []}
    probed = config_repos(cfg)  # every role's writes.repos is in it (test_router.py checks)
    unreadable = [r for r in probed if not gh.can_read(r)]
    skip = frozenset(unreadable)
    if unreadable and len(unreadable) == len(probed):
        raise SystemExit("the router's App can read none of its repos: check its token, not the config")
    if unreadable:
        print(f"::warning::the router's App can't read {', '.join(unreadable)}: install the router's App there. "
              f"Skipped this pass; every other repo was routed.")
        cfg = without_repos(cfg, skip)
        report["skipped"] = unreadable
    started: set[str] = set()  # roles started this pass, so the resume rule never starts one twice

    def ensure_labels(repo: str, wanted: set[str]):
        have = {l["name"] for l in gh.get(f"repos/{repo}/labels", per_page=100)}
        for name in sorted(wanted - have):
            gh.write("POST", f"repos/{repo}/labels", {"name": name, "color": "ededed"})

    sims = dict(product_bugs(cfg).get("sim_repos") or {})
    for repo in product_bugs(cfg).get("repos", []):
        ensure_labels(repo, {"routed", "type:bug", "severity:high", "severity:medium", *sims.values()})
        for issue in gh.get(f"repos/{repo}/issues", state="open", per_page=100):
            if "pull_request" in issue or "routed" in labels_of(issue):
                continue
            link = sim_link(issue.get("body") or "", sims)
            incident = gh.get(f"repos/{ORG}/{link.group(1)}/issues/{link.group(2)}") if link else None
            plan = product_bug_plan(issue, incident, sims)
            if plan:
                gh.write("PATCH", f"repos/{repo}/issues/{issue['number']}", {"body": (issue.get("body") or "") + plan["append"], "labels": plan["labels"]})
                report["routed"].append(f"{repo}#{issue['number']} product_bug")
                report["latency_hours"].append(round((now - ts(issue["created_at"])).total_seconds() / 3600, 1))

    tool_label = cfg.get("tool_request_label") or None
    for repo in cfg.get("tool_requests", []):
        ensure_labels(repo, {"routed", "type:feature"} | ({tool_label} if tool_label else set()))
        for issue in gh.get(f"repos/{repo}/issues", state="open", labels="tool-request", per_page=100):
            if "pull_request" in issue or "routed" in labels_of(issue):
                continue
            plan = tool_request_plan(issue, tool_label)
            if plan:
                gh.write("PATCH", f"repos/{repo}/issues/{issue['number']}", {"body": (issue.get("body") or "") + plan["append"], "labels": plan["labels"]})
                report["routed"].append(f"{repo}#{issue['number']} tool_request")
                report["latency_hours"].append(round((now - ts(issue["created_at"])).total_seconds() / 3600, 1))
                d = dispatch_plan(repo, issue)
                if d:
                    gh.write("POST", f"repos/{POLICY_REPO}/actions/workflows/{d['workflow']}/dispatches", {"ref": "main", "inputs": d["inputs"]})
                    gh.write("POST", f"repos/{repo}/issues/{issue['number']}/comments", {"body": f"Dispatched to the `{d['role']}` role ({POLICY_REPO} `{d['workflow']}`, signal = this issue). It opens a PR or answers here. {DISPATCH_MARK.format(role=d['role'])}" + FOOTER})
                    report["dispatched"].append(f"{repo}#{issue['number']} -> {d['role']}")
                    started.add(d["role"])

    logins = agent_logins()
    for repo in cfg.get("reviews", []):
        role = owner_role(repo)
        if not role:
            continue
        for pr in gh.get(f"repos/{repo}/pulls", state="open", per_page=50):
            if (pr.get("user") or {}).get("login", "") not in logins:
                continue
            n = pr["number"]
            head_at = gh.get(f"repos/{repo}/commits/{pr['head']['sha']}")["commit"]["committer"]["date"]
            plan = review_plan(pr, head_at, gh.get(f"repos/{repo}/pulls/{n}/reviews", per_page=100),
                               gh.get(f"repos/{repo}/pulls/{n}/comments", per_page=100),
                               gh.get(f"repos/{repo}/issues/{n}/comments", per_page=100), logins)
            if not plan:
                continue
            gh.write("POST", f"repos/{POLICY_REPO}/actions/workflows/role-{role}.yml/dispatches", {"ref": "main", "inputs": {"signal": plan["signal"]}})
            gh.write("POST", f"repos/{repo}/issues/{n}/comments", {"body": f"Review ({plan['kind']}) seen; the `{role}` role is starting on it (signal = this PR). It answers each thread, pushes, and re-requests review. {REVIEW_MARK.format(id=plan['id'])}" + FOOTER})
            report["dispatched"].append(f"{repo}#{n} review -> {role}")
            started.add(role)
            report["latency_hours"].append(round((now - ts(plan["at"])).total_seconds() / 3600, 1))

    if cfg.get("resume"):
        open_by_repo: dict[str, list[dict]] = {}
        def open_prs(r: str) -> list[dict]:
            if r not in open_by_repo:
                open_by_repo[r] = gh.get(f"repos/{r}/pulls", state="open", per_page=100)
            return open_by_repo[r]
        closed_by_repo: dict[str, list[dict]] = {}
        def closed_prs(r: str) -> list[dict]:
            if r not in closed_by_repo:  # one page, newest updated first: never the whole history
                closed_by_repo[r] = [{**p, "repo": r} for p in gh.get(f"repos/{r}/pulls", max_pages=1, state="closed", sort="updated", direction="desc", per_page=50)]
            return closed_by_repo[r]
        for repo in cfg.get("tool_requests", []):
            role = owner_role(repo)
            if not role or role in started:
                continue
            requests = sorted((i for i in gh.get(f"repos/{repo}/issues", state="open", labels="tool-request", per_page=100)
                               if "pull_request" not in i and "routed" in labels_of(i)), key=lambda i: i["number"])
            if not requests:
                continue
            repos = role_repos(role, skip=skip) or [repo]
            if repo not in repos:
                repos = [*repos, repo]
            named = {key for r in repos for p in open_prs(r) for key in signal_refs(p.get("body"), r)}
            requests = [i for i in requests if (repo, i["number"]) not in named]
            if not requests:
                continue
            own = (role_file(role).get("identity") or {}).get("github_login")
            budget = budget_for(role, cfg.get("pr_budget", 2))
            open_count = role_open_count(role, own, repos, {r: open_prs(r) for r in repos})
            if open_count >= budget:
                continue
            closed = [p for r in repos for p in closed_prs(r)]
            if not any(is_roles_pr(p, role, own) for p in closed):
                continue
            runs = f"repos/{POLICY_REPO}/actions/workflows/role-{role}.yml/runs"
            if any(gh.get(runs, max_pages=1, status=s, per_page=1).get("total_count", 0) for s in ("queued", "in_progress")):
                continue  # a run already going reads its requests itself; the next pass looks again
            who = role_logins(role)
            for issue in requests:
                n = issue["number"]
                plan = resume_plan(role, who, own, issue, gh.get(f"repos/{repo}/issues/{n}/comments", per_page=100),
                                   open_count, budget, closed, False, (repo, n))
                if not plan:
                    continue
                freed = ", ".join(plan["freed"])
                gh.write("POST", f"repos/{POLICY_REPO}/actions/workflows/role-{role}.yml/dispatches", {"ref": "main", "inputs": {"signal": plan["signal"]}})
                gh.write("POST", f"repos/{repo}/issues/{n}/comments", {"body": f"A PR slot is free ({freed} closed), so the `{role}` role is starting on this again (signal = this issue). {RESUME_MARK.format(role=role)}" + FOOTER})
                report["dispatched"].append(f"{repo}#{n} resume -> {role}")
                started.add(role)
                break

    owner = owner_login()
    da = cfg.get("digest_answers") or {}
    if da and owner:
        clerk, short = da["role"], POLICY_REPO.split("/", 1)[1]
        for issue in gh.get(f"repos/{POLICY_REPO}/issues", state="open", labels=da.get("label", "digest"), per_page=10):
            n = issue["number"]
            plan = digest_answer_plan(gh.get(f"repos/{POLICY_REPO}/issues/{n}/comments", per_page=100), owner)
            if not plan:
                continue
            gh.write("POST", f"repos/{POLICY_REPO}/actions/workflows/role-{clerk}.yml/dispatches", {"ref": "main", "inputs": {"signal": plan["signal"]}})
            gh.write("POST", f"repos/{POLICY_REPO}/issues/{n}/comments", {"body": f"Answers seen; the `{clerk}` role is recording them (signal = that comment). It replies here with the decisions-log PRs. {DIGEST_MARK.format(id=plan['id'])}" + FOOTER})
            report["dispatched"].append(f"{short}#{n} digest answers -> {clerk}")
            report["latency_hours"].append(round((now - ts(plan["at"])).total_seconds() / 3600, 1))

    for repo, product in (cfg.get("ci_main") or {}).items():
        runs = gh.get(f"repos/{repo}/actions/runs", branch="main", event="push", per_page=50)
        runs = runs.get("workflow_runs", []) if isinstance(runs, dict) else []
        latest = latest_by_workflow(runs)
        if not latest:
            continue
        open_issues = gh.get(f"repos/{repo}/issues", state="open", labels="ci-failure", per_page=100)
        plan = ci_plan(latest, open_issues, product)
        if plan["open"]:
            ensure_labels(repo, {"ci-failure", "type:bug", product, "severity:high"})
        for item in plan["open"]:
            gh.write("POST", f"repos/{repo}/issues", {"title": item["title"], "body": item["body"], "labels": item["labels"]})
            report["routed"].append(f"{repo} ci_main {item['title']!r}")
            report["latency_hours"].append(round((now - ts(item["signal_at"])).total_seconds() / 3600, 1))
        for item in plan["close"]:
            gh.write("POST", f"repos/{repo}/issues/{item['number']}/comments", {"body": item["comment"]})
            gh.write("PATCH", f"repos/{repo}/issues/{item['number']}", {"state": "closed", "state_reason": "completed"})
            report["closed"].append(f"{repo}#{item['number']}")

    if cfg.get("delivered"):
        backfill = cfg["delivered"].get("backfill") if isinstance(cfg["delivered"], dict) else None
        since = delivered_since(now, backfill).date().isoformat()
        pending: set[tuple[str, int]] = set()
        for repo in cfg.get("tool_requests", []):
            for pr in gh.get(f"repos/{repo}/pulls", state="open", per_page=100):
                pending |= set(signal_refs(pr.get("body"), repo))
        requests: dict[tuple[str, int], dict] = {}
        for repo in cfg.get("tool_requests", []):
            found = gh.get("search/issues", q=f"repo:{repo}+is:pr+is:merged+merged:>={since}", per_page=100).get("items", [])
            for pr in found:
                pr = {**pr, "merged_at": (pr.get("pull_request") or {}).get("merged_at") or pr.get("closed_at") or ""}
                for key, kind in signal_refs(pr.get("body"), repo).items():
                    if kind == "close" or key[0] not in cfg["tool_requests"]:
                        continue
                    if key not in requests:
                        # A Signal link to an issue that doesn't exist (a bare #N meant for another repository) is skipped,
                        # not the end of the pass (every pass once failed on a "#606" meant for another repository).
                        requests[key] = gh.get_or_none(f"repos/{key[0]}/issues/{key[1]}")
                        if requests[key] is None:
                            print(f"::warning::{repo}#{pr.get('number')}: its Signal links {key[0]}#{key[1]}, which doesn't exist; skipped")
                    req = requests[key]
                    if req is None or req.get("state") != "open" or "tool-request" not in labels_of(req) or "pull_request" in req:
                        continue
                    plan = delivered_plan(pr, repo, req, gh.get(f"repos/{key[0]}/issues/{key[1]}/comments", per_page=100), key in pending, owner_role(key[0]))
                    if not plan:
                        continue
                    gh.write("POST", f"repos/{key[0]}/issues/{key[1]}/comments", {"body": plan["comment"]})
                    if plan.get("label"):
                        ensure_labels(key[0], {plan["label"]})
                        gh.write("POST", f"repos/{key[0]}/issues/{key[1]}/labels", {"labels": [plan["label"]]})
                    report["routed"].append(f"{key[0]}#{key[1]} delivered ({'part' if kind == 'part' else 'flagged'}) by {repo}#{pr['number']}")
    for repo in cfg.get("run_failed", []):
        workflows = gh.get(f"repos/{repo}/actions/workflows", per_page=100).get("workflows", [])
        roles = {Path(w["path"]).stem.removeprefix("role-"): w for w in workflows if re.fullmatch(r"\.github/workflows/role-[\w-]+\.ya?ml", w["path"])}
        open_issues = {i["title"]: i for i in gh.get(f"repos/{repo}/issues", state="open", labels="run-failure", per_page=100) if "pull_request" not in i}
        ensured = False
        for role, w in sorted(roles.items()):
            runs = gh.get(f"repos/{repo}/actions/workflows/{w['id']}/runs", status="completed", per_page=20).get("workflow_runs", [])
            issue = open_issues.get(run_failed_title(role))
            comments = gh.get(f"repos/{repo}/issues/{issue['number']}/comments", per_page=100) if issue else []
            plan = run_failed_plan(role, runs, issue, comments, [], [], owner)
            if plan and ("open" in plan or "comment" in plan):
                run_id = RUN_FAILED_MARK_RE.search((plan.get("open") or plan.get("comment"))["body"]).group(1)
                jobs = gh.get(f"repos/{repo}/actions/runs/{run_id}/jobs", per_page=100).get("jobs", [])
                artifacts = gh.get(f"repos/{repo}/actions/runs/{run_id}/artifacts", per_page=100).get("artifacts", [])
                plan = run_failed_plan(role, runs, issue, comments, jobs, artifacts, owner)
            if not plan:
                continue
            if "open" in plan:
                if not ensured:
                    ensure_labels(repo, {"run-failure", "type:bug", "severity:high"})
                    ensured = True
                item = plan["open"]
                gh.write("POST", f"repos/{repo}/issues", {"title": item["title"], "body": item["body"], "labels": item["labels"]})
                report["routed"].append(f"{repo} run_failed {item['title']!r}")
                report["latency_hours"].append(round((now - ts(item["signal_at"])).total_seconds() / 3600, 1))
            elif "comment" in plan:
                gh.write("POST", f"repos/{repo}/issues/{plan['comment']['number']}/comments", {"body": plan["comment"]["body"]})
                report["routed"].append(f"{repo}#{plan['comment']['number']} run_failed again")
            else:
                gh.write("POST", f"repos/{repo}/issues/{plan['close']['number']}/comments", {"body": plan["close"]["comment"]})
                gh.write("PATCH", f"repos/{repo}/issues/{plan['close']['number']}", {"state": "closed", "state_reason": "completed"})
                report["closed"].append(f"{repo}#{plan['close']['number']}")

    if cfg.get("override"):
        watched = set(review_required_repos())
        since = (now - timedelta(hours=48)).date().isoformat()  # whole days; merged_at below trims to 48 hours
        not_deployer = f"+-author:app/{DEPLOYER_LOGIN.removesuffix('[bot]')}" if DEPLOYER_LOGIN else ""
        result = gh.get("search/issues", q=f"org:{ORG}+is:pr+is:merged+merged:>={since}{not_deployer}", sort="updated", order="desc", per_page=100)
        found = result.get("items", [])
        if result.get("total_count", 0) > len(found):  # one search page; say so rather than skip silently
            report["override_unchecked"] = result["total_count"] - len(found)
        for item in found:
            repo = item["repository_url"].split("/repos/", 1)[1]
            merged_at = (item.get("pull_request") or {}).get("merged_at") or ""
            if repo not in watched or not merged_at or ts(merged_at) < now - timedelta(hours=48):
                continue
            n = item["number"]
            comments = gh.get(f"repos/{repo}/issues/{n}/comments", per_page=100)
            if not any((c.get("user") or {}).get("login") == REVIEWER_LOGIN and "Verdict: FAIL (" in (c.get("body") or "") for c in comments):
                continue  # no FAIL from the reviewer at all: skip the PR fetch
            plan = override_plan(gh.get(f"repos/{repo}/pulls/{n}"), comments)
            if not plan:
                continue
            gh.write("POST", f"repos/{repo}/issues/{n}/comments", {"body": plan["comment"]})
            report["routed"].append(f"{repo}#{n} override asked")
            report["latency_hours"].append(round((now - ts(merged_at)).total_seconds() / 3600, 1))

    refused: list[str] = []
    gr = cfg.get("gate_rerun") or {}
    if gr.get("repos"):
        repos = gr["repos"]
        reach = {p.name: set(role_repos(p.name)) | {r for r in repos if owner_role(r) == p.name}
                 for p in sorted(ROLES.iterdir()) if (p / "role.yaml").is_file()}
        log = subprocess.run(["git", "-C", str(TOP), "log", f"--since={int(gr.get('window_days', 7))}.days",
                              "--format=%x00%cI", "--name-only", "HEAD"], capture_output=True, text=True, check=True).stdout
        changed = gate_policy_changes(parse_policy_log(log), repos, reach)
        left = int(gr.get("max_per_pass", 20))
        lines = report["gate_rerun"] = []
        for repo in repos:
            if repo not in changed:
                continue
            for pr in gh.get(f"repos/{repo}/pulls", state="open", per_page=100):
                n = pr["number"]
                runs = gh.get(f"repos/{repo}/actions/runs", head_sha=pr["head"]["sha"], per_page=100)
                plan = gate_rerun_plan(pr, runs.get("workflow_runs", []) if isinstance(runs, dict) else [], changed[repo])
                if not plan:
                    continue
                if "skip" in plan:
                    lines.append(f"{repo}#{n}: skipped, {plan['skip']}")
                    continue
                if left <= 0:
                    lines.append(f"{repo}#{n}: run {plan['rerun']} waits for the next pass (max_per_pass)")
                    continue
                left -= 1
                err = gh.attempt("POST", f"repos/{repo}/actions/runs/{plan['rerun']}/rerun", {})
                if err:
                    refused.append(f"{repo}#{n}: run {plan['rerun']} rerun refused: {err}")
                    lines.append(refused[-1])
                else:
                    lines.append(f"{repo}#{n} run {plan['rerun']} rerun requested")

    lat = report["latency_hours"]
    report["routed_within_1h"] = round(sum(h <= 1.0 for h in lat) / len(lat), 2) if lat else None
    report["writes"] = gh.writes if a.dry_run else len(gh.writes)
    print(json.dumps(report, indent=2))
    if refused:
        print(f"::error::{len(refused)} autonomy-gate re-run(s) refused; those PRs keep a verdict from before the policy change", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
