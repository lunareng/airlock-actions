#!/usr/bin/env python3
"""Check a policy repo: every repos/*.yaml (and AUTONOMY.yaml) and roles/*/role.yaml, against the schemas Airlock
publishes (fetched at run time, never copied) and the charter's rules every tenant shares, with the floors config.yaml
sets (`floors:`, read by the gate's own code, so the two can't disagree).

  check.py --airlock <origin> --owner <org> [--base <git ref>]
    Run in the policy repo's checkout. Each problem is one `::error file=<path>,line=<n>::<message>`; the exit code is 1
    if there are any, and `problems=<count>` goes to $GITHUB_OUTPUT. With --base, a level loosened since that commit, or
    a carve-out removed, is a problem too (it waits for the human owner).

What it checks, beyond the schema:
  config.yaml        its `floors` (optional): actions and paths only, each a level from L1 to L4, and none looser than
                     the default every tenant has (the gate's DEFAULT_FLOORS): floors only add to it or tighten it.
  repos/<name>.yaml  its `repo` is <owner>/<name>; no action or path above its floor; no carve-out looser than
                     the default; policy.loosen is review; an L4 default has a judge with a check.
  roles/<r>/role.yaml  `role` is <r>; every repository it writes to has a repos/ file; every repository it clones or
                     judges by is the owner's; its brief and goals exist; an L4 role's judge runs in CI, with a check.
Fails closed: Airlock unreachable, or answering outside 2xx, is a failure, and a file that isn't YAML is a problem.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

# The floors are the gate's: the same default and the same reading of config.yaml's `floors`. This is the action's own
# directory, never the checkout's, so nothing in the policy repo can stand in for it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gate"))
from gate import DEFAULT_FLOORS, FLOOR_KINDS, RANK, FloorError, floors_of  # noqa: E402


class Problems:
    def __init__(self):
        self.items: list[tuple[str, int, str]] = []

    def add(self, path: str, line: int, message: str) -> None:
        self.items.append((path, line, message))

    def print(self) -> None:
        for path, line, message in self.items:
            msg = re.sub(r"[\x00-\x1f\x7f]", " ", message).replace("%", "%25")
            loc = path.replace("%", "%25").replace(",", "%2C").replace(":", "%3A")
            print(f"::error file={loc},line={line}::{msg}")


def lines(text: str) -> dict[tuple, int]:
    """Each key path in a YAML document -> its 1-based line, so a problem points at the line that has it."""
    out: dict[tuple, int] = {(): 1}

    def walk(node, path):
        out[path] = node.start_mark.line + 1
        if isinstance(node, yaml.MappingNode):
            for k, v in node.value:
                walk(v, path + (k.value,))
                out[path + (k.value,)] = k.start_mark.line + 1
        elif isinstance(node, yaml.SequenceNode):
            for i, v in enumerate(node.value):
                walk(v, path + (i,))

    node = yaml.compose(text)
    if node is not None:
        walk(node, ())
    return out


def line_of(index: dict[tuple, int], path) -> int:
    path = tuple(path)
    while path not in index and path:
        path = path[:-1]
    return index.get(path, 1)


def fetch_schema(airlock: str, name: str) -> dict:
    url = f"{airlock}/schema/{name}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"::error title=airlock validate::Airlock: couldn't check the policy (HTTP {e.code} for {url})")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise SystemExit(f"::error title=airlock validate::Airlock: couldn't check the policy ({type(e).__name__} for {url})")


def load(path: Path, rel: str, problems: Problems):
    text = path.read_text()
    try:
        doc = yaml.safe_load(text)
        index = lines(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        problems.add(rel, (mark.line + 1) if mark else 1, f"not YAML: {getattr(e, 'problem', e)}")
        return None, {}
    if not isinstance(doc, dict):
        problems.add(rel, 1, "the file must be a YAML mapping")
        return None, {}
    return doc, index


def schema_problems(doc: dict, schema: dict, rel: str, index, problems: Problems) -> None:
    for err in sorted(Draft202012Validator(schema).iter_errors(doc), key=lambda e: list(e.absolute_path)):
        where = ".".join(str(p) for p in err.absolute_path)
        problems.add(rel, line_of(index, err.absolute_path), f"{where + ': ' if where else ''}{err.message}")


def check_floors(doc: dict, rel: str, index, problems: Problems) -> dict[str, dict[str, str]]:
    """config.yaml's `floors`, every problem with it, and the floors as the gate will hold them (the default's alone
    when they can't be read: validate fails then anyway)."""
    floors = doc.get("floors")
    if floors is None:
        return floors_of({})
    if not isinstance(floors, dict):
        problems.add(rel, line_of(index, ["floors"]), "floors must be a mapping, with actions: and paths:")
        return floors_of({})
    for kind, given in floors.items():
        if kind not in FLOOR_KINDS:
            problems.add(rel, line_of(index, ["floors", kind]), f"floors.{kind} isn't a kind of floor; floors takes {' and '.join(FLOOR_KINDS)}")
            continue
        if given is None:
            continue
        if not isinstance(given, dict):
            problems.add(rel, line_of(index, ["floors", kind]), f"floors.{kind} must be a mapping of {'words' if kind == 'paths' else 'actions'} to levels")
            continue
        for key, level in given.items():
            where = line_of(index, ["floors", kind, key])
            if not isinstance(key, str) or not key.strip():
                problems.add(rel, where, f"floors.{kind} has a key that isn't a word ({key!r}); quote it")
                continue
            if level not in RANK:
                problems.add(rel, where, f"floors.{kind}.{key} is {level!r}; a floor is a level from L1 to L4")
                continue
            for word, floor in DEFAULT_FLOORS[kind].items():
                covers = word in key.lower() if kind == "paths" else word == key
                if covers and RANK[level] > RANK[floor]:
                    what = f"the default floor for '{word}'" if kind == "paths" else "the default floor"
                    problems.add(rel, where, f"floors.{kind}.{key} is {level}, looser than {what} ({floor}); "
                                             "a tenant's floors only add to the default or tighten it")
    try:
        return floors_of(doc)
    except FloorError:
        return floors_of({})


