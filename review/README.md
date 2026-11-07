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

The app's automatic settings are independent of the workflow variable: enabling them causes app reviews on
pushes even while the workflow selects Claude. The pilot selects Codex reviews explicitly, while role runs keep
their existing provider. The model input applies to Claude; the GitHub app chooses its own review model.

GitHub Actions bot mentions were tested and **rejected** by the app as an unconnected Codex user. The production
bridge therefore sends no mentions or other writes. A connected person can still request `@codex review` or
`@codex security review`, for example after editing a PR body without pushing a commit. There is no API-key fallback.

## What counts

The bridge uses read-only GitHub API calls, never a checkout of PR code. It verifies both OpenAI's immutable
GitHub app and bot IDs and reads the observed `codex-review-state:v1` envelope. A pass needs the full current
40-character head SHA, `completed`, and both finding flags explicitly false. A reaction, prose, short hash,
resolved thread, missing review kind, older result or unreadable/truncated response is no pass. The newest
trusted summary wins, including a newer running or failed review. Review-format changes fail closed.

Code Review also needs a trusted completion receipt after the PR body's last edit; the short hash in that
receipt establishes freshness only, never identity without the full-SHA state. The current head and body are
checked before and after polling. The `edited` PR event reruns the check; an old intent review waits for a new
app review. Signal/tool-request delivery markers are checked deterministically before model results count.

The installed rubric preserves signal, intent, floors, proof, secrets/data and goals. An agent's workflow or
`AGENTS.md` change still requires a human's approval on the current commit, even with a clean app result. The
existing policy remains: verdicts bind configured agents; a person's PR gets a warning and its merger owns the
review. A writer's exact-commit approval can take over. Human Markdown-only PRs skip model review.

Security is a separate typed result, never inferred from Code Review or an absence of posted findings. Its
findings remain advisory. A missing or failed security review fails the scan step visibly; it never pretends
that a scan completed. Account access and reporting thresholds are configured in Codex, outside this action.

## Caller

```yaml
with:
  provider: ${{ vars.AIRLOCK_REVIEW_PROVIDER || vars.AIRLOCK_AGENT_PROVIDER || 'claude' }}
  claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }} # needed only for Claude
```

`codex-wait-seconds` defaults to 900 (maximum 900), within the 20-minute job budget. Codex alone needs only
Contents, Issues and Pull requests read. Existing mixed-provider callers retain Claude's write permissions;
only the separate existing row-reconciliation job has Actions write. No `pull_request_target` is added.

[Official GitHub review setup](https://learn.chatgpt.com/docs/third-party/github) and
[Security Review access and thresholds](https://learn.chatgpt.com/docs/security/security-review).

## Verification

`python test/review/test_review.py` retains the Claude verdict/approval/retry tests.
`python test/review/test_codex.py` plants forged identities, stale/short hashes, duplicate JSON, missing security,
resolved findings, modified/missing base rules, changed intent and head races. A non-gating live observer probe
checks the app's code and security receipts. It cannot override the human-review rule for workflow changes.
