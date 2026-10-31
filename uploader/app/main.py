from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Any, BinaryIO

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse

from app.config import read_meta, settings, shared_lock_path, workspace_size
from app.locking import LockBusy, exclusive_workspace_lock
from app.storage import build_slug, finalize_pdf, write_batch_metadata


class LimitedMultipartRoute(APIRoute):
    def get_route_handler(self):
        original_handler = super().get_route_handler()

        async def limited_handler(request):
            original_form = request.form

            async def limited_form(*, max_files=1000, max_fields=1000, max_part_size=1024 * 1024):
                return await original_form(
                    max_files=settings.max_files,
                    max_fields=settings.max_files * 3,
                    max_part_size=settings.max_multipart_overhead_bytes,
                )

            request.form = limited_form
            return await original_handler(request)

        return limited_handler


app = FastAPI(
    title="Books Library Uploader",
    description="Upload up to 5 PDFs with metadata. Docs are served at /docs.",
    version="0.2.0",
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
app.router.route_class = LimitedMultipartRoute


def _restore_binary_format(schema: dict[str, Any]) -> None:
    for component in schema.get("components", {}).get("schemas", {}).values():
        for prop in component.get("properties", {}).values():
            target = prop.get("items") if prop.get("type") == "array" else prop
            if isinstance(target, dict) and target.get("contentMediaType") == "application/octet-stream":
                target.pop("contentMediaType", None)
                target["format"] = "binary"


def _prioritize_upload_route(schema: dict[str, Any]) -> None:
    paths = schema.get("paths", {})
    if "/upload" in paths:
        schema["paths"] = {
            "/upload": paths["/upload"],
            **{path: item for path, item in paths.items() if path != "/upload"},
        }


def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
    _restore_binary_format(schema)
    _prioritize_upload_route(schema)
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi


class MultipartSizeLimit:
    """Cap bytes handed to multipart parsing before UploadFile spooling."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/upload":
            await self.inner(scope, receive, send)
            return
        max_bytes = settings.max_batch_bytes + settings.max_multipart_overhead_bytes
        headers = dict(scope.get("headers", []))
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                if int(raw_length) > max_bytes:
                    await self._too_large(send)
                    return
            except ValueError:
                pass

        seen = 0
        exceeded = False

        async def limited_receive():
            nonlocal seen, exceeded
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > max_bytes:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def limited_send(message):
            if exceeded:
                if message["type"] == "http.response.start":
                    await self._too_large(send)
                return
            await send(message)

        await self.inner(scope, limited_receive, limited_send)

    @staticmethod
    async def _too_large(send):
        body = b'{"detail":"multipart request exceeds configured size limit"}'
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(MultipartSizeLimit)

PDF_MAGIC = b"%PDF"
WORD_RE = re.compile(r"\S+")
COPY_CHUNK_BYTES = 256 * 1024
UPLOAD_TIMEOUT_SEC = 120
SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "upload-commit.sh"
CLEANUP_PATH = Path(__file__).resolve().parent.parent / "scripts" / "cleanup-workspace.py"
ERROR_LOG_PATH = Path(__file__).resolve().parent.parent / "error.txt"


@app.get("/", tags=["system"])
def root():
    return {"name": "books-library-uploader", "status": "ok", "docs": "/docs"}


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}


def _word_count(title: str) -> int:
    return len(WORD_RE.findall(title.strip()))


def _validate_title(title: str) -> str:
    cleaned = title.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="title must not be empty")
    if _word_count(cleaned) < 2:
        raise HTTPException(status_code=400, detail="title must contain at least two words")
    return cleaned


def _copy_upload(source: BinaryIO, destination: Path, filename: str | None, byte_budget: int) -> int:
    if not filename or not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="file must have .pdf extension")
    total = 0
    prefix = bytearray()
    try:
        with destination.open("xb") as out:
            while True:
                remaining = byte_budget - total
                chunk = source.read(min(COPY_CHUNK_BYTES, remaining + 1))
                if not chunk:
                    break
                if total + len(chunk) > byte_budget:
                    raise HTTPException(status_code=413, detail="file or batch exceeds configured size limit")
                if len(prefix) < len(PDF_MAGIC):
                    prefix.extend(chunk[: len(PDF_MAGIC) - len(prefix)])
                    if len(prefix) == len(PDF_MAGIC) and bytes(prefix) != PDF_MAGIC:
                        raise HTTPException(status_code=400, detail="file is not a valid PDF (missing %PDF header)")
                total += len(chunk)
                out.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="empty PDF file")
        if bytes(prefix) != PDF_MAGIC:
            raise HTTPException(status_code=400, detail="file is not a valid PDF (missing %PDF header)")
        return total
    except Exception:
        destination.unlink(missing_ok=True)
        raise


async def _publish(library: Path, paths: list[str]) -> None:
    if not SCRIPT_PATH.is_file():
        raise RuntimeError("publisher script is missing")
    env = os.environ.copy()
    env["UPLOADER_TARGET_BRANCH"] = settings.target_branch
    env["UPLOADER_REMOTE_URL"] = settings.remote_url
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = await asyncio.create_subprocess_exec(
        "bash", str(SCRIPT_PATH), str(library), str(ERROR_LOG_PATH), *paths,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=UPLOAD_TIMEOUT_SEC)
    except asyncio.TimeoutError as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        raise RuntimeError("Git publication timed out") from exc
    if proc.returncode != 0:
        raise RuntimeError("Git publication failed")


async def _cleanup_if_needed() -> None:
    try:
        if workspace_size() < settings.cleanup_threshold_bytes:
            return
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(CLEANUP_PATH), "--if-over-threshold",
            cwd=str(settings.library_dir),
            env={
                **os.environ,
                "UPLOADER_LIBRARY_DIR": str(settings.library_dir),
                "UPLOADER_TARGET_BRANCH": settings.target_branch,
                "UPLOADER_REMOTE_URL": settings.remote_url,
                "UPLOADER_CLEANUP_THRESHOLD_BYTES": str(settings.cleanup_threshold_bytes),
                "UPLOADER_MAX_WORKSPACE_BYTES": str(settings.max_workspace_bytes),
                "UPLOADER_LOCK_FILE": str(shared_lock_path()),
            },
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception:
        # A published upload remains successful; the next upload is gated by
        # the hard workspace ceiling if maintenance could not reduce usage.
        return


@app.post("/upload", tags=["upload"])
async def upload(
    files: Annotated[list[UploadFile] | None, File(description="PDF files (1-5)")] = None,
    titles: Annotated[list[str] | None, Form(description="Titles, one per file, each at least two words")] = None,
    descriptions: Annotated[list[str] | None, Form(description="Optional descriptions, one per file")] = None,
    authors: Annotated[list[str] | None, Form(description="Optional authors, one per file")] = None,
):
    if not files:
        raise HTTPException(status_code=400, detail="at least one file required")
    titles = titles or []
    if len(files) > settings.max_files:
        raise HTTPException(status_code=400, detail=f"batch limit is {settings.max_files} files")
    if len(titles) != len(files):
        raise HTTPException(status_code=400, detail="titles count must match files count")
    descriptions = descriptions or []
    authors = authors or []
    if descriptions and len(descriptions) != len(files):
        raise HTTPException(status_code=400, detail="descriptions count must match files count or be omitted")
    if authors and len(authors) != len(files):
        raise HTTPException(status_code=400, detail="authors count must match files count or be omitted")
    descriptions = descriptions or [""] * len(files)
    authors = authors or [""] * len(files)
    cleaned_titles = [_validate_title(title) for title in titles]

    library = settings.library_dir.resolve()
    library.mkdir(parents=True, exist_ok=True)
    lock_path = shared_lock_path()
    try:
        lock = exclusive_workspace_lock(lock_path)
        lock.__enter__()
    except LockBusy:
        raise HTTPException(status_code=409, detail="Uploader workspace is busy; retry in 15 seconds", headers={"Retry-After": "15"})

    uploaded: list[dict[str, Any]] = []
    try:
        current_size = workspace_size(library)
        remaining_storage = settings.max_workspace_bytes - current_size
        if remaining_storage <= 0:
            raise HTTPException(
                status_code=507,
                detail=f"Uploader workspace reached its configured storage limit ({settings.max_workspace_bytes} bytes); retry after cleanup",
            )

        meta_path = library / "meta_dt.json"
        try:
            meta = read_meta(meta_path)
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=500, detail="metadata is invalid") from exc

        available_for_upload = remaining_storage - settings.workspace_reserve_bytes
        batch_budget = min(settings.max_batch_bytes, max(0, available_for_upload // 2))
        if batch_budget <= 0:
            raise HTTPException(status_code=507, detail="Insufficient workspace space for this upload")
        known_sizes = [upload_file.size for upload_file in files]
        if all(size is not None for size in known_sizes):
            if any(size > settings.max_pdf_bytes for size in known_sizes):
                raise HTTPException(status_code=413, detail="file exceeds configured per-file size limit")
            known_batch_size = sum(known_sizes)
            if known_batch_size > settings.max_batch_bytes:
                raise HTTPException(status_code=413, detail="batch exceeds configured aggregate PDF limit")
            if known_batch_size > batch_budget:
                raise HTTPException(status_code=507, detail="Insufficient workspace space for this upload")

        with tempfile.TemporaryDirectory(prefix=".uploader-staging-", dir=library) as stage_name:
            stage = Path(stage_name)
            sizes: list[int] = []
            aggregate = 0
            for index, upload_file in enumerate(files):
                per_file_budget = min(settings.max_pdf_bytes, batch_budget - aggregate)
                if per_file_budget <= 0:
                    raise HTTPException(status_code=413, detail="batch exceeds configured aggregate PDF limit")
                size = _copy_upload(
                    upload_file.file,
                    stage / f"{index}.pdf",
                    upload_file.filename,
                    per_file_budget,
                )
                aggregate += size
                sizes.append(size)
            if aggregate > settings.max_batch_bytes:
                raise HTTPException(status_code=413, detail="batch exceeds configured aggregate PDF limit")

            occupied = dict(meta)
            uploads_dir = library / "uploads"
            uploads_dir.mkdir(parents=True, exist_ok=True)
            for pdf in uploads_dir.glob("*.pdf"):
                occupied.setdefault(pdf.stem, {})
            slugs: list[str] = []
            for title in cleaned_titles:
                slug = build_slug(title, occupied)
                occupied[slug] = {}
                slugs.append(slug)

            created: list[Path] = []
            try:
                for index, slug in enumerate(slugs):
                    target = uploads_dir / f"{slug}.pdf"
                    finalize_pdf(stage / f"{index}.pdf", target)
                    created.append(target)
                write_batch_metadata(
                    meta_path,
                    meta,
                    [(slugs[i], cleaned_titles[i], descriptions[i] or None, authors[i] or None) for i in range(len(files))],
                )
            except Exception:
                for path in created:
                    path.unlink(missing_ok=True)
                raise

            relative_paths = [f"uploads/{slug}.pdf" for slug in slugs]
            uploaded = [
                {
                    "slug": slugs[i],
                    "title": cleaned_titles[i],
                    "description": descriptions[i],
                    "author": authors[i],
                    "filename": f"{slugs[i]}.pdf",
                    "path": relative_paths[i],
                    "size_bytes": sizes[i],
                }
                for i in range(len(files))
            ]
        try:
            await _publish(library, relative_paths)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(
                status_code=503,
                detail="GitHub publication failed; uploaded files and metadata were preserved for recovery",
            ) from exc
    finally:
        lock.__exit__(None, None, None)

    await _cleanup_if_needed()
    return JSONResponse(status_code=status.HTTP_200_OK, content={"uploaded": uploaded, "count": len(uploaded)})
