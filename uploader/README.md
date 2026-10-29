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
  uploader.service   # systemd unit
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

## Production (systemd)

```bash
sudo mkdir -p /opt/books_lib && sudo cp -r uploader /opt/books_lib/
sudo cp -r library /opt/books_lib/  # or git clone <library-remote> /opt/books_lib/library
cd /opt/books_lib/uploader && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo cp uploader.service /etc/systemd/system/uploader.service
sudo systemctl daemon-reload
sudo systemctl enable --now uploader
sudo systemctl status uploader
# logs
journalctl -u uploader -f
cat /opt/books_lib/uploader/error.txt
```

Service file uses `EnvironmentFile=/opt/books_lib/uploader/.env` and `ExecStart=/opt/books_lib/uploader/.venv/bin/uvicorn app.main:app --host ${UPLOADER_HOST} --port ${UPLOADER_PORT}`. Update env then `sudo systemctl restart uploader`.

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
