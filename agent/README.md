# Claude Code and Codex runner support

Implementation for airlock-actions#53. This code is not on a release tag until Matthew reviews and releases it.
Existing `@v1` callers do not gain this implementation merely because the branch exists.

The role workflow selects a CLI independently from its authentication/billing mode:

| Provider | Auth mode | Credential |
| --- | --- | --- |
| `claude` | `subscription` | `CLAUDE_CODE_OAUTH_TOKEN` |
| `claude` | `api-key` | `ANTHROPIC_API_KEY` |
| `codex` | `subscription` | Dedicated ChatGPT login restored/saved by the credential broker |
| `codex` | `api-key` | `OPENAI_API_KEY` (passed as `CODEX_API_KEY` only to the Codex process) |

Only the selected pair's credential is required. Missing login never selects API billing.
Provider precedence: workflow `provider` input, caller repo `AIRLOCK_AGENT_PROVIDER`, role `run.provider`, then
`claude`. Auth precedence: workflow `auth` input, `AIRLOCK_AGENT_AUTH`, then `subscription`.
`AIRLOCK_CLAUDE_MODEL` and `AIRLOCK_CODEX_MODEL` override the role's model for that CLI. Set a compatible Codex
model before overriding a Claude role to Codex; sending a Claude model ID to Codex is refused.

The generated callers expose optional provider/auth manual-dispatch inputs and pass all three optional
secrets to the reusable workflow. Empty inputs let schedules and router dispatches follow the fleet setting.
Clearing the fleet override restores the role policy. The job summary records actual provider/auth/model.

## Subscription lifecycle

For Codex subscription mode, set `AIRLOCK_RUNNER_AUTH_URL` to the HTTPS origin of a compatible private-job
credential broker. Lunar's implementation is infra#109; no production login is installed by this change.
The broker must implement acquire/commit/release with a fresh GitHub OIDC proof for audience
`lunar-runner-auth`, atomic exclusive leases and versioned refreshed-auth write-back. The client never sends
credentials through job outputs. See infra's `runbooks/runner-auth.md` for the exact protocol and operator setup.

The runtime uses a private, per-job CODEX_HOME outside the writable checkout. It validates ChatGPT-managed
auth, lets Codex refresh normally, saves the refreshed file even after a failed run, and removes the local home
before uploading sanitized results. A failed save fails the run and retains the lease until expiry. A runner
lost after rotating its login can require reseeding; this cannot be repaired by replaying the original secret.
Only one serialized job stream may use a dedicated CI login. Do not use an active desktop login or expose
subscription auth to public/fork jobs. API-key jobs never acquire a subscription lease.

## Policy mapping and limits

GitHub tokens, MCP sessions, clone scopes and the mint action's always-running post-step revoke are shared by
both providers. Only validated issue/PR signal URLs in the caller's organization enter the prompt, and a
`routine:<name>` signal only when `roles/<role>/routines/<name>.md` is a file in the policy repo, which the prompt
then names; any other signal stays out. Nonzero CLI exits, Claude `is_error`, Codex failed turns and failed auth cleanup
fail the job. A usage-limit failure never replays a partially completed job with another provider.

Codex runs with workspace-write sandboxing, no escalation approval, project config untrusted, optional apps,
plugins, browsers and automatic skill dependency installation disabled, and per-tool checks from this pinned
action. The action bypasses only **hook trust prompts** for its bundled, reviewed hook; it does not bypass the
sandbox. Hook/config/state live outside the checkout. Git metadata write access is added only for declared
write clones so an allowed Git command can commit without giving access to the policy checkout's Git metadata.

The guard permits literal argv prefixes from `run.bash`, read commands, permitted edits, and the role's MCP
tool names. Shell composition (pipelines, redirects, substitutions and multiline shell scripts) is refused;
issue one allowed command at a time. Claude's turn cap maps conservatively to a Codex tool-call cap, counted
atomically across hooks. Inference is capped at 50 minutes, leaving cleanup time inside a 55-minute lease.
The CLI sandbox, scoped GitHub grant and upstream MCP session policies remain the security boundaries;
hooks are an additional guardrail, not an isolated credential vault.

**Not yet supported for Codex:** native `WebFetch`, `WebSearch` and `Agent` grants. Roles requesting these stop
during planning before receiving credentials. They can keep using Claude until a tested equivalent mapping
exists. Arbitrary MCP names are not silently granted; exact tool lists are also passed to Codex when possible.
Codex usage does not yet enter Airlock's Claude telemetry totals.

## Verification

`node --test test/agent/agent.test.mjs` drives all four auth combinations using fake provider binaries, tests
refresh persistence on failure, and checks cleanup and redaction. `python3 test/agent/test_guard.py` checks the
command guard and budget. `node --test test/agent/codex-cli.test.mjs` drives the pinned real CLI against a local
mock Responses API: an undeclared command is blocked and a declared Git commit succeeds inside its sandbox.
It uses no provider credentials or paid inference. CI installs exactly the pinned Codex release for that test.
Actual subscription smoke jobs remain an operator proof before rollout.
