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
        results={kind:b.decision(comments,kind,sha,since['since'],require_start=since['edited']) for kind in ['code','security']}
        if all(value=='pass' for value in results.values()):break
        if any(value in ('findings','failed') for value in results.values()) or time.monotonic()>=deadline:
            print(json.dumps({'probeOnly':True,'head':sha,'results':results}),flush=True)
            raise b.Refused('app did not produce clean current-head results; inspect review findings/automation/access')
        time.sleep(min(45,max(0,deadline-time.monotonic())))
    current()
    print(json.dumps({'probeOnly':True,'head':sha,'results':results}),flush=True)
    print('App receipts verified. This does not replace current-head human review of this workflow PR.')

if __name__=='__main__':
    try:main()
    except b.Refused as e:print('::error::'+str(e));sys.exit(1)
