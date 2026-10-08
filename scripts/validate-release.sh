#!/usr/bin/env bash
# Run from trusted main before a separate publisher creates the tag.
set -Eeuo pipefail

if [[ $# != 2 || ! "$2" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'usage: validate-release.sh VERSION FULL_COMMIT_SHA' >&2
  exit 1
fi
version=$1
commit=$2
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY must name the release repository}"
git fetch --tags origin +refs/heads/main:refs/remotes/origin/main
trusted_sha=$(git rev-parse origin/main)
runtime=$(mktemp -d)
trap 'rm -rf "$runtime"' EXIT
for script in validate_commits.py scan_committed_secrets.py; do
  git show "$trusted_sha:scripts/$script" >"$runtime/$script"
done
python3 "$runtime/validate_commits.py" --version "$version"
if git show-ref --verify --quiet "refs/tags/$version"; then
  echo "tag '$version' already exists" >&2
  exit 1
fi
release_base=$(python3 "$runtime/validate_commits.py" --release-base-for "$commit")
python3 "$runtime/validate_commits.py" --version "$version" --tag-commit "$commit" \
  --base "$release_base" --head "$commit"
if [[ -n "$release_base" ]]; then
  python3 "$runtime/validate_commits.py" --base "$release_base" --head "$commit"
  git rev-list --reverse "$release_base..$commit" >"$runtime/commits"
else
  # Legacy commit messages remain exempt, but the proposed commit is checked.
  if parent=$(git rev-parse "$commit^" 2>/dev/null); then
    python3 "$runtime/validate_commits.py" --base "$parent" --head "$commit"
  else
    python3 "$runtime/validate_commits.py" --head "$commit"
  fi
  # Credentials are scanned across the full reachable history on first release.
  git rev-list --reverse "$commit" >"$runtime/commits"
fi
# Include the final tree even when the proposed commit range is empty.
printf '%s\n' "$commit" >>"$runtime/commits"
while IFS= read -r revision; do
  python3 "$runtime/scan_committed_secrets.py" --repository . --commit "$revision"
done <"$runtime/commits"

gh api --paginate "repos/$GITHUB_REPOSITORY/commits/$commit/check-runs?filter=latest&per_page=100" \
  --jq '.check_runs[] | {name,head_sha,status,conclusion,app_id:.app.id}' >"$runtime/checks.jsonl"
python3 - "$runtime/checks.jsonl" "$commit" <<'PY'
import json
import sys
from pathlib import Path
checks = [json.loads(line) for line in Path(sys.argv[1]).read_text().splitlines()]
for name in (
    "Build without registering a runner",
    "Enforce conventional commits and pull-request title",
):
    matching = [check for check in checks if check["name"] == name and check["app_id"] == 15368]
    if not matching or any(check["head_sha"] != sys.argv[2]
                           or check["status"] != "completed"
                           or check["conclusion"] != "success" for check in matching):
        raise SystemExit(f"release requires successful exact-commit check: {name}")
PY
printf 'Validated %s at %s; no tag was created.\n' "$version" "$commit"
