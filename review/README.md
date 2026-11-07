# Review providers

`review` and `review/security` keep the caller's existing job names (`claude-review` and the advisory
`claude-security-review`). `provider: claude` remains the compatible default. `provider: codex` observes
reviews from the connected **ChatGPT Codex Connector GitHub app**, without running Codex CLI, acquiring the
runner credential lease, requiring a Claude credential, or using a personal GitHub token.

## Enable Codex

1. In Codex repository settings, enable automatic Code Review for **All PRs**, on every push. Enable automatic
   Security Review whenever Code Review runs, with the intended reporting threshold (Medium for the pilot).
   Confirm the account has Security Review access. Settings are shared at the repository level.
2. Merge the reviewed charter rubric into the repository's root `AGENTS.md`. `adopt.codex_review_rules: true`
   installs the action's six-check rubric, preserving existing guidance. The bridge requires the same rubric
   in a regular file at the PR's **base commit**, so adding or changing it in a PR cannot approve that PR.
3. Update the review action pins, then set `AIRLOCK_REVIEW_PROVIDER=codex`. The template falls back to
   `AIRLOCK_AGENT_PROVIDER`, then `claude`. Unknown providers fail. `adopt.review_ref` can pin this review
   implementation independently of the role-workflow SHA already trusted by Mint and the runner broker.
4. Verify a real bot-authored push produces an automatic app result and the required exact-head review check.
   A manually requested review or a person marking a draft ready does not establish unattended push coverage.

The app's automatic settings are independent of the workflow variable. Verify their push behavior with the
actual bot identity before activation, even while the workflow still selects Claude. The pilot selects Codex reviews explicitly, while role runs keep
their existing provider. The model input applies to Claude; the GitHub app chooses its own review model.

GitHub Actions bot mentions were tested and **rejected** by the app as an unconnected Codex user. The production
bridge therefore sends no mentions or other writes. A connected person can still request `@codex review` or
`@codex security review`, for example after editing a PR body without pushing a commit. There is no API-key fallback.
A role run needs neither: every role that opens PRs is told (`agent/plan.py`) to finish the description before the
push that starts Code Review, and to push one more commit, empty if nothing else changed, after any later body edit.

## What counts

The bridge uses read-only GitHub API calls, never a checkout of PR code. It verifies both OpenAI's immutable
GitHub app and bot IDs and reads the observed `codex-review-state:v1` envelope. A clean verdict needs the full current
40-character head SHA, `completed`, and both finding flags explicitly false. A reaction, prose, short hash,
resolved thread, missing review kind, older result or unreadable/truncated response is no pass. A single authoritative summary is required; ambiguous summaries fail. Its running or failed state never reuses an earlier pass. Review-format changes fail closed.

Code Review needs its own authenticated completion metadata and freshness proof. A matching full-head
Code Review start recovered from authenticated history can establish freshness without a standalone receipt
comment; automatic reviews can omit that extra comment. If no such start is available, an unedited body can
use the existing fresh-receipt path. For an edited body, authenticated history must always prove a matching
Code Review started after that edit. GitHub reports body edits at whole-second precision, so a start in
that same second is refused as ambiguous, even with a later receipt. A receipt’s short hash never establishes identity without the full-SHA state. The current head and body are
checked before and after polling. A later Security Review cannot freshen an old Code Review through the summary’s shared update timestamp. The `edited` PR event reruns the check; an old intent review waits for a new
app review. Signal/tool-request delivery markers are checked deterministically before model results count.

The installed rubric preserves signal, intent, floors, proof, secrets/data and goals. An agent's workflow or
`AGENTS.md` change still requires a human's approval on the current commit, even with a clean app result. The
existing policy remains: verdicts bind configured agents; a person's PR gets a warning and its merger owns the
review. A writer's exact-commit approval can take over. Human Markdown-only PRs skip model review.

