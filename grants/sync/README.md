# grants/sync

Keeps a server's agent grants in step with your role files. Some servers (an api, an MCP server) enforce what each role may do on their own routes. They keep a copy of each role's `scopes`, `writes.repos`, `run.bash`, level and floors, which are its grant. This action sends that server exactly what `roles/<role>/role.yaml` says at the pushed commit, so the copy follows `main`. The server should re-read the same file at that commit and refuse a body that differs.

```yaml
name: sync grants
on:
  push:
    branches: [main]
    paths: ["roles/*/role.yaml"]
permissions:
  contents: read
jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: lunareng/airlock-actions/grants/sync@<release commit>
        with:
          api-url: https://api.example.com
          app-id: ${{ secrets.GRANT_SYNC_APP_ID }}
          private-key: ${{ secrets.GRANT_SYNC_PRIVATE_KEY }}
```

| Input | Default | |
| --- | --- | --- |
| `api-url` | | Required: the server's origin. |
| `app-id`, `private-key` | | The GitHub App the server trusts to sync grants. Pass secrets. |
| `roles` | every role the push changed | Space-separated role names. |
| `dry-run` | `false` | `true` prints each body and sends nothing. |

A role whose file has no `scopes` is skipped: there is nothing to grant.

## The protocol a server implements

1. `POST <api>/api/public/agents/sessions`, with `Authorization: Bearer <GitHub App JWT>` and the body `{"runtime", "run_ref", "launched_by"}`. It answers `{"token": <session>}`.
2. `PUT <api>/api/agents/roles/<role>`, with `X-Agent-Session: <session>` and the body `{"sha", "level", "scopes", "repos", "bash_prefixes", "floors"}`. Any answer of 300 or above fails the run.
3. `DELETE <api>/api/agents/sessions/self`, which always runs, so the session ends even when a PUT is refused.
