"""Real git/bare-remote tests. No GitHub connection, credential or project-code execution."""
import copy
import io
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('restack', Path(__file__).resolve().parents[2] / 'restack/restack.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class Fixture:
    def __init__(self, directory, conflict=None, extra=False):
        self.path = Path(directory)
        self.remote, self.source = self.path / 'origin.git', self.path / 'source'
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.env.update({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                         'GIT_AUTHOR_NAME': 'Example author', 'GIT_AUTHOR_EMAIL': 'author@example.invalid',
                         'GIT_COMMITTER_NAME': 'Example author', 'GIT_COMMITTER_EMAIL': 'author@example.invalid'})
        self.cmd(self.path, 'init', '--bare', '--quiet', str(self.remote))
        self.cmd(self.path, 'init', '--quiet', '--initial-branch=main', str(self.source))
        self.cmd(self.source, 'remote', 'add', 'origin', str(self.remote))
        self.write('shared.txt', ''.join(f'line {i}\n' for i in range(1, 16)))
        self.commit('initial')
        self.cmd(self.source, 'checkout', '--quiet', '-b', 'stack/a')
        self.write('a.txt', 'feature A\n')
        self.a = self.commit('A')
        self.cmd(self.source, 'checkout', '--quiet', '-b', 'stack/b')
        self.write('b.txt', 'feature B\n')
        if conflict == 'b':
            self.write('shared.txt', 'B changes this\n')
        if conflict == 'context':
            self.write('shared.txt', (self.source / 'shared.txt').read_text().replace('line 8\n', 'B line 8\n'))
        self.b = self.commit('B')
        self.cmd(self.source, 'checkout', '--quiet', '-b', 'stack/c')
        self.write('c.txt', 'feature C\n')
        if conflict == 'c':
            self.write('shared.txt', 'C changes this\n')
        self.c = self.commit('C')
        self.cmd(self.source, 'push', '--quiet', 'origin', 'main', 'stack/a', 'stack/b', 'stack/c')
        for n, sha in [(1, self.a), (2, self.b), (3, self.c)]:
            self.cmd(self.source, 'push', '--quiet', 'origin', f'{sha}:refs/pull/{n}/head')
        self.cmd(self.source, 'checkout', '--quiet', 'main')
        self.cmd(self.source, 'cherry-pick', self.a)
        self.cmd(self.source, 'commit', '--quiet', '--amend', '-m', 'A merged by rebase')
        self.merge = self.head()
        if conflict in ('b', 'c'):
            self.write('shared.txt', 'main changes this\n')
            self.commit('concurrent main change')
        if conflict == 'context':
            self.write('shared.txt', (self.source / 'shared.txt').read_text().replace('line 6\n', 'main line 6\n'))
            self.commit('new patch context')
        if extra:
            self.write('unrelated.txt', 'another main commit\n'); self.commit('main advanced')
        self.cmd(self.source, 'push', '--quiet', 'origin', 'main')
        self.repo = {'id': 100, 'default_branch': 'main'}
        self.documents = {
            1: self.pull_doc(1, 'stack/a', 'main', self.a, state='closed', merged=True, merge_commit_sha=self.merge),
            2: self.pull_doc(2, 'stack/b', 'stack/a', self.b),
            3: self.pull_doc(3, 'stack/c', 'stack/b', self.c),
        }
        self.comments, self.retargets, self.push_calls = [], [], 0
        self.protected = set()
        self.fail_patch = False
        self.patch_response_lost = False
        self.before_push = None
        self.before_retarget = None

    def cmd(self, cwd, *args):
        p = subprocess.run(['git', *args], cwd=cwd, env=self.env, capture_output=True)
        if p.returncode:
            raise AssertionError((args, p.stderr.decode()))
        return p.stdout.decode().strip()

    def write(self, name, text):
        (self.source / name).write_text(text)

    def commit(self, message):
        self.cmd(self.source, 'add', '.')
        self.cmd(self.source, 'commit', '--quiet', '-m', message)
        return self.head()

    def head(self):
        return self.cmd(self.source, 'rev-parse', 'HEAD')

    def ref(self, name):
        return self.cmd(self.remote, 'rev-parse', 'refs/heads/' + name)

    def heads(self):
        return self.cmd(self.remote, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads/')

    def pull_doc(self, number, branch, base, sha, **extra):
        return {'number': number, 'state': 'open', 'head': {'ref': branch, 'sha': sha, 'repo': {'id': 100}},
                'base': {'ref': base, 'repo': {'id': 100}}, **extra}

    def api(self):
        f = self
        class API:
            def repo(self):
                return copy.deepcopy(f.repo)
            def pull(self, n):
                p = copy.deepcopy(f.documents[n])
                if p['state'] == 'open' and (p['head'].get('repo') or {}).get('id') == f.repo['id']:
                    p['head']['sha'] = f.ref(p['head']['ref'])
                    f.cmd(f.remote, 'update-ref', f'refs/pull/{n}/head', p['head']['sha'])
                return p
            def pulls(self):
                return [self.pull(n) for n, p in f.documents.items() if p['state'] == 'open']
            def branch(self, name):
                p = subprocess.run(['git', 'rev-parse', '--verify', 'refs/heads/' + name], cwd=f.remote, env=f.env, capture_output=True)
                if p.returncode:
                    return None
                return {'protected': name in f.protected, 'commit': {'sha': p.stdout.decode().strip()}}
            def warn(self, n, body, marker, app_id):
                if not any(marker == m for _, _, m in f.comments):
                    f.comments.append((n, body, marker))
            def retarget(self, n, base):
                if f.before_retarget:
                    f.before_retarget()
                if f.fail_patch:
                    raise r.Refused('github_request_refused')
                f.documents[n]['base']['ref'] = base
                f.retargets.append((n, base))
                if f.patch_response_lost:
                    raise r.Refused('github_unreadable')
                return self.pull(n)
        return API()

    def git(self):
        f = self
        class Git(r.Git):
            def push(self, nodes):
                if any(n['old'] != n['new'] for n in nodes):
                    f.push_calls += 1
                if f.before_push:
                    f.before_push()
                return super().push(nodes)
        return Git(tempfile.mkdtemp(dir=self.path), 'acme/example', '', remote=str(self.remote))

    def run(self, dry=False):
        return r.restack(self.api(), self.git(), 1, 99, dry)


class RestackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def fixture(self, **kwargs):
        return Fixture(self.tmp.name, **kwargs)

    def test_three_pr_stack_preserves_every_patch_and_only_retargets_direct_child(self):
        f = self.fixture(extra=True)
        default = f.ref('main')
        out = f.run()
        self.assertEqual(out['status'], 'published')
        self.assertEqual(f.push_calls, 1)
        self.assertNotEqual(f.ref('stack/b'), f.b)
        self.assertNotEqual(f.ref('stack/c'), f.c)
        self.assertEqual(f.ref('main'), default)
        self.assertEqual(f.retargets, [(2, 'main')])
        self.assertEqual(f.documents[3]['base']['ref'], 'stack/b')
        self.assertEqual(f.cmd(f.remote, 'show', 'stack/c:a.txt'), 'feature A')
        self.assertEqual(f.cmd(f.remote, 'show', 'stack/c:b.txt'), 'feature B')
        self.assertEqual(f.cmd(f.remote, 'show', 'stack/c:c.txt'), 'feature C')
        for old, parent, new, target in [(f.b, f.a, f.ref('stack/b'), default), (f.c, f.b, f.ref('stack/c'), f.ref('stack/b'))]:
            diff = f.cmd(f.remote, 'range-diff', f'{parent}..{old}', f'{target}..{new}')
            self.assertRegex(diff, r' = ')
            self.assertNotRegex(diff, r' ! | < | > ')
        self.assertEqual(f.comments, [])

    def test_b_conflict_pushes_nothing_and_warns_once_even_after_retry(self):
        f = self.fixture(conflict='b')
        heads = f.heads()
        for _ in range(2):
            with self.assertRaises(r.Refused) as e:
                f.run()
            self.assertEqual(e.exception.reason, 'conflict')
        self.assertEqual(f.heads(), heads)
        self.assertEqual(f.push_calls, 0)
        self.assertEqual(len(f.comments), 1)
        self.assertEqual(f.comments[0][0], 2)
        self.assertIn('shared.txt', f.comments[0][1])
        self.assertIn(f.b, f.comments[0][1])
        self.assertEqual(f.retargets, [])

    def test_c_conflict_does_not_publish_the_successfully_prepared_b(self):
        f = self.fixture(conflict='c')
        heads = f.heads()
        with self.assertRaises(r.Refused) as e:
            f.run()
        self.assertEqual(e.exception.reason, 'conflict')
        self.assertEqual(f.heads(), heads)
        self.assertEqual(f.push_calls, 0)
        self.assertEqual(f.comments[0][0], 3)

    def test_changed_patch_context_fails_range_diff_before_any_push(self):
        f = self.fixture(conflict='context')
        heads = f.heads()
        with self.assertRaises(r.Refused) as e:
            f.run()
        self.assertEqual(e.exception.reason, 'patch_changed')
        self.assertEqual(f.heads(), heads)
        self.assertEqual(f.push_calls, 0)

    def test_dry_run_proves_stack_without_github_writes(self):
        f = self.fixture()
        heads = f.heads()
        result = f.run(dry=True)
        self.assertEqual(result['status'], 'prepared')
        self.assertEqual(len(result['nodes']), 2)
        self.assertEqual(f.heads(), heads)
        self.assertEqual((f.retargets, f.comments, f.push_calls), ([], [], 0))

    def test_dry_run_conflict_does_not_post_comment(self):
        f = self.fixture(conflict='c')
        with self.assertRaises(r.Refused):
            f.run(dry=True)
        self.assertEqual(f.comments, [])

    def test_forks_are_not_fetched_or_changed(self):
        f = self.fixture()
        f.documents[2]['head']['repo']['id'] = 200
        heads = f.heads()
        out = f.run()
        self.assertEqual(out['nodes'], [])
        self.assertEqual(f.heads(), heads)
        self.assertEqual(f.retargets, [])

    def test_fork_root_is_skipped(self):
        f = self.fixture()
        f.documents[1]['head']['repo']['id'] = 200
        self.assertEqual(f.run()['reason'], 'fork_root')
        self.assertEqual(f.push_calls, 0)

    def test_protected_child_refuses_whole_stack(self):
        f = self.fixture()
        f.protected.add('stack/c')
        heads = f.heads()
        with self.assertRaises(r.Refused) as e:
            f.run()
        self.assertEqual(e.exception.reason, 'protected_or_missing_branch')
        self.assertEqual(f.heads(), heads)

    def test_default_branch_cannot_be_rewritten(self):
        f = self.fixture()
        f.documents[2]['head']['ref'] = 'main'
        heads = f.heads()
        with self.assertRaises(r.Refused) as e:
            f.run()
        self.assertEqual(e.exception.reason, 'protected_branch')
        self.assertEqual(f.heads(), heads)

    def test_unmerged_or_wrong_base_root_refused(self):
        f = self.fixture()
        f.documents[1]['merged'] = False
        with self.assertRaises(r.Refused) as e:
            f.run()
        self.assertEqual(e.exception.reason, 'root_not_merged_to_default')

    def test_reused_root_branch_is_not_treated_as_merged_parent(self):
        f = self.fixture()
        f.cmd(f.remote, 'update-ref', 'refs/heads/stack/a', f.b)
        with self.assertRaises(r.Refused) as e:
            f.run()
        self.assertEqual(e.exception.reason, 'root_branch_reused')

    def test_atomic_leases_prevent_partial_push_on_concurrent_child_change(self):
        f = self.fixture()
        f.cmd(f.source, 'checkout', '--quiet', 'stack/c')
        f.write('other-agent.txt', 'new work\n'); concurrent = f.commit('another agent')
        f.cmd(f.source, 'push', '--quiet', 'origin', f'{concurrent}:refs/heads/staging')
        f.before_push = lambda: f.cmd(f.remote, 'update-ref', 'refs/heads/stack/c', concurrent)
        with self.assertRaises(r.Refused):
            f.run()
        self.assertEqual(f.ref('stack/b'), f.b)
        self.assertEqual(f.ref('stack/c'), concurrent)
        self.assertEqual(f.retargets, [])

    def test_retry_after_patch_failure_only_completes_metadata(self):
        f = self.fixture()
        f.fail_patch = True
        with self.assertRaises(r.Refused) as e:
            f.run()
        self.assertEqual(e.exception.reason, 'metadata_pending')
        heads = f.heads()
        f.fail_patch = False
        # Main may advance between the push and metadata retry.
        f.write('later.txt', 'later\n'); f.commit('later main'); f.cmd(f.source, 'push', '--quiet', 'origin', 'main')
        out = f.run()
        self.assertEqual(out['status'], 'published')
        self.assertEqual(f.ref('stack/b'), next(line.split()[1] for line in heads.splitlines() if line.startswith('refs/heads/stack/b ')))
        self.assertEqual(f.retargets, [(2, 'main')])
        self.assertEqual(f.push_calls, 1)

    def test_lost_patch_response_is_safe_to_retry(self):
        f = self.fixture()
        f.patch_response_lost = True
        with self.assertRaises(r.Refused):
            f.run()
        heads = f.heads()
        f.patch_response_lost = False
        f.run()
        self.assertEqual(f.heads(), heads)
        self.assertEqual(f.push_calls, 1)
        self.assertEqual(f.retargets, [(2, 'main')])

    def test_deleted_root_auto_retargeted_child_is_discovered_from_exact_ancestry(self):
        f = self.fixture()
        f.cmd(f.remote, 'update-ref', '-d', 'refs/heads/stack/a')
        f.documents[2]['base']['ref'] = 'main'
        out = f.run()
        self.assertEqual(len(out['nodes']), 2)
        self.assertNotEqual(f.ref('stack/b'), f.b)
        self.assertEqual(f.retargets, [])

    def test_hostile_global_hooks_and_config_are_not_executed(self):
        f = self.fixture()
        hooks = f.path / 'hostile-hooks'; hooks.mkdir()
        marker = f.path / 'executed'
        for name in ['post-checkout', 'post-rewrite', 'pre-rebase', 'pre-push']:
            p = hooks / name; p.write_text(f'#!/bin/sh\ntouch "{marker}"\n'); p.chmod(0o700)
        config = f.path / 'hostile-config'
        config.write_text(f'[core]\n hooksPath = {hooks}\n')
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(config), 'GIT_CONFIG_COUNT': '1',
                                     'GIT_CONFIG_KEY_0': 'core.hooksPath', 'GIT_CONFIG_VALUE_0': str(hooks)}):
            f.run()
        self.assertFalse(marker.exists())

    def test_merge_commits_in_child_are_refused_not_flattened(self):
        f = self.fixture()
        f.cmd(f.source, 'checkout', '--quiet', 'stack/b')
        f.cmd(f.source, 'checkout', '--quiet', '-b', 'side')
        f.write('side.txt', 'side\n'); f.commit('side')
        f.cmd(f.source, 'checkout', '--quiet', 'stack/b')
        f.write('b2.txt', 'b2\n'); f.commit('b2')
        f.cmd(f.source, 'merge', '--no-ff', '--no-edit', 'side')
        f.cmd(f.source, 'push', '--quiet', 'origin', 'stack/b')
        heads = f.heads()
        with self.assertRaises(r.Refused) as e:
            f.run()
        self.assertEqual(e.exception.reason, 'merge_commits')
        self.assertEqual(f.heads(), heads)

    def test_no_stack_is_no_write(self):
        f = self.fixture()
        f.documents[2]['state'] = 'closed'; f.documents[3]['state'] = 'closed'
        heads = f.heads()
        self.assertEqual(f.run()['nodes'], [])
        self.assertEqual(f.heads(), heads)
        self.assertEqual((f.push_calls, f.retargets, f.comments), (0, [], []))

    def test_tracked_askpass_cannot_replace_the_authentication_helper(self):
        f = self.fixture()
        f.cmd(f.source, 'checkout', '--quiet', 'stack/b')
        f.write('askpass', '#!/bin/sh\nexit 97\n')
        f.commit('tracked helper-shaped file')
        f.cmd(f.source, 'push', '--quiet', 'origin', 'stack/b')
        f.documents[3]['state'] = 'closed'
        g = r.Git(tempfile.mkdtemp(dir=f.path), 'acme/example', '')
        helper = Path(g.env['GIT_ASKPASS'])
        original = helper.read_text()
        g.run('config', 'protocol.file.allow', 'always')
        g.run('remote', 'set-url', 'origin', str(f.remote))
        r.restack(f.api(), g, 1, 99, False)
        self.assertNotEqual(helper.parent, g.directory)
        self.assertEqual(helper.read_text(), original)
        self.assertEqual((g.directory / 'askpass').read_text(), '#!/bin/sh\nexit 97\n')

    def test_repository_api_has_no_trailing_slash(self):
        api = r.GitHub('acme/example', '')
        class Opener:
            def open(self, request, timeout):
                self.url = request.full_url
                return io.BytesIO(b'{"id":100,"default_branch":"main"}')
        api.opener = Opener()
        self.assertEqual(api.repo()['id'], 100)
        self.assertEqual(api.opener.url, 'https://api.github.com/repos/acme/example')

    def test_empty_child_commit_is_preserved(self):
        f = self.fixture()
        f.cmd(f.source, 'checkout', '--quiet', 'stack/b')
        f.cmd(f.source, 'commit', '--quiet', '--allow-empty', '-m', 'intentional empty marker')
        before = f.head()
        f.cmd(f.source, 'push', '--quiet', 'origin', 'stack/b')
        f.documents[3]['state'] = 'closed'
        f.run()
        self.assertEqual(f.cmd(f.remote, 'rev-list', '--count', f'{f.ref("main")}..{f.ref("stack/b")}'), '2')
        diff = f.cmd(f.remote, 'range-diff', f'{f.a}..{before}', f'{f.ref("main")}..{f.ref("stack/b")}')
        self.assertEqual(len(diff.splitlines()), 2)
        self.assertTrue(all(' = ' in row for row in diff.splitlines()))

    def test_default_move_during_preparation_publishes_nothing(self):
        f = self.fixture()
        heads = f.heads()
        g = f.git()
        prepare = g.prepare
        def advance_after_prepare(*args):
            new = prepare(*args)
            f.cmd(f.remote, 'update-ref', 'refs/heads/main', f.a)
            return new
        g.prepare = advance_after_prepare
        with self.assertRaises(r.Refused) as e:
            r.restack(f.api(), g, 1, 99, False)
        self.assertEqual(e.exception.reason, 'default_moved')
        self.assertEqual(f.ref('stack/b'), f.b)
        self.assertEqual(f.ref('stack/c'), f.c)
        self.assertEqual(f.retargets, [])

    def test_child_base_changed_by_another_writer_is_not_overwritten(self):
        f = self.fixture()
        g = f.git()
        prepare = g.prepare
        def edit_after_prepare(*args):
            new = prepare(*args)
            f.documents[2]['base']['ref'] = 'another-base'
            return new
        g.prepare = edit_after_prepare
        heads = f.heads()
        with self.assertRaises(r.Refused) as e:
            r.restack(f.api(), g, 1, 99, False)
        self.assertEqual(e.exception.reason, 'head_or_base_moved')
        self.assertEqual(f.heads(), heads)
        self.assertEqual(f.documents[2]['base']['ref'], 'another-base')


if __name__ == '__main__':
    unittest.main()
