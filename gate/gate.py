#!/usr/bin/env python3
"""The autonomy gate (moved from lunareng/autonomy, Matthew 2026-10-30): compute a pull request's effective level from its diff and the repo's AUTONOMY.yaml.

Library:
  effective_level(policy, changed_paths) -> (level, reasons)
    The level is the strictest of the policy default and every carve-out whose glob matches a changed
    path. The tenant's floors apply on top: `floors` is config.yaml's `floors.paths`, held over DEFAULT_FLOORS
    (floor_table). Reasons name each path and the rule that set its level.

CLI (run inside the repo under test, on the PR head):
  gate.py --base <ref> [--policy AUTONOMY.yaml] [--main-policy <file>] [--branch <name>] [--author <login>] [--json]
    Exit 0: the PR may be merged by whoever opened it.
    --main-policy: the repo's repos/<repo>.yaml on lunareng/autonomy's main. The stricter of it and AUTONOMY.yaml
    scores the PR (with_main_policy), so a tightening on main binds open PRs before the copy is rolled out (autonomy#160).
    Exit 1: an agent opened it and its level is L1 or L2, so a human must take it from here.
    A human-authored PR always exits 0: CODEOWNERS and branch protection route L1/L2 to the owner.
    --approved: the human owner's latest review on this very commit is APPROVED, which is "a human took
    it from here": the PR passes at L1/L2 too, so Matthew's approve-then-merge needs no bypass (api#1082).
    --stamp-file: for a PR Airlock's App opened (config.yaml `airlock.app_login`), the JSON Airlock answered for
    its head commit ({"role": ..., "reason": ...}, GET /v1/stamps); missing or without a role, the PR fails at
    any level until the owner approves that commit (lunareng/airlock build step 3).
    --pr-file: the PR as JSON ({"body", "auto_merge", "proofs": [the owner's comment and review bodies]}). At L1
    auto-merge is always off, so a person presses merge, and the owner's approval of the head commit is enough
    when the PR owes no proof (Matthew 2026-10-30: "a human approval is good enough"). When a person must act
    (autonomy#146, Matthew 2026-10-27), the PR names it in a `## Proof owed` section whose every item names a
    command and its expected output, and then also needs a `## Proof` comment from the owner covering every item.
    The body is the author's to edit after an approval, so auto-merge stays off even when it owes nothing: a
    `## Proof owed` deleted after the approval can't merge without a person pressing the button.

Library, what a run shows:
  verdict_line(result) -> the job summary's verdict for the CLI's --json result.
  annotation(result) -> a failing run's `::error` workflow command: the verdict, what blocks it and what set the
    level, so the Checks tab says why without opening the job summary (Matthew 2026-10-30).

Library, the L1 second key:
  second_key(pr) -> (ok, reasons)
    proof_owed_items(body) numbers the section's top-level list items; proof_given(proofs) is the item numbers
    an owner's `## Proof` comment answers (`1.` or `### 1`, with something after it). Ticks in the body count for
    nothing: the proof goes in a comment.

Agent PRs are recognized by the author's login (config.yaml `agent_markers.logins`, every role's App), never by the branch.
Every role on Airlock shares one App login, so for those PRs Airlock's stamp says which role pushed the commit.
"""
import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RANK = {"L1": 1, "L2": 2, "L3": 3, "L4": 4}

# Floors are the tenant's policy: config.yaml's `floors:` in its policy repo, `actions` by name and `paths` by a word a
# changed path contains (lowercased, anywhere in it). Without one, this small default holds for every tenant, and a
# tenant's floors only add to it or tighten it: floor_table takes the stricter of the two for every key, so a floor
# written looser than the default is held at the default (and validate fails it). validate/check.py reads the same.
DEFAULT_FLOORS = {
    "actions": {"delete_data": "L1", "change_policy": "L2"},
    "paths": {"secrets": "L1"},
}
FLOOR_KINDS = tuple(DEFAULT_FLOORS)


class FloorError(ValueError):
    """config.yaml's floors can't be read; the gate fails closed rather than score without them."""


