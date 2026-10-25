#!/usr/bin/env bash
# Open (or update) one GitHub issue per day holding the sweep digest.
# Usage: gh_digest_issue.sh DIGEST_FILE YYYY-MM-DD   (needs GH_TOKEN and the gh CLI)
set -euo pipefail

digest_file="${1:?digest file}"
day="${2:?date}"
label="jobsweep"
title="Job sweep digest ${day}"

if [ ! -s "$digest_file" ] || grep -qiE '^\s*(no new matches|0 new)' "$digest_file"; then
  echo "No new matches; not opening an issue."
  exit 0
fi

gh label create "$label" --color 5319e7 --description "Daily jobsweep digests" >/dev/null 2>&1 || true

body_file="$(mktemp)"
{
  cat "$digest_file"
  echo
  echo "---"
  echo "Board: see the GitHub Pages site for this repository. Statuses you set there stay in your browser."
} > "$body_file"

existing="$(gh issue list --label "$label" --state open --search "\"$title\" in:title" --json number --jq '.[0].number' 2>/dev/null || true)"
if [ -n "$existing" ]; then
  gh issue edit "$existing" --body-file "$body_file"
  echo "Updated issue #$existing"
else
  gh issue create --title "$title" --label "$label" --body-file "$body_file"
fi
rm -f "$body_file"
