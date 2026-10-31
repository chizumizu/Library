from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncByteStream, AsyncClient

from app import main as uploader_main
from app.config import settings


ROOT = Path(__file__).resolve().parents[1]
PUBLISH = ROOT / "scripts" / "upload-commit.sh"
CLEANUP = ROOT / "scripts" / "cleanup-workspace.py"
DELETE = ROOT / "scripts" / "delete-books.py"


def run(*args: str, cwd: Path | None = None, check: bool = True, env=None):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, env=env)
    if check and result.returncode:
        raise AssertionError(f"{args} failed:\n{result.stdout}\n{result.stderr}")
    return result


def git(path: Path, *args: str, check: bool = True):
    return run("git", "-C", str(path), *args, check=check)


def make_remote(tmp_path: Path):
    remote = tmp_path / "remote.git"
    run("git", "init", "-q", "--bare", "--initial-branch=main", str(remote))
    run("git", "-C", str(remote), "config", "uploadpack.allowFilter", "true")
    seed = tmp_path / "seed"
    run("git", "clone", "-q", str(remote), str(seed))
    run("git", "-C", str(seed), "config", "user.name", "Test")
    run("git", "-C", str(seed), "config", "user.email", "test@example.invalid")
    (seed / "meta_dt.json").write_text(json.dumps({
        "old-one": {"slug": "old-one", "title": "Old One"},
        "old-two": {"slug": "old-two", "title": "Old Two"},
    }) + "\n")
    (seed / "uploads").mkdir()
    (seed / "uploads" / "old-one.pdf").write_bytes(b"%PDF old one")
    (seed / "uploads" / "old-two.pdf").write_bytes(b"%PDF old two")
    git(seed, "add", "meta_dt.json", "uploads")
    git(seed, "commit", "-m", "initial archive")
    git(seed, "push", "origin", "main")
    return remote, seed


def clone_working(remote: Path, destination: Path):
    run("git", "clone", "-q", str(remote), str(destination))
    git(destination, "config", "user.name", "Test Uploader")
    git(destination, "config", "user.email", "uploader@example.invalid")


def test_publish_reconciles_remote_and_pushes_batch(tmp_path):
    remote, seed = make_remote(tmp_path)
    working = tmp_path / "working"
    clone_working(remote, working)

    # Advance the remote independently after the upload checkout was cloned.
    (seed / "remote-note.txt").write_text("remote update\n")
    git(seed, "add", "remote-note.txt")
    git(seed, "commit", "-m", "independent remote update")
    git(seed, "push", "origin", "main")

    (working / "uploads" / "new-book.pdf").write_bytes(b"%PDF new book")
    (working / "meta_dt.json").write_text(json.dumps({"new-book": {"title": "New Book"}}) + "\n")
    env = {**os.environ, "UPLOADER_TARGET_BRANCH": "main", "UPLOADER_REMOTE_URL": str(remote), "GIT_TERMINAL_PROMPT": "0"}
    run("bash", str(PUBLISH), str(working), str(tmp_path / "errors.log"), "uploads/new-book.pdf", env=env)

    bare_head = run("git", "--git-dir", str(remote), "rev-parse", "refs/heads/main").stdout.strip()
    assert bare_head == git(working, "rev-parse", "HEAD").stdout.strip()
    assert (working / "remote-note.txt").read_text() == "remote update\n"
    tree = run("git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main").stdout.splitlines()
    assert "uploads/new-book.pdf" in tree
    assert "meta_dt.json" in tree


def test_failed_push_preserves_local_commit_for_retry(tmp_path):
    remote, _ = make_remote(tmp_path)
    working = tmp_path / "working"
    clone_working(remote, working)
    old_head = run("git", "--git-dir", str(remote), "rev-parse", "refs/heads/main").stdout.strip()
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (working / "uploads" / "retry-book.pdf").write_bytes(b"%PDF retry")
    (working / "meta_dt.json").write_text(json.dumps({"retry-book": {"title": "Retry Book"}}) + "\n")
    env = {**os.environ, "UPLOADER_TARGET_BRANCH": "main", "UPLOADER_REMOTE_URL": str(remote)}
    result = run("bash", str(PUBLISH), str(working), str(tmp_path / "errors.log"),
                 "uploads/retry-book.pdf", env=env, check=False)
    assert result.returncode != 0
    assert git(working, "status", "--porcelain").stdout == ""
    assert git(working, "rev-parse", "HEAD").stdout.strip() != old_head
    assert run("git", "--git-dir", str(remote), "rev-parse", "refs/heads/main").stdout.strip() == old_head
    assert git(working, "show", "HEAD:uploads/retry-book.pdf").stdout == "%PDF retry"


