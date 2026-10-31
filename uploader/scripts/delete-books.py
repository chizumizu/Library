#!/usr/bin/env python3
"""Delete selected books from the configured Git branch, safely and atomically."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import atomic_write_json, read_meta, settings, shared_lock_path
from app.locking import LockBusy, exclusive_workspace_lock

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
OPERATION_MARKERS = (
    "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG",
    "rebase-apply", "rebase-merge", "sequencer",
)


class DeleteError(RuntimeError):
    pass


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o ConnectTimeout=10 -o BatchMode=yes"),
    }
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        env=env,
    )
    if check and result.returncode:
        raise DeleteError(f"git {args[0]} failed")
    return result


def validate_checkout(root: Path) -> str:
    if not root.is_dir() or not (root / ".git").is_dir():
        raise DeleteError("configured library path is not a standard Git checkout")
    branch = git(root, "branch", "--show-current").stdout.strip()
    if branch != settings.target_branch:
        raise DeleteError("checkout branch does not match the configured target branch")
    remote = git(root, "remote", "get-url", "origin").stdout.strip()
    if remote != settings.remote_url:
        raise DeleteError("origin does not match the configured repository URL")
    status = git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout
    if status.strip():
        raise DeleteError("checkout has staged, modified, or untracked files; resolve them first")
    git_dir = Path(git(root, "rev-parse", "--git-dir").stdout.strip())
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    if any((git_dir / marker).exists() for marker in OPERATION_MARKERS):
        raise DeleteError("a Git operation is in progress; resolve it first")
    return remote


def remote_head(remote: str) -> str:
    result = subprocess.run(
        ["git", "ls-remote", remote, f"refs/heads/{settings.target_branch}"],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o ConnectTimeout=10 -o BatchMode=yes"),
        },
    )
    if result.returncode:
        raise DeleteError("could not check the configured remote branch")
    values = result.stdout.split()
    if not values:
        raise DeleteError("configured remote branch does not exist")
    return values[0]


def selected_books(root: Path, slugs: list[str]) -> tuple[dict, list[Path], list[Path]]:
    if len(set(slugs)) != len(slugs):
        raise DeleteError("slug list contains duplicates")
    for slug in slugs:
        if not SLUG_RE.fullmatch(slug) or slug in {".", ".."} or ".." in slug:
            raise DeleteError(f"invalid book slug: {slug!r}")

    metadata_path = root / "meta_dt.json"
    if metadata_path.is_symlink() or not metadata_path.is_file():
        raise DeleteError("metadata file is missing or is not a regular file")
    try:
        metadata = read_meta(metadata_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise DeleteError("metadata is missing or invalid") from exc
    if not metadata:
        raise DeleteError("metadata contains no books")

    uploads = root / "uploads"
    if uploads.is_symlink() or (uploads.exists() and not uploads.is_dir()):
        raise DeleteError("uploads path is not a normal directory")

    all_tree_paths = set(git(root, "ls-tree", "-r", "--name-only", "HEAD", "--", "uploads").stdout.splitlines())
    paths: list[Path] = []
    materialized: list[Path] = []
    for slug in slugs:
        rel = f"uploads/{slug}.pdf"
        if slug not in metadata:
            raise DeleteError(f"book slug is not present in metadata: {slug}")
        if rel not in all_tree_paths:
            raise DeleteError(f"tracked PDF is missing from the target branch: {slug}")
        path = root / rel
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise DeleteError(f"PDF path is not a regular file: {slug}")
        if path.exists():
            materialized.append(path)
        else:
            index_state = git(root, "ls-files", "-t", "--", rel).stdout.strip()
            if not index_state.startswith("S "):
                raise DeleteError(f"PDF is unexpectedly absent from the working tree: {slug}")
        paths.append(Path(rel))
    return metadata, paths, materialized


def ensure_remote_current(root: Path, remote: str, *, fetch: bool) -> str:
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    if fetch:
        git(root, "fetch", "--no-tags", "origin", settings.target_branch)
        published = git(root, "rev-parse", "FETCH_HEAD").stdout.strip()
    else:
        published = remote_head(remote)
    if head != published:
        raise DeleteError("checkout HEAD differs from the remote; synchronize it and retry")
    return head


def restore_before_commit(root: Path, materialized: list[Path], paths: list[Path]) -> None:
    restore_worktree = ["meta_dt.json", *(path.as_posix() for path in materialized)]
    git(root, "restore", "--source=HEAD", "--staged", "--worktree", "--", *restore_worktree)
    sparse_paths = [path.as_posix() for path in paths if root / path not in materialized]
    if sparse_paths:
        git(root, "restore", "--source=HEAD", "--staged", "--", *sparse_paths)


def confirm(slugs: list[str], metadata: dict, *, yes: bool, dry_run: bool) -> bool:
    print(f"Books selected for deletion from {settings.target_branch}:")
    for slug in slugs:
        title = "".join(
            character for character in " ".join(str(metadata[slug].get("title", "")).split())
            if character.isprintable()
        )[:120]
        print(f"  {slug} — {title}")
    if dry_run:
        print("dry run: no library files or Git refs changed")
        return False
    if yes:
        return True
    if not sys.stdin.isatty():
        raise DeleteError("interactive confirmation is unavailable; rerun with --yes after reviewing the slugs")
    answer = input(f"Delete and publish {len(slugs)} book(s)? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def delete_books(slugs: list[str], *, dry_run: bool, yes: bool) -> int:
    root = settings.library_dir.resolve()
    try:
        lock = exclusive_workspace_lock(shared_lock_path())
        lock.__enter__()
    except LockBusy as exc:
        raise DeleteError("uploader workspace is busy; retry later") from exc
    try:
        remote = validate_checkout(root)
        metadata, paths, materialized = selected_books(root, slugs)
        before_head = ensure_remote_current(root, remote, fetch=not dry_run)
        if not confirm(slugs, metadata, yes=yes, dry_run=dry_run):
            if not dry_run:
                print("cancelled; no changes made")
            return 0

        # Recheck after interactive confirmation before modifying anything.
        if not dry_run:
            validate_checkout(root)
            metadata, paths, materialized = selected_books(root, slugs)
            if ensure_remote_current(root, remote, fetch=True) != before_head:
                raise DeleteError("remote branch changed during confirmation; inspect and retry")

        original_head = git(root, "rev-parse", "HEAD").stdout.strip()
        updated_metadata = {slug: record for slug, record in metadata.items() if slug not in set(slugs)}
        rel_paths = [path.as_posix() for path in paths]
        try:
            git(root, "rm", "--sparse", "--", *rel_paths)
            atomic_write_json(root / "meta_dt.json", updated_metadata)
            git(root, "add", "--", "meta_dt.json")
            staged = set(git(root, "diff", "--cached", "--name-only", "-z").stdout.split("\0")) - {""}
            expected = {"meta_dt.json", *rel_paths}
            if staged != expected:
                raise DeleteError("staged paths differ from the requested books; refusing to commit")
            git(
                root,
                "-c", "user.name=Library Uploader",
                "-c", "user.email=uploader@users.noreply.github.com",
                "commit", "-m", f"Remove {len(slugs)} book(s)",
            )
        except Exception as exc:
            current_head = git(root, "rev-parse", "HEAD").stdout.strip()
            if current_head == original_head:
                try:
                    restore_before_commit(root, materialized, paths)
                except Exception as restore_error:
                    raise DeleteError("automatic rollback failed; inspect the checkout before retrying") from restore_error
            else:
                print("local deletion commit was created and has been preserved for recovery", file=sys.stderr)
            raise

        # Do not roll back a completed local commit if the push fails.
        try:
            git(root, "push", "origin", f"HEAD:refs/heads/{settings.target_branch}")
            published = remote_head(remote)
            local = git(root, "rev-parse", "HEAD").stdout.strip()
            if published != local:
                raise DeleteError("remote did not confirm the deletion commit")
        except DeleteError as exc:
            raise DeleteError(f"{exc}; local deletion commit is preserved for retry") from exc
        print(f"Deleted and published {len(slugs)} book(s) to {settings.target_branch}.")
        return 0
    finally:
        lock.__exit__(None, None, None)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove books from the configured library branch and publish the deletions"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="list selected books without changing anything")
    mode.add_argument("--yes", action="store_true", help="skip interactive confirmation")
    parser.add_argument("slugs", nargs="+", help="one or more metadata slugs to remove")
    args = parser.parse_args()
    try:
        return delete_books(args.slugs, dry_run=args.dry_run, yes=args.yes)
    except (DeleteError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"delete refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