def floor_table(kind: str, given=None) -> dict[str, str]:
    """The floors of one kind the gate holds: the tenant's `floors.<kind>` in its order, each the stricter of it and
    the default, then every default the tenant doesn't name. A path floor's word is lowercased, as the path is."""
    if given is None:
        given = {}
    if not isinstance(given, dict):
        raise FloorError(f"floors.{kind} is not a mapping of {'words' if kind == 'paths' else 'actions'} to levels")
    out: dict[str, str] = {}
    for key, level in given.items():
        if not isinstance(key, str) or not key.strip():
            raise FloorError(f"floors.{kind} has a key that isn't a word ({key!r}); quote it")
        if not isinstance(level, str) or level not in RANK:
            raise FloorError(f"floors.{kind}.{key} is {level!r}, not a level from L1 to L4")
        k = key.lower() if kind == "paths" else key
        if k not in out or RANK[level] < RANK[out[k]]:
            out[k] = level
    for k, level in DEFAULT_FLOORS[kind].items():
        if k not in out or RANK[level] < RANK[out[k]]:
            out[k] = level
    return out


def floors_of(config: dict | None) -> dict[str, dict[str, str]]:
    """config.yaml's floors, every kind, as the gate holds them (floor_table). Raises FloorError for a `floors` that
    isn't a mapping or names a kind there isn't: a typo there would drop the tenant's floors without a word."""
    floors = (config or {}).get("floors")
    if floors is None:
        floors = {}
    if not isinstance(floors, dict):
        raise FloorError("floors is not a mapping (actions: and paths:)")
    unknown = [repr(k) if not isinstance(k, str) else k for k in floors if k not in FLOOR_KINDS]
    if unknown:
        raise FloorError(f"floors has {', '.join(unknown)}; it takes only {' and '.join(FLOOR_KINDS)}")
    return {kind: floor_table(kind, floors.get(kind)) for kind in FLOOR_KINDS}


def _match(glob: str, path: str) -> bool:
    """CODEOWNERS-style matching: `**` spans directories; a bare filename matches at the repo root."""
    if "/" not in glob.rstrip("/"):
        return fnmatch.fnmatch(path, glob) or fnmatch.fnmatch(Path(path).name, glob)
    g = glob.lstrip("/")
    if g.endswith("/**"):
        prefix = g[:-3]
        if not any(c in prefix for c in "*?["):
            return path.startswith(prefix + "/") or path == prefix
        # A wildcard prefix (src/**/send/**) matches any of the path's leading directories (autonomy#128).
        parts = path.split("/")
        heads = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
        return any(fnmatch.fnmatch(h, prefix) or fnmatch.fnmatch(h, prefix.replace("**/", "")) for h in heads)
    return fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path, g.replace("**/", ""))


def levels(doc: dict) -> dict[str, str]:
    """Flatten a policy to {key: level}; mirrors validate.py."""
    out = {"default": doc.get("default", "L1")}
    for k, v in (doc.get("actions") or {}).items():
        out[f"actions.{k}"] = v
    for p in doc.get("paths") or []:
        out[f"paths.{p['glob']}"] = p["level"]
    return out


def only_tightens(before: dict | None, after: dict) -> bool:
    """True when every level in `after` is the same or stricter than in `before` and no carve-out was removed.
    Decided 2026-10-19: such a policy change may merge at the level the rest of the diff computes."""
    if before is None:
        return False
    b, a = levels(before), levels(after)
    if any(k.startswith("paths.") and k not in a for k in b):
        return False
    return all(RANK[v] <= RANK[b[k]] for k, v in a.items() if k in b)


def shown(path: str) -> str:
    """A path as the reasons show it. It is matched exactly as git stores it, but a control character or a byte that
    isn't UTF-8 is shown escaped (\\n, \\x1b), so a file name can't add a line to the job summary or the log."""
    out = []
    for c in path:
        o = ord(c)
        if 0xDC80 <= o <= 0xDCFF:  # a byte that isn't UTF-8, as surrogateescape decoded it
            out.append(f"\\x{o - 0xDC00:02x}")
        elif not c.isprintable():
            out.append(f"\\x{o:02x}" if o < 0x100 else f"\\u{o:04x}")
        else:
            out.append(c)
    return "".join(out)


def effective_level(policy: dict, changed_paths: list[str], base_policy: dict | None = None,
                    floors: dict | None = None) -> tuple[str, list[str]]:
    """`floors` is the tenant's `floors.paths` as config.yaml writes it; None is the default's alone."""
    path_floors = floor_table("paths", floors)
    level = policy.get("default", "L2")
    reasons = [f"default {level}"]
    for path in changed_paths:
        hit = None
        for rule in policy.get("paths", []):
            if _match(rule["glob"], path):
                if hit is None or RANK[rule["level"]] < RANK[hit["level"]]:
                    hit = rule
        if hit and RANK[hit["level"]] < RANK[level]:
            level = hit["level"]
        if hit:
            reasons.append(f"{shown(path)}: {hit['level']} ({hit['glob']})")
        low = path.lower()
        for key, floor in path_floors.items():
            if key in low and RANK[floor] < RANK[level]:
                level = floor
                reasons.append(f"{shown(path)}: floor {floor} ('{key}')")
        if path in ("AUTONOMY.yaml",):
            if only_tightens(base_policy, policy):
                reasons.append("AUTONOMY.yaml: tightening only; merges at the level the rest of the diff computes")
            elif RANK["L2"] < RANK[level]:
                level = "L2"
                reasons.append("AUTONOMY.yaml: policy change is L2 (loosening waits for Matthew)")
    return level, reasons


