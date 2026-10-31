#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  echo "usage: upload-commit.sh <library-dir> <error-log> uploads/<slug>.pdf [...]" >&2
  exit 2
}

[ "$#" -ge 3 ] || usage
LIBRARY_DIR="${1%/}"
ERROR_LOG="$2"
shift 2
TARGET_BRANCH="${UPLOADER_TARGET_BRANCH:-main}"
EXPECTED_REMOTE="${UPLOADER_REMOTE_URL:-git@github.com:chizumizu/Library.git}"
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -o ConnectTimeout=10 -o BatchMode=yes"

mkdir -p "$(dirname "$ERROR_LOG")"
fail() {
  printf '[%s] upload-commit: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$1" >> "$ERROR_LOG"
  exit 1
}

[ -d "$LIBRARY_DIR/.git" ] || fail "not a Git repository"
origin_url=$(git -C "$LIBRARY_DIR" remote get-url origin 2>/dev/null) || fail "origin remote is unavailable"
[ "$origin_url" = "$EXPECTED_REMOTE" ] || fail "origin remote does not match UPLOADER_REMOTE_URL"
branch=$(git -C "$LIBRARY_DIR" symbolic-ref --short HEAD 2>/dev/null) || fail "detached or unborn HEAD"
[ "$branch" = "$TARGET_BRANCH" ] || fail "current branch does not match UPLOADER_TARGET_BRANCH"
[ -n "$TARGET_BRANCH" ] || fail "target branch is empty"
git_dir=$(git -C "$LIBRARY_DIR" rev-parse --git-dir) || fail "could not inspect Git directory"
case "$git_dir" in /*) ;; *) git_dir="$LIBRARY_DIR/$git_dir" ;; esac
for marker in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD BISECT_LOG rebase-apply rebase-merge sequencer; do
  [ ! -e "$git_dir/$marker" ] || fail "a Git operation is in progress"
done

paths=(meta_dt.json)
for path in "$@"; do
  [[ "$path" =~ ^uploads/[a-z0-9._-]+\.pdf$ ]] || fail "refusing unexpected upload path"
  [[ "$path" != *..* ]] || fail "refusing path traversal"
  [ -f "$LIBRARY_DIR/$path" ] || fail "requested PDF is missing"
  paths+=("$path")
done

git -C "$LIBRARY_DIR" diff --quiet --cached || fail "index contains unrelated staged changes"

allowed_path() {
  local candidate="$1" path
  [ "$candidate" = "meta_dt.json" ] && return 0
  for path in "${paths[@]:1}"; do
    [ "$candidate" = "$path" ] && return 0
  done
  return 1
}

status=$(git -C "$LIBRARY_DIR" status --porcelain=v1 --untracked-files=all) || fail "could not inspect workspace state"
while IFS= read -r changed; do
  [ -n "$changed" ] || continue
  if ! allowed_path "${changed:3}"; then
    fail "workspace contains unrelated modified or untracked files"
  fi
done <<< "$status"

for path in "${paths[@]}"; do
  [ -f "$LIBRARY_DIR/$path" ] || fail "requested publish path is missing"
done

if ! git -C "$LIBRARY_DIR" add --sparse -- "${paths[@]}"; then
  fail "could not stage upload batch"
fi

staged=$(git -C "$LIBRARY_DIR" diff --cached --name-only)
while IFS= read -r changed; do
  [ -n "$changed" ] || continue
  if ! allowed_path "$changed"; then
    fail "staging included an unrelated path"
  fi
done <<< "$staged"

if git -C "$LIBRARY_DIR" diff --cached --quiet; then
  fail "upload batch produced no Git changes"
fi

git -C "$LIBRARY_DIR" -c user.name="Library Uploader" -c user.email="uploader@users.noreply.github.com" \
  commit -m "Add uploaded book batch" >/dev/null 2>&1 || fail "could not create upload commit"

git -C "$LIBRARY_DIR" fetch --no-tags origin "$TARGET_BRANCH" >/dev/null 2>&1 || fail "could not fetch target branch"
remote_head=$(git -C "$LIBRARY_DIR" rev-parse FETCH_HEAD 2>/dev/null) || fail "remote branch could not be resolved"
git -C "$LIBRARY_DIR" -c user.name="Library Uploader" -c user.email="uploader@users.noreply.github.com" \
  rebase "$remote_head" >/dev/null 2>&1 || fail "remote changes conflict with upload; local commit preserved for recovery"
git -C "$LIBRARY_DIR" push origin "HEAD:refs/heads/$TARGET_BRANCH" >/dev/null 2>&1 || fail "push failed; local commit preserved for retry"
local_head=$(git -C "$LIBRARY_DIR" rev-parse HEAD)
published_head=$(git -C "$LIBRARY_DIR" ls-remote origin "refs/heads/$TARGET_BRANCH" | awk 'NR == 1 { print $1 }')
[ "$local_head" = "$published_head" ] || fail "remote did not confirm the published commit"

printf '[%s] upload-commit: publish succeeded to %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$TARGET_BRANCH" >&2
