#!/usr/bin/env python3
"""A stand-in for gh, for adopt's tests: GitHub is a directory of bare repositories (FAKE_GH_REMOTES/<org>/<name>.git),
and every call is logged to FAKE_GH_LOG, one JSON line each. Only the calls adopt.py makes are understood; any other
call fails, so a new one shows up in the tests."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REMOTES = Path(os.environ["FAKE_GH_REMOTES"])
LOG = Path(os.environ["FAKE_GH_LOG"])
STATE = REMOTES / "state.json"


def git(*a, **kw):
    return subprocess.run(["git", *a], capture_output=True, text=True, **kw)


def bare(slug: str) -> Path:
    return REMOTES / f"{slug}.git"


def state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {"next": 1, "pulls": []}


def main(argv: list[str]) -> int:
    stdin = sys.stdin.read() if "--input" in argv and argv[argv.index("--input") + 1] == "-" else None
    with LOG.open("a") as f:
        f.write(json.dumps({"args": argv, "stdin": stdin}) + "\n")
    if argv[:2] == ["repo", "clone"]:
        src = bare(argv[2])
        if not src.is_dir():
            print(f"GraphQL: Could not resolve to a Repository with the name '{argv[2]}'.", file=sys.stderr)
            return 1
        return git("clone", "-q", str(src), argv[3]).returncode
    if argv[0] != "api":
        return 2
    method, fields, endpoint, i = "GET", {}, None, 1
    while i < len(argv):
        a = argv[i]
        if a == "-X":
            method = argv[i + 1]; i += 2
        elif a in ("-f", "-F"):
            k, v = argv[i + 1].split("=", 1); fields[k] = v; i += 2
        elif a in ("-q", "--input"):
            i += 2
        else:
            endpoint = a; i += 1
    path, _, query = endpoint.partition("?")
    parts = path.split("/")
    s = state()
    if method == "POST" and parts[0] == "orgs" and parts[2] == "repos":
        body = json.loads(stdin)
        dst = bare(f"{parts[1]}/{body['name']}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        git("init", "-q", "--bare", "-b", "main", str(dst))
        with tempfile.TemporaryDirectory() as t:
            git("clone", "-q", str(dst), t)
            Path(t, "README.md").write_text(f"# {body['name']}\n")
            git("add", "-A", cwd=t)
            git("commit", "-q", "-m", "Initial commit", cwd=t)
            git("push", "-q", "origin", "HEAD:main", cwd=t)
        print(json.dumps({"full_name": f"{parts[1]}/{body['name']}"}))
        return 0
    if parts[0] != "repos" or len(parts) < 3:
        return 2
    slug = f"{parts[1]}/{parts[2]}"
    rest = parts[3:]
    if not bare(slug).is_dir():
        print("Not Found (HTTP 404)", file=sys.stderr)
        return 1
    if method == "GET" and not rest:
        print(json.dumps({"full_name": slug}))
        return 0
    if method == "GET" and rest[:1] == ["branches"] and len(rest) > 1:
        ref = "refs/heads/" + "/".join(rest[1:])
        return 0 if git("--git-dir", str(bare(slug)), "rev-parse", "--verify", "-q", ref).returncode == 0 else 1
    if method == "GET" and rest[:1] == ["contents"]:
        return git("--git-dir", str(bare(slug)), "cat-file", "-e", f"main:{'/'.join(rest[1:])}").returncode
    if rest == ["pulls"] and method == "GET":
        q = dict(kv.split("=", 1) for kv in query.split("&") if kv)
        head = q["head"].split(":", 1)[1]
        found = [p for p in s["pulls"] if p["repo"] == slug and p["head"] == head]
        print(found[0]["number"] if found else "")
        return 0
    if rest == ["pulls"] and method == "POST":
        n = s["next"]
        s["pulls"].append({"repo": slug, "number": n, **fields})
        s["next"] = n + 1
        STATE.write_text(json.dumps(s))
        print(n)
        return 0
    if method == "PUT" and rest == ["branches", "main", "protection"]:
        json.loads(stdin)
        return 0
    if method == "PATCH" and not rest:
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
