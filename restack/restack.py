#!/usr/bin/env python3
"""Prepare a whole PR stack, prove its patches unchanged, then publish atomically.

Only Git and the GitHub API see repository data; no repository program or hook runs.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
MAX_OPEN = 100
MAX_STACK = 20
MAX_COMMITS = 100


class Refused(Exception):
    def __init__(self, reason, pr=None, commit=None, files=()):
        self.reason, self.pr, self.commit, self.files = reason, pr, commit, list(files)
        super().__init__(reason)


class NotFound(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, repository, token):
        self.repository, self.token = repository, token
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, method, path, body=None):
        req = urllib.request.Request(
            'https://api.github.com/repos/' + self.repository + ('/' + path if path else ''),
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28'},
        )
        try:
            with self.opener.open(req, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound() from None
            raise Refused('github_request_refused') from None
        except (OSError, ValueError):
            raise Refused('github_unreadable') from None

    def pages(self, path):
        out = []
        for page in range(1, 11):
            sep = '&' if '?' in path else '?'
            rows = self.request('GET', f'{path}{sep}per_page=100&page={page}')
            if not isinstance(rows, list):
                raise Refused('github_unreadable')
            out.extend(rows)
            if len(rows) < 100:
                return out
        raise Refused('github_truncated')

    def repo(self):
        return self.request('GET', '')

    def pull(self, number):
        return self.request('GET', f'pulls/{number}')

    def pulls(self):
        return self.pages('pulls?state=open')

    def branch(self, name):
        try:
            return self.request('GET', 'branches/' + urllib.parse.quote(name, safe=''))
        except NotFound:
            return None

    def retarget(self, number, base):
        return self.request('PATCH', f'pulls/{number}', {'base': base})

    def warn(self, number, body, marker, app_id):
        comments = self.pages(f'issues/{number}/comments')
        if any((c.get('performed_via_github_app') or {}).get('id') == app_id
               and (c.get('user') or {}).get('type') == 'Bot' and marker in (c.get('body') or '') for c in comments):
            return
        self.request('POST', f'issues/{number}/comments', {'body': body})


class Git:
    def __init__(self, directory, repository, token, remote=None):
        control = Path(directory)
        self.directory = control / 'repository'
        self.directory.mkdir()
        # Do not inherit Git config, tracing, credential helpers, filters, hooks or repo-selection env.
        self.env = {k: v for k, v in os.environ.items() if k in ('PATH', 'SYSTEMROOT', 'TMPDIR')}
        self.env.update({'HOME': str(control), 'GIT_CONFIG_NOSYSTEM': '1',
                         'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
                         'GIT_AUTHOR_NAME': 'Stack maintenance', 'GIT_AUTHOR_EMAIL': 'restack@example.invalid',
                         'GIT_COMMITTER_NAME': 'Stack maintenance', 'GIT_COMMITTER_EMAIL': 'restack@example.invalid',
                         'GIT_EDITOR': 'true', 'GIT_SEQUENCE_EDITOR': 'true', 'LC_ALL': 'C'})
        self.run('init', '--quiet', '--template=')
        for key, value in [('core.hooksPath', os.devnull), ('core.fsmonitor', 'false'),
                           ('core.attributesFile', os.devnull), ('rerere.enabled', 'false'),
                           ('credential.helper', ''), ('commit.gpgsign', 'false'),
                           ('http.followRedirects', 'false'), ('protocol.allow', 'never'),
                           ('protocol.https.allow', 'always')]:
            self.run('config', key, value)
        if remote is not None:  # Test injection only; the CLI never accepts a remote URL/path.
            self.run('config', 'protocol.file.allow', 'always')
        else:
            # Keep executable helpers outside the tree: checkout --force can replace any tracked path.
            helper = control / 'askpass'
            helper.write_text('#!/bin/sh\ncase "$1" in *Username*) echo x-access-token;; *) printf "%s\\n" "$RESTACK_TOKEN";; esac\n')
            helper.chmod(0o700)
            self.env.update({'GIT_ASKPASS': str(helper), 'RESTACK_TOKEN': token})
        self.run('remote', 'add', 'origin', remote or f'https://github.com/{repository}.git')

    def run(self, *args, check=True):
        try:
            p = subprocess.run(['git', *args], cwd=self.directory, env=self.env,
                               capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            raise Refused('git_unavailable') from None
        if check and p.returncode:
            # Git stderr may include untrusted commit text or credential-bearing remote errors.
            raise Refused('git_refused')
        return p

    def text(self, *args):
        return self.run(*args).stdout.decode('utf-8', errors='replace').strip()

    def valid_branch(self, name):
        if not isinstance(name, str) or not name or self.run('check-ref-format', 'refs/heads/' + name, check=False).returncode:
            raise Refused('invalid_branch')
        return name

    def fetch(self, default, root, pulls):
        refs = [f'+refs/heads/{default}:refs/restack/base', f'+refs/pull/{root["number"]}/head:refs/restack/root']
        refs += [f'+refs/pull/{p["number"]}/head:refs/restack/pr/{p["number"]}' for p in pulls]
        self.run('fetch', '--quiet', '--no-tags', '--no-recurse-submodules', 'origin', *refs)
        if self.text('rev-parse', 'refs/restack/root') != root['head']['sha']:
            raise Refused('root_moved')
        for p in pulls:
            if self.text('rev-parse', f'refs/restack/pr/{p["number"]}') != p['head']['sha']:
                raise Refused('head_moved', p['number'], p['head']['sha'])
        return self.text('rev-parse', 'refs/restack/base')

    def ancestor(self, older, newer):
        p = self.run('merge-base', '--is-ancestor', older, newer, check=False)
        if p.returncode not in (0, 1):
            raise Refused('history_unreadable')
        return p.returncode == 0

    def prepare(self, parent, target, head, number):
        if not self.ancestor(parent, head):
            raise Refused('parent_not_in_history', number, head)
        old = f'{parent}..{head}'
        count = int(self.text('rev-list', '--count', old))
        if count > MAX_COMMITS:
            raise Refused('too_many_commits', number, head)
        if self.text('rev-list', '--merges', old):
            raise Refused('merge_commits', number, head)
        self.run('checkout', '--quiet', '--detach', '--force', head)
        p = self.run('rebase', '--no-fork-point', '--no-autosquash', '--no-update-refs', '--no-autostash',
                     '--reapply-cherry-picks', '--empty=keep', '--keep-empty', '--onto', target, parent,
                     check=False)
        if p.returncode:
            stopped = self.run('rev-parse', '--verify', 'REBASE_HEAD', check=False).stdout.decode().strip()
            files = self.run('diff', '--name-only', '--diff-filter=U', '-z').stdout.decode('utf-8', errors='replace').split('\0')
            self.run('rebase', '--abort', check=False)
            raise Refused('conflict' if any(files) else 'rebase_refused', number,
                          stopped if SHA.fullmatch(stopped) else head, [f for f in files if f])
        new = self.text('rev-parse', 'HEAD')
        comparison = self.text('range-diff', '--no-color', '--no-dual-color', '--no-ext-diff', old, f'{target}..{new}')
        rows = comparison.splitlines() if comparison else []
        if len(rows) != count or any(not re.fullmatch(r'\s*\d+:\s+[0-9a-f]+\s+=\s+\d+:\s+[0-9a-f]+(?:\s+.*)?', row) for row in rows):
            files = self.text('diff', '--name-only', head, new).splitlines()
            raise Refused('patch_changed', number, head, files)
        return new

    def push(self, nodes):
        changed = [n for n in nodes if n['old'] != n['new']]
        if not changed:
            return
        leases = [f'--force-with-lease=refs/heads/{n["branch"]}:{n["old"]}' for n in changed]
        refs = [f'{n["new"]}:refs/heads/{n["branch"]}' for n in changed]
        self.run('push', '--porcelain', '--atomic', *leases, 'origin', *refs)


def same_repo(pull, repo):
    return (pull.get('head', {}).get('repo') or {}).get('id') == repo['id'] and (pull.get('base', {}).get('repo') or {}).get('id') == repo['id']


def checked_pull(p):
    if (type(p.get('number')) is not int or p['number'] <= 0
            or not SHA.fullmatch(str((p.get('head') or {}).get('sha', '')))
            or not isinstance((p.get('head') or {}).get('ref'), str)
            or not isinstance((p.get('base') or {}).get('ref'), str)):
        raise Refused('pull_unreadable')
    return p


def warning(api, root, refusal, app_id, dry_run):
    if dry_run or not refusal.pr:
        return
    marker = f'<!-- airlock:restack root={root["number"]} head={refusal.commit or root["head"]["sha"]} reason={refusal.reason} -->'
    files = '\n'.join(json.dumps(f, ensure_ascii=True).replace('`', '\\u0060') for f in refusal.files) or '(none available)'
    body = (f'Restacking stopped: **{refusal.reason}**.\n\nCommit: `{refusal.commit or root["head"]["sha"]}`.\n\n'
            f'Files:\n```text\n{files}\n```\n\nNo branch was pushed by this preparation. Resolve the stack manually before retrying.\n\n{marker}')
    api.warn(refusal.pr, body, marker, app_id)


def restack(api, git, root_number, app_id, dry_run=True):
    repo = api.repo()
    if type(repo.get('id')) is not int or not isinstance(repo.get('default_branch'), str):
        raise Refused('repo_unreadable')
    default = git.valid_branch(repo['default_branch'])
    root = checked_pull(api.pull(root_number))
    if root.get('state') != 'closed' or root.get('merged') is not True or root['base']['ref'] != default:
        raise Refused('root_not_merged_to_default')
    if not same_repo(root, repo):
        return {'status': 'skipped', 'reason': 'fork_root', 'nodes': []}
    if not SHA.fullmatch(str(root.get('merge_commit_sha', ''))):
        raise Refused('merge_unreadable')
    git.valid_branch(root['head']['ref'])
    root_branch = api.branch(root['head']['ref'])
    if root_branch is not None and root_branch.get('commit', {}).get('sha') != root['head']['sha']:
        raise Refused('root_branch_reused')
    pulls = [checked_pull(p) for p in api.pulls() if same_repo(p, repo)]
    if len(pulls) > MAX_OPEN:
        raise Refused('too_many_open_pulls')
    for p in pulls:
        git.valid_branch(p['head']['ref']); git.valid_branch(p['base']['ref'])
    main = git.fetch(default, root, pulls)
    if not git.ancestor(root['merge_commit_sha'], main):
        raise Refused('merge_not_on_default')
    nodes, visited = [], set()
    old_root = root['head']['sha']
    try:
        def walk(parent_ref, parent_old, parent_new, direct=False):
            for p in pulls:
                # Deleted merged branches may already have been retargeted by GitHub. Exact old-root
                # ancestry proves their dependency when the rebase/squash merge replaced those commits.
                inherited = (direct and p['base']['ref'] == default and not git.ancestor(old_root, main)
                             and git.ancestor(old_root, p['head']['sha']))
                if p['base']['ref'] != parent_ref and not inherited:
                    continue
                n, branch, head = p['number'], p['head']['ref'], p['head']['sha']
                if n in visited or len([q for q in pulls if q['head']['ref'] == branch]) != 1:
                    raise Refused('ambiguous_stack', n, head)
                visited.add(n)
                if len(visited) > MAX_STACK:
                    raise Refused('too_many_children', n, head)
                if branch in (default, 'main', 'master', root['head']['ref']):
                    raise Refused('protected_branch', n, head)
                info = api.branch(branch)
                if info is None or info.get('protected') is not False:
                    raise Refused('protected_or_missing_branch', n, head)
                if info.get('commit', {}).get('sha') != head:
                    raise Refused('head_moved', n, head)
                already = (direct and git.ancestor(root['merge_commit_sha'], head) and not git.ancestor(old_root, head))
                if git.ancestor(parent_new, head) or already:
                    new = head
                else:
                    new = git.prepare(parent_old, parent_new, head, n)
                node = {'number': n, 'branch': branch, 'old': head, 'new': new,
                        'base': p['base']['ref'], 'target_base': default if direct else p['base']['ref']}
                nodes.append(node)
                walk(branch, head, new)
        walk(root['head']['ref'], old_root, main, True)
    except Refused as e:
        warning(api, root, e, app_id, dry_run)
        raise
    if dry_run:
        return {'status': 'prepared', 'nodes': nodes}
    # Re-read authorization and every destination immediately before the single atomic push.
    fresh_root = api.pull(root_number)
    if (fresh_root.get('merged') is not True or fresh_root.get('merge_commit_sha') != root['merge_commit_sha']
            or fresh_root.get('head', {}).get('sha') != old_root or fresh_root.get('base', {}).get('ref') != default):
        raise Refused('root_moved')
    current_main = api.branch(default)
    if current_main is None or current_main.get('commit', {}).get('sha') != main:
        raise Refused('default_moved')
    for node in nodes:
        fresh = checked_pull(api.pull(node['number']))
        info = api.branch(node['branch'])
        if (not same_repo(fresh, repo) or fresh.get('state') != 'open' or fresh['head']['ref'] != node['branch']
                or fresh['head']['sha'] != node['old'] or fresh['base']['ref'] not in (node['base'], node['target_base'])
                or info is None or info.get('protected') is not False or info.get('commit', {}).get('sha') != node['old']):
            raise Refused('head_or_base_moved', node['number'], node['old'])
    git.push(nodes)
    # API metadata cannot be atomic with Git refs. A failed PATCH is visible; a retry sees the
    # already-rebased heads and only completes remaining retargets, without replaying old commits.
    for node in nodes:
        if node['base'] == node['target_base']:
            continue
        fresh = checked_pull(api.pull(node['number']))
        if (not same_repo(fresh, repo) or fresh.get('state') != 'open' or fresh['head']['ref'] != node['branch']
                or fresh['head']['sha'] != node['new'] or fresh['base']['ref'] not in (node['base'], node['target_base'])):
            raise Refused('metadata_pending', node['number'], node['new'])
        if fresh['base']['ref'] != node['target_base']:
            try:
                answer = api.retarget(node['number'], node['target_base'])
                if answer.get('base', {}).get('ref') != node['target_base']:
                    raise Refused('metadata_pending')
            except (Refused, NotFound):
                raise Refused('metadata_pending', node['number'], node['new']) from None
    return {'status': 'published', 'nodes': nodes}


def main():
    report = {'status': 'refused', 'reason': 'invalid_input'}
    try:
        repo, raw = os.environ.get('RESTACK_REPOSITORY', ''), os.environ.get('RESTACK_PR', '')
        token, app = os.environ.get('RESTACK_TOKEN', ''), os.environ.get('RESTACK_APP_ID', '')
        dry = os.environ.get('RESTACK_DRY_RUN', 'true')
        if not REPO.fullmatch(repo) or not raw.isdigit() or int(raw) < 1 or not token or not app.isdigit() or int(app) < 1 or dry not in ('true', 'false'):
            raise Refused('invalid_input')
        with tempfile.TemporaryDirectory(prefix='airlock-restack-') as directory:
            report = restack(GitHub(repo, token), Git(directory, repo, token), int(raw), int(app), dry != 'false')
    except Refused as e:
        report = {'status': 'refused', 'reason': e.reason, 'pr': e.pr, 'commit': e.commit, 'files': e.files}
    except (NotFound, KeyError, TypeError, ValueError, OSError):
        report = {'status': 'refused', 'reason': 'unreadable_state'}
    output = json.dumps(report, indent=2)
    print(output)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
            f.write('### Stack maintenance\n\n```json\n' + output.replace('`', '\\u0060') + '\n```\n')
    return 1 if report['status'] == 'refused' else 0


if __name__ == '__main__':
    sys.exit(main())