def cleanup_env(library: Path, tmp_path: Path):
    return {
        **os.environ,
        "UPLOADER_LIBRARY_DIR": str(library),
        "UPLOADER_TARGET_BRANCH": "main",
        "UPLOADER_REMOTE_URL": str(library.parent / "remote.git"),
        "UPLOADER_LOCK_FILE": str(tmp_path / "library.lock"),
        "UPLOADER_CLEANUP_THRESHOLD_BYTES": "1",
        "UPLOADER_MAX_WORKSPACE_BYTES": str(1024**3),
        "UPLOADER_WORKSPACE_RESERVE_BYTES": "0",
    }


def test_cleanup_builds_sparse_partial_checkout_and_new_pdf_still_pushes(tmp_path):
    remote, _ = make_remote(tmp_path)
    working = tmp_path / "library"
    clone_working(remote, working)
    old_oids = {
        line.split()[2]
        for line in git(working, "ls-tree", "-r", "HEAD", "uploads").stdout.splitlines()
    }
    env = cleanup_env(working, tmp_path)
    run(sys.executable, str(CLEANUP), "--if-over-threshold", cwd=working, env=env)

    assert (working / "meta_dt.json").is_file()
    assert not (working / "uploads").exists()
    assert git(working, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
    assert git(working, "config", "--get", "remote.origin.promisor").stdout.strip() == "true"
    assert git(working, "config", "--get", "remote.origin.partialclonefilter").stdout.strip() == "blob:none"
    local_objects = set(git(working, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").stdout.splitlines())
    assert not (old_oids & local_objects)

    (working / "uploads").mkdir()
    (working / "uploads" / "new-book.pdf").write_bytes(b"%PDF new book")
    metadata = json.loads((working / "meta_dt.json").read_text())
    metadata["new-book"] = {"title": "New Book"}
    (working / "meta_dt.json").write_text(json.dumps(metadata) + "\n")
    git(working, "add", "--sparse", "--", "meta_dt.json", "uploads/new-book.pdf")
    git(working, "commit", "-m", "new upload after compaction")
    git(working, "push", "origin", "main")

    remote_tree = run("git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main").stdout.splitlines()
    assert {"uploads/old-one.pdf", "uploads/old-two.pdf", "uploads/new-book.pdf"}.issubset(remote_tree)
    assert not (old_oids & set(git(working, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").stdout.splitlines()))


def test_cleanup_refuses_dirty_checkout_without_replacing_it(tmp_path):
    remote, _ = make_remote(tmp_path)
    working = tmp_path / "library"
    clone_working(remote, working)
    marker = working / "local-change.txt"
    marker.write_text("preserve me")
    env = cleanup_env(working, tmp_path)
    result = run(sys.executable, str(CLEANUP), "--dry-run", cwd=working, env=env, check=False)
    assert result.returncode != 0
    assert marker.read_text() == "preserve me"
    assert working.is_dir()


def test_cleanup_refuses_unpublished_local_commit(tmp_path):
    remote, _ = make_remote(tmp_path)
    working = tmp_path / "library"
    clone_working(remote, working)
    (working / "local-note.txt").write_text("local commit")
    git(working, "add", "local-note.txt")
    git(working, "commit", "-m", "unpublished local change")
    local_head = git(working, "rev-parse", "HEAD").stdout.strip()
    result = run(sys.executable, str(CLEANUP), cwd=working, env=cleanup_env(working, tmp_path), check=False)
    assert result.returncode != 0
    assert working.is_dir()
    assert git(working, "rev-parse", "HEAD").stdout.strip() == local_head


def test_cleanup_refuses_remote_that_does_not_honor_blob_filter(tmp_path):
    remote, _ = make_remote(tmp_path)
    git(remote, "config", "uploadpack.allowFilter", "false")
    working = tmp_path / "library"
    clone_working(remote, working)
    before = git(working, "rev-parse", "HEAD").stdout.strip()
    result = run(sys.executable, str(CLEANUP), cwd=working, env=cleanup_env(working, tmp_path), check=False)
    assert result.returncode != 0
    assert working.is_dir()
    assert git(working, "rev-parse", "HEAD").stdout.strip() == before


def test_delete_script_removes_pdf_and_metadata_as_one_published_commit(tmp_path):
    remote, _ = make_remote(tmp_path)
    working = tmp_path / "library"
    clone_working(remote, working)
    env = cleanup_env(working, tmp_path)

    dry_run = run(sys.executable, str(DELETE), "--dry-run", "old-one", cwd=working, env=env)
    assert "Old One" in dry_run.stdout
    assert (working / "uploads" / "old-one.pdf").exists()

    run(sys.executable, str(DELETE), "--yes", "old-one", "old-two", cwd=working, env=env)
    remote_tree = run("git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main").stdout.splitlines()
    assert "uploads/old-one.pdf" not in remote_tree
    assert "uploads/old-two.pdf" not in remote_tree
    remote_meta = json.loads(run("git", "--git-dir", str(remote), "show", "main:meta_dt.json").stdout)
    assert remote_meta == {}
    assert git(working, "status", "--porcelain").stdout == ""
    assert git(working, "rev-parse", "HEAD").stdout.strip() == run(
        "git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"
    ).stdout.strip()


def test_delete_script_operates_from_sparse_checkout(tmp_path):
    remote, _ = make_remote(tmp_path)
    working = tmp_path / "library"
    clone_working(remote, working)
    env = cleanup_env(working, tmp_path)
    run(sys.executable, str(CLEANUP), cwd=working, env=env)
    assert not (working / "uploads").exists()

    run(sys.executable, str(DELETE), "--yes", "old-one", cwd=working, env=env)
    assert not (working / "uploads").exists()
    remote_tree = run("git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main").stdout.splitlines()
    assert "uploads/old-one.pdf" not in remote_tree
    assert "uploads/old-two.pdf" in remote_tree
    remote_meta = json.loads(run("git", "--git-dir", str(remote), "show", "main:meta_dt.json").stdout)
    assert "old-one" not in remote_meta and "old-two" in remote_meta


def test_delete_push_failure_preserves_commit_for_retry(tmp_path):
    remote, _ = make_remote(tmp_path)
    working = tmp_path / "library"
    clone_working(remote, working)
    env = cleanup_env(working, tmp_path)
    before = git(working, "rev-parse", "HEAD").stdout.strip()
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    result = run(sys.executable, str(DELETE), "--yes", "old-one", cwd=working, env=env, check=False)
    assert result.returncode != 0
    assert git(working, "status", "--porcelain").stdout == ""
    assert git(working, "rev-parse", "HEAD").stdout.strip() != before
    assert "uploads/old-one.pdf" in run("git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main").stdout.splitlines()

    hook.unlink()
    retry_env = {**env, "UPLOADER_LOCK_FILE": str(tmp_path / "library.lock")}
    run("bash", str(ROOT / "scripts" / "retry-publish.sh"), str(working), cwd=working, env=retry_env)
    remote_tree = run("git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main").stdout.splitlines()
    assert "uploads/old-one.pdf" not in remote_tree


@pytest.mark.anyio
async def test_api_upload_publishes_to_configured_temporary_remote(tmp_path, monkeypatch):
    remote, _ = make_remote(tmp_path)
    working = tmp_path / "library"
    clone_working(remote, working)
    monkeypatch.setattr(settings, "library_dir", working)
    monkeypatch.setattr(settings, "remote_url", str(remote))
    monkeypatch.setattr(settings, "target_branch", "main")
    monkeypatch.setattr(settings, "lock_file", tmp_path / "api.lock")
    monkeypatch.setattr(settings, "max_pdf_bytes", 1024)
    monkeypatch.setattr(settings, "max_batch_bytes", 2048)
    monkeypatch.setattr(settings, "max_workspace_bytes", 1024**3)
    monkeypatch.setattr(settings, "workspace_reserve_bytes", 0)
    monkeypatch.setattr(settings, "cleanup_threshold_bytes", 1)
    monkeypatch.setattr(uploader_main, "ERROR_LOG_PATH", tmp_path / "uploader-errors.log")

    boundary = "api-publish-boundary"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"titles\"\r\n\r\nUploaded Book\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"new.pdf\"\r\n"
        "Content-Type: application/pdf\r\n\r\n"
    ).encode() + b"%PDF integration" + f"\r\n--{boundary}--\r\n".encode()

    class Body(AsyncByteStream):
        async def __aiter__(self):
            yield body

    async with AsyncClient(transport=ASGITransport(app=uploader_main.app), base_url="http://test") as client:
        response = await client.post(
            "/upload",
            content=Body(),
            headers={"content-type": f"multipart/form-data; boundary={boundary}", "content-length": str(len(body))},
        )
    assert response.status_code == 200, response.text
    branch_tree = run("git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main").stdout.splitlines()
    assert "meta_dt.json" in branch_tree
    assert len([path for path in branch_tree if path.startswith("uploads/") and path.endswith(".pdf")]) == 3
    assert not (working / "uploads").exists()
    assert git(working, "config", "--get", "remote.origin.partialclonefilter").stdout.strip() == "blob:none"
    assert not (tmp_path / "uploader-errors.log").exists()
