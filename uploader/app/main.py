from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from app.config import settings
from app.storage import build_slug, register_book, save_pdf
from app.config import read_meta

app = FastAPI(
    title="Books Library Uploader",
    description="Upload up to 5 PDFs with metadata. Docs are served at /docs.",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    swagger_ui_parameters={
        "defaultModelsExpandDepth": -1,
        "defaultModelExpandDepth": -1,
        "defaultModelRendering": "example",
        "docExpansion": "list",
        "displayRequestDuration": True,
        "filter": True,
        "tryItOutEnabled": True,
        "deepLinking": True,
        "persistAuthorization": True,
        "syntaxHighlight": {"theme": "obsidian"},
    },
)


def _restore_binary_format(schema: dict[str, Any]) -> None:
    for component in schema.get("components", {}).get("schemas", {}).values():
        for prop in component.get("properties", {}).values():
            target = prop.get("items") if prop.get("type") == "array" else prop
            if isinstance(target, dict) and target.get("contentMediaType") == "application/octet-stream":
                target.pop("contentMediaType", None)
                target["format"] = "binary"


def _prioritize_upload_route(schema: dict[str, Any]) -> None:
    paths = schema.get("paths", {})
    if "/upload" not in paths:
        return
    schema["paths"] = {
        "/upload": paths["/upload"],
        **{path: item for path, item in paths.items() if path != "/upload"},
    }


def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    _restore_binary_format(schema)
    _prioritize_upload_route(schema)
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi

_upload_lock = asyncio.Lock()
PDF_MAGIC = b"%PDF"
WORD_RE = re.compile(r"\S+")
UPLOAD_TIMEOUT_SEC = 60
SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "upload-commit.sh"
ERROR_LOG = Path(__file__).resolve().parent.parent / "error.txt"


@app.get("/", tags=["system"])
def root():
    return {"name": "books-library-uploader", "status": "ok", "docs": "/docs"}


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}


def _word_count(title: str) -> int:
    return len(WORD_RE.findall(title.strip()))


def _validate_title(title: str) -> str:
    t = title.strip()
    if not t:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="title must not be empty")
    if _word_count(t) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="title must contain at least two words",
        )
    return t


def _validate_pdf_content(content: bytes, filename: str | None, content_type: str | None) -> None:
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"empty file: {filename or 'unknown'}")
    if len(content) > settings.max_pdf_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"file too large ({len(content)} bytes), max {settings.max_pdf_bytes}",
        )
    if not content.startswith(PDF_MAGIC):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"file is not a valid PDF (missing %PDF header): {filename or 'unknown'}",
        )
    if filename and not filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"file must have .pdf extension: {filename}"
        )


def _relative_upload_path(slug: str) -> str:
    return f"uploads/{slug}.pdf"


@app.post("/upload", status_code=status.HTTP_202_ACCEPTED, tags=["upload"])
async def upload(
    files: Annotated[list[UploadFile], File(description="PDF files (1-5)")],
    titles: Annotated[list[str], Form(description="Titles, one per file, each at least two words")],
    descriptions: Annotated[list[str] | None, Form(description="Optional descriptions, one per file")] = None,
    authors: Annotated[list[str] | None, Form(description="Optional authors, one per file")] = None,
):
    if not files or len(files) == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="at least one file required")
    if len(files) > 5:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="batch limit is 5 files")
    if len(titles) != len(files):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"titles count ({len(titles)}) must match files count ({len(files)})",
        )
    descriptions = descriptions or []
    authors = authors or []
    if descriptions and len(descriptions) != len(files):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"descriptions count ({len(descriptions)}) must match files count ({len(files)}) or be omitted",
        )
    if authors and len(authors) != len(files):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"authors count ({len(authors)}) must match files count ({len(files)}) or be omitted",
        )
    if not descriptions:
        descriptions = ["" for _ in files]
    if not authors:
        authors = ["" for _ in files]

    cleaned_titles: list[str] = []
    for t in titles:
        cleaned_titles.append(_validate_title(t))

    contents: list[bytes] = []
    for f in files:
        data = await f.read()
        _validate_pdf_content(data, f.filename, f.content_type)
        contents.append(data)

    slugs: list[str] = []
    relative_paths: list[str] = []
    results: list[dict] = []

    async with _upload_lock:
        meta_path = settings.library_dir / "meta_dt.json"
        meta = {}
        try:
            meta = read_meta(meta_path)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

        existing_keys = set(meta.keys())
        batch_slugs: set[str] = set()

        for idx, title in enumerate(cleaned_titles):
            base_meta = {**meta, **{s: {} for s in batch_slugs}}
            slug = build_slug(title, base_meta)
            while slug in existing_keys or slug in batch_slugs:
                slug = build_slug(title + " " + slug, base_meta)  # fallback entropy
            batch_slugs.add(slug)
            slugs.append(slug)

        for idx, slug in enumerate(slugs):
            save_pdf(slug, contents[idx])
            relative_paths.append(_relative_upload_path(slug))

        for idx, slug in enumerate(slugs):
            register_book(slug, cleaned_titles[idx], descriptions[idx] or None, authors[idx] or None)
            results.append(
                {
                    "slug": slug,
                    "title": cleaned_titles[idx],
                    "description": descriptions[idx],
                    "author": authors[idx],
                    "filename": f"{slug}.pdf",
                    "path": relative_paths[idx],
                    "size_bytes": len(contents[idx]),
                }
            )

    if SCRIPT_PATH.exists():
        try:
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT_PATH),
                    str(settings.library_dir.resolve()),
                    str(ERROR_LOG.resolve()),
                    *relative_paths,
                ],
                capture_output=True,
                text=True,
                timeout=UPLOAD_TIMEOUT_SEC,
                env={**dict(__import__("os").environ), "GIT_TERMINAL_PROMPT": "0"},
            )
            if proc.returncode != 0:
                msg = (proc.stderr or proc.stdout or f"upload-commit failed code {proc.returncode}").strip()
                ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
                with ERROR_LOG.open("a", encoding="utf-8") as fh:
                    import datetime

                    fh.write(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] upload-commit failed: {msg}\n")
        except subprocess.TimeoutExpired:
            ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
            with ERROR_LOG.open("a", encoding="utf-8") as fh:
                import datetime

                fh.write(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] upload-commit timed out after {UPLOAD_TIMEOUT_SEC}s\n")
        except Exception as e:
            ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
            with ERROR_LOG.open("a", encoding="utf-8") as fh:
                import datetime

                fh.write(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] upload-commit error: {e}\n")

    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={"uploaded": results, "count": len(results)})
