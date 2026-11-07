# Restack dependent pull requests

Part of [autonomy#79](https://github.com/lunareng/autonomy/issues/79). The action prepares an entire stack of
same-repository PR branches after their bottom PR merges, verifies that each commit's patch is unchanged,
and publishes the rewritten heads in one atomic Git push with an explicit lease for every changed branch.
Only direct children are retargeted to the repository's default branch; descendants keep their parent branch.

This is the engine, not automatic rollout. A trusted policy-repository workflow must supply an Airlock-minted
App token and the merged PR number. The merged-event handoff is tracked in
[airlock#233](https://github.com/lunareng/airlock/issues/233). Mechanical pushes must also have correct run
attribution under the tenant's gate before activation
([airlock#234](https://github.com/lunareng/airlock/issues/234)). Do not mint a token from a PR event, copy a user PAT,
or pass `GITHUB_TOKEN`: its pushes would suppress the CI this action relies on. This change enables no timer,
repository variable, new credential or production workflow.

## Calling the action

Run from trusted default-branch workflow code on Linux with Git 2.38 or newer. A checkout of the target
repository is unnecessary; the action fetches into an isolated temporary repository and removes it afterwards.
Pin the action to a reviewed full commit, as with other Airlock actions.

```yaml
- uses: lunareng/airlock-actions/restack@<reviewed-full-commit>
  with:
    token: ${{ steps.airlock.outputs.token }}
    repository: acme/widgets
    merged-pr: "12"
    app-id: "123456"
    dry-run: "true"
```

The token needs Contents and Pull requests write on the target repository, plus Workflows write if the
rebased commits change workflow files. The role's policy must authorize mechanical restacking and list that
repository. `app-id` is the tenant's configured App id, used only to deduplicate refusal comments; it is not
an authorization credential. The trusted caller supplies the token; the action cannot identify a token's
issuing App from a repository read. Never give this job a token from a PR branch or a fork.

`dry-run` defaults to `true`: fetch, rebase and verify locally, without a branch push, PR edit or comment.
Only the exact value `false` publishes. The JSON report is printed and appended to the job summary.
A refusal exits nonzero, including a partially completed metadata publication.

## What can change

The root must be merged into the repository's default branch, its merge commit must be in that branch,
and its head must belong to the same repository. An existing root branch that has advanced since the merge
is refused. Fork PRs are left alone. All affected heads must be unprotected, distinct branches, and cannot
be the default branch, `main`, `master` or the merged root's branch.

Open PR base relationships define the stack. If GitHub already retargeted a child after deleting the merged
branch, the action can also recognize an exact dependency on the replaced root commit in a default-based
PR's ancestry. It does not guess from similar patches or branch-name prefixes. Discovery is bounded to
100 same-repository open PRs, 20 affected PRs and 100 commits per child.

All rebases happen before any push. Conflicts, changed patch context, merge commits, ambiguous relationships
or unreadable state refuse the preparation. There is no automatic conflict resolution, rerere reuse, skipped
cherry-pick or dropped empty commit. `git range-diff` must contain exactly one unchanged (`=`) pair for each
original child commit. A refusal names the stopped commit and files in one deduplicated PR comment;
dry runs never comment. A conflict in a grandchild also leaves its successfully prepared parent unpushed.

Git runs with isolated configuration and no hooks, fsmonitor, credential helpers, external attributes file,
recursive submodule fetch or signing. Only the supplied credential helper runs, and it lives outside every
fetched tree so even a tracked file named `askpass` cannot replace it. Repository programs never run.
Git stderr, API error bodies and credentials are never printed. Fetch and push use only the fixed GitHub.com
repository URL and HTTPS, with redirects disabled.

Before publication the action re-reads the root, default branch, affected PRs and branch protections. The
atomic push refuses the entire batch if a changed branch's explicit lease is lost. A later advance of the
default branch does not erase work; ordinary mergeability checks apply again. Each real App push triggers
normal CI, and rewritten commits invalidate old approvals. The action never merges a PR or relaxes a gate.

## Failure and retry

Git refs and GitHub PR metadata cannot be committed atomically. If a base retarget fails after the atomic
push, the action fails with `metadata_pending`. Retrying recognizes heads already based on the merged
parent and completes outstanding retargets without applying the old commits again, even if default has
advanced. It rechecks each PR immediately before changing its base; a concurrent head/base change stops
metadata publication rather than rolling back another writer's work. A failure after a successful API
response was lost is safe to retry too. Nothing automatically replays a refused push or merge.

## Proof

Run `python3 -I -m unittest discover -s test/restack -v`. The tests use real Git repositories and a bare remote,
with an in-memory GitHub boundary; they need no network, credentials or model inference. They exercise
A ← B ← C, both conflict positions, changed patches, lease races, unchanged default/fork/protected branches,
metadata retries, deleted-root retargeting and hostile hooks/config/helper-shaped files.

Live GitHub event delivery, push-triggered CI, correct maintenance attribution and end-to-end rollout are
still owed under autonomy#79. Local Git tests do not substitute for those activation checks.
