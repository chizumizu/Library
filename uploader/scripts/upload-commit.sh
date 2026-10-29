#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  echo "usage: upload-commit.sh <library-dir> <error-log> <relative-path> [...]" >&2
  exit 2
}

[ "$#" -ge 3 ] || usage

LIBRARY_DIR="${1%/}"
ERROR_LOG="$2"
shift 2

mkdir -p "$(dirname "$ERROR_LOG")"
touch "$ERROR_LOG"

append_error() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$1" >> "$ERROR_LOG"
}

trap 'append_error "upload-commit: unexpected error at line $LINENO"' ERR

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$1" >&2
}

fetch_joke() {
  local key="${API_NINJAS_API_KEY:-}"
  [ -n "$key" ] || return 1
  local response
  if ! response=$(curl -fsS --connect-timeout 3 --max-time 8 -H "X-Api-Key: $key" "https://api.api-ninjas.com/v1/jokes" 2>/dev/null); then
    return 1
  fi
  printf '%s' "$response" | python3 -c '
import json, sys
raw = sys.stdin.read()
data = json.loads(raw)
if not isinstance(data, list) or not data:
    raise ValueError("empty payload")
joke = data[0].get("joke") if isinstance(data[0], dict) else None
if not isinstance(joke, str) or not joke.strip():
    raise ValueError("missing joke")
print(" ".join(joke.strip().split()))
' 2>/dev/null
}

commit_message() {
  local joke
  if joke=$(fetch_joke); then
    printf '%s' "$joke"
  else
    printf '%s I wont give up' "$(date '+%Y-%m-%d %H:%M:%S')"
  fi
}

if [ ! -d "$LIBRARY_DIR/.git" ]; then
  append_error "upload-commit: not a git repository: $LIBRARY_DIR"
  exit 1
fi

paths=()
for p in "$@"; do
  if [[ ! "$p" =~ ^uploads/[a-z0-9._-]+\.pdf$ ]]; then
    append_error "upload-commit: refusing unexpected path: $p"
    exit 1
  fi
  if [[ "$p" == *".."* ]]; then
    append_error "upload-commit: refusing path with traversal: $p"
    exit 1
  fi
  if [ ! -f "$LIBRARY_DIR/$p" ]; then
    append_error "upload-commit: missing file: $LIBRARY_DIR/$p"
    exit 1
  fi
  paths+=("$p")
done

if ! current_branch=$(git -C "$LIBRARY_DIR" symbolic-ref --short HEAD 2>/dev/null); then
  append_error "upload-commit: cannot determine branch (unborn HEAD?)"
  exit 1
fi

if [ "$current_branch" != "main" ]; then
  append_error "upload-commit: expected branch main, found '$current_branch'"
  exit 1
fi

if ! git -C "$LIBRARY_DIR" diff --quiet --cached 2>/dev/null; then
  append_error "upload-commit: index has unrelated staged changes; aborting"
  exit 1
fi

if ! out=$(git -C "$LIBRARY_DIR" add -- meta_dt.json "${paths[@]}" 2>&1); then
  append_error "upload-commit: git add failed: $out"
  exit 1
fi

if git -C "$LIBRARY_DIR" diff --quiet --cached; then
  msg=$(commit_message)
  log "nothing new to commit; retrying push of main ($msg)"
else
  msg=$(commit_message)
  if ! out=$(git -C "$LIBRARY_DIR" commit -m "$msg" 2>&1); then
    append_error "upload-commit: git commit failed: $out"
    exit 1
  fi
fi

export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -o ConnectTimeout=10 -o BatchMode=yes"

if ! out=$(git -C "$LIBRARY_DIR" push origin main 2>&1); then
  append_error "upload-commit: git push origin main failed: $out"
  exit 1
fi

log "push to origin main succeeded"
