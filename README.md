# airlock-actions

The public half of [Airlock](https://airlock.lunareng.com/docs): the action your role workflows call to get each agent run its own key. The key is a GitHub token and MCP sessions, good for one hour and no wider than the role's file in your policy repo, and it is handed back when the job ends.

The full guide is at https://airlock.lunareng.com/docs/role-workflow, and every input and output is at https://airlock.lunareng.com/docs/actions.

## Use it

In your policy repo, in `.github/workflows/role-<role>.yml`:

```yaml
permissions:
  contents: read
  id-token: write # the run's proof of who it is, for Airlock
jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 50
    steps:
      - uses: actions/checkout@v4
      - uses: lunareng/airlock-actions@v1
        id: airlock
        with:
          role: triage
      - name: run the agent
        if: steps.airlock.outputs.token != '' # empty while the role is in shadow
        env:
          GH_TOKEN: ${{ steps.airlock.outputs.token }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: claude -p "$(cat roles/triage/BRIEF.md)" --max-turns 60
```

- **The main step** proves the run with the job's GitHub OIDC token and asks Airlock for the role's token. It masks the token, the sessions and any read-only token before anything else, and hands them to later steps only as outputs.
- **The post step** runs on success, failure and cancel. It hands the token back to Airlock and ends the read-only token at GitHub. It never fails the job: if it can't reach Airlock, it says so, and the token ends with its hour.
- **In shadow** (any role whose file doesn't say `identity.key_store: airlock`), the step passes, prints what Airlock would have handed out, and leaves `token` empty. Turning the role live changes nothing in this file.
- **A refusal** fails the step with Airlock's reason and message. Each reason is listed at https://airlock.lunareng.com/docs/refusals.

## Inputs and outputs

| Input | Required | Default | What it is |
| --- | --- | --- | --- |
| `role` | Yes | | The role this run is: the `{role}` in `roles/{role}/role.yaml` and `.github/workflows/role-{role}.yml`. |
| `airlock-url` | No | `https://airlock.lunareng.com` | Airlock's origin, and the audience of the OIDC token the action requests. |

| Output | What it is |
| --- | --- |
| `token` | The run's GitHub installation token, masked. Empty in shadow. |
| `expires-at` | When the token expires (ISO 8601). |
| `sessions` | The run's MCP sessions as JSON, keyed by connector: `fromJSON(steps.airlock.outputs.sessions).<connector>.token`. |
| `read-token` | A token that can only read the repositories the file's `run.clone` names and its writes don't. Empty when there are none. |

## Versions

Every release has an exact tag (`v1.0.0`) and a major tag (`v1`) that moves to the newest release of that major. Pin `@v1` to follow releases, or a release's full commit SHA to stay put; Dependabot's `github-actions` ecosystem keeps either current. Breaking changes come only in a new major. The action sends its version to Airlock as the `Airlock-Action` header.

## Develop

No dependencies: `src/` is what runs, on the runner's own Node.

```bash
npm test   # both steps against stand-ins for GitHub and Airlock, no network
```

CI also runs the action on a real runner against a stand-in Airlock (`.github/workflows/test.yml`). In that run, a job whose agent step fails still hands its token back, and the job's log shows the token only as `***`.
