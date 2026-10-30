# airlock-actions

The step a role's workflow uses to get its keys from [Airlock](https://airlock.lunareng.com/docs): this run's GitHub token, its MCP sessions, and its Claude Code telemetry, all no wider than the role's file in your policy repo. The token goes back to Airlock when the job ends.

```yaml
permissions:
  contents: read
  id-token: write   # the run's proof of who it is; Airlock reads nothing else
jobs:
  run:
    steps:
      - uses: lunareng/airlock-actions@<commit sha> # v1.0.0
        id: airlock
        with:
          role: triage
          signal: ${{ inputs.signal }}   # what the run was dispatched for, for its report
      - name: run the agent
        env:
          GH_TOKEN: ${{ steps.airlock.outputs.token }}
          CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
        run: claude -p "$(cat roles/triage/BRIEF.md)"
```

- **Main step:** proves the run with its GitHub OIDC token and asks Airlock's Mint for its token. It masks every secret before writing anything, then sets `token`, `expires-at`, one `session_<connector>` per MCP server (and `sessions` as JSON), and `read-token` when the role clones repositories it doesn't write. A live role that gets anything but a grant stops the run.
- **Telemetry, by default:** when your tenant has telemetry on for role runs (Airlock → Telemetry → Settings), Airlock's answer carries Claude Code's telemetry env with a session for this run alone. The step masks the session and exports the env to every later step, so Claude Code sends to Airlock with nothing for you to configure. Airlock's switch is the only control, with a per-role opt-out beside it. Telemetry off: nothing is exported.
- **Post step:** runs when the job ends, on success, failure or cancel. It hands the token back (`POST /v1/revoke`, with a fresh OIDC token) and ends the read-only token. It never fails the job and never prints a token.

| Input | Default | What it is |
| --- | --- | --- |
| `role` | (required) | The role this run is: `roles/<role>/role.yaml` and `.github/workflows/role-<role>.yml` in your policy repo. |
| `signal` | `""` | The issue or pull request the run was dispatched for, in your organization. Recorded for the run's report. |
| `sessions` | `""` | MCP connectors the run needs a session for, comma-separated (the keys of `run.mcp`). A missing one stops the run. |
| `shadow` | `false` | `true` for a role still in shadow: the step reports what Airlock would hand out and never fails. |
| `airlock-url` | `https://airlock.lunareng.com` | Airlock's origin, and the OIDC token's audience. |

| Output | What it is |
| --- | --- |
| `token` | The run's GitHub installation token, masked. Empty in shadow. |
| `expires-at` | When the token expires (ISO 8601). |
| `sessions` | The MCP sessions as JSON, keyed by connector; each is also `session_<connector>`. Masked. |
| `read-token` | Contents read on the repositories the role clones but doesn't write, masked. The post step ends it. |
| `decision` | `granted`, `would_grant` or `refused`. |
| `run` | This run in Airlock's log, `<run id>.<attempt>`. |
| `telemetry` | `true` when the telemetry env was exported, else `false`. |

## Versions

Every release is an immutable `vX.Y.Z` tag, with a moving `vX` tag on the latest. Pin a commit SHA with the tag in a comment, as above; organizations that require SHA pins get nothing less. Only Matthew cuts releases (an org ruleset protects `v*` tags).

## Developing

No dependencies and nothing to build: `action.yml` runs `main.mjs` and `post.mjs` on the runner's Node. `npm test` runs both steps as the runner does, against a stand-in Airlock and GitHub OIDC endpoint (`test/action.test.mjs`).
