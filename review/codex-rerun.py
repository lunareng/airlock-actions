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
    kinds=['code','security']
    # Only the exact adoption workflow and current PR head; never cancel/rerun another head's work.
    runs=api.call('actions/workflows/claude-review.yml/runs?event=pull_request&head_sha='+sha+'&per_page=100')
    entries=runs.get('workflow_runs',[])
    if runs.get('total_count')!=len(entries):raise b.Refused('workflow run list incomplete')
    matches=[r for r in entries if r.get('head_sha')==sha and r.get('event')=='pull_request' and r.get('path')=='.github/workflows/claude-review.yml' and len(r.get('pull_requests',[]))==1 and r['pull_requests'][0].get('number')==pr]
    if not matches:return 'no push-time review yet'
    run=max(matches,key=lambda r:r['id'])
    deadline=clock()+wait_seconds
    while True:
        latest=api.pull(pr)
        if latest.get('head',{}).get('sha')!=sha or latest.get('state')!='open':return 'head moved; no rerun'
        jobs=api.call(f'actions/runs/{run["id"]}/jobs?filter=latest&per_page=100')
        if jobs.get('total_count')!=len(jobs.get('jobs',[])):raise b.Refused('review job list incomplete')
        targets=[]
        for kind in kinds:
            name='claude-review' if kind=='code' else 'claude-security-review'
            candidates=[j for j in jobs['jobs'] if j.get('name')==name]
            if len(candidates)!=1:raise b.Refused('review job unavailable or ambiguous')
            targets.append(candidates[0])
        if all(j.get('status')=='completed' for j in targets):break
        if clock()>=deadline:raise b.Refused('review still busy; invalidation was not discarded')
        sleep(min(15,max(0,deadline-clock())))
    for job in targets:
        latest=api.pull(pr)
        if latest.get('head',{}).get('sha')!=sha or latest.get('state')!='open':return 'head moved; no rerun'
        job_id=job.get('id')
        if type(job_id) is not int or job_id<1:raise b.Refused('invalid review job id')
        rerun(job_id)
    return 'current-head review jobs rerun; no verdict issued by this handler'

def main():
    repo=os.environ.get('REPO','')
    if not b.REPO.fullmatch(repo):raise b.Refused('invalid repository')
    if os.environ.get('PROVIDER')!='codex':return
    try:event=b.strict_json(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    except (KeyError,OSError,ValueError):raise b.Refused('review event unreadable') from None
    if (event.get('repository') or {}).get('full_name')!=repo:raise b.Refused('cross-repository event refused')
    api=b.GitHub(repo)
    def rerun(job_id):
        try:r=subprocess.run(['gh','api','--method','POST',f'repos/{repo}/actions/jobs/{job_id}/rerun'],capture_output=True,timeout=30)
        except (OSError,subprocess.TimeoutExpired):raise b.Refused('could not refresh review job') from None
        if r.returncode:raise b.Refused('could not refresh review job')
    print(update(api,event,rerun))

if __name__=='__main__':
    try:main()
    except b.Refused as e:print('::error::'+str(e));sys.exit(1)
