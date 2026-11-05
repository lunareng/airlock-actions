#!/usr/bin/env bash
# Exit 0 when a person with write access approved this exact commit: each person's latest review that says anything
# (never a bot or a deleted account), with no change still requested by anyone who can write. Anyone can review a public
# repository, so a stranger's approval is none. Write access is read from the repository's own answer, because
# author_association depends on who is asking: a private org member reads as a contributor to the workflow's token.
# env: GH_TOKEN, REPO, PR, SHA
set -uo pipefail
reviews="$(gh api --paginate "repos/$REPO/pulls/$PR/reviews?per_page=100" \
  --jq '[.[] | select(((.user.login // "[bot]") | endswith("[bot]") | not) and (.state == "APPROVED" or .state == "CHANGES_REQUESTED" or .state == "DISMISSED"))] | group_by(.user.login) | map(max_by(.submitted_at)) | .[] | "\(.user.login) \(.state) \(.commit_id)"')" || exit 1
ok=1
while read -r login state commit; do
  [ -n "${login:-}" ] || continue
  permission="$(gh api "repos/$REPO/collaborators/$login/permission" --jq .permission 2>/dev/null)" || permission=none
  case "$permission" in admin|maintain|write) ;; *) continue ;; esac
  if [ "$state" = CHANGES_REQUESTED ]; then exit 1; fi
  if [ "$state" = APPROVED ] && [ "$commit" = "$SHA" ]; then ok=0; fi
done <<< "$reviews"
exit "$ok"
