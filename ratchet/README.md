# The ratchet

`lunareng/airlock-actions/ratchet` moves levels on evidence, weekly, across every repository your policy repo has a `repos/<name>.yaml` for (the policy repo's own file aside: its level is your owner's to move).

- **A catch** is something your owner had to correct: an issue labelled `catch` in that repository, or a line naming the marker in one of your `catch_logs` files kept there. **An incident** is an issue labelled `incident`, or a catch labelled `severity:high` or `severity:critical`.
- **Promotion is proposed, never applied.** A repository below L4 whose last catch is `weeks` or more weeks ago gets one open `proposal` issue in the policy repo, "Promote <repo> to <level>", answerable yes or no. The clock starts when its `repos/` file landed, or at its last ratchet move (`ratchet.promoted_on` or `ratchet.demoted_on` in the file). L3 to L4 also needs a `judge` and `goals`, and the proposal says when they're missing.
- **Demotion is automatic.** An incident in the last 7 days lowers the repository one level (L4 to L3, L3 to L2) by two tightening PRs on `agent/<role>/demote-<repo>-<date>`: its `repos/` file in the policy repo, with `ratchet.demoted_on` and the evidence, and its own `AUTONOMY.yaml`. Both are set to auto-merge. Nothing is restored automatically.
- **No judge, no L4.** Every L4 repository and L4 role names where its judge runs (`judge.check`: a workflow on a branch, or an issue a report is commented on). A judge that hasn't run (a completed run, success or failure, or the reporter's comment) for its `max_age_days` gets one `catch` + `judge-missing` issue in the policy repo, which resets that repository's clock. One that hasn't run for `judge_demote_days` drops from L4 to L3 by the same tightening PR (for a role, its `role.yaml`). A check that never ran counts from the day it landed. The ratchet never reads a judge's number, and a check it can't read writes nothing.

## Use it

> Not in a release yet: `v1` doesn't carry `ratchet/`. Until a release does, pin `uses:` to the commit on `main` that added it.

In your policy repo, `.github/workflows/role-ratchet.yml`, with a `roles/ratchet/role.yaml` whose `writes` are `repos:` the policy repo and every repository it may demote, `issues: [create]` and `pull_requests: [open, merge]`:

```yaml
name: ratchet
on:
  workflow_dispatch:
    inputs:
      dry_run:
        description: print decisions instead of opening proposals and PRs
        type: boolean
        default: true
permissions:
  contents: read
  id-token: write # the run's proof of who it is, for Airlock
jobs:
  ratchet:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0 # the clocks start when each file landed
      - uses: lunareng/airlock-actions@v1
        id: airlock
        with:
          role: ratchet
      - name: no token, no run
        if: steps.airlock.outputs.token == ''
        run: |
          echo "::error::Airlock handed ratchet no token; the run stops here"
          exit 1
      - uses: lunareng/airlock-actions/ratchet@v1
        with:
          token: ${{ steps.airlock.outputs.token }}
          dry-run: ${{ inputs.dry_run }}
```

| Input | Required | Default | What it is |
| --- | --- | --- | --- |
| `token` | Yes | | The token it reads and writes with: the Airlock action's `token` output. |
| `dry-run` | No | `true` | `false` opens the proposals, catches and PRs. Anything else prints the decisions to the job summary and writes nothing. |
| `role` | No | `ratchet` | The role the run is, for its branches: `agent/<role>/demote-…`. |
| `weeks` | No | `ratchet.weeks` | Clean weeks a promotion proposal needs. |
| `policy-dir` | No | the workspace | The policy repo's checkout, with its full history. |

| Output | What it is |
| --- | --- |
| `report` | The path of the run's decisions as JSON: each repository's clock and decision, each judge's last runs, and what was proposed, demoted and caught. |

The organization is the workflow's own (`github.repository_owner`), and proposals, catches and policy PRs go in the workflow's repository (`github.repository`). It stops, writing nothing, when the token is empty, the checkout is shallow or has no `config.yaml` and `repos/`, or its settings below are malformed.

## Settings

In your policy repo's `config.yaml`. Every key is optional, and the defaults are shown.

```yaml
# Files where a line "- YYYY-MM-DD ..." naming `marker` (a whole word) records a catch in `repo`. The digest reads
# the same list.
catch_logs:
  - {repo: acme/handbook, path: ops/LEARNINGS.md, marker: Wile}
ratchet:
  skip_repos: []            # repos/<name>.yaml files the ratchet never moves (an archived repository)
  weeks: 4                  # clean weeks a promotion proposal needs
  judge_demote_days: 14     # days without a judge run before an L4 drops to L3
  owner_name: the owner     # who the proposals and PRs say merges a promotion and restores a level
  legacy_demote_prefixes: [] # branch prefixes older demotion PRs were opened on, so none is opened twice
```
