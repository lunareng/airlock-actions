#!/usr/bin/env python3
"""The router's watchdog: one open issue while its passes fail, closed by the next pass that succeeds.

The router reports every failure but its own (autonomy#209: 278 failed passes and nothing said so for four days). This
runs as a job of its own beside the router's, on the workflow's GITHUB_TOKEN, with no Airlock token and none of the
router's code, so a refused mint or a bug in a pass still gets reported. Stdlib only.

  watchdog.py --result <the route job's result> --run-url <url> [--mention <login>]   (env GH_TOKEN, REPO)
"""
import argparse
import json
import os
import subprocess
import sys

TITLE = "Router: passes are failing"
LABEL = "router-down"


def watchdog_plan(result: str, issue: dict | None) -> str | None:
    """"open", "close" or None. Only failure and success say anything: a cancelled or skipped pass ran nothing. An open
    issue stays quiet while passes keep failing, so a pass every 30 minutes isn't a comment every 30 minutes."""
    if result == "failure" and issue is None:
        return "open"
    if result == "success" and issue is not None:
        return "close"
    return None


def gh(*args: str, body: dict | None = None) -> str:
    return subprocess.run(["gh", "api", *args, *(["--input", "-"] if body is not None else [])], check=True, capture_output=True, text=True,
                          input=json.dumps(body) if body is not None else None).stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", required=True)
    ap.add_argument("--run-url", required=True)
    ap.add_argument("--mention", default="")
    a = ap.parse_args()
    repo = os.environ["REPO"]
    found = [i for i in json.loads(gh(f"repos/{repo}/issues?state=open&labels={LABEL}&per_page=100")) if i.get("title") == TITLE and "pull_request" not in i]
    issue = found[0] if found else None
    plan = watchdog_plan(a.result, issue)
    if plan == "open":
        subprocess.run(["gh", "api", "-X", "POST", f"repos/{repo}/labels", "--input", "-"], input=json.dumps({"name": LABEL, "color": "b60205"}), capture_output=True, text=True)
        who = f"@{a.mention}: " if a.mention else ""
        body = (f"## Signal\n{who}a router pass failed: {a.run_url}\n\nWhile this is open, every rule the pass didn't reach is off: reviews don't wake roles, "
                f"digest answers aren't read, and failed runs aren't reported. The next pass that succeeds closes this issue.\n\n"
                f"_Opened by the router's watchdog (lunareng/airlock-actions router/watchdog), which runs without the router's token or code._")
        gh("-X", "POST", f"repos/{repo}/issues", body={"title": TITLE, "body": body, "labels": [LABEL]})
    elif plan == "close":
        gh("-X", "POST", f"repos/{repo}/issues/{issue['number']}/comments", body={"body": f"A pass succeeded: {a.run_url}. Closing."})
        gh("-X", "PATCH", f"repos/{repo}/issues/{issue['number']}", body={"state": "closed", "state_reason": "completed"})
    print(f"watchdog: result={a.result} issue={issue['number'] if issue else None} plan={plan}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
