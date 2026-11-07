#!/usr/bin/env python3
"""Non-gating observer probe. No review requests, credentials, or production gate bypass."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
spec=importlib.util.spec_from_file_location('bridge',Path(__file__).with_name('codex.py'))
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)

def main():
    repo,pr,sha=(os.environ.get(k,'') for k in ['REPO','PR','SHA'])
    if not b.REPO.fullmatch(repo) or not pr.isdigit() or not b.SHA.fullmatch(sha):raise b.Refused('invalid probe context')
    api=b.GitHub(repo);initial=api.pull(pr)
    body=initial.get('body') or ''
    since=api.intent_time(pr,body,sha)
    def current():
        p=api.pull(pr)
        if p.get('state')!='open' or p.get('head',{}).get('sha')!=sha or (p.get('body') or '')!=body:
            raise b.Refused('probe head or intent changed')
    deadline=time.monotonic()+1200
    while True:
        current();comments=api.comments(pr)
        results={kind:b.decision(comments,kind,sha,since['since'],require_start=since['edited'],repo=repo,pr=int(pr)) for kind in ['code','security']}
        # Match production policy: clean binding code review and a completed,
        # separately authenticated advisory scan. Unknown findings are never clean.
        if results['code']=='pass' and results['security'] in ('pass','findings','completed_findings_unknown'):break
        if results['code'] in ('findings','failed') or results['security']=='failed' or time.monotonic()>=deadline:
            print(json.dumps({'probeOnly':True,'head':sha,'results':results}),flush=True)
            raise b.Refused('app did not produce clean code review and a completed advisory scan on this head; inspect findings/automation/access')
        time.sleep(min(45,max(0,deadline-time.monotonic())))
    current()
    print(json.dumps({'probeOnly':True,'head':sha,'results':results}),flush=True)
    if results['security']=='completed_findings_unknown':
        print('::warning::Security scan completed but finding flags are unavailable; no clean security verdict is claimed.')
    print('App receipts verified. This does not replace current-head human review of this workflow PR.')

if __name__=='__main__':
    try:main()
    except b.Refused as e:print('::error::'+str(e));sys.exit(1)
