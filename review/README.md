# Review providers

`review` and `review/security` keep the caller's existing job names (`claude-review` and the advisory
`claude-security-review`). `provider: claude` is the compatible default. `provider: codex` requests reviews
from the connected **ChatGPT Codex Connector GitHub app**, without running Codex CLI, acquiring the runner
credential lease, or requiring a Claude credential. The app must already have Code Review and Security
Review access to the repository. These use the account's included allowance/credits; there is no API-key fallback.

The adoption template selects `AIRLOCK_REVIEW_PROVIDER`, then `AIRLOCK_AGENT_PROVIDER`, then `claude`.
An unknown provider fails. Set `AIRLOCK_REVIEW_PROVIDER=codex` to change reviews independently of role runs.
The provider-specific `model` input applies to Claude only; OpenAI's app controls its review model.

## What counts

The bridge uses the GitHub API, never a checkout of PR code. It verifies both OpenAI's immutable GitHub app
and bot identities and reads the observed `codex-review-state:v1` envelope. A pass needs the full 40-character
current head SHA, `completed`, and both finding flags explicitly false. A reaction, prose, short commit,
resolved thread, missing kind, old result, failed request or unreadable/truncated API response is no pass.
The newest trusted summary wins, including a newer running/failed review. Review-format changes fail closed.

A Code Review request includes the same six charter checks as the Claude prompt (`codex-rules.md`): signal,
intent, floors, proof, secrets/data and goals. A deterministic signal/tool-request check runs first. A request
is bound to the head, PR-body hash and rubric hash. Reuse needs an exact, unedited workflow-bot request and a
subsequent trusted completion receipt plus the full-SHA summary, or an observed running-to-completed transition.
The PR head and body are checked again before returning a result. The `edited` PR event reruns intent reviews.

Security is a separate request and state, never inferred from Code Review or an absence of findings. Since the
app's security completion receipt is not a documented interface, the bridge conservatively requires observing
that security review running on this head and then completing. It requests a fresh security review on each run;
a review too fast to observe fails visibly and can be rerun. Findings remain advisory, as in the Claude action.

The existing policy remains: the verdict binds configured agents; a person's PR gets a warning and its merger
owns the review. A writer's approval on the exact commit can take over. Workflow changes still require a human
on an agent's PR, even if Codex is clean. Codex also treats changes to `AGENTS.md` as requiring that review so a
PR cannot change its own review instructions and approve itself. Human Markdown-only PRs skip model review.

## Caller

```yaml
# Keep the existing jobs and permissions; add this input to both review actions.
with:
  provider: ${{ vars.AIRLOCK_REVIEW_PROVIDER || vars.AIRLOCK_AGENT_PROVIDER || 'claude' }}
  claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }} # needed only for Claude
```

`codex-wait-seconds` defaults to 900 (maximum 900), within the existing 20-minute job budget. The job's GitHub
token needs Contents read and Issues/Pull requests write; the model never receives that token. The separate
existing row-reconciliation job alone retains Actions write. No `pull_request_target` or broad App token is added.
Enable Code Review/Security Review in [Codex's GitHub integration](https://learn.chatgpt.com/docs/third-party/github)
and confirm [Security Review access](https://learn.chatgpt.com/docs/security/security-review) before choosing Codex.
A bot-triggered request must be proven in the adopting repository before relying on unattended reviews.

## Verification

`python test/review/test_review.py` retains the Claude verdict/approval/retry tests.
`python test/review/test_codex.py` plants forged identities, stale/short hashes, duplicate JSON, missing security,
resolved findings, changed intent and head races. A live transport probe must additionally show that workflow-bot
requests actually reach the app and yield full-head completed states. Such a probe does not override the human
approval rule for workflow/review-instruction changes and cannot mint a production gate pass.
