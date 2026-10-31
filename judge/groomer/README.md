# judge/groomer

The groomer's judge: the numbers a backlog-grooming role is held to and cannot write. It reads GitHub and nothing
else, and fails its step when the pass condition fails or when it can't measure.

Over the trailing `weeks` (4) it finds every **pick** (an issue given `pick-label`, `triage:this-week` by default)
in the repositories it measures, and reports:

| Field | What it counts |
| --- | --- |
| `pick_conversion` | Picks that reached a merged PR within `window-days` (14) of being picked, over all picks. A PR counts when its title or body says `#N` (in the issue's own repository) or `<repo>#N` (anywhere in the repository owner's organization, through search). `null` with no picks. |
| `picks_without_signal` | Picks whose body has no `## Signal` section with a link, an `owner/repo#N` or a `#N` in it. |
| `unranked_over_7d` | Open issues older than 7 days with no label starting with `priority-prefix` (`priority:`). |
| `stale_demand` | Open `demand-label` (`type:demand`) issues missing a `demand-fields` line (`field: value`), or whose `looked_up:` date is more than 14 days old. |

It **passes** when there were no picks, or when `pick_conversion >= min-conversion` (0.5) and
`picks_without_signal <= max-without-signal` (0). The last two numbers are reported, not judged: they are the
role's own hygiene goals.

The repositories are `writes.repos` in `roles/<role>/role.yaml` in the policy repo checkout, unless `repos` names
them. Nothing in the action names an organization: each repository's owner scopes its own search.

## Use

In the policy repo, a workflow the groomer can't write (its `judge.check` names it):

```yaml
name: judge groomer
on:
  workflow_dispatch: {}
  schedule:
    - cron: "0 12 * * 1"
permissions:
  contents: read
  issues: read
  pull-requests: read
jobs:
  judge:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: lunareng/airlock-actions/judge/groomer@<sha> # vX.Y.Z
        with:
          token: ${{ secrets.READ_TOKEN || github.token }}
```

`github.token` reads only the policy repo. To measure the product repositories, pass a token that reads their
issues and pull requests (a fine-grained token with Issues and Pull requests read on the organization). Without
one, every read of another repository fails and the judge reports it couldn't measure.

## Inputs

| Input | Default | |
| --- | --- | --- |
| `role` | `groomer` | whose role file names the repositories |
| `repos` | the role file's | `owner/name` list, comma or space separated |
| `pick-label` | `triage:this-week` | the label that makes an issue a pick |
| `priority-prefix` | `priority:` | an issue with no such label is unranked |
| `demand-label` | `type:demand` | |
| `demand-fields` | `mrr_at_risk pipeline_blocked askers moat looked_up` | `looked_up` is always one |
| `weeks`, `window-days` | `4`, `14` | |
| `min-conversion`, `max-without-signal` | `0.5`, `0` | the pass condition; keep them equal to the role file's `judge.pass` |
| `token` | `github.token` | read-only use, through `gh api` |
| `policy-path` | `.` | the policy repo checkout |
| `artifact` | `groomer-judge` | upload the report under this name; empty for none |

## Outputs and exit

`pass` (`true` or `false`), `pick-conversion` (empty with no picks) and `report` (the JSON report's path). The
report is also the job summary. The step fails with the judge's exit: 1 when the pass condition fails, 2 when it
couldn't measure (no role file, no `writes.repos`, a repository that isn't `owner/name`, a GitHub read that failed
three times). It never passes by default.

## Tests

`python3 test/judge-groomer/test_judge_groomer.py` (needs pyyaml) runs the judge against recorded GitHub answers for
an invented `acme` organization (`test/judge-groomer/fixtures/`) through a fake `gh`, as of a fixed date.
`.github/workflows/test-judge-groomer.yml` runs that, then the action itself against the same fake.
