#!/usr/bin/env python3
"""Bridge OpenAI's GitHub app receipts to the existing review jobs. Never execute PR code."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

APP_ID, BOT_ID = 1144995, 199175422
SUMMARY = '<!-- codex-pull-request-review-summary -->'
STATE = re.compile(r'<!-- codex-review-state:v1 (.+) -->')
SECURITY_STATE = re.compile(r'<!-- codex-security-review:v1 (.+) -->')
SHA = re.compile(r'[0-9a-f]{40}')
REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
RULES = Path(__file__).with_name('codex-rules.md').read_text()

class Refused(Exception):
    pass

def strict_json(text):
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out: raise ValueError('duplicate key')
            out[k] = v
        return out
    def constant(_): raise ValueError('non-JSON number')
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)

def source_bot(comment):
    user=comment.get('user') or {}
    return user.get('type')=='Bot' and user.get('id')==BOT_ID and user.get('login')=='chatgpt-codex-connector[bot]'

def origin(comment):
    return source_bot(comment) and (comment.get('performed_via_github_app') or {}).get('id')==APP_ID

def trusted(comment):
    return origin(comment) and comment.get('_history_verified') is True

class ReviewChanged(Refused):
    pass

def stamp(text):
    try:
        value = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
        if value.tzinfo is None: raise ValueError()
        return value.timestamp()
    except (AttributeError, TypeError, ValueError):
        raise Refused('unreadable review timestamp') from None

def summary_of(comments, kind, sha):
    summaries = [c for c in comments if origin(c) and isinstance(c.get('body'), str) and c['body'].splitlines()[:1] == [SUMMARY]]
    if not summaries: return None
    if len(summaries) != 1: raise Refused('ambiguous Codex summaries')
    latest = summaries[0]
    if not trusted(latest): raise Refused('Codex summary edit history is untrusted')
    lines = latest['body'].splitlines()
    if len(lines) < 2 or len(latest['body']) > 65536: raise Refused('unreadable Codex summary')
    match = STATE.fullmatch(lines[1])
    if not match: raise Refused('unknown Codex summary format')
    try: data = strict_json(match[1])
    except (ValueError, TypeError): raise Refused('invalid Codex summary JSON') from None
    if not isinstance(data, dict) or set(data) != {'code_review', 'security_review'}: raise Refused('unknown Codex review state')
    state = data.get(kind + '_review')
    if not isinstance(state, dict): raise Refused('missing Codex review kind')
    if state.get('commit_sha') != sha: return None
    if state.get('status') == 'completed':
        null_security = kind == 'security' and all(k in state and state[k] is None for k in ('has_any_findings','has_unresolved_findings'))
        if not null_security and (type(state.get('has_any_findings')) is not bool or type(state.get('has_unresolved_findings')) is not bool):
            raise Refused('missing Codex finding flags')
    return {'comment': latest, 'state': state}

RULES_BEGIN, RULES_END = '<!-- airlock:codex-review:begin -->', '<!-- airlock:codex-review:end -->'

def rules_block():
    return RULES_BEGIN + '\n## Code Review Rules\n\n' + RULES.rstrip() + '\n' + RULES_END

def has_rules(text):
    if text.count(RULES_BEGIN) != 1 or text.count(RULES_END) != 1: return False
    begin, end = text.index(RULES_BEGIN), text.index(RULES_END) + len(RULES_END)
    if begin >= end: return False
    block = text[begin:end]
    normalize = lambda value: re.sub(r'[ \t\r\n]+', ' ', value).strip()
    return normalize(block) == normalize(rules_block())

def fresh_receipt(comments, sha, since):
    # A short SHA is never sufficient alone: this only establishes completion after our request;
    # summary_of must independently match the complete 40-character head SHA.
    for c in comments:
        body = c.get('body', '')
        if not trusted(c) or not isinstance(body, str) or body.splitlines()[:1] != ['<!-- codex-review-receipt:v1 -->']: continue
        if stamp(c.get('created_at')) < since: continue
        matches = re.findall(r'^\*\*Reviewed commit:\*\* `([0-9a-f]{10,40})`\s*$', body, re.M)
        if len(matches) == 1 and sha.startswith(matches[0]): return True
    return False

def code_completed_at(found, sha):
    # The summary's updated_at is shared with Security Review. Use the Code Review row's
    # own service-generated completion time, cross-checked against its full-SHA machine state.
    rows=[]
    for line in found['comment']['body'].splitlines():
        cells=[c.strip() for c in line.split('|')]
        if len(cells)==6 and cells[1]=='📝 **Code Review**': rows.append(cells)
    if len(rows)!=1: raise Refused('Code Review completion metadata unavailable')
    value=re.fullmatch(r'✅ \*\*Completed\*\* <relative-time datetime="([^"]+)">[^<]*</relative-time>',rows[0][2])
    short=re.fullmatch(r'`([0-9a-f]{7,40})`',rows[0][3])
    if not value or not short or not sha.startswith(short[1]): raise Refused('Code Review completion metadata invalid')
    return stamp(value[1])

def code_started_at(found, sha):
    starts=[]
    for version in found['comment'].get('_review_versions', []):
        snapshot={**found['comment'], 'body':version['body'], 'updated_at':version['at'], '_review_versions':[]}
        prior=summary_of([snapshot], 'code', sha)
        if not prior or prior['state'].get('status')!='running': continue
        rows=[[c.strip() for c in line.split('|')] for line in version['body'].splitlines()]
        rows=[c for c in rows if len(c)==6 and c[1]=='📝 **Code Review**']
        if len(rows)!=1: continue
        value=re.fullmatch(r'🔄 \*\*Running\*\* since <relative-time datetime="([^"]+)">[^<]*</relative-time>',rows[0][2])
        short=re.fullmatch(r'`([0-9a-f]{7,40})`', rows[0][3])
        if value and short and sha.startswith(short[1]): starts.append(stamp(value[1]))
    return max(starts) if starts else None

def security_scan_only(found, sha, repo, pr):
    # Observed Security Review v1 can publish a completed scan with null finding
    # flags. Its separate envelope proves completion, never absence of findings.
    lines=found['comment']['body'].splitlines()
    match=SECURITY_STATE.fullmatch(lines[2]) if len(lines)>2 else None
    if not match: raise Refused('security finding flags unavailable and scan completion unproven')
    try: scan=strict_json(match[1])
    except (ValueError,TypeError): raise Refused('invalid security scan metadata') from None
    if not isinstance(scan,dict) or set(scan)!={'blockingSeverityThreshold','headSha','mergeGateEnabled','pullRequestNumber','repository','status'}:
        raise Refused('unknown security scan metadata')
    if not REPO.fullmatch(repo or '') or type(pr) is not int or pr<1 or scan['repository']!=repo or type(scan['pullRequestNumber']) is not int or scan['pullRequestNumber']!=pr or scan['headSha']!=sha:
        raise Refused('security scan context mismatch')
    if scan['status']!='completed' or scan['mergeGateEnabled'] is not False or scan['blockingSeverityThreshold'] not in ('P0','P1','P2','P3'):
        raise Refused('advisory security scan completion unproven')
    return 'completed_findings_unknown'

def decision(comments, kind, sha, intent_since, require_start=True, repo=None, pr=None):
    found = summary_of(comments, kind, sha)
    if not found: return 'pending'
    state = found['state']
    if state.get('status') in ('failed', 'cancelled', 'error'): return 'failed'
    if state.get('status') != 'completed': return 'pending'
    if kind=='security' and state['has_any_findings'] is None:
        return security_scan_only(found,sha,repo,pr)
    if state['has_any_findings'] or state['has_unresolved_findings']: return 'findings'
    if kind == 'code':
        completed = code_completed_at(found, sha)
        started = code_started_at(found, sha)
        if completed < intent_since: return 'pending'
        if started is not None and started > completed: return 'pending'
        if require_start and (started is None or started < intent_since): return 'pending'
        # Automatic reviews may omit the standalone receipt. A full-head running
        # snapshot authenticated through edit history proves freshness directly.
        if not fresh_receipt(comments, sha, intent_since) and (started is None or started < intent_since): return 'pending'
    return 'pass'

def agent(login, pattern):
    return re.fullmatch(re.escape(pattern).replace(r'\*', '.*'), login, re.I) is not None

def protected(files):
    return any(p.startswith('.github/workflows/') or p.split('/')[-1] == 'AGENTS.md' for p in files)

class GitHub:
    def __init__(self, repo):
        self.root = 'repos/' + repo
        self._history_cache = {}
    def call(self, path, pages=False):
        args = ['gh', 'api', '--method', 'GET', self.root + '/' + path]
        if pages: args += ['--paginate', '--slurp']
        try: r = subprocess.run(args, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired): raise Refused('GitHub request unavailable') from None
        if r.returncode or len(r.stdout) > 15000000: raise Refused('GitHub review data unavailable')
        try: value = strict_json(r.stdout)
        except ValueError: raise Refused('GitHub returned unreadable review data') from None
        if pages:
            if not isinstance(value, list) or any(not isinstance(p, list) for p in value): raise Refused('incomplete GitHub pages')
            value = [item for page in value for item in page]
            if len(value) > 5000: raise Refused('review data limit exceeded')
        return value
    def review_rules(self, base_sha):
        import base64
        if not SHA.fullmatch(base_sha or ''): raise Refused('base commit unavailable')
        tree = self.call('git/trees/' + base_sha)
        if tree.get('truncated') is not False: raise Refused('base tree incomplete')
        entries = [e for e in tree.get('tree', []) if e.get('path') == 'AGENTS.md']
        if len(entries) != 1 or entries[0].get('mode') not in ('100644', '100755') or entries[0].get('type') != 'blob':
            raise Refused('install the reviewed Codex charter rules in base AGENTS.md before enabling this provider')
        blob = self.call('git/blobs/' + entries[0]['sha'])
        try:
            if blob.get('encoding') != 'base64' or blob.get('size', 1000001) > 1000000: raise ValueError()
            text = base64.b64decode(blob['content']).decode('utf8')
        except (KeyError, ValueError, UnicodeError): raise Refused('base AGENTS.md unreadable') from None
        if not has_rules(text): raise Refused('base AGENTS.md lacks the reviewed Codex charter rules')
    def intent_time(self, pr, body, sha):
        owner, name = self.root[6:].split('/')
        query = 'query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){body headRefOid createdAt lastEditedAt}}}'
        try:
            r = subprocess.run(['gh','api','graphql','-f','query='+query,'-f','owner='+owner,'-f','name='+name,'-F','number='+str(pr)],capture_output=True,text=True,timeout=30)
            if r.returncode: raise ValueError()
            value = strict_json(r.stdout)['data']['repository']['pullRequest']
            if value['body'] != body or value['headRefOid'] != sha: raise Refused('PR head or intent changed')
            return {'since':stamp(value.get('lastEditedAt') or value['createdAt']), 'edited':value.get('lastEditedAt') is not None}
        except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired): raise Refused('PR intent timestamp unavailable') from None
    def pull(self, pr): return self.call(f'pulls/{pr}')
    def graphql(self, query, variables):
        try:
            r = subprocess.run(['gh','api','graphql','--input','-'], input=json.dumps({'query':query,'variables':variables}), capture_output=True,text=True,timeout=30)
            if r.returncode or len(r.stdout)>15000000: raise ValueError()
            result=strict_json(r.stdout)
            if result.get('errors'): raise ValueError()
            return result['data']
        except (ValueError,KeyError,OSError,subprocess.TimeoutExpired): raise Refused('comment edit history unavailable') from None
    def _comment_evidence(self, raw):
        app=[c for c in raw if source_bot(c)]
        if any(not origin(c) for c in app):raise Refused('OpenAI bot comment app identity unavailable')
        if len(app)>1000: raise Refused('app comment limit exceeded')
        for offset in range(0,len(app),50):
            chunk=app[offset:offset+50]
            ids=[c.get('node_id') for c in chunk]
            if any(not isinstance(i,str) for i in ids): raise Refused('comment identity unavailable')
            query='query($ids:[ID!]!){nodes(ids:$ids){... on IssueComment{id body updatedAt lastEditedAt editor{__typename ... on Bot{databaseId}} userContentEdits(first:1){totalCount}}}}'
            nodes=self.graphql(query,{'ids':ids})['nodes']
            if len(nodes)!=len(chunk): raise Refused('incomplete comment metadata')
            for c,node in zip(chunk,nodes):
                if not node or node.get('id')!=c['node_id'] or node.get('body')!=c['body'] or stamp(node['updatedAt'])!=stamp(c['updated_at']):
                    raise ReviewChanged('app comment changed while validating')
                count=node['userContentEdits']['totalCount']
                if type(count) is not int or not 0<=count<=1000: raise Refused('comment history limit exceeded')
                key=(node['id'],node['updatedAt'],node['lastEditedAt'],count,hashlib.sha256(node['body'].encode()).hexdigest())
                if key in self._history_cache:
                    c['_review_versions']=self._history_cache[key];c['_history_verified']=True;continue
                versions=[]
                if count:
                    editor=node.get('editor') or {}
                    if editor.get('__typename')!='Bot' or editor.get('databaseId')!=BOT_ID: raise Refused('app comment edited outside OpenAI')
                    cursor=None
                    while True:
                        q='query($id:ID!,$cursor:String){node(id:$id){... on IssueComment{userContentEdits(first:100,after:$cursor){totalCount nodes{diff editedAt editor{__typename ... on Bot{databaseId}}} pageInfo{hasNextPage endCursor}}}}}'
                        history=self.graphql(q,{'id':node['id'],'cursor':cursor})['node']['userContentEdits']
                        if history['totalCount']!=count: raise ReviewChanged('app comment history changed')
                        for edit in history['nodes']:
                            editor=edit.get('editor') or {}
                            if editor.get('__typename')!='Bot' or editor.get('databaseId')!=BOT_ID or not isinstance(edit.get('diff'),str):
                                raise Refused('app comment history contains a non-OpenAI or unreadable edit')
                            stamp(edit['editedAt'])
                            versions.append({'body':edit['diff'],'at':edit['editedAt']})
                        if len(versions)>1000: raise Refused('comment history limit exceeded')
                        page=history['pageInfo']
                        if not page['hasNextPage']:break
                        if not page.get('endCursor') or page['endCursor']==cursor:raise Refused('comment history incomplete')
                        cursor=page['endCursor']
                    if len(versions)!=count or not versions or versions[0]['body']!=node['body'] or stamp(versions[0]['at'])!=stamp(node['lastEditedAt']):
                        raise ReviewChanged('app comment changed while validating history')
                elif node.get('lastEditedAt') is not None or node.get('editor') is not None:
                    raise Refused('comment edit metadata inconsistent')
                c['_review_versions']=versions;c['_history_verified']=True;self._history_cache[key]=versions
        return raw
    def comments(self, pr):
        for _ in range(2):
            raw=self.call(f'issues/{pr}/comments?per_page=100',pages=True)
            # Never accept a verification flag from a transport response.
            for c in raw:c['_history_verified']=False;c['_review_versions']=[]
            try:return self._comment_evidence(raw)
            except ReviewChanged:continue
        raise Refused('app comment did not stabilize')
    def files(self, pr, count):
        entries = self.call(f'pulls/{pr}/files?per_page=100', pages=True)
        if len(entries) != count or count > 3000: raise Refused('PR paths are incomplete')
        paths = []
        for f in entries:
            if not isinstance(f.get('filename'), str): raise Refused('unreadable PR path')
            paths.append(f['filename'])
            if f.get('previous_filename'): paths.append(f['previous_filename'])
        return paths
    def approved(self, pr, sha):
        # Configured service identities are machines even when GitHub calls them User.
        try:
            reviews=self.call(f'pulls/{pr}/reviews?per_page=100',pages=True)
            latest={}
            for r in sorted(reviews,key=lambda x:(x.get('submitted_at') or '',x.get('id',0))):
                user=r.get('user') or {};login=user.get('login')
                if not isinstance(login,str) or user.get('type')!='User' or login.lower().endswith('[bot]') or agent(login,os.environ.get('AGENTS','*[bot]')):continue
                if r.get('state') in ('APPROVED','CHANGES_REQUESTED','DISMISSED'):latest[login]=r
            approved=False
            for login,r in latest.items():
                permission=self.call('collaborators/'+login+'/permission').get('permission')
                if permission not in ('admin','maintain','write'):continue
                if r['state']=='CHANGES_REQUESTED':return False
                if r['state']=='APPROVED' and r.get('commit_id')==sha:approved=True
            return approved
        except Refused:return False

def signal_problem(pull, api, binding=False):
    body = pull.get('body') or ''
    section = re.search(r'^## Signal\s*\n(.*?)(?=^## |\Z)', body, re.M | re.S)
    if not section or not re.sub(r'<!--.*?-->', '', section[1], flags=re.S).strip(): return 'missing nonempty ## Signal'
    # The app still reviews the meaning, intent, proof and floors. This deterministic part cannot be waived by a receipt.
    links = set(re.findall(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)', section[1]))
    if not binding and not (pull.get('user') or {}).get('login', '').endswith('[bot]'): return None
    for repo, number in links:
        issue = GitHub(repo).call(f'issues/{number}') if api is None else api(repo, number)
        if issue.get('state') != 'open' or not any(x.get('name') == 'tool-request' for x in issue.get('labels', [])): continue
        directive = re.compile(r'(?im)^\s*(?:[-*]\s*)?(?:closes|fixes|resolves|part of)\s+' + re.escape(repo + '#' + number) + r'(?![0-9])')
        if not directive.search(body): return 'linked open tool-request needs Closes or Part of'
    return None

def main():
    repo, raw_pr, sha = (os.environ.get(k, '') for k in ('REPO', 'PR', 'SHA'))
    kind = os.environ.get('KIND', 'code')
    if not REPO.fullmatch(repo) or not re.fullmatch(r'[1-9][0-9]*', raw_pr) or not SHA.fullmatch(sha) or kind not in ('code', 'security'):
        raise Refused('invalid review context')
    wait = os.environ.get('WAIT', '900')
    if not re.fullmatch(r'[0-9]{1,4}', wait) or not 0 <= int(wait) <= 900: raise Refused('invalid review wait')
    api, pr = GitHub(repo), int(raw_pr)
    expected_body = None
    def current():
        pull = api.pull(pr)
        if pull.get('state') != 'open' or pull.get('head', {}).get('sha') != sha: raise Refused('PR head moved; refusing a stale verdict')
        if expected_body is not None and hashlib.sha256((pull.get('body') or '').encode()).hexdigest() != expected_body:
            raise Refused('PR body changed; refusing an outdated intent review')
        return pull
    pull = current()
    expected_body = hashlib.sha256((pull.get('body') or '').encode()).hexdigest()
    author = pull.get('user', {}).get('login')
    if not isinstance(author, str): raise Refused('PR author unavailable')
    binding = agent(author, os.environ.get('AGENTS', '*[bot]'))
    machine = binding or author.endswith('[bot]') or pull.get('user',{}).get('type')=='Bot'
    paths = api.files(pr, pull.get('changed_files'))
    if protected(paths):
        current()
        if machine and not api.approved(pr, sha): raise Refused('workflow or review-rule change requires a human approval on this commit')
        current()
        print('No model verdict: workflow/review-rule change; its human reviewer owns this commit.')
        return 0
    if not machine and paths and all(p.endswith('.md') for p in paths):
        current();print('PASS: Markdown-only PR by a person; its merger is its reviewer.');return 0
    if kind == 'code' and binding and api.approved(pr, sha):
        current();print('PASS: a writer approved this exact commit.');return 0
    problem = signal_problem(pull, None, binding) if kind == 'code' else None
    if problem:
        current();print(('::error::' if binding else '::warning::') + problem)
        if binding: return 1
    api.review_rules(pull.get('base', {}).get('sha'))
    intent = api.intent_time(pr, pull.get('body') or '', sha)
    deadline = time.monotonic() + int(wait)
    while True:
        current()
        comments = api.comments(pr)
        result = decision(comments, kind, sha, intent['since'], require_start=intent['edited'], repo=repo, pr=pr)
        if result != 'pending' or time.monotonic() >= deadline: break
        time.sleep(min(45, max(0, deadline - time.monotonic())))
    intent = api.intent_time(pr, pull.get('body') or '', sha)
    result = decision(api.comments(pr), kind, sha, intent['since'], require_start=intent['edited'], repo=repo, pr=pr)
    current()  # Recheck evidence, head and body before returning; update handler also refreshes after completion.
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    message = f'Codex GitHub app {kind} review: {result}; commit {sha}.'
    if summary:
        with open(summary, 'a') as f: f.write(message + '\n')
    print(message)
    if result == 'pass': return 0
    if kind == 'code' and api.approved(pr, sha):
        current();print('PASS: a writer took responsibility for this exact commit.');return 0
    if kind == 'security' and result == 'findings':
        print('::warning::Codex Security reported findings; this scan is advisory.');return 0
    if kind == 'security' and result == 'completed_findings_unknown':
        print('::warning::Codex Security scan completed; finding flags are unavailable. Inspect the security report. No clean verdict is claimed.');return 0
    if kind == 'code' and not binding:
        print('::warning::No passing Codex verdict; the person merging this PR is its reviewer.');return 0
    raise Refused('Codex review has no clean, completed verdict on this exact head')

if __name__ == '__main__':
    try: sys.exit(main())
    except Refused as e:
        print('::error::' + str(e));sys.exit(1)