Security is a separate typed result, never inferred from Code Review or an absence of posted findings. Its
findings remain advisory. A missing or failed security review fails the scan step visibly; it never pretends
that a scan completed. Account access and reporting thresholds are configured in Codex, outside this action.

The observed Security Review v1 can mark a scan completed while both finding flags remain `null`. In that case,
the authenticated `codex-security-review:v1` envelope must independently match the full head, repository and PR,
say `completed`, and have its native merge gate off. The result is `completed_findings_unknown`, with a visible
warning and no clean security verdict. Missing, partial or inconsistent metadata still fails. This exception
cannot authorize Code Review. The live probe requires clean Code Review plus a completed advisory security scan;
it prints their separate results, including unavailable security finding counts.

This shape is captured from the real app in `test/review/fixtures/codex-security-null-findings.json`, with invented
repository, PR and commit identifiers. The code-review finding-flag requirements remain unchanged.

## Caller

```yaml
with:
  provider: ${{ vars.AIRLOCK_REVIEW_PROVIDER || vars.AIRLOCK_AGENT_PROVIDER || 'claude' }}
  claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }} # needed only for Claude
```

`codex-wait-seconds` defaults to 900 (maximum 900), within the 20-minute job budget. Codex alone needs only
Contents, Issues and Pull requests read. Existing mixed-provider callers retain Claude's write permissions;
only the separate row-reconciliation and app-result refresh jobs have Actions write. No `pull_request_target` is added.

[Official GitHub review setup](https://learn.chatgpt.com/docs/third-party/github) and
[Security Review access and thresholds](https://learn.chatgpt.com/docs/security/security-review).

## Verification

`python test/review/test_review.py` retains the Claude verdict/approval/retry tests.
`python test/review/test_codex.py` plants forged identities, stale/short hashes, duplicate JSON, missing security,
resolved findings, modified/missing base rules, changed intent and head races. A non-gating live observer probe
checks the app's code and security receipts. It cannot override the human-review rule for workflow changes.

## Result changes on the same commit

Opting into the rubric also installs `codex-review-results.yml`. It handles OpenAI app summary/receipt creation,
edits and deletion on the default branch, with no PR checkout. The pinned `review/codex-rerun` action validates
immutable app identity, repository and current head, then waits for the complete adopted workflow and reruns it once, refreshing both `claude-review` and
`claude-security-review` plus their dependent row reconciliation. A replacement summary with an old or null head also invalidates the current rows, matching the observer’s refusal rules. Busy workflows are waited out, including dependent jobs. The handler never writes a passing
status itself. Its Actions-write token is isolated from model/review jobs, and it never follows an old head.

## Authentic edits and intent

Original comment authorship is insufficient: writers can edit PR comments. The reader checks every OpenAI-bot
comment against its GraphQL edit metadata and complete edit history. Every editor must be the OpenAI bot; missing,
deleted or non-OpenAI history refuses the evidence. REST and GraphQL body/version snapshots must agree. A cache
is keyed by the body, timestamps and edit count, rather than trusting a previously checked comment ID.

Those authenticated historical snapshots also recover Code Review’s own running/start metadata and full SHA.
That full-head start plus the same review kind’s completion is independent proof when the automatic app
review posts no standalone receipt; a missing/stale start cannot take this path.
A body edited during a review cannot borrow that review’s later completion. After a body edit, push a correction
or request a new app review (a role run pushes; see above); a completed review alone is not evidence that the new intent was read. The observer
rechecks intent and evidence before returning. A body changed and restored still has a newer edit timestamp.

The result updater retains events while review jobs are busy, waits for the whole workflow and dependent jobs to finish, then refreshes both reviews with one workflow rerun.
It does not assume a busy job will fetch the update. Invalidation is conservative for every OpenAI-bot comment
edit/deletion, even if a receipt header was removed. This only schedules a fresh check; it cannot produce PASS.

Refresh concurrency belongs to the app-filtered job, not the workflow: an ordinary PR comment cannot cancel an in-flight receipt invalidation.