def with_main_policy(level: str, reasons: list[str], main_policy: dict | None, changed_paths: list[str],
                     policy_file: str = "AUTONOMY.yaml", floors: dict | None = None) -> tuple[str, list[str]]:
    """The stricter of the PR's AUTONOMY.yaml and the repo's file on lunareng/autonomy's main. Main is the source
    of truth and AUTONOMY.yaml its copy, which lags a policy change until it is rolled out, so a tightening on main
    reaches an open PR as soon as its gate runs again (autonomy#160), and the copy can never score a PR looser.
    The PR's own edit to AUTONOMY.yaml is judged once, by effective_level against the base's copy."""
    if main_policy is None:
        return level, [*reasons, "lunareng/autonomy main has no policy file for this repo: AUTONOMY.yaml alone scores it"]
    main_level, main_reasons = effective_level(main_policy, [p for p in changed_paths if p != policy_file], floors=floors)
    if RANK[main_level] < RANK[level]:
        return main_level, [*reasons, f"lunareng/autonomy main scores this diff {main_level}, stricter than AUTONOMY.yaml's {level}:",
                            *(f"main: {r}" for r in main_reasons)]
    return level, reasons


def is_agent(branch: str | None, author: str | None, markers: dict) -> bool:
    # The branch is never consulted: any App that can push can name a branch anything, so a branch that
    # exempted its PRs let an agent's L1/L2 change pass as Matthew's (lunar-luncom#1, 2026-10-24).
    # An author the gate can't name fails closed. GitHub shows a deleted account or App as `ghost`, so once a
    # role's old App is deleted (autonomy#139), its open PRs would otherwise score as a person's.
    if not author or author == "ghost":
        return True
    return author in markers.get("logins", [])


ROLE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")


def airlock_credit(author: str | None, airlock: dict, stamp: dict | None) -> tuple[bool, str | None]:
    """(credited, reason) for one PR. Only a PR Airlock's App opened needs credit: its head commit must carry a
    role in Airlock's stamp. Every other PR is credited by its author's login, as before."""
    login = (airlock or {}).get("app_login")
    if not login or author != login:
        return True, None
    stamp = stamp if isinstance(stamp, dict) else {}
    role = stamp.get("role")
    if isinstance(role, str) and ROLE_NAME.fullmatch(role):
        return True, f"Airlock: the {role} role pushed this commit (stamp agent: {role})"
    why = stamp.get("reason") or "Airlock has no stamp for this commit"
    why = re.sub(r"[^\x20-\x7e]", "?", str(why))[:200]
    return False, f"Airlock: no role is credited with this commit ({why}); a human takes it from here"


FENCE = re.compile(r"^\s*(?:(?:[-*+]|\d+[.)]|#{3,6}\s+\d+[.):]?)\s+)?(```|~~~)")  # also `1. ```` opening one
H2 = re.compile(r"^##\s+(.*?)\s*#*\s*$")
ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s+(.*)$")
PROOF_ENTRY = re.compile(r"^(?:#{3,6}\s+(\d+)\b[.):]?|(\d+)[.)])[ \t]*(.*)$")
COMMAND = re.compile(r"`[^`\n]+`")
EXPECTED = re.compile(r"\bexpect", re.I)


def _section(text: str, title: str) -> list[str] | None:
    """The lines under the `## <title>` heading, up to the next `## ` heading outside a code fence; None if absent."""
    out, inside, fenced = None, False, False
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if FENCE.match(line):
            fenced = not fenced
        h = None if fenced else H2.match(line)
        if h:
            inside = h.group(1).strip().lower() == title.lower()
            if inside and out is None:
                out = []
            continue
        if inside:
            out.append(line)
    return out


def proof_owed_items(body: str) -> list[str] | None:
    """The `## Proof owed` section's top-level list items (each with its continuation lines), or None without one."""
    lines = _section(body, "Proof owed")
    if lines is None:
        return None
    items, fenced = [], False
    for line in lines:
        start = not fenced and ITEM.match(line)
        if FENCE.match(line):
            fenced = not fenced
        if start:
            items.append(start.group(1))
        elif items:
            items[-1] += "\n" + line
    return [i.strip() for i in items]


