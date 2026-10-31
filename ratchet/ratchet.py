#!/usr/bin/env python3
"""The ratchet: levels are earned and lost by evidence (the ratchet action, ratchet/action.yml).

Runs weekly in a checkout of a policy repo, on the token Airlock mints for the ratchet role (its demotion PRs on
agent/<role>/ branches). Reads GitHub and the checkout's git history; writes only what the rules below say.

Evidence, per repo (every repos/<name>.yaml but the policy repo's own and ratchet.skip_repos):
  catch     an issue labelled `catch` in that repo (the owner files it, or an agent self-reports what the owner had
            to correct), dated by its creation; and every line naming its marker in a config.yaml `catch_logs`
            file kept in that repo.
  incident  an issue labelled `incident` (production broken or data at risk), or a `catch` labelled
            severity:critical or severity:high.

Rules:
  promotion   a repo whose default is below L4 and whose last catch is `weeks` (4) or more weeks ago gets ONE open
              `proposal` issue in the policy repo, "Promote <repo> to <next level>", answerable yes/no. L3 to L4
              also needs a judge and goals in repos/<repo>.yaml; the proposal says so when they're missing.
              Promotion never happens without the owner's yes: they edit the policy, a loosening, which waits for them.
  demotion    an incident in the last 7 days lowers the repo's default one level (L4 to L3, L3 to L2; L2 and L1
              stay) by a tightening PR in the policy repo (repos/<repo>.yaml, ratchet.demoted_on and evidence) and
              the same change to AUTONOMY.yaml in the repo. Tightening merges on green; the gate lets it through.
              A demoted repo is not restored automatically: it earns a promotion proposal like any other.
  judge_ran   every L4 repo and L4 role names where its judge runs (judge.check: a workflow on a branch, or the
              issue a report is commented on). A judge ran if a run completed, success or failure (skipped and
              cancelled don't count), or its report was commented, within max_age_days. Not run for that long:
              one `catch` + `judge-missing` issue in the policy repo, "Judge not running: <repo or role>", which as
              a catch resets that repo's promotion clock. Not run for `judge_demote_days` (14): the same tightening
              PR as a demotion, L4 to L3, with the evidence. A judge that has never run counts from the day its
              check landed. The ratchet never reads, computes or interprets a judge's number; an unreadable check
              writes nothing.

The organization and the policy repo are the caller's (--owner, --policy-repo; GITHUB_REPOSITORY_OWNER and
GITHUB_REPOSITORY by default), and the policy files are its checkout (--policy-dir; full history for the clocks).

  ratchet/ratchet.py [--dry-run] [--weeks 4] [--role ratchet] [--policy-dir .] [--owner o] [--policy-repo o/r]
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

RANK = {"L1": 1, "L2": 2, "L3": 3, "L4": 4}
UP = {"L1": "L2", "L2": "L3", "L3": "L4"}
DOWN = {"L4": "L3", "L3": "L2"}
DEFAULTS = {"skip_repos": [], "weeks": 4, "judge_demote_days": 14, "owner_name": "the owner", "legacy_demote_prefixes": []}


class ConfigError(Exception):
    pass


def gh(path: str, **params):
    items, page = [], 1
    while True:
        q = "&".join(f"{k}={v}" for k, v in {**params, "page": page}.items())
        for attempt in range(3):
            out = subprocess.run(["gh", "api", f"{path}?{q}"], capture_output=True, text=True)
            if out.returncode == 0:
                break
            time.sleep(2 * (attempt + 1))
        else:
            return None
        data = json.loads(out.stdout)
        if not isinstance(data, list):
            return data
        items += data
        if len(data) < int(params.get("per_page", 30)):
            return items
        page += 1


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def labels_of(i: dict) -> set[str]:
    return {l["name"] for l in i.get("labels", [])}


# ---------- the policy repo's settings ----------

def catch_logs(config: dict) -> list[dict]:
    """config.yaml `catch_logs`: files in which a line `- YYYY-MM-DD ...` naming `marker` records a catch."""
    logs = config.get("catch_logs") or []
    if not isinstance(logs, list):
        raise ConfigError("catch_logs must be a list")
    for i, log in enumerate(logs):
        if not isinstance(log, dict) or not all(isinstance(log.get(k), str) and log.get(k) for k in ("repo", "path", "marker")):
            raise ConfigError(f"catch_logs[{i}] needs repo, path and marker, each a string")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", log["repo"]):
            raise ConfigError(f"catch_logs[{i}].repo must be owner/name")
    return logs


def settings(config: dict) -> dict:
    """config.yaml's `ratchet` block over the defaults, and `catch_logs`; a malformed block stops the run."""
    block = config.get("ratchet") or {}
    if not isinstance(block, dict):
        raise ConfigError("ratchet must be a mapping")
    unknown = set(block) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"ratchet has unknown keys: {', '.join(sorted(unknown))}")
    out = {**DEFAULTS, **block}
    for k in ("skip_repos", "legacy_demote_prefixes"):
        if not isinstance(out[k], list) or not all(isinstance(x, str) and x for x in out[k]):
            raise ConfigError(f"ratchet.{k} must be a list of strings")
    for k in ("weeks", "judge_demote_days"):
        if not isinstance(out[k], int) or isinstance(out[k], bool) or out[k] < 1:
            raise ConfigError(f"ratchet.{k} must be a whole number of at least 1")
    if not isinstance(out["owner_name"], str) or not out["owner_name"].strip():
        raise ConfigError("ratchet.owner_name must be a name")
    return {**out, "skip_repos": set(out["skip_repos"]), "catch_logs": catch_logs(config)}


