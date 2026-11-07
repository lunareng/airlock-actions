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

def trusted(comment):
    user = comment.get('user') or {}
    app = comment.get('performed_via_github_app') or {}
    return (user.get('type') == 'Bot' and user.get('id') == BOT_ID and
            user.get('login') == 'chatgpt-codex-connector[bot]' and app.get('id') == APP_ID)

def stamp(text):
    try:
        value = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
        if value.tzinfo is None: raise ValueError()
        return value.timestamp()
    except (AttributeError, TypeError, ValueError):
        raise Refused('unreadable review timestamp') from None

def summary_of(comments, kind, sha):
    summaries = [c for c in comments if trusted(c) and isinstance(c.get('body'), str) and c['body'].splitlines()[:1] == [SUMMARY]]
    if not summaries: return None
    latest = max(summaries, key=lambda c: (stamp(c.get('updated_at')), c.get('id', 0)))
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
        if type(state.get('has_any_findings')) is not bool or type(state.get('has_unresolved_findings')) is not bool:
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

def decision(comments, kind, sha, intent_since):
    found = summary_of(comments, kind, sha)
    if not found: return 'pending'
    state = found['state']
    if state.get('status') in ('failed', 'cancelled', 'error'): return 'failed'
    if state.get('status') != 'completed': return 'pending'
    if state['has_any_findings'] or state['has_unresolved_findings']: return 'findings'
    if kind == 'code' and (code_completed_at(found, sha) < intent_since or not fresh_receipt(comments, sha, intent_since)):
        return 'pending'
    return 'pass'

def agent(login, pattern):
    return re.fullmatch(re.escape(pattern).replace(r'\*', '.*'), login) is not None

def protected(files):
    return any(p.startswith('.github/workflows/') or p.split('/')[-1] == 'AGENTS.md' for p in files)

class GitHub:
    def __init__(self, repo): self.root = 'repos/' + repo
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
            return stamp(value.get('lastEditedAt') or value['createdAt'])
        except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired): raise Refused('PR intent timestamp unavailable') from None
    def pull(self, pr): return self.call(f'pulls/{pr}')
    def comments(self, pr): return self.call(f'issues/{pr}/comments?per_page=100', pages=True)
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
        env = {**os.environ, 'REPO': self.root[6:], 'PR': str(pr), 'SHA': sha}
        try: r = subprocess.run(['bash', str(Path(__file__).with_name('approved.sh'))], env=env, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired): return False
        return r.returncode == 0

def signal_problem(pull, api):
    body = pull.get('body') or ''
    section = re.search(r'^## Signal\s*\n(.*?)(?=^## |\Z)', body, re.M | re.S)
    if not section or not re.sub(r'<!--.*?-->', '', section[1], flags=re.S).strip(): return 'missing nonempty ## Signal'
    # The app still reviews the meaning, intent, proof and floors. This deterministic part cannot be waived by a receipt.
    links = set(re.findall(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)', section[1]))
    if not (pull.get('user') or {}).get('login', '').endswith('[bot]'): return None
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
    paths = api.files(pr, pull.get('changed_files'))
    if protected(paths):
        current()
        if author.endswith('[bot]') and not api.approved(pr, sha): raise Refused('workflow or review-rule change requires a human approval on this commit')
        current()
        print('No model verdict: workflow/review-rule change; its human reviewer owns this commit.')
        return 0
    if not author.endswith('[bot]') and paths and all(p.endswith('.md') for p in paths):
        current();print('PASS: Markdown-only PR by a person; its merger is its reviewer.');return 0
    if kind == 'code' and binding and api.approved(pr, sha):
        current();print('PASS: a writer approved this exact commit.');return 0
    problem = signal_problem(pull, None) if kind == 'code' else None
    if problem:
        current();print(('::error::' if binding else '::warning::') + problem)
        if binding: return 1
    api.review_rules(pull.get('base', {}).get('sha'))
    intent_since = api.intent_time(pr, pull.get('body') or '', sha)
    deadline = time.monotonic() + int(wait)
    while True:
        current()
        comments = api.comments(pr)
        result = decision(comments, kind, sha, intent_since)
        if result != 'pending' or time.monotonic() >= deadline: break
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    current()  # Never write an old head's result after polling or a last-moment push.
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
    if kind == 'code' and not binding:
        print('::warning::No passing Codex verdict; the person merging this PR is its reviewer.');return 0
    raise Refused('Codex review has no clean, completed verdict on this exact head')

if __name__ == '__main__':
    try: sys.exit(main())
    except Refused as e:
        print('::error::' + str(e));sys.exit(1)