def check_repo(doc: dict, rel: str, name: str | None, owner: str, index, problems: Problems,
               floors: dict[str, dict[str, str]] | None = None) -> None:
    floors = floors or floors_of({})
    if name is not None and doc.get("repo") != f"{owner}/{name}":
        problems.add(rel, line_of(index, ["repo"]), f"repo must be {owner}/{name}, the repository this file is for")
    default = doc.get("default", "L1")
    for action, floor in floors["actions"].items():
        lvl = (doc.get("actions") or {}).get(action)
        if lvl in RANK and RANK[lvl] > RANK[floor]:
            problems.add(rel, line_of(index, ["actions", action]), f"actions.{action} is {lvl}; the charter's floor is {floor}")
    for i, p in enumerate(doc.get("paths") or []):
        if not isinstance(p, dict) or p.get("level") not in RANK:
            continue
        if default in RANK and RANK[p["level"]] > RANK[default]:
            problems.add(rel, line_of(index, ["paths", i, "level"]), f"path {p.get('glob')} is {p['level']}, looser than the default {default}")
        for key, floor in floors["paths"].items():
            if key in str(p.get("glob", "")).lower() and RANK[p["level"]] > RANK[floor]:
                problems.add(rel, line_of(index, ["paths", i, "level"]), f"path {p.get('glob')} is {p['level']}; the floor for '{key}' is {floor}")
    if (doc.get("policy") or {}).get("loosen", "review") != "review":
        problems.add(rel, line_of(index, ["policy", "loosen"]), "policy.loosen must be review")
    if default == "L4" and not (doc.get("judge") or {}).get("check"):
        problems.add(rel, line_of(index, ["judge"]), "an L4 repo's judge needs a check, so the ratchet can see it run")