def proof_given(proofs: list[str], owed: int) -> set[int]:
    """The item numbers an owner's `## Proof` comment answers. An entry is `1.` or `### 1` at the start of a line
    outside a code fence, with anything after it (the output, a code block). With one item owed, a `## Proof`
    section with no numbered entry answers it."""
    given = set()
    for text in proofs:
        lines = _section(text, "Proof")
        if lines is None:
            continue
        entries, current, fenced = {}, None, False
        for line in lines:
            m = not fenced and PROOF_ENTRY.match(line)
            if FENCE.match(line):
                fenced = not fenced
            if m:
                current = int(m.group(1) or m.group(2))
                entries.setdefault(current, "")
                entries[current] += m.group(3)
            elif current is not None:
                entries[current] += "\n" + line
        given |= {n for n, t in entries.items() if t.strip()}
        if not entries and owed == 1 and "".join(lines).strip():
            given.add(1)
    return given


def second_key(pr: dict | None) -> tuple[bool, list[str]]:
    """At L1 auto-merge is off, so a person merges, and the owner's approval of the head commit is enough when the
    PR owes no proof (no `## Proof owed` items: no section, or one saying none; Matthew 2026-10-30). A PR that owes
    proof also needs the second key (autonomy#146): every Proof owed item names a command and its expected output,
    and an owner's `## Proof` comment answers each."""
    if not isinstance(pr, dict):
        return False, ["L1: the gate has no PR data (--pr-file), so it cannot check Proof owed or auto-merge"]
    # Auto-merge first, for every L1 PR: the body can change after the approval, and a person pressing merge is
    # what stops a `## Proof owed` deleted after it from merging on its own.
    if pr.get("auto_merge"):
        return False, ["L1: auto-merge is on; turn it off, a person presses merge (#146, 3)"]
    items = proof_owed_items(pr.get("body") or "")
    if not items:
        return True, ["L1: no proof owed and auto-merge is off, so the owner's approval of this commit is the human check (Matthew, 2026-10-30)"]
    ok, reasons = True, []
    for n, item in enumerate(items, 1):
        if not (COMMAND.search(item) or "```" in item) or not EXPECTED.search(item):
            ok = False
            reasons.append(f"L1: Proof owed item {n} needs an exact command in backticks and its expected output")
    proofs = [p for p in (pr.get("proofs") or []) if isinstance(p, str)]
    missing = [n for n in range(1, len(items) + 1) if n not in proof_given(proofs, len(items))]
    if missing:
        ok = False
        reasons.append("L1: no proof yet for Proof owed item(s) " + ", ".join(map(str, missing)) +
                       "; the owner posts a `## Proof` comment with `1.`, `2.`... and each output")
    if ok:
        reasons.append(f"L1: proof for all {len(items)} Proof owed item(s) is on the PR and auto-merge is off; a person merges")
    return ok, reasons


def verdict_line(g: dict) -> str:
    """The job summary's verdict for a --json result."""
    if g["allowed"]:
        return "Verdict: may merge at this level."
    if g["level"] == "L1" and any(r.startswith("the human owner approved") for r in g["reasons"]):
        return ("Verdict: L1, so a person presses merge with auto-merge off; and if the PR owes proof, the owner's approval "
                "needs a second key: each `## Proof owed` item naming its command and expected output, and the owner's "
                "`## Proof` comment answering every item (`1.`, `2.`...). A proof comment posted after the approval needs "
                "this check re-run.")
    if not g.get("credited", True):
        return ("Verdict: Airlock credits no role with this commit. A human takes it from here: an approving review from "
                "the owner on this commit passes the gate.")
    return ("Verdict: agent PR at L1/L2. A human takes it from here: an approving review from the owner on this commit "
            "passes the gate.")


def _escape(text: str, prop: bool = False) -> str:
    """Workflow-command escaping: a reason can't end the command or start another."""
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if prop else text


def annotation(g: dict, shown: int = 5) -> str:
    """A failing run's `::error` line. What blocks the PR comes first, in full; the reasons that set its level follow,
    the first `shown` of them, since a large diff names every path (the job summary has them all)."""
    level_reasons = g["reasons"][:g.get("level_reasons", len(g["reasons"]))]
    blockers = g["reasons"][len(level_reasons):]
    lines = [verdict_line(g).removeprefix("Verdict: ")]
    lines += [f"- {r}" for r in blockers]
    lines.append(f"Why {g['level']}:")
    lines += [f"- {r}" for r in level_reasons[:shown]]
    if len(level_reasons) > shown:
        lines.append(f"- and {len(level_reasons) - shown} more in the job summary")
    title = f"autonomy gate: {g['level']}, {'agent' if g['agent'] else 'human'} PR, not yet mergeable"
    return f"::error title={_escape(title, prop=True)}::{_escape(chr(10).join(lines))}"


