#!/usr/bin/env python3
"""Non-gating integration probe for workflow-token requests to OpenAI's app.
It cannot issue a production review verdict or bypass workflow human approval.
"""
import hashlib
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
    api=b.GitHub(repo)
    initial=api.pull(pr)
    context=hashlib.sha256((initial.get('body') or '').encode()).hexdigest()
    def current():
        p=api.pull(pr)
        if p.get('state')!='open' or p.get('head',{}).get('sha')!=sha or hashlib.sha256((p.get('body') or '').encode()).hexdigest()!=context:
            raise b.Refused('probe head or intent changed')
    for kind in ['code','security']:
        current();body=b.request_body(kind,sha,context)
        request=b.request_of(api.comments(pr),body) if kind=='code' else None
        if request is None:request=api.call(f'issues/{pr}/comments',{'body':body})
        if not b.trusted(request,actions=True):raise b.Refused('probe requires the workflow GitHub token')
        end=time.monotonic()+360;saw_running=False
        while True:
            current();comments=api.comments(pr);found=b.summary_of(comments,kind,sha)
            if found and found['state'].get('status')=='running' and b.stamp(found['comment']['updated_at'])>=b.stamp(request['created_at']):saw_running=True
            result=b.decision(comments,kind,sha,request,saw_running)
            if result!='pending' or time.monotonic()>=end:break
            time.sleep(5)
        current()
        print(json.dumps({'probeOnly':True,'kind':kind,'head':sha,'requestId':request['id'],'result':result,'observedRunning':saw_running}),flush=True)
        if result!='pass':raise b.Refused('app transport did not produce a clean current-head result; inspect the app request/access')
    print('Transport verified. This does not replace the production human-review requirement for this workflow PR.')

if __name__=='__main__':
    try:main()
    except b.Refused as e:print('::error::'+str(e));sys.exit(1)
