# Books Library Uploader

FastAPI service for adding PDFs to the GitHub-backed library. The repository is the permanent archive; the OCI checkout is a working copy that can be compacted without checking out historical PDFs.

## Behavior and limits

- `GET /` and `GET /health` are health endpoints.
- `POST /upload` accepts 1–5 PDFs with one title per file. Titles must contain at least two whitespace-separated words. Descriptions and authors are optional parallel lists.
- Defaults are 20 MiB per PDF, 100 MiB aggregate PDF payload, and a 102 MiB maximum multipart request (including 2 MiB for form fields and multipart framing).
- The endpoint copies each upload in 256 KiB chunks, validates the `.pdf` extension, non-empty content, and `%PDF` signature, and never overwrites a file already on disk.
- A shared `flock` file serializes upload, publication, retry, and cleanup operations. If busy, the API returns `409` with `Retry-After: 15`.
- Publishing is synchronous. The API returns `200` only after the configured remote branch reports the commit. A publishing failure returns `503` and keeps the files and metadata for recovery.
- `UPLOADER_TARGET_BRANCH` defaults to `main`. Production should use `main`; use another value only for isolated development against a matching test remote.
- At 300 MiB, a successful publish triggers best-effort checkout compaction. Uploads are rejected with `507` once the checkout has reached the 1 GiB ceiling or the conservative projected footprint would exceed it.

The ASGI middleware rejects oversized request bodies before handing excess bytes to Starlette's multipart parser, including requests without `Content-Length`. The route caps multipart parsing at five files and 15 text fields (title, description, and author per file), with each text part limited to 2 MiB. For the tested Starlette 1.10.0, file parts use `SpooledTemporaryFile` with a 1 MiB in-memory rollover threshold; the 102 MiB total body cap limits the file data that can be spooled before endpoint validation. The reverse proxy or ASGI server may already have buffered a larger receive chunk before application code sees it. Tests used FastAPI 0.162.0 and python-multipart 0.0.32 as well. Production requirements remain minimum-version ranges, so record the resolved versions when deploying.

The 1 GiB check measures regular file sizes in the configured library checkout, including `.git`. Multipart temporary files live outside that checkout. During compaction, the old checkout, the small candidate clone, and up to 102 MiB of multipart spool can coexist; reserve that extra headroom on the filesystem.

## Configuration

All settings use the `UPLOADER_` prefix and are listed in `.env.example`.

| Setting | Default | Meaning |
| --- | ---: | --- |
| `HOST` | `127.0.0.1` | Uvicorn bind host |
| `PORT` | `8000` | Uvicorn port |
| `MAX_PDF_BYTES` | `20971520` | Per-file limit (20 MiB) |
| `MAX_BATCH_BYTES` | `104857600` | Aggregate PDF payload limit (100 MiB) |
| `MAX_MULTIPART_OVERHEAD_BYTES` | `2097152` | Extra request-body allowance (2 MiB) |
| `MAX_FILES` | `5` | Maximum files per request |
| `MAX_WORKSPACE_BYTES` | `1073741824` | Checkout ceiling (1 GiB) |
| `WORKSPACE_RESERVE_BYTES` | `8388608` | Space reserved for metadata and Git overhead (8 MiB) |
| `CLEANUP_THRESHOLD_BYTES` | `314572800` | Post-publish cleanup trigger (300 MiB) |
| `LIBRARY_DIR` | parent checkout | Git working checkout |
| `REMOTE_URL` | `git@github.com:chizumizu/Library.git` | required `origin` remote |
| `TARGET_BRANCH` | `main` | Branch to publish |
| `LOCK_FILE` | sibling `.library-uploader.lock` | Shared lock outside replaceable checkout |

The workspace preflight reserves 8 MiB for metadata and Git overhead, plus approximately twice the incoming PDF bytes for working files and new Git blobs. Git object compression and filesystem allocation can vary.

## Local development and tests

From `uploader/`:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The Git integration tests create temporary bare remotes and never access the configured GitHub remote. Do not point tests at a real checkout.

## Failed publish recovery

The API preserves the upload files and metadata when publishing fails. Inspect the library checkout and `error.txt` (in the deployed uploader directory). If the upload commit exists locally and the checkout is clean, retry it with:

```bash
UPLOADER_TARGET_BRANCH=main bash ~/uploader/scripts/retry-publish.sh ~/library
```

The retry command uses the shared lock, fetches the configured branch, rebases without force-pushing, and pushes. If there are uncommitted changes or a rebase conflict, stop and resolve the state while preserving the files. Cleanup refuses dirty or unpublished work.

## Delete books

`scripts/delete-books.py` removes one or more slugs from `meta_dt.json` and deletes the corresponding tracked PDFs in one commit. It supports both a full checkout and the compact sparse checkout. It uses the same lock as uploads and cleanup, requires a clean checkout at the configured remote branch, and refuses stale or unpublished local state.

Preview the selected records, then run the deletion interactively:

```bash
cd ~/uploader
.venv/bin/python scripts/delete-books.py --dry-run slug-one slug-two
.venv/bin/python scripts/delete-books.py slug-one slug-two
```

The second command lists the titles and asks for confirmation. Add `--yes` only when invoking it non-interactively and after reviewing the slugs. If the push fails, the local deletion commit is retained; retry it with `retry-publish.sh` as described above. The script removes books from the current branch; it does not rewrite Git history or erase old blobs from previous commits.

## Workspace cleanup

Dry-run and manual cleanup use the same lock and safety checks as uploads:

```bash
cd ~/uploader
.venv/bin/python scripts/cleanup-workspace.py --dry-run
.venv/bin/python scripts/cleanup-workspace.py
du -sh ~/library
```

Cleanup refuses a wrong branch or remote, modified/staged/untracked/ignored files, unpublished commits, unresolved Git operations, an unavailable remote, or a busy lock. It clones a depth-one `blob:none` sparse checkout containing `meta_dt.json`, verifies that historical PDF blobs were not downloaded, then swaps it into place with rollback. The old checkout is removed only after post-swap validation.

Install daily cleanup only after local testing and user review. The supplied user service and timer target `/home/ubuntu`; the timer runs at 21:00 UTC, which is 02:30 IST the next day. The service passes `--if-over-threshold`, so scheduled cleanup runs only when the checkout reaches the configured 300 MiB threshold. Copy both unit files to `~/.config/systemd/user/`. Ensure the `ubuntu` user manager runs while logged out (`loginctl enable-linger ubuntu`), review the paths and environment, then run:

```bash
systemctl --user daemon-reload
systemctl --user enable --now uploader-cleanup.timer
systemctl --user list-timers uploader-cleanup.timer
```

Disable with `systemctl --user disable --now uploader-cleanup.timer`. The cleanup service has explicit checkout, branch, and lock settings and does not load the uploader's secret environment file.

The 300 MiB threshold is checked after successful uploads and by the daily timer. If post-publish cleanup fails, the publication remains successful; uploads are blocked when the 1 GiB checkout ceiling prevents another safe write. Cleanup does not delete unpublished files or commits.

## Existing deployment layout

- `~/library` is the persistent Git checkout.
- `~/uploader` is the deployed application copy, separate from that checkout.
- `uploader.service` is the existing systemd user-service template.
- `uploader-cleanup.service` and `uploader-cleanup.timer` are templates only; they are not installed or enabled by this repository change.

No OCI deployment is performed as part of local development. Preserve the current `.env`, virtual environment, SSH setup, and service configuration when updating the deployed copy. Never copy or commit the real `.env`.
