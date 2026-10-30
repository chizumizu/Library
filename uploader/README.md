# Books Library Uploader

FastAPI service that accepts up to 5 PDFs per request, stores them in the library repository (`uploads/{slug}.pdf` + `meta_dt.json`), and commits/pushes to `main` via `scripts/upload-commit.sh`. The uploader source lives inside the library repo, but the running copy is deployed separately at `~/uploader`, pointing at the library checkout at `~/library`. Swagger at `/docs`.

## Features

- `GET /` and `GET /health` — root and health check
- `POST /upload` `202 Accepted` — multipart batch upload (1–5 files). Each file requires `titles` (≥2 words); optional `descriptions`, `authors` (parallel arrays). Validates PDF magic `%PDF`, `.pdf` extension, size limit (`UPLOADER_MAX_PDF_BYTES`, default 20 MiB), slug uniqueness (`python-slugify` + random suffix), atomic `meta_dt.json` writes, per-request `asyncio.Lock`.
- Git commit script `scripts/upload-commit.sh <library-dir> <error-log> uploads/<slug>.pdf ...` — joke-based commit message (`https://api.api-ninjas.com/v1/jokes` + `API_NINJAS_API_KEY`, fallback `"I wont give up"` + timestamp), strict path validation (`uploads/[a-z0-9._-]+\.pdf`, no `..`), branch `main` only, refuses unrelated staged changes, `GIT_TERMINAL_PROMPT=0` + `GIT_SSH_COMMAND` batch mode, timeout 60 s from the API, failures appended to `error.txt`.

Payload example (curl):
```bash
curl -X POST http://127.0.0.1:8000/upload \
  -F "titles=My Great Book" -F "titles=Second Book Title" \
  -F "files=@book1.pdf" -F "files=@book2.pdf" \
  -F "descriptions=optional" -F "authors=Optional Author"
```

## Deployment Model

- `~/library` — the cloned library git repository (tracked by git).
- `~/uploader` — a **copy** of `~/library/uploader`, run separately by systemd. This directory is outside the repo and is not tracked by git.
- Uploads are written to `~/library/uploads/` and committed from `~/library`.

The uploader source in this repo is only a template. Do not run it from inside the repo and do not commit the deployed copy, its `.env`, `.venv`, or `error.txt`.

## Project Layout

```
library/                 # cloned repo at ~/library (git root)
  uploader/              # source template only
    app/
      config.py        # Settings (UPLOADER_ prefix, .env), atomic JSON, read_meta
      storage.py       # slug, save_pdf, register_book
      main.py          # FastAPI app, validation, 202 + script trigger
    scripts/upload-commit.sh
    uploader.service   # systemd --user unit
    requirements.txt
    .env.example
  uploads/{slug}.pdf
  meta_dt.json         # { slug: { slug, title, description, author, tag } }

uploader/                # deployed copy at ~/uploader (not tracked)
  app/
  scripts/
  .venv/
  .env                 # copied from .env.example
  error.txt            # push/commit failures (created on demand)
```

## Environment

| Var | Default | Notes |
|-----|---------|-------|
| `UPLOADER_HOST` | `127.0.0.1` | bind |
| `UPLOADER_PORT` | `8000` | systemd reads `UPLOADER_PORT` |
| `UPLOADER_MAX_PDF_BYTES` | `20971520` | 20 MiB |
| `UPLOADER_LIBRARY_DIR` | `../library` | relative to `~/uploader`, i.e. `~/library`; use an absolute path if the repo is cloned elsewhere |
| `API_NINJAS_API_KEY` | _(empty)_ | for joke commit message; fallback if missing/timeout |

## Deploy the Uploader

Follow these steps exactly. They assume the library repo is cloned at `~/library` and the running uploader will live at `~/uploader`.

1. Clone the library repo into your home directory (required to get the uploader source):
```bash
git clone <repo-url> ~/library
```

2. Copy the uploader source out of the repo, separate from it, and enter the deployed copy:
```bash
mkdir -p ~/uploader
cp -r ~/library/uploader/app ~/library/uploader/scripts \
      ~/library/uploader/requirements.txt ~/library/uploader/uploader.service \
      ~/library/uploader/.env.example ~/uploader/
cd ~/uploader
```
Only source files are copied, so any local `.env`, `.venv`, `error.txt`, or `__pycache__` inside the repo checkout is left behind.

3. Create the virtualenv, install dependencies, and copy the env file with default values:
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # edit API_NINJAS_API_KEY / UPLOADER_PORT if needed
```

4. Enable lingering **before** installing the service, so the user manager starts at boot and survives logout:
```bash
loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger   # should print Linger=yes
```
If your distro's polkit policy rejects this for non-root users, an administrator must run the same command once.

5. Install and start the user unit from `~/uploader`:
```bash
mkdir -p ~/.config/systemd/user
cp uploader.service ~/.config/systemd/user/uploader.service
systemctl --user daemon-reload
systemctl --user enable --now uploader
systemctl --user status uploader
```

6. Logs and control:
```bash
journalctl --user -u uploader -f
systemctl --user restart uploader
systemctl --user disable --now uploader
cat ~/uploader/error.txt
```

The unit runs with `WorkingDirectory=%h/uploader`, so `UPLOADER_LIBRARY_DIR=../library` points at `~/library`, the library git root. `EnvironmentFile=%h/uploader/.env` supplies `API_NINJAS_API_KEY` to `scripts/upload-commit.sh`. Because the service runs as you, `git push` uses your SSH key; verify non-interactive pushes with `ssh -T git@github.com` (or use a deploy key) before relying on uploads. Update `.env`, then `systemctl --user restart uploader`.

If the library repo is cloned somewhere other than `~/library` or the uploader is copied somewhere other than `~/uploader`, adjust `WorkingDirectory`, `EnvironmentFile`, `ExecStart`, and `UPLOADER_LIBRARY_DIR` accordingly.

### Updating the deployed uploader

The deployed copy does not update itself. After pulling changes in `~/library`, re-copy the source and restart:

```bash
cp -r ~/library/uploader/app ~/library/uploader/scripts \
      ~/library/uploader/requirements.txt ~/library/uploader/uploader.service \
      ~/library/uploader/.env.example ~/uploader/
cd ~/uploader && .venv/bin/pip install -r requirements.txt
systemctl --user restart uploader
```
This copy keeps the existing `~/uploader/.env` (it is never overwritten).

## Local Run (without systemd)

From the deployed copy:

```bash
cd ~/uploader
.venv/bin/uvicorn app.main:app --host ${UPLOADER_HOST:-127.0.0.1} --port ${UPLOADER_PORT:-8000} --reload
# verify
curl http://127.0.0.1:8000/health
curl -F "titles=Hello World Book" -F "files=@sample.pdf" http://127.0.0.1:8000/upload
```

## API Errors

- `400` empty/short title, titles/files count mismatch, non-PDF, bad extension
- `413` file too large
- `500` malformed `meta_dt.json`

Push failures never fail the upload response; details go to `error.txt`.

## Security Notes

- No secrets in repo; joke API key via `API_NINJAS_API_KEY` env only
- Script never embeds keys, validates all paths, no `git config`, no branch switches, no force push
- Verification done against ephemeral `mktemp` libraries; the real library checkout is not touched by tests

Feedback: https://github.com/anomalyco/opencode