def changed_files(base: str) -> list[str]:
    # --no-renames: a file moved off a strict path is scored at that path too, not only where it landed.
    # -z and core.quotePath=false: each path exactly as stored. Git otherwise quotes a path with a tab, a newline or a
    # non-ASCII byte ("secrets/k\303\251y"), which no glob matches, so the file would escape its level.
    out = subprocess.check_output(["git", "-c", "core.quotePath=false", "diff", "--name-only", "--no-renames", "-z", f"{base}...HEAD"])
    return [path for path in out.decode("utf-8", "surrogateescape").split("\0") if path]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--policy", default="AUTONOMY.yaml")
    ap.add_argument("--main-policy", help="this repo's file on lunareng/autonomy's main (repos/<repo>.yaml); the stricter scores")
    ap.add_argument("--branch")
    ap.add_argument("--author")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--approved", action="store_true", help="the human owner approved this head commit")
    ap.add_argument("--stamp-file", help="Airlock's stamp for the head commit, as JSON (a PR Airlock's App opened)")
    ap.add_argument("--pr-file", help="the PR's body, auto-merge state and the owner's comments, as JSON (the L1 second key)")
    a = ap.parse_args()

    policy = yaml.safe_load(Path(a.policy).read_text())
    config = yaml.safe_load(Path(a.config).read_text()) or {}
    try:
        floors_of(config)
    except FloorError as e:
        print(f"::error title=autonomy gate::config.yaml {shown(str(e))}; no floors, no gate")
        return 1
    path_floors = (config.get("floors") or {}).get("paths")
    markers = config.get("agent_markers", {})
    stamp = None
    if a.stamp_file and Path(a.stamp_file).exists():
        try:
            stamp = json.loads(Path(a.stamp_file).read_text())
        except ValueError:
            stamp = None
    pr = None
    if a.pr_file and Path(a.pr_file).exists():
        try:
            pr = json.loads(Path(a.pr_file).read_text())
        except ValueError:
            pr = None
    paths = changed_files(a.base)
    base_policy = None
    if a.policy in paths:
        try:
            base_policy = yaml.safe_load(subprocess.check_output(["git", "show", f"{a.base}:{a.policy}"], text=True, stderr=subprocess.DEVNULL))
        except subprocess.CalledProcessError:
            base_policy = None
    level, reasons = effective_level(policy, paths, base_policy, floors=path_floors)
    if a.main_policy:
        main_file = Path(a.main_policy)
        level, reasons = with_main_policy(level, reasons, yaml.safe_load(main_file.read_text()) if main_file.is_file() else None, paths, a.policy,
                                          floors=path_floors)
    level_reasons = len(reasons)
    agent = is_agent(a.branch, a.author, markers)
    credited, credit = airlock_credit(a.author, config.get("airlock", {}), stamp)
    if credit:
        reasons.append(credit)
    allowed = credited and not (agent and RANK[level] <= 2)
    if not allowed and a.approved:
        if level == "L1":
            allowed, why = second_key(pr)
            reasons += why
        else:
            allowed = True
        reasons.append("the human owner approved this commit: the PR may merge" if allowed else
                       "the human owner approved this commit, but at L1 a person must also act (the L1 lines above)")

    # Every reason once more through shown(): a glob or a key from the PR's own AUTONOMY.yaml can't add a line either.
    reasons = [shown(r) for r in reasons]
    result = {"level": level, "agent": agent, "credited": credited, "allowed": allowed, "changed": len(paths), "reasons": reasons,
              "level_reasons": level_reasons}
    if a.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"level: {level}  ({'agent' if agent else 'human'} PR, {len(paths)} files)")
        for r in reasons:
            print(f"  - {r}")
        print("verdict: " + ("may merge at this level" if allowed else
                             "L1: the owner approved, but a person must also act: proof for every Proof owed item in a comment, auto-merge off" if a.approved and level == "L1" else
                             "Airlock PR with no credited role: a human takes it from here (an approving review on this commit passes it)" if not credited else
                             "agent PR at L1/L2: a human takes it from here (an approving review on this commit passes it)"))
    return 0 if allowed else 1


if __name__ == "__main__":
    sys.exit(main())
