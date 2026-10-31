#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings, shared_lock_path, workspace_size
from app.locking import LockBusy, exclusive_workspace_lock


def git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o ConnectTimeout=10 -o BatchMode=yes"),
    }
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        capture_output=True,
        env=env,
    )
    if check and result.returncode:
        raise RuntimeError(f"git {args[0]} failed")
    return result


def ensure_clean_checkout(root: Path, *, fetch: bool) -> tuple[str, str]:
    if not root.is_dir() or not (root / ".git").is_dir():
        raise RuntimeError("configured library path is not a Git checkout")
    branch = git(root, "branch", "--show-current").stdout.strip()
    if branch != settings.target_branch:
        raise RuntimeError("checkout branch does not match configured target branch")
    remote = git(root, "remote", "get-url", "origin").stdout.strip()
    if remote != settings.remote_url:
        raise RuntimeError("origin remote does not match configured remote")
    status = git(root, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching").stdout
    if status.strip():
        raise RuntimeError("checkout contains modified, staged, untracked, or ignored files")
    git_dir = Path(git(root, "rev-parse", "--git-dir").stdout.strip())
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    operation_markers = (
        "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-apply",
        "rebase-merge", "sequencer",
    )
    if any((git_dir / name).exists() for name in operation_markers):
        raise RuntimeError("a Git operation is in progress")
    if fetch:
        git(root, "fetch", "--no-tags", "origin", settings.target_branch)
        remote_head = git(root, "rev-parse", "FETCH_HEAD").stdout.strip()
        if git(root, "merge-base", "--is-ancestor", "HEAD", remote_head, check=False).returncode:
            raise RuntimeError("checkout has unpublished commits or diverged from origin")
    else:
        refs = subprocess.run(
            ["git", "ls-remote", remote, f"refs/heads/{settings.target_branch}"],
            text=True,
            capture_output=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o ConnectTimeout=10 -o BatchMode=yes")},
        )
        if refs.returncode:
            raise RuntimeError("remote branch could not be checked")
        remote_head = refs.stdout.split()[0] if refs.stdout.split() else ""
        local_head = git(root, "rev-parse", "HEAD").stdout.strip()
        if not remote_head or remote_head != local_head:
            raise RuntimeError("dry-run requires checkout HEAD to match the remote branch")
    return remote, branch


def validate_candidate(candidate: Path, remote: str, remote_head: str) -> None:
    if git(candidate, "branch", "--show-current").stdout.strip() != settings.target_branch:
        raise RuntimeError("candidate branch validation failed")
    if git(candidate, "remote", "get-url", "origin").stdout.strip() != remote:
        raise RuntimeError("candidate remote validation failed")
    if git(candidate, "rev-parse", "HEAD").stdout.strip() != remote_head:
        raise RuntimeError("candidate does not match the target branch head")
    if git(candidate, "rev-parse", "--is-shallow-repository").stdout.strip() != "true":
        raise RuntimeError("candidate is not shallow")
    promisor = git(candidate, "config", "--get", "remote.origin.promisor", check=False).stdout.strip()
    filter_spec = git(candidate, "config", "--get", "remote.origin.partialclonefilter", check=False).stdout.strip()
    if promisor.lower() != "true" or filter_spec != "blob:none":
        raise RuntimeError("remote did not establish a blob:none partial clone")
    patterns = git(candidate, "sparse-checkout", "list").stdout.splitlines()
    if patterns != ["/meta_dt.json"]:
        raise RuntimeError("candidate sparse checkout patterns are unexpected")
    metadata_path = candidate / "meta_dt.json"
    data = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("candidate metadata is not a JSON object")
    if (candidate / "uploads").exists():
        raise RuntimeError("candidate unexpectedly materialized uploads")
    tree = git(candidate, "ls-tree", "-r", "HEAD", "uploads").stdout.splitlines()
    pdf_oids = [line.split()[2] for line in tree if line.split() and line.split()[-1].lower().endswith(".pdf")]
    for oid in pdf_oids:
        if git(candidate, "cat-file", "-e", oid, check=False).returncode == 0:
            raise RuntimeError("candidate contains a historical PDF blob")


def cleanup(dry_run: bool, threshold_only: bool) -> int:
    root = settings.library_dir.resolve()
    if threshold_only and workspace_size(root) < settings.cleanup_threshold_bytes:
        print("workspace is below cleanup threshold")
        return 0
    lock_path = shared_lock_path()
    try:
        lock = exclusive_workspace_lock(lock_path)
        lock.__enter__()
    except LockBusy:
        raise RuntimeError("uploader workspace is busy; retry later")
    try:
        remote, _ = ensure_clean_checkout(root, fetch=not dry_run)
        size = workspace_size(root)
        if dry_run:
            print(f"dry run: eligible checkout, {size} bytes; would replace with shallow blob:none sparse checkout")
            return 0

        remote_head = git(root, "rev-parse", "FETCH_HEAD").stdout.strip()
        parent = root.parent
        candidate = parent / f".{root.name}.cleanup-{uuid.uuid4().hex}"
        backup = parent / f".{root.name}.backup-{uuid.uuid4().hex}"
        try:
            clone = subprocess.run(
                ["git", "clone", "--no-local", "--depth=1", "--filter=blob:none", "--sparse",
                 "--branch", settings.target_branch, remote, str(candidate)],
                text=True,
                capture_output=True,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o ConnectTimeout=10 -o BatchMode=yes")},
            )
            if clone.returncode:
                raise RuntimeError("candidate clone failed")
            git(candidate, "sparse-checkout", "set", "--no-cone", "/meta_dt.json")
            validate_candidate(candidate, remote, remote_head)

            os.replace(root, backup)
            try:
                os.replace(candidate, root)
                validate_candidate(root, remote, remote_head)
            except Exception:
                if root.exists():
                    shutil.rmtree(root)
                os.replace(backup, root)
                raise
            shutil.rmtree(backup)
        finally:
            if candidate.exists():
                shutil.rmtree(candidate)
        print(f"workspace compacted; size is now {workspace_size(root)} bytes")
        return 0
    finally:
        lock.__exit__(None, None, None)


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely compact the uploader's Git checkout")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--if-over-threshold", action="store_true")
    args = parser.parse_args()
    try:
        return cleanup(args.dry_run, args.if_over_threshold)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"cleanup refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
