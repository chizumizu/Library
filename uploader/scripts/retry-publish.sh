#!/usr/bin/env bash
set -Eeuo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: retry-publish.sh <library-dir>" >&2
  exit 2
fi
LIBRARY_DIR="${1%/}"
TARGET_BRANCH="${UPLOADER_TARGET_BRANCH:-main}"
EXPECTED_REMOTE="${UPLOADER_REMOTE_URL:-git@github.com:chizumizu/Library.git}"
LOCK_FILE="${UPLOADER_LOCK_FILE:-$(dirname "$LIBRARY_DIR")/.library-uploader.lock}"
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -o ConnectTimeout=10 -o BatchMode=yes"

mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "uploader workspace is busy; retry in 15 seconds" >&2
  exit 75
fi

branch=$(git -C "$LIBRARY_DIR" branch --show-current)
[ "$branch" = "$TARGET_BRANCH" ] || { echo "checkout branch does not match target" >&2; exit 1; }
origin_url=$(git -C "$LIBRARY_DIR" remote get-url origin)
[ "$origin_url" = "$EXPECTED_REMOTE" ] || { echo "origin does not match configured remote" >&2; exit 1; }
git_dir=$(git -C "$LIBRARY_DIR" rev-parse --git-dir)
case "$git_dir" in /*) ;; *) git_dir="$LIBRARY_DIR/$git_dir" ;; esac
for marker in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD BISECT_LOG rebase-apply rebase-merge sequencer; do
  [ ! -e "$git_dir/$marker" ] || { echo "a Git operation is in progress; resolve it before retrying" >&2; exit 1; }
done
[ -z "$(git -C "$LIBRARY_DIR" status --porcelain=v1 --untracked-files=all)" ] || {
  echo "checkout has uncommitted changes; inspect and preserve them before retrying" >&2
  exit 1
}
git -C "$LIBRARY_DIR" fetch --no-tags origin "$TARGET_BRANCH"
remote_head=$(git -C "$LIBRARY_DIR" rev-parse FETCH_HEAD)
if git -C "$LIBRARY_DIR" merge-base --is-ancestor HEAD "$remote_head"; then
  if [ "$(git -C "$LIBRARY_DIR" rev-parse HEAD)" = "$remote_head" ]; then
    echo "target branch is already up to date"
    exit 0
  fi
  echo "local branch has no unpublished commit to retry" >&2
  exit 1
fi
git -C "$LIBRARY_DIR" -c user.name="Library Uploader" -c user.email="uploader@users.noreply.github.com" rebase "$remote_head"
git -C "$LIBRARY_DIR" push origin "HEAD:refs/heads/$TARGET_BRANCH"
local_head=$(git -C "$LIBRARY_DIR" rev-parse HEAD)
published_head=$(git -C "$LIBRARY_DIR" ls-remote origin "refs/heads/$TARGET_BRANCH" | awk 'NR == 1 { print $1 }')
[ "$local_head" = "$published_head" ] || { echo "remote did not confirm the published commit" >&2; exit 1; }