def check_role(doc: dict, rel: str, folder: str, owner: str, known: set[str], root: Path, index, problems: Problems) -> None:
    if doc.get("role") != folder:
        problems.add(rel, line_of(index, ["role"]), f"role '{doc.get('role')}' must match its folder '{folder}'")
    for i, repo in enumerate((doc.get("writes") or {}).get("repos") or []):
        if repo not in known:
            problems.add(rel, line_of(index, ["writes", "repos", i]), f"writes to {repo}, which has no repos/*.yaml policy")
    for i, repo in enumerate((doc.get("run") or {}).get("clone") or []):
        if not str(repo).startswith(f"{owner}/"):
            problems.add(rel, line_of(index, ["run", "clone", i]), f"clones {repo}, which isn't one of {owner}'s repositories")
    judge = doc.get("judge") or {}
    checks = judge.get("check") if isinstance(judge, dict) else None
    for i, c in enumerate(checks if isinstance(checks, list) else [checks] if isinstance(checks, dict) else []):
        if "repo" in c and not str(c["repo"]).startswith(f"{owner}/"):
            problems.add(rel, line_of(index, ["judge", "check"]), f"judge check {c['repo']} isn't one of {owner}'s repositories")
    for key in ("brief", "goals"):
        target = doc.get(key)
        if isinstance(target, str) and not (root / target).is_file():
            problems.add(rel, line_of(index, [key]), f"{key} {target} does not exist")
    if doc.get("level") == "L4":
        if not str(judge.get("source", "")).startswith("ci:"):
            problems.add(rel, line_of(index, ["judge"]), "an L4 role's judge must be computed by CI (source starts with ci:)")
        if not judge.get("check"):
            problems.add(rel, line_of(index, ["judge"]), "an L4 role's judge needs a check, so the ratchet can see it run")


def levels(doc: dict) -> dict[str, str]:
    out = {"default": doc.get("default", "L1")}
    for k, v in (doc.get("actions") or {}).items():
        out[f"actions.{k}"] = v
    for p in doc.get("paths") or []:
        if isinstance(p, dict):
            out[f"paths.{p.get('glob')}"] = p.get("level")
    return out


def check_loosening(rel: str, doc: dict, base: str, root: Path, index, problems: Problems) -> None:
    try:
        old = subprocess.check_output(["git", "-C", str(root), "show", f"{base}:{rel}"], stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return  # a new file: nothing to compare
    before, after = levels(yaml.safe_load(old) or {}), levels(doc)
    for key, new in after.items():
        prev = before.get(key)
        if prev in RANK and new in RANK and RANK[new] > RANK[prev]:
            problems.add(rel, 1, f"{key} loosened {prev} -> {new}; a loosening waits for the human owner")
    for key in before.keys() - after.keys():
        if key.startswith("paths."):
            problems.add(rel, 1, f"carve-out {key[6:]} removed; a loosening waits for the human owner")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--airlock", required=True)
    ap.add_argument("--owner", required=True)
    ap.add_argument("--base")
    a = ap.parse_args()
    airlock = a.airlock.rstrip("/")
    if not re.fullmatch(r"https://[a-z0-9.-]+(:\d+)?|http://127\.0\.0\.1(:\d+)?", airlock):
        print("::error title=airlock validate::airlock-url must be an https origin, like https://airlock.lunareng.com")
        return 1
    root = Path.cwd()
    repo_schema, role_schema = fetch_schema(airlock, "autonomy.schema.json"), fetch_schema(airlock, "role.schema.json")
    problems = Problems()
    floors = floors_of({})
    if (root / "config.yaml").is_file():
        doc, index = load(root / "config.yaml", "config.yaml", problems)
        if doc is not None:
            floors = check_floors(doc, "config.yaml", index, problems)
    repo_files = sorted(root.glob("repos/*.yaml"))
    known = {f"{a.owner}/{p.stem}" for p in repo_files}
    for path in repo_files + ([root / "AUTONOMY.yaml"] if (root / "AUTONOMY.yaml").is_file() else []):
        rel = path.relative_to(root).as_posix()
        doc, index = load(path, rel, problems)
        if doc is None:
            continue
        schema_problems(doc, repo_schema, rel, index, problems)
        check_repo(doc, rel, path.stem if rel.startswith("repos/") else None, a.owner, index, problems, floors)
        if a.base:
            check_loosening(rel, doc, a.base, root, index, problems)
    for path in sorted(root.glob("roles/*/role.yaml")):
        rel = path.relative_to(root).as_posix()
        doc, index = load(path, rel, problems)
        if doc is None:
            continue
        schema_problems(doc, role_schema, rel, index, problems)
        check_role(doc, rel, path.parent.name, a.owner, known, root, index, problems)
    problems.print()
    n = len(problems.items)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"problems={n}\n")
    print(f"{'FAIL' if n else 'OK'}: {n} problem(s) in {len(repo_files)} repository file(s) and {len(list(root.glob('roles/*/role.yaml')))} role file(s)")
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
