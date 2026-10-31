# adopt

Tools a person runs to bring an organization's repositories under its policy repo. They need `gh` (signed in), `git`,
Python 3.10 or later with pyyaml, a checkout of this repository at the commit your policy pins, and a checkout of the
policy repo.

```bash
A=path/to/airlock-actions      # at the commit config.yaml airlock_actions.ref names
P=path/to/policy               # the policy repo, on main

python3 $A/adopt/adopt.py rollout  --org acme --policy $P --signal "acme/policy#12"   # every repos/*.yaml but adopt.skip
python3 $A/adopt/adopt.py rollout  --org acme --policy $P --signal "acme/policy#12" api web
python3 $A/adopt/adopt.py new-repo --org acme --policy $P blog
python3 $A/adopt/adopt.py protect  --org acme --policy $P blog [--dry-run]
python3 $A/adopt/adopt.py render   --policy $P --out /tmp/acme-workflows
```

| Command | What it does | Needs |
| --- | --- | --- |
| `rollout` | Opens one PR per repository, on `adopt.branch`, wherever its `.github/workflows/autonomy-gate.yml`, `.github/workflows/claude-review.yml` or the autonomy section of `CLAUDE.md` differ from the policy's. A repository that already matches gets no PR, and an open PR is updated, not duplicated. A repository whose `package.json` uses prettier gets the files formatted with its own prettier. `--signal` heads every PR's `## Signal`. | push and PR rights on each repository |
| `new-repo` | After `repos/<name>.yaml` (with an `owner_role`) has merged: creates the repository (private, `main`, rebase merges only, auto-merge on, branches deleted on merge), then opens its first PR on `chore/autonomy-policy`. That PR has `AUTONOMY.yaml` (the policy file), `.github/CODEOWNERS` (the generated one), the two workflows, `seeds/<name>/` and the CLAUDE.md section. Then it protects `main`. You can run it again safely: it creates the repository only if it's missing, populates it only once, and protects it every time. | an org owner |
| `protect` | `PUT /repos/<org>/<repo>/branches/main/protection` with the repository's generated body. `--dry-run` prints the request instead. | repository admin |
| `render` | Writes the two workflows as this policy renders them, so you can read them. | nothing |

Each tool fails closed. A missing or malformed setting stops it before it calls GitHub. So does a stale generated file: run [generate](../generate/README.md) with `--write` and merge that first. `new-repo` also stops if the policy checkout isn't on `main`. When `rollout` can't clone or push one repository, it says so and moves on to the next, then exits 1.

## What every repository gets

- `adopt/templates/autonomy-gate.yml` and `adopt/templates/claude-review.yml`, with each `<airlock:…>` filled from `config.yaml`. The gate's job is `autonomy-gate`, and the review's jobs are `claude-review` and `claude-security-review`. Those are the check names branch protection requires.
- The CLAUDE.md section is the policy repo's own text, from the file `adopt.claude_section` names, because what it says (who the owner is, how work is handed over) is the tenant's policy. It must start with `<!-- autonomy:begin` and end with `<!-- autonomy:end -->`. Each repository's `CLAUDE.md` gets that block in place of its old one; the rest of the file stays as it was.

## Settings, in the policy repo's `config.yaml`

The tools also read generate's settings: `airlock_actions.ref` and `.note` pin the workflows, and `generated.*` locates the CODEOWNERS and protection bodies.

| Key | Default | What |
| --- | --- | --- |
| `adopt.runner` | `ubuntu-latest` | The runner label the gate and review jobs use. |
| `adopt.runner_note` | | A one-line comment after the first `runs-on:` in each workflow. |
| `adopt.agent_logins` | `*[bot]` | The logins whose PRs the review verdict binds (review's `agent-logins`). |
| `adopt.human_owner_name` | `the human owner` | Who merges L1/L2 PRs, as the reviewer names them (review's `human-owner`). |
| `adopt.claude_section` | `seeds/CLAUDE-section.md` | The CLAUDE.md section, a file in the policy repo. |
| `adopt.skip` | `[]` | Repositories `rollout` leaves out when given none, such as the policy repo itself or an archived one. |
| `adopt.branch` | `airlock/adopt` | The branch `rollout` pushes. |

```yaml
# acme's config.yaml
adopt:
  runner: acme-ci
  agent_logins: "acme-*[bot]"
  human_owner_name: Dana
  skip: [policy]
```

## Tests

`python3 test/adopt/test_adopt.py` runs every command against the invented acme policy repo, `test/generate/fixtures/policy`. GitHub is played by a stand-in `gh`, `test/adopt/fake_gh.py`, backed by bare repositories. To check the templates against a policy's current workflows byte for byte, set `REPRODUCE=<policy checkout>` and `REPRODUCE_TEMPLATES=<dir>`.
