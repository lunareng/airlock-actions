# The digest

`lunareng/airlock-actions/digest` writes the week's digest: one issue in your policy repo, labelled `digest` and titled `Digest: week of <Monday>`, that your owner reads in about ten minutes and answers on ("1 yes, 2 no"). It reads every repository your policy repo has a `repos/<name>.yaml` for, in your organization, and writes only that one issue, updating it in place when the week's issue exists. It never closes anything.

Sections, each at most five lines, empty ones left out:

| Section | What it shows |
| --- | --- |
| Proposals | Open `proposal` issues across your repositories, oldest first, numbered for the yes/no answer. |
| Shipped | Pull requests merged this week, with the `level:Lx` label the gate set, and `agent` or `human` by `agent_markers.branch_prefixes`. |
| Waiting on you | Open, non-draft pull requests labelled `level:L1` or `level:L2`. |
| Declined | Open issues labelled `declined` that changed this week. |
| Health | Role runs this week in the policy repo (`role-*.yml` runs started by `workflow_dispatch`, workflow roles left out, cancelled and skipped runs not counted), open `ci-failure` issues, and the repositories the token could not read. |
| Catches | Lines dated this week in each `catch_logs` file that name its marker. |
| Delivered? | Open issues the router labelled `delivered?`. |
| Overrides | Pull requests merged over a FAIL `claude-review` verdict since last week's Monday, with the reason the router asked for, or "no reason given" after 7 days. |

## Use it

> Not in a release yet: `v1` doesn't carry `digest/`, so the example pins `digest/` to a commit on `main` (`<sha>`) until a release does.

In your policy repo, `.github/workflows/role-digest.yml`, with a `roles/digest/role.yaml` whose `writes` are `repos: [<your org>/<policy repo>]` and `issues: [create, edit_body]`:

```yaml
name: digest
on:
  workflow_dispatch:
    inputs:
      dry_run:
        description: print the digest instead of writing the issue
        type: boolean
        default: true
permissions:
  contents: read
  id-token: write # the run's proof of who it is, for Airlock
jobs:
  digest:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: lunareng/airlock-actions@v1
        id: airlock
        with:
          role: digest
      - name: no token, no run
        if: steps.airlock.outputs.token == ''
        run: |
          echo "::error::Airlock handed digest no token; the run stops here"
          exit 1
      - uses: lunareng/airlock-actions/digest@<sha> # a commit on main, until a release carries digest/
        with:
          token: ${{ steps.airlock.outputs.token }}
          dry-run: ${{ inputs.dry_run }}
```

| Input | Required | Default | What it is |
| --- | --- | --- | --- |
| `token` | Yes | | The token it reads and writes with: the Airlock action's `token` output. |
| `dry-run` | No | `true` | `false` writes the issue. Anything else prints the digest to the job summary and writes nothing. |
| `week-of` | No | this week | A date (`YYYY-MM-DD`) in the week to digest. |
| `repos` | No | every `repos/*.yaml` | `owner/name` repositories in your organization, comma-separated, in place of the policy repo's list. |
| `policy-dir` | No | the workspace | The policy repo's checkout. |

| Output | What it is |
| --- | --- |
| `url` | The digest issue's URL. Empty on a dry run. |

The organization is the workflow's own (`github.repository_owner`) and the issue goes in the workflow's repository (`github.repository`), and every repository it reads (`repos`, `catch_logs`) must be in that organization. It stops, writing nothing, when the token is empty, the checkout has no `config.yaml` and `repos/`, a key it reads is malformed, a repository named is outside the organization, or the lookup of the week's open digest issue fails (rather than open a second one).

## Settings

In your policy repo's `config.yaml`. Every key is optional.

```yaml
# Files where a line "- YYYY-MM-DD ..." naming `marker` (a whole word) records a catch: something the owner had
# to correct. The digest lists this week's; the ratchet counts them against that repository's clean weeks.
catch_logs:
  - {repo: acme/handbook, path: ops/LEARNINGS.md, marker: Wile}
digest:
  skip_repos: [archive]              # repos/<name>.yaml files the digest leaves out (an archived repository)
  router_logins: ["acme-router[bot]"] # logins whose override marker counts, besides airlock.app_login
```

It also reads `required_checks` (which repositories require `claude-review`, for Overrides), `agent_markers.branch_prefixes` (agent or human, for Shipped), `airlock.app_login`, and `kind: workflow` in `roles/*/role.yaml` (the roles left out of the role-runs line).

The Overrides section reads the router's marker, `<!-- router:override pr=<n> -->`, and the reply `Override: <one line>`; both are the router's contract, and the digest keeps its own copy of them.
