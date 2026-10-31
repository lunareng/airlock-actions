# `lunareng/airlock-actions/router`: the signal router

One pass of the signal router over a tenant's repositories. Signals become issues an agent can work from cold, and
the role that owns the work is started with the signal:

| Rule | What it does |
| --- | --- |
| `product_bug` | An issue filed from a sim incident (label `product-bug`, or a link to an issue in one of `product_bugs.sim_repos`) with no `## Signal` gets one, plus `type:bug`, the sim's product label and a severity from the incident signature. |
| `tool_request` | A `tool-request` with no `## Signal` gets one naming the requester, `type:feature` and `tool_request_label`, and the repo's `owner_role` is started with the issue as its signal. |
| `review` | A person's review on an agent's open PR (changes requested, an inline comment, or a comment that @-mentions the agent, newer than the head commit) starts the owning role, once per newest comment. |
| `resume` | A `tool-request` a role deferred for its PR budget (`pr_budget`) starts the role again once one of its PRs closes. |
| `digest_answer` | The human owner's "1 yes, 2 no" on an open `digest` issue in the policy repo starts `digest_answers.role`. |
| `ci_main` | A failing workflow on a repo's main gets one `CI: <workflow> failing on main` issue; the router closes it when main is green again. |
| `delivered` | A merged PR whose `## Signal` names an open `tool-request` without `Closes` gets one comment (and `delivered?` when it named no keyword). |
| `run_failed` | A failing role workflow (`role-*.yml`) gets one `Role run: <role> failing` issue, a comment per further failure, the human owner mentioned at the third in a row; closed on the next success. |
| `override` | A PR merged over a FAIL `claude-review` verdict gets one comment asking whoever merged it for `Override: <one line>`. |
| `gate_rerun` | A commit on the policy repo's main that changes what the gate scores by re-runs `autonomy-gate` on the open PRs it touches. |

It writes nothing but issues, labels, comments, role dispatches and gate re-runs, and every rule checks GitHub
before it writes, so a second pass writes nothing. It never names a tenant: the organization is the policy repo's
owner (`GITHUB_REPOSITORY`), the policy repo is the checkout, and everything else is the policy repo's
`router.yaml`. Before a pass it checks `router.yaml` against the policy repo's own files and routes nothing if any
check fails.

## Use

From the router role's own workflow in the policy repo, `.github/workflows/role-<role>.yml` (Airlock mints a role's
token only for that workflow), after the Airlock action has minted the role's token:

```yaml
permissions:
  contents: read
  id-token: write   # the OIDC token Airlock mints this run's GitHub token for
concurrency:
  group: role-router
  cancel-in-progress: false   # two passes reading the same issues before either writes would both write
jobs:
  route:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<sha>
        with:
          fetch-depth: 0   # gate_rerun reads the last window_days of the policy repo's commits
      - uses: actions/setup-node@<sha>
        with:
          node-version: "22"
      - id: app
        uses: lunareng/airlock-actions@<sha>
        with:
          role: router
          airlock-url: https://airlock.example.com
      - uses: lunareng/airlock-actions/router@<sha>
        with:
          token: ${{ steps.app.outputs.token }}
          dry-run: ${{ inputs.dry_run }}
```

| Input | Default | |
| --- | --- | --- |
| `token` | (required) | The GitHub token for the pass. Empty: the pass routes nothing and fails. |
| `dry-run` | `true` | `true` reads GitHub and prints the writes it would make; `false` makes them. Anything else fails. |
| `config` | `router.yaml` | The router's config, relative to the checkout. |
| `role` | `router` | The router's role (`roles/<role>/role.yaml`): its `identity.github_login` counts as the router's own, beside `config.yaml` `airlock.app_login`. |

Output `report`: the path of the pass's JSON report (`routed`, `dispatched`, `closed`, `routed_within_1h`, the
router's judge number, and `writes`). The job summary shows it too. A refused gate re-run fails the pass after
the report.

The token needs, on every repo the config names: Issues write, Pull requests read, Actions read (and write where
`gate_rerun` re-runs and on the policy repo, where it starts role workflows), Contents read and Metadata read. In a
role file that is `writes.issues`, `writes.reads: [actions]` and `writes.dispatch: true` on the repos in
`writes.repos`.

## `router.yaml`

At the top of the policy repo. Every repo is `owner/name` in the policy repo's own organization. A key the router
doesn't know is a problem, so a typo never switches a rule off silently.

```yaml
product_bugs:                    # product_bug: where sim triage files, and each sim repo's product label
  repos: [acme/api]
  sim_repos: {sim-shop: product:shop}
tool_requests: [acme/api, acme/cli]   # tool_request, resume and delivered: every repo a role writes to
tool_request_label: product:core      # optional: the product label a request with none gets
reviews: [acme/api, acme/cli]         # review: repos whose agent PRs a person's review wakes the owner of
delivered: true                       # or {backfill: [<from>, <until>]}: look back to <from> until <until>
resume: true
pr_budget: {default: 2, solo-agent: 1}   # open PRs a role may hold, across its writes.repos
digest_answers: {role: clerk}         # optional label: digest (the label of the digest issue)
override: true
deployer_login: "acme-deployer[bot]"  # optional: whose merged PRs override skips
clock_login: "acme-clock[bot]"        # optional: whose dispatches read as scheduled runs in run-failure issues
gate_rerun:
  repos: [acme/api]
  window_days: 7
  max_per_pass: 20
run_failed: [acme/policy]             # repos whose role-*.yml workflows run_failed watches
ci_main: {acme/api: product:core}     # repo: the product label its CI-failure issues carry
```

It also reads the policy repo's `config.yaml` (`human_owner`, `agent_markers.logins`, `airlock.app_login`,
`required_checks`), `repos/<name>.yaml` (`owner_role`) and `roles/<role>/role.yaml` (`identity`, `writes.repos`,
`writes.dispatch`).

The checks before every pass:

- every role's `writes.repos` is in some list here, so a repo the router's token can't read is probed and skipped
  rather than ending the pass;
- every `gate_rerun` repo is in the router role's `writes.repos`, with `writes.dispatch: true` (a re-run needs Actions
  write), and has a `repos/<name>.yaml`;
- every repo is in the policy repo's organization;
- `product_bugs` is a mapping and `digest_answers` names its role.

## Tests

`python3 test/router/test_router.py` (needs pyyaml), on the invented acme tenant in `test/router/fixtures/policy`.
`.github/workflows/test-router.yml` runs it on every pull request and push to main.
