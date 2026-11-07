#!/usr/bin/env python3
"""Trusted default-branch event handler. Can rerun a review job, never manufacture its verdict."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
spec=importlib.util.spec_from_file_location('bridge',Path(__file__).with_name('codex.py'))
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)

def update(api, event, rerun, wait_seconds=1200, sleep=time.sleep, clock=time.monotonic):
    comment=event.get('comment') or {}
    # Invalidation is conservative: webhook projections may omit performed_via_github_app,
    # and editing/removing a receipt header must not suppress refresh. This cannot issue PASS.
    # The verdict reader separately requires app identity and authenticates every edit.
    if not b.source_bot(comment):return 'untrusted comment ignored'
    issue=event.get('issue') or {};pr=issue.get('number')
    if type(pr) is not int or pr<1 or not issue.get('pull_request'):raise b.Refused('invalid PR event')
    pull=api.pull(pr);sha=pull.get('head',{}).get('sha')
    if pull.get('state')!='open' or pull.get('draft') or not b.SHA.fullmatch(sha or ''):return 'no open reviewable PR'
    # Only the exact adoption workflow and current PR head; never cancel/rerun another head's work.
    runs=api.call('actions/workflows/claude-review.yml/runs?event=pull_request&head_sha='+sha+'&per_page=100')
    entries=runs.get('workflow_runs',[])
    if runs.get('total_count')!=len(entries):raise b.Refused('workflow run list incomplete')
    matches=[r for r in entries if r.get('head_sha')==sha and r.get('event')=='pull_request' and r.get('path')=='.github/workflows/claude-review.yml' and len(r.get('pull_requests',[]))==1 and r['pull_requests'][0].get('number')==pr]
    if not matches:return 'no push-time review yet'
    run=max(matches,key=lambda r:r['id'])
    run_id=run.get('id')
    if type(run_id) is not int or run_id<1:raise b.Refused('invalid review workflow id')
    deadline=clock()+wait_seconds
    while True:
        latest=api.pull(pr)
        if latest.get('head',{}).get('sha')!=sha or latest.get('state')!='open':return 'head moved; no rerun'
        current=api.call(f'actions/runs/{run_id}')
        if current.get('id')!=run_id or current.get('head_sha')!=sha or current.get('event')!='pull_request' or current.get('path')!='.github/workflows/claude-review.yml' or len(current.get('pull_requests',[]))!=1 or current['pull_requests'][0].get('number')!=pr:
            raise b.Refused('review workflow identity changed')
        # A job rerun also restarts its dependents. Wait for the entire workflow,
        # including row reconciliation, then refresh both reviews atomically.
        if current.get('status')=='completed':
            jobs=api.call(f'actions/runs/{run_id}/jobs?filter=latest&per_page=100')
            if jobs.get('total_count')!=len(jobs.get('jobs',[])):raise b.Refused('review job list incomplete')
            for name in ['claude-review','claude-security-review']:
                if len([j for j in jobs['jobs'] if j.get('name')==name])!=1:raise b.Refused('review job unavailable or ambiguous')
            if all(j.get('status')=='completed' for j in jobs['jobs']):
                latest=api.pull(pr)
                if latest.get('head',{}).get('sha')!=sha or latest.get('state')!='open':return 'head moved; no rerun'
                if rerun(run_id) is not False:
                    return 'current-head review workflow rerun; no verdict issued by this handler'
        if clock()>=deadline:raise b.Refused('review still busy; invalidation was not discarded')
        sleep(min(15,max(0,deadline-clock())))

def main():
    repo=os.environ.get('REPO','')
    if not b.REPO.fullmatch(repo):raise b.Refused('invalid repository')
    if os.environ.get('PROVIDER')!='codex':return
    try:event=b.strict_json(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    except (KeyError,OSError,ValueError):raise b.Refused('review event unreadable') from None
    if (event.get('repository') or {}).get('full_name')!=repo:raise b.Refused('cross-repository event refused')
    api=b.GitHub(repo)
    def rerun(run_id):
        try:r=subprocess.run(['gh','api','--method','POST',f'repos/{repo}/actions/runs/{run_id}/rerun'],capture_output=True,timeout=30)
        except (OSError,subprocess.TimeoutExpired):raise b.Refused('could not refresh review job') from None
        if r.returncode:
            # Another reconciler can restart it between our read and POST.
            # Retain the invalidation and wait out that attempt too.
            if api.call(f'actions/runs/{run_id}').get('status')!='completed':return False
            raise b.Refused('could not refresh review workflow')
        return True
    print(update(api,event,rerun))

if __name__=='__main__':
    try:main()
    except b.Refused as e:print('::error::'+str(e));sys.exit(1)
