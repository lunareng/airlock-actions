#!/usr/bin/env python3
"""Make a pull request's push-time check show the verdict a review just changed (autonomy#157).

The gate and claude-review run on `pull_request` and on `pull_request_review`, and GitHub keeps one check row per
event. An approval that passes an agent's L1/L2 PR used to add a green review-time row beside the push-time red one,
so a mergeable PR read "1 failing". After the review-time job computes its own verdict, this re-runs the push-time
job when its result disagrees, so that row tracks the PR's current state: a failed one that should now pass, and a
passed one a dismissal should now fail. The review-time job keeps its own verdict as its result: a green row named
`autonomy-gate` is a passing required check, so ending it green on a failing verdict would let a COMMENTED review
unlock a PR.

Library (stdlib only: the claude-review job has no pyyaml):
  latest_run(runs) -> the newest `pull_request` run in a list of workflow runs, or None
  job_named(jobs, name) -> that run's job with the check's name, or None
  rerun_plan(passed, run, job) -> (action, why), action "none", "wait" or "rerun"

CLI (a pull_request_review job, after its verdict; env GH_TOKEN, REPO, HEAD, RUN_ID):
  rerun.py --job <check name> --passed true|false [--wait-seconds N]
    Exit 0: nothing to re-run, or the push-time job was re-run (the summary names the run and the head).
    Exit 1: the re-run API refused, or the push-time run was still going after N seconds (the error names the run).
"""
import argparse
import json
import os
import subprocess
import sys
import time


def latest_run(runs: list[dict]) -> dict | None:
    prs = [r for r in runs if isinstance(r, dict) and r.get("event") == "pull_request"]
    return max(prs, key=lambda r: (r.get("created_at") or "", r.get("id") or 0), default=None)


def job_named(jobs: list[dict], name: str) -> dict | None:
    named = [j for j in jobs if isinstance(j, dict) and j.get("name") == name]
    return named[-1] if named else None


def rerun_plan(passed: bool, run: dict | None, job: dict | None) -> tuple[str, str]:
    if run is None:
        return "none", "no pull_request run for this head: this run's verdict is the only one"
    if run.get("status") != "completed":
        return "wait", f"gate run {run.get('id')} is still {run.get('status')}"
    if job is None:
        return "none", f"gate run {run.get('id')} has no job by this check's name"
    conclusion = job.get("conclusion")
    if conclusion == "skipped":
        return "none", f"gate run {run.get('id')} skipped this job"
    if (conclusion == "success") == passed:
        return "none", f"gate run {run.get('id')} already says {conclusion}"
    return "rerun", f"gate run {run.get('id')} says {conclusion}, this review makes it {'pass' if passed else 'fail'}"


def gh(*args: str) -> str:
    return subprocess.run(["gh", "api", *args], check=True, capture_output=True, text=True).stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True, help="the check's name: the push-time job to re-run")
    ap.add_argument("--passed", required=True, choices=["true", "false"], help="this review-time job's verdict")
    ap.add_argument("--wait-seconds", type=int, default=300, help="how long to wait for a push-time run still going")
    a = ap.parse_args()
    repo, head, run_id = os.environ["REPO"], os.environ["HEAD"], os.environ["RUN_ID"]
    passed = a.passed == "true"

    workflow = json.loads(gh(f"repos/{repo}/actions/runs/{run_id}"))["workflow_id"]
    deadline = time.time() + a.wait_seconds
    while True:
        runs = json.loads(gh(f"repos/{repo}/actions/workflows/{workflow}/runs?event=pull_request&head_sha={head}&per_page=100"))
        run = latest_run(runs.get("workflow_runs") or [])
        job = None
        if run is not None and run.get("status") == "completed":
            job = job_named(json.loads(gh(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100")).get("jobs") or [], a.job)
        action, why = rerun_plan(passed, run, job)
        if action != "wait":
            break
        if time.time() >= deadline:
            print(f"::error::{why} after {a.wait_seconds}s, so its row may not show this review; re-run gate run {run.get('id')} by hand")
            return 1
        print(why + "; waiting")
        time.sleep(15)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if action == "rerun":
        try:
            gh("-X", "POST", f"repos/{repo}/actions/jobs/{job['id']}/rerun")
        except subprocess.CalledProcessError as e:
            print(f"::error::{why}, but GitHub refused to re-run gate run {run['id']} (job {job['id']}): {e.stderr.strip()}")
            return 1
        line = f"re-ran gate run {run['id']} for {head} ({why})"
    else:
        line = f"nothing to re-run: {why}"
    print(line)
    if summary:
        with open(summary, "a") as f:
            f.write(f"\n{line}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
