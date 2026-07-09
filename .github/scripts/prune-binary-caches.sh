#!/usr/bin/env bash
#
# Keep at most $KEEP GitHub Actions caches whose key starts with $PREFIX,
# deleting the oldest beyond that. This bounds the per-binary, per-platform
# build cache (see issue #156) to a small number of recent upstream versions,
# so bouncing between a handful of upstream commits stays a cache hit while
# stale versions are reclaimed instead of relying solely on GitHub's global
# eviction.
#
# Run this before the cache created by the current job is saved (the actions/
# cache post step runs later), so it counts only pre-existing caches. Keeping
# $KEEP here means the steady state is $KEEP (plus at most the one this run is
# about to add, pruned by the next run).
#
# Inputs (environment):
#   GH_TOKEN  token with actions:write on $REPO (required; use github.token)
#   REPO      owner/name of the repository (required)
#   PREFIX    cache-key prefix to prune (required)
#   KEEP      max number of caches to retain for the prefix (optional, default 5)

set -euo pipefail

repo="${REPO:?REPO must be set}"
prefix="${PREFIX:?PREFIX must be set}"
keep="${KEEP:-5}"

# List caches newest-first (paginated), keep only those matching the prefix,
# skip the newest $keep, and delete the rest by id. @tsv keeps id and key on a
# single tab-separated line so a key can never be split on whitespace.
gh api --paginate \
  "repos/${repo}/actions/caches?per_page=100&sort=created_at&direction=desc" \
  --jq '.actions_caches[] | [.id, .key] | @tsv' \
  | awk -F '\t' -v p="$prefix" 'index($2, p) == 1 { print $1 }' \
  | tail -n "+$((keep + 1))" \
  | while read -r id; do
      [ -n "$id" ] || continue
      echo "Deleting old cache id=${id}"
      gh api -X DELETE "repos/${repo}/actions/caches/${id}" >/dev/null
    done
