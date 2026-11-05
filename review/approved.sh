#!/usr/bin/env bash
# Exit 0 when a person with write access approved this exact commit: each person's latest review that says anything
# (never a bot or a deleted account), with no change still requested by anyone who can write. Anyone can review a public
# repository, so a stranger's approval is none. Write access is read from the repository's own answer, because
# author_association depends on who is asking: a private org member reads as a contributor to the workflow's token.
# env: GH_TOKEN, REPO, PR, SHA
set -uo pipefail
# Every page first, then each person's latest: per-page grouping would keep an approval a later page takes back.
reviews="$(gh api --paginate "repos/$REPO/pulls/$PR/reviews?per_page=100" \
  --jq '.[] | select(((.user.login // "[bot]") | endswith("[bot]") | not) and (.state == "APPROVED" or .state == "CHANGES_REQUESTED" or .state == "DISMISSED")) | "\(.submitted_at) \(.user.login) \(.state) \(.commit_id)"')" || exit 1
latest="$(printf '%s\n' "$reviews" | sort | awk 'NF { last[$2] = $2 " " $3 " " $4 } END { for (k in last) print last[k] }')"
ok=1
while read -r login state commit; do
  [ -n "${login:-}" ] || continue
  # A lookup that fails decides nothing: it could be hiding a change still requested.
  if ! permission="$(gh api "repos/$REPO/collaborators/$login/permission" --jq .permission)"; then
    echo "::warning::could not read $login's permission on $REPO, so no approval counts on this run"
    exit 1
  fi
  case "$permission" in admin|maintain|write) ;; *) continue ;; esac
  if [ "$state" = CHANGES_REQUESTED ]; then exit 1; fi
  if [ "$state" = APPROVED ] && [ "$commit" = "$SHA" ]; then ok=0; fi
done <<< "$latest"
exit "$ok"
