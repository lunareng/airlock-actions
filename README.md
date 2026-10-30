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
          signal: ${{ inputs.signal }} # the issue or pull request the run was dispatched for, for its report
      - name: run the agent
        if: steps.airlock.outputs.token != '' # empty while the role is in shadow
        env:
          GH_TOKEN: ${{ steps.airlock.outputs.token }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: claude -p "$(cat roles/triage/BRIEF.md)" --max-turns 60
```

- **The main step** proves the run with the job's GitHub OIDC token and asks Airlock for the role's token. It masks the token, the sessions and any read-only token before anything else, and hands them to later steps only as outputs.
- **Telemetry, by default:** when your tenant sends role runs' telemetry to Airlock (one switch in Airlock's Telemetry settings, with a per-role opt-out), Airlock's answer carries Claude Code's telemetry env with a session for this run alone. The step masks the session and exports the env to every later step, live or shadow, so Claude Code sends the run's cost, tokens and tool use to Airlock with nothing to configure. It exports only `CLAUDE_CODE_ENABLE_TELEMETRY` and `OTEL_*` variables, and only to Airlock's own `/v1/otlp`; anything else, and the run goes on without telemetry.
- **The post step** runs on success, failure and cancel. It hands the token back to Airlock and ends the read-only token at GitHub. It never fails the job: if it can't reach Airlock, it says so, and the token ends with its hour.
- **In shadow** (any role whose file doesn't say `identity.key_store: airlock`), the step passes, prints what Airlock would have handed out, and leaves `token` empty. Turning the role live changes nothing in this file.
- **A refusal** fails the step with Airlock's reason and message. Each reason is listed at https://airlock.lunareng.com/docs/refusals.

## Inputs and outputs

| Input | Required | Default | What it is |
| --- | --- | --- | --- |
| `role` | Yes | | The role this run is: the `{role}` in `roles/{role}/role.yaml` and `.github/workflows/role-{role}.yml`. |
| `signal` | No | | What the run was dispatched for: an issue or pull request URL in your organization. Recorded for the run's report. |
| `airlock-url` | No | `https://airlock.lunareng.com` | Airlock's origin, and the audience of the OIDC token the action requests. |

| Output | What it is |
| --- | --- |
| `token` | The run's GitHub installation token, masked. Empty in shadow. |
| `expires-at` | When the token expires (ISO 8601). |
| `sessions` | The run's MCP sessions as JSON, keyed by connector: `fromJSON(steps.airlock.outputs.sessions).<connector>.token`. |
| `read-token` | A token that can only read the repositories the file's `run.clone` names and its writes don't. Empty when there are none. |
| `telemetry` | `true` when Claude Code's telemetry env was exported to the later steps; `false` when the tenant's telemetry is off. |

## The gate

`lunareng/airlock-actions/gate` is the autonomy gate every repository runs on its pull requests. It scores the PR's level from the paths it touches, its `AUTONOMY.yaml` and its file in your policy repo (the stricter of the two), and labels it `level:Lx`. It fails an agent's L1 or L2 PR until your human owner approves its current commit, and says why on the Checks tab.

It reads your policy repo through Airlock, so the workflow must be `.github/workflows/autonomy-gate.yml` and its job must be named `autonomy-gate`:

```yaml
name: autonomy gate
on:
  pull_request:
    types: [opened, synchronize, reopened, edited, auto_merge_enabled, auto_merge_disabled]
  pull_request_review:
    types: [submitted, dismissed]
permissions:
  contents: read
  pull-requests: write
  id-token: write   # the OIDC token Airlock reads the policy for
  actions: write    # a review re-runs the push-time job, so the PR keeps one autonomy-gate row
jobs:
  gate:
    name: autonomy-gate
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: lunareng/airlock-actions/gate@v1
```

| Input | Default | |
| --- | --- | --- |
| `airlock-url` | `https://airlock.lunareng.com` | Airlock's origin. |
| `rerun-wait-seconds` | `300` | On a review, how long to wait for a push-time run still going. |

| Output | |
| --- | --- |
| `level` | The PR's level, `L1` to `L4`. |
| `passed` | `true` or `false` once the level is computed; empty if the gate stopped before. |
## The role workflow

`lunareng/airlock-actions/.github/workflows/role.yml` is one whole role run, so a role's workflow in your policy repo is only its triggers and this call. It reads `roles/<role>/role.yaml` at the run's commit:
- the run's token, MCP sessions and telemetry from Airlock;
- the repositories it clones (on its token for those it writes to, on a read-only token for the rest);
- its model, turns, allowed tools and minutes.

```yaml
name: role triage
on:
  workflow_dispatch:
    inputs:
      signal:
        type: string
        default: ""
permissions:
  contents: read
  id-token: write
concurrency:
  group: role-triage
jobs:
  run:
    uses: lunareng/airlock-actions/.github/workflows/role.yml@v1
    with:
      role: triage
      signal: ${{ inputs.signal }}
    secrets:
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

The file must be `.github/workflows/role-<role>.yml` on your policy repo's default branch. Airlock accepts this workflow only at a release tag or a release's commit, never a branch (`workflow_unreleased`).

| Input | Default | |
| --- | --- | --- |
| `role` | | The role's name. |
| `signal` | `""` | A signal URL for the run to lead with. |
| `runner` | `ubuntu-latest` | The runner label for the run's jobs. |
| `airlock-url` | `https://airlock.lunareng.com` | Airlock's origin. |
| `git-email-domain` | `users.noreply.github.com` | Commits are by `<role>@<domain>`. |
| `claude-code-version` | `2.1.304` | The Claude Code release the run installs. |

## Validate

`lunareng/airlock-actions/validate` checks your policy repo on its pull requests. It covers every `repos/*.yaml`, `AUTONOMY.yaml` and `roles/*/role.yaml`, against two things:
- **the schemas Airlock publishes,** fetched at run time, so your repo keeps no copy;
- **the rules every tenant shares:**
  - no action or path above the charter's floors;
  - carve-outs only tighten;
  - each repository file names its own repository;
  - a role's repositories are your organization's, and the ones it writes to have a policy file;
  - a role's brief and goals exist;
  - an L4 judge runs in CI, with a check.

Each problem is an error annotation on its file and line. With `base`, a level loosened since that commit, or a removed carve-out, is a problem too: a loosening waits for your human owner.

```yaml
on: pull_request
permissions:
  contents: read
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: lunareng/airlock-actions/validate@v1
        with:
          base: ${{ github.event.pull_request.base.sha }}
```

| Input | Default | |
| --- | --- | --- |
| `airlock-url` | `https://airlock.lunareng.com` | Where the schemas are published. |
| `base` | `""` | A commit to compare levels with; any loosening since it is a problem. |

Output `problems`: how many it found.

## Versions

Every release has an exact tag (`v1.0.0`) and a major tag (`v1`) that moves to the newest release of that major. Pin `@v1` to follow releases, or a release's full commit SHA to stay put; Dependabot's `github-actions` ecosystem keeps either current. Breaking changes come only in a new major. The action sends its version to Airlock as the `Airlock-Action` header.

## Develop

No dependencies: `src/` is what runs, on the runner's own Node.

```bash
npm test                          # both steps against stand-ins for GitHub and Airlock, no network
python3 test/gate/test_gate.py    # the gate's scoring, verdicts and re-run plan (needs pyyaml)
python3 test/gate/test_policy.py  # its policy read, against a stand-in Airlock and GitHub on 127.0.0.1
```

CI also runs the action on a real runner against a stand-in Airlock (`.github/workflows/test.yml`). In that run, a job whose agent step fails still hands its token back, and the job's log shows the token only as `***`.
