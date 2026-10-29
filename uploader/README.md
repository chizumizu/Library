# Books Library Uploader

FastAPI service that accepts up to 5 PDFs per request, stores them in the sibling `../library` repository (`uploads/{slug}.pdf` + `meta_dt.json`), and commits/pushes to `main` via `scripts/upload-commit.sh`. Swagger at `/docs`.

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

## Project Layout

```
uploader/
  app/
    config.py        # Settings (UPLOADER_ prefix, .env), atomic JSON, read_meta
    storage.py       # slug, save_pdf, register_book
    main.py          # FastAPI app, validation, 202 + script trigger
  scripts/upload-commit.sh
  uploader.service   # systemd --user unit
  requirements.txt
  .env.example
  error.txt          # push/commit failures (created on demand)
```

Library sibling (outside service repo):
```
../library/
  uploads/{slug}.pdf
  meta_dt.json       # { slug: { slug, title, description, author, tag } }
```

## Environment

| Var | Default | Notes |
|-----|---------|-------|
| `UPLOADER_HOST` | `127.0.0.1` | bind |
| `UPLOADER_PORT` | `8000` | systemd reads `UPLOADER_PORT` |
| `UPLOADER_MAX_PDF_BYTES` | `20971520` | 20 MiB |
| `UPLOADER_LIBRARY_DIR` | `../library` | absolute or relative to `uploader/` |
| `API_NINJAS_API_KEY` | _(empty)_ | for joke commit message; fallback if missing/timeout |

Copy `.env.example` → `.env` and set `API_NINJAS_API_KEY` for production commits.

## Local Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # edit UPLOADER_PORT / API_NINJAS_API_KEY
.venv/bin/uvicorn app.main:app --host ${UPLOADER_HOST:-127.0.0.1} --port ${UPLOADER_PORT:-8000} --reload
# verify
curl http://127.0.0.1:8000/health
curl -F "titles=Hello World Book" -F "files=@sample.pdf" http://127.0.0.1:8000/upload
```

## User-level systemd (no sudo)

Run the uploader as your own login user with `systemd --user`. Nothing is written to `/etc`, `/opt`, or another user's home. The unit uses `%h` (your home directory) and assumes the checkout is at `~/Desktop/books_lib/library/uploader`; adjust the paths in `uploader.service` if yours differs.

1. Create the virtualenv and env file:
```bash
cd ~/Desktop/books_lib/library/uploader
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # set UPLOADER_PORT / API_NINJAS_API_KEY
```

2. Install and start the user unit:
```bash
mkdir -p ~/.config/systemd/user
cp uploader.service ~/.config/systemd/user/uploader.service
systemctl --user daemon-reload
systemctl --user enable --now uploader
systemctl --user status uploader
```

3. Enable lingering so it keeps running without an active login:
```bash
loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger   # should print Linger=yes
```
Linger starts your user manager at boot and keeps it after logout, so the service survives closing the terminal. If your distro's polkit policy rejects this for non-root users, an administrator must run the same command once.

4. Logs and control:
```bash
journalctl --user -u uploader -f
systemctl --user restart uploader
systemctl --user disable --now uploader
cat ~/Desktop/books_lib/library/uploader/error.txt
```

The unit runs with `WorkingDirectory=.../uploader`, so `UPLOADER_LIBRARY_DIR=../library` points at the sibling library checkout. `EnvironmentFile=.../.env` supplies `API_NINJAS_API_KEY` to `scripts/upload-commit.sh`. Because the service runs as you, `git push` uses your SSH key; verify non-interactive pushes with `ssh -T git@github.com` (or use a deploy key) before relying on uploads. Update `.env`, then `systemctl --user restart uploader`.

## API Errors

- `400` empty/short title, titles/files count mismatch, non-PDF, bad extension
- `413` file too large
- `500` malformed `meta_dt.json`

Push failures never fail the upload response; details go to `error.txt`.

## Security Notes

- No secrets in repo; joke API key via `API_NINJAS_API_KEY` env only
- Script never embeds keys, validates all paths, no `git config`, no branch switches, no force push
- Verification done against ephemeral `mktemp` libraries; real `../library` not touched by tests

Feedback: https://github.com/anomalyco/opencode