# ---------- pure decisions (test/ratchet/test_ratchet.py) ----------

def evidence(issues: list[dict], logs: list[dict] | None, now: datetime, since: datetime | None = None) -> dict:
    """Catches and incidents from a repo's issues (any state) and its catch logs ({path, text, marker}).
    Clean weeks count from the later of the last catch and `since` (when the repo's policy landed, or its last
    ratchet move): a repo with no catch log yet has not earned anything."""
    catches, incidents = [], []
    for i in issues:
        if "pull_request" in i:
            continue
        labs = labels_of(i)
        when = ts(i["created_at"])
        if "catch" in labs:
            catches.append((when, i["html_url"]))
            if labs & {"severity:critical", "severity:high"}:
                incidents.append((when, i["html_url"]))
        if "incident" in labs:
            incidents.append((when, i["html_url"]))
    for log in logs or []:
        word = re.compile(rf"\b{re.escape(log['marker'])}\b")
        for line in (log.get("text") or "").splitlines():
            m = re.match(r"^\s*[-*]\s*(\d{4}-\d{2}-\d{2})\b(.*)", line)
            if m and word.search(m.group(2)):
                catches.append((ts(m.group(1) + "T00:00:00Z"), f"{log['path']} {m.group(1)}"))
    last_catch = max((c[0] for c in catches), default=None)
    start = max([d for d in (last_catch, since) if d], default=None)
    clean_weeks = ((now - start).days // 7) if start else None
    recent_incident = [i for i in incidents if now - i[0] <= timedelta(days=7)]
    return {"catches": len(catches), "last_catch": last_catch, "clean_weeks": clean_weeks, "recent_incidents": recent_incident}


def decide(repo: str, policy: dict, ev: dict, open_proposals: set[str], weeks: int) -> dict:
    """{"promote": title | None, "demote": new_level | None, "why": str}."""
    level = policy.get("default", "L1")
    if ev["recent_incidents"] and level in DOWN:
        return {"promote": None, "demote": DOWN[level], "why": f"incident in the last 7 days: {ev['recent_incidents'][0][1]}"}
    if level in UP:
        clean = ev["clean_weeks"]
        if clean is not None and clean >= weeks:
            title = f"Promote {repo} to {UP[level]}"
            if title in open_proposals:
                return {"promote": None, "demote": None, "why": "proposal already open"}
            return {"promote": title, "demote": None, "why": f"{clean} clean weeks; needs {weeks}"}
    return {"promote": None, "demote": None, "why": f"{level}, {ev['clean_weeks']} clean weeks" if ev["clean_weeks"] is not None else f"{level}, no catch on record"}


def proposal_body(repo: str, policy: dict, ev: dict, new_level: str, owner_name: str = DEFAULTS["owner_name"], weeks: int = DEFAULTS["weeks"]) -> str:
    missing = []
    if new_level == "L4":
        if not policy.get("judge"):
            missing.append("a `judge` the agent cannot write to")
        if not policy.get("goals"):
            missing.append("a `goals` file")
    lines = [
        f"## Proposal\nRaise `default` in `repos/{repo.split('/')[1]}.yaml` (and the repo's `AUTONOMY.yaml`) from {policy.get('default')} to {new_level}.",
        f"\n## Evidence\n{ev['catches']} catch(es) on record; last catch {ev['last_catch'].date().isoformat() if ev['last_catch'] else 'none'}; {ev['clean_weeks'] if ev['clean_weeks'] is not None else 'no'} clean weeks.",
        ("\n## Not yet\nL4 also needs " + " and ".join(missing) + ". Say yes and the next PR adds them with the level.") if missing else "",
        f"\n## Question\nPromote? Yes or no. A yes is a loosening, so {owner_name} merges the PR that follows.",
        f"\n_Opened by Airlock's ratchet action. Promotion after {weeks} clean weeks is proposed, never applied, by the ratchet._",
    ]
    return "\n".join(l for l in lines if l)


def demoted_policy(text: str, new_level: str, today: str, why: str) -> str:
    """Rewrite a policy file's default and ratchet block as text, keeping everything else byte for byte."""
    doc = yaml.safe_load(text)
    doc["default"] = new_level
    doc["ratchet"] = {**(doc.get("ratchet") or {}), "demoted_on": today, "evidence": why}
    out = re.sub(r"^default:\s*L\d\s*$", f"default: {new_level}", text, count=1, flags=re.M)
    block = "ratchet:\n" + "".join(f"  {k}: {json.dumps(v)}\n" for k, v in doc["ratchet"].items())
    if re.search(r"^ratchet:\s*$", out, re.M):
        out = re.sub(r"^ratchet:\s*\n(?:  .*\n)*", block, out, count=1, flags=re.M)
    else:
        out = out.rstrip("\n") + "\n" + block
    assert yaml.safe_load(out)["default"] == new_level
    return out


def judge_checks(doc: dict) -> list[dict]:
    """judge.check as a list: one check, or several when the judge has several sources."""
    check = (doc.get("judge") or {}).get("check")
    return [] if not check else check if isinstance(check, list) else [check]


def check_name(check: dict) -> str:
    return f"{check['repo']} {check['workflow']}" if check.get("workflow") else f"{check['repo']} `{check['issue_label']}` report"


def last_workflow_run(runs: list[dict]) -> datetime | None:
    """The newest completed run that succeeded or failed: a failing judge is still a working judge."""
    done = [ts(r["created_at"]) for r in runs if r.get("status") == "completed" and r.get("conclusion") in ("success", "failure")]
    return max(done, default=None)


def last_report(issues: list[dict], comments: dict[int, list[dict]]) -> datetime | None:
    """The newest comment on an open report issue by the account that opened it (the reporter), so a comment
    anyone else leaves there never counts as the report."""
    out = []
    for i in issues:
        if "pull_request" in i:
            continue
        author = (i.get("user") or {}).get("login")
        out += [ts(c["created_at"]) for c in comments.get(i["number"], []) if (c.get("user") or {}).get("login") == author]
    return max(out, default=None)


def judge_title(subject: str) -> str:
    return f"Judge not running: {subject}"


def judge_plan(subject: str, level: str, ran: list[tuple[dict, datetime | None]], since: datetime | None, now: datetime,
               open_catches: set[str], demotion_open: bool, demote_days: int = DEFAULTS["judge_demote_days"]) -> dict:
    """{"catch": title | None, "demote": new_level | None, "age_days": int, "why": str} for one L4 repo or role.
    `ran` pairs each check with its last run (None: never); a check that never ran counts from `since`, the day it
    landed. The judge is as late as its latest check."""
    if level != "L4" or not ran:
        return {"catch": None, "demote": None, "age_days": None, "why": f"{level}, no judge check"}
    ages, late = [], []
    for check, last in ran:
        start = last or since or now
        age = (now - start).days
        ages.append(age)
        if age >= check["max_age_days"]:
            late.append(f"{check_name(check)}: last ran {last.date().isoformat() if last else 'never'}")
    age = max(ages)
    if not late:
        return {"catch": None, "demote": None, "age_days": age, "why": f"judge ran within {age} day(s)"}
    why = f"judge not running for {age} day(s): " + "; ".join(late)
    title = judge_title(subject)
    demote = DOWN["L4"] if age >= demote_days and not demotion_open else None
    return {"catch": None if title in open_catches else title, "demote": demote, "age_days": age, "why": why}


def judge_catch_body(subject: str, plan: dict, ran: list[tuple[dict, datetime | None]], owner_name: str = DEFAULTS["owner_name"],
                     demote_days: int = DEFAULTS["judge_demote_days"]) -> str:
    lines = [f"## Signal\nThe ratchet's judge_ran rule: {subject} is L4, and its judge has not run for {plan['age_days']} day(s).",
             "\n## Evidence"]
    lines += [f"- {check_name(c)}: last completed run {last.date().isoformat() if last else 'none'} (max_age_days {c['max_age_days']})" for c, last in ran]
    lines += [f"\n## What happens next\nAs a catch, this resets {subject}'s promotion clock. At {demote_days} days without a run, the ratchet opens the tightening PR that drops it from L4 to L3. "
              f"The fix is a judge that runs, from the owning role or {owner_name}; the ratchet never closes this.",
              "\n_Opened by Airlock's ratchet action._"]
    return "\n".join(lines)


def demoted_role(text: str, new_level: str, today: str, why: str) -> str:
    """A role file with its level lowered, keeping everything else byte for byte; the evidence goes on the line."""
    note = re.sub(r"[\r\n]+", " ", why)
    out = re.sub(r"^level:.*$", lambda _: f"level: {new_level}   # ratchet {today}: {note}", text, count=1, flags=re.M)
    assert yaml.safe_load(out)["level"] == new_level
    return out


def demote_prefixes(role: str, tail: str, legacy: list[str] = ()) -> tuple[str, ...]:
    """The branch prefixes a demotion PR for one subject (`tail`: "<repo>-" or "role-<role>-") may be open on: the
    role's agent/<role>/demote- branch, and each ratchet.legacy_demote_prefixes one older runs opened."""
    return (f"agent/{role}/demote-{tail}",) + tuple(f"{p}{tail}" for p in legacy)


def demotion_pr_open(open_prs: list[dict], prefixes: tuple[str, ...]) -> bool:
    """An open demotion PR for this subject on any of its branch prefixes, so no duplicate is opened."""
    return any((p.get("head") or {}).get("ref", "").startswith(prefixes) for p in open_prs)


# ---------- the run ----------

class Ctx:
    """One run: the policy repo's checkout and name, its organization, the role the run is, its settings."""

    def __init__(self, root: Path, owner: str, policy_repo: str, role: str, cfg: dict, dry_run: bool):
        self.root, self.owner, self.policy_repo, self.role, self.cfg, self.dry_run = root, owner, policy_repo, role, cfg, dry_run


def policy_start(ctx: Ctx, path: Path, policy: dict) -> datetime | None:
    """When the clock started: the last ratchet move, else the day the policy file landed on main."""
    r = policy.get("ratchet") or {}
    dates = [d for d in (r.get("promoted_on"), r.get("demoted_on")) if d]
    if dates:
        return ts(max(dates) + "T00:00:00Z")
    out = subprocess.run(["git", "-C", str(ctx.root), "log", "--diff-filter=A", "--format=%cI", "--", str(path.relative_to(ctx.root))], capture_output=True, text=True).stdout.strip().splitlines()
    return ts(out[-1]) if out else None


def check_since(ctx: Ctx, path: Path) -> datetime | None:
    """The day the file's judge check landed on main (the commit that first added max_age_days)."""
    out = subprocess.run(["git", "-C", str(ctx.root), "log", "-S", "max_age_days", "--format=%cI", "--", str(path.relative_to(ctx.root))], capture_output=True, text=True).stdout.strip().splitlines()
    return ts(out[-1]) if out else None


class Unreadable(Exception):
    pass


def check_last(check: dict, now: datetime) -> datetime | None:
    """When the check last ran. A workflow file that doesn't exist (404) has never run; anything else that
    can't be read raises Unreadable, so the ratchet writes nothing on it."""
    repo = check["repo"]
    if check.get("workflow"):
        out = subprocess.run(["gh", "api", f"repos/{repo}/actions/workflows/{check['workflow']}/runs?branch={check['branch']}&status=completed&per_page=100"], capture_output=True, text=True)
        if out.returncode != 0:
            if "HTTP 404" in out.stderr and gh(f"repos/{repo}") is not None:
                return None
            raise Unreadable(check_name(check))
        return last_workflow_run(json.loads(out.stdout).get("workflow_runs", []))
    issues = gh(f"repos/{repo}/issues", state="open", labels=check["issue_label"], per_page=100)
    if issues is None:
        raise Unreadable(check_name(check))
    comments = {}
    for i in issues:
        if "pull_request" not in i:
            c = gh(f"repos/{repo}/issues/{i['number']}/comments", per_page=100, since=(now - timedelta(days=30)).isoformat())
            if c is None:
                raise Unreadable(check_name(check))
            comments[i["number"]] = c
    return last_report(issues, comments)


def judge_ran(ctx: Ctx, subject: str, level: str, path: Path, doc: dict, now: datetime, judge_issues: list[dict], open_prs: list[dict], tail: str) -> tuple[dict, list]:
    """The plan for one L4 repo or role, and the (check, last run) pairs it was made from."""
    try:
        ran = [(c, check_last(c, now)) for c in judge_checks(doc)]
    except Unreadable as e:
        return {"catch": None, "demote": None, "age_days": None, "why": f"unreadable: {e}"}, []
    open_catches = {i["title"] for i in judge_issues if i.get("state") == "open"}
    demotion_open = demotion_pr_open(open_prs, demote_prefixes(ctx.role, tail, ctx.cfg["legacy_demote_prefixes"]))
    return judge_plan(subject, level, ran, check_since(ctx, path), now, open_catches, demotion_open, ctx.cfg["judge_demote_days"]), ran


def open_catch(ctx: Ctx, subject: str, plan: dict, ran: list) -> None:
    subprocess.run(["gh", "api", "-X", "POST", f"repos/{ctx.policy_repo}/labels", "--input", "-"], input=json.dumps({"name": "judge-missing", "color": "d93f0b", "description": "An L4 judge that has not run (the ratchet's judge_ran rule)"}), capture_output=True, text=True)
    subprocess.run(["gh", "api", "-X", "POST", f"repos/{ctx.policy_repo}/issues", "--input", "-"], input=json.dumps({"title": plan["catch"], "body": judge_catch_body(subject, plan, ran, ctx.cfg["owner_name"], ctx.cfg["judge_demote_days"]), "labels": ["catch", "judge-missing"]}), check=True, capture_output=True, text=True)


def judge_report(plan: dict, ran: list) -> dict:
    return {"checks": {check_name(c): last.date().isoformat() if last else None for c, last in ran}, "age_days": plan["age_days"], "decision": plan["why"]}


def logs_for(ctx: Ctx, repo: str) -> list[dict] | None:
    """The catch logs kept in `repo`, each with its text (None when it can't be read)."""
    out = []
    for log in ctx.cfg["catch_logs"]:
        if log["repo"] != repo:
            continue
        f = gh(f"repos/{log['repo']}/contents/{log['path']}")
        out.append({**log, "text": base64.b64decode(f["content"]).decode() if f and f.get("content") else None})
    return out or None


def run(ctx: Ctx, weeks: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    report = {"as_of": today, "repos": {}, "judges": {}, "proposed": [], "demoted": [], "judge_catches": []}
    proposals = {i["title"] for i in (gh(f"repos/{ctx.policy_repo}/issues", state="open", labels="proposal", per_page=100) or [])}
    judge_issues = gh(f"repos/{ctx.policy_repo}/issues", state="all", labels="judge-missing", per_page=100, since=(now - timedelta(weeks=26)).isoformat())
    open_prs = gh(f"repos/{ctx.policy_repo}/pulls", state="open", per_page=100)
    if judge_issues is None or open_prs is None:
        print(f"judge_ran: {ctx.policy_repo}'s judge-missing issues or open PRs are unreadable; judges are skipped this run", file=sys.stderr)
    own = ctx.policy_repo.split("/")[1]
    for path in sorted((ctx.root / "repos").glob("*.yaml")):
        repo = f"{ctx.owner}/{path.stem}"
        # The policy repo's own level is the owner's to move, never the ratchet's.
        if path.stem == own or path.stem in ctx.cfg["skip_repos"]:
            continue
        policy = yaml.safe_load(path.read_text())
        issues = gh(f"repos/{repo}/issues", state="all", per_page=100, since=(now - timedelta(weeks=26)).isoformat())
        if issues is None:
            report["repos"][path.stem] = "unreadable"
            continue
        jp = None
        if policy.get("default") == "L4" and judge_checks(policy) and judge_issues is not None and open_prs is not None:
            # This repo's judge-missing catches live in the policy repo; as catches they reset its promotion clock.
            issues = issues + [i for i in judge_issues if i["title"] == judge_title(repo)]
            jp, ran = judge_ran(ctx, repo, "L4", path, policy, now, judge_issues, open_prs, f"{path.stem}-")
            report["judges"][repo] = judge_report(jp, ran)
            if jp["catch"]:
                report["judge_catches"].append(jp["catch"])
                if not ctx.dry_run:
                    open_catch(ctx, repo, jp, ran)
        ev = evidence(issues, logs_for(ctx, repo), now, policy_start(ctx, path, policy))
        d = decide(repo, policy, ev, proposals, weeks)
        report["repos"][path.stem] = {"default": policy.get("default"), "clean_weeks": ev["clean_weeks"], "catches": ev["catches"], "decision": d["why"]}
        if d["promote"]:
            body = proposal_body(repo, policy, ev, UP[policy["default"]], ctx.cfg["owner_name"], weeks)
            report["proposed"].append(d["promote"])
            if not ctx.dry_run:
                subprocess.run(["gh", "api", "-X", "POST", f"repos/{ctx.policy_repo}/issues", "--input", "-"], input=json.dumps({"title": d["promote"], "body": body, "labels": ["proposal", "ratchet"]}), check=True, capture_output=True, text=True)
        if d["demote"]:
            new_text = demoted_policy(path.read_text(), d["demote"], today, d["why"])
            report["demoted"].append(f"{repo}: {policy['default']} -> {d['demote']}")
            if not ctx.dry_run:
                demote(ctx, repo, path, new_text, d, today, weeks)
        elif jp and jp["demote"]:
            # An incident's demotion above already lowers it one step; the judge's adds no second PR.
            jd = {"demote": jp["demote"], "why": jp["why"]}
            report["demoted"].append(f"{repo}: L4 -> {jp['demote']} ({jp['why']})")
            if not ctx.dry_run:
                demote(ctx, repo, path, demoted_policy(path.read_text(), jp["demote"], today, jp["why"]), jd, today, weeks)
    for path in sorted((ctx.root / "roles").glob("*/role.yaml")):
        doc = yaml.safe_load(path.read_text())
        if doc.get("level") != "L4" or not judge_checks(doc) or judge_issues is None or open_prs is None:
            continue
        subject = f"role {doc['role']}"
        jp, ran = judge_ran(ctx, subject, "L4", path, doc, now, judge_issues, open_prs, f"role-{doc['role']}-")
        report["judges"][subject] = judge_report(jp, ran)
        if jp["catch"]:
            report["judge_catches"].append(jp["catch"])
            if not ctx.dry_run:
                open_catch(ctx, subject, jp, ran)
        if jp["demote"]:
            report["demoted"].append(f"{subject}: L4 -> {jp['demote']} ({jp['why']})")
            if not ctx.dry_run:
                demote_role(ctx, doc["role"], path, demoted_role(path.read_text(), jp["demote"], today, jp["why"]), jp, today)
    return report


def open_pr(target: str, filepath: str, branch: str, content_for, msg: str, body: str) -> None:
    """A branch off the target's default branch, one file changed on it, a PR, and auto-merge (rebase)."""
    base = gh(f"repos/{target}")["default_branch"]
    head_sha = gh(f"repos/{target}/git/ref/heads/{base}")["object"]["sha"]
    subprocess.run(["gh", "api", "-X", "POST", f"repos/{target}/git/refs", "--input", "-"], input=json.dumps({"ref": f"refs/heads/{branch}", "sha": head_sha}), check=True, capture_output=True, text=True)
    current = gh(f"repos/{target}/contents/{filepath}", ref=base)
    content = content_for(current)
    subprocess.run(["gh", "api", "-X", "PUT", f"repos/{target}/contents/{filepath}", "--input", "-"], input=json.dumps({"message": msg, "content": base64.b64encode(content.encode()).decode(), "sha": current["sha"], "branch": branch}), check=True, capture_output=True, text=True)
    pr = json.loads(subprocess.run(["gh", "api", "-X", "POST", f"repos/{target}/pulls", "--input", "-"], input=json.dumps({"title": msg, "head": branch, "base": base, "body": body}), check=True, capture_output=True, text=True).stdout)
    subprocess.run(["gh", "pr", "merge", str(pr["number"]), "--repo", target, "--auto", "--rebase"], capture_output=True, text=True)


def demote_role(ctx: Ctx, role: str, path: Path, new_text: str, d: dict, today: str):
    """One tightening PR in the policy repo: the role file's level, one step down."""
    branch = f"agent/{ctx.role}/demote-role-{role}-{today}"
    msg = f"Demote role {role} to {d['demote']}: {d['why']}"
    body = (f"**Intent:** automatic demotion (the ratchet's judge_ran rule). {d['why']}\n\n**Level:** tightening. Restoration is "
            f"{ctx.cfg['owner_name']}'s, never automatic.\n\n_Opened by Airlock's ratchet action._")
    open_pr(ctx.policy_repo, str(path.relative_to(ctx.root)), branch, lambda _: new_text, msg, body)


def demote(ctx: Ctx, repo: str, path: Path, new_text: str, d: dict, today: str, weeks: int):
    """Two tightening PRs: the repo's file in the policy repo, and the repo's own AUTONOMY.yaml. Both merge on green."""
    short = path.stem
    branch = f"agent/{ctx.role}/demote-{short}-{today}"
    msg = f"Demote {repo} to {d['demote']}: {d['why']}"
    body = (f"**Intent:** automatic demotion (the ratchet). {d['why']}\n\n**Level:** tightening; merges on green. Restoration is by "
            f"promotion proposal after {weeks} clean weeks, never automatic.\n\n_Opened by Airlock's ratchet action._")
    open_pr(ctx.policy_repo, f"repos/{short}.yaml", branch, lambda _: new_text, msg, body)
    open_pr(repo, "AUTONOMY.yaml", branch, lambda current: demoted_policy(base64.b64decode(current["content"]).decode(), d["demote"], today, d["why"]), msg, body)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--weeks", type=int)
    ap.add_argument("--role", default="ratchet")
    ap.add_argument("--policy-dir", default=os.environ.get("GITHUB_WORKSPACE") or ".")
    ap.add_argument("--owner", default=os.environ.get("GITHUB_REPOSITORY_OWNER"))
    ap.add_argument("--policy-repo", default=os.environ.get("GITHUB_REPOSITORY"))
    a = ap.parse_args(argv)
    root = Path(a.policy_dir).resolve()
    # Fail closed: no organization, no policy repo, no config.yaml, or malformed settings, and nothing is read or written.
    if not a.owner or not a.policy_repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", a.policy_repo) or a.policy_repo.split("/")[0] != a.owner:
        print("ratchet: --owner and --policy-repo (owner/name, in that owner) are required", file=sys.stderr)
        return 2
    if not re.fullmatch(r"[a-z][a-z0-9-]*", a.role):
        print("ratchet: --role must be a role name", file=sys.stderr)
        return 2
    if not (root / "config.yaml").is_file() or not (root / "repos").is_dir():
        print(f"ratchet: {root} is not a policy repo checkout (config.yaml and repos/)", file=sys.stderr)
        return 2
    try:
        cfg = settings(yaml.safe_load((root / "config.yaml").read_text()) or {})
    except ConfigError as e:
        print(f"ratchet: config.yaml: {e}", file=sys.stderr)
        return 2
    report = run(Ctx(root, a.owner, a.policy_repo, a.role, cfg, a.dry_run), a.weeks or cfg["weeks"])
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
