from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from httpx2 import ASGITransport, AsyncByteStream, AsyncClient

from app import main
from app.config import settings
from app.locking import exclusive_workspace_lock


@pytest.fixture
def client_config(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setattr(settings, "library_dir", library)
    monkeypatch.setattr(settings, "lock_file", tmp_path / "uploader.lock")
    monkeypatch.setattr(settings, "max_pdf_bytes", 1024)
    monkeypatch.setattr(settings, "max_batch_bytes", 2048)
    monkeypatch.setattr(settings, "max_workspace_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "workspace_reserve_bytes", 0)
    monkeypatch.setattr(settings, "cleanup_threshold_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "max_files", 5)
    monkeypatch.setattr(settings, "target_branch", "main")
    async def publish(_library, _paths):
        return None
    monkeypatch.setattr(main, "_publish", publish)
    return main.app, library


async def post(client_config, path="/upload", **kwargs):
    app, _library = client_config
    data = kwargs.pop("data", {})
    files = kwargs.pop("files", {})
    include_content_length = kwargs.pop("include_content_length", True)
    boundary = "uploader-test-boundary"
    chunks: list[bytes] = []
    data_items = data.items() if isinstance(data, dict) else data
    for name, value in data_items:
        chunks.extend([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode(),
            str(value).encode(),
            b"\r\n",
        ])
    file_items = files.items() if isinstance(files, dict) else files
    for name, file_info in file_items:
        filename, content, content_type = file_info
        chunks.extend([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n".encode(),
            content,
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)

    class Body(AsyncByteStream):
        async def __aiter__(self):
            yield body

    headers = {"content-type": f"multipart/form-data; boundary={boundary}"}
    if include_content_length:
        headers["content-length"] = str(len(body))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            path,
            content=Body(),
            headers=headers,
            **kwargs,
        )


class CountingReader(io.BytesIO):
    def __init__(self, value: bytes):
        super().__init__(value)
        self.bytes_read = 0

    def read(self, size=-1):
        assert size >= 0, "copy must never request an unbounded read"
        value = super().read(size)
        self.bytes_read += len(value)
        return value


def test_title_rule():
    assert main._validate_title("  My\tBook  ") == "My\tBook"
    with pytest.raises(Exception, match="two words"):
        main._validate_title("One")


@pytest.mark.parametrize(
    ("content", "filename", "budget", "status"),
    [
        (b"", "empty.pdf", 20, 400),
        (b"NOPE data", "bad.pdf", 20, 400),
        (b"%PDF data", "bad.txt", 20, 400),
        (b"%PDF 123456", "large.pdf", 8, 413),
    ],
)
def test_streaming_rejects_invalid_uploads(tmp_path, content, filename, budget, status):
    destination = tmp_path / "stage.pdf"
    with pytest.raises(Exception) as error:
        main._copy_upload(io.BytesIO(content), destination, filename, budget)
    assert getattr(error.value, "status_code", None) == status
    assert not destination.exists()


def test_streaming_is_bounded_and_stops_at_first_over_limit_byte(tmp_path):
    source = CountingReader(b"%PDF" + b"x" * 100_000)
    with pytest.raises(Exception) as error:
        main._copy_upload(source, tmp_path / "stage.pdf", "book.PDF", 10)
    assert error.value.status_code == 413
    assert source.bytes_read == 11


def test_pdf_exactly_at_size_limit_is_accepted(tmp_path):
    source = CountingReader(b"%PDF1234")
    assert main._copy_upload(source, tmp_path / "stage.pdf", "book.pdf", 8) == 8


def test_shared_lock_excludes_an_independent_process(tmp_path):
    lock_path = tmp_path / "shared.lock"
    source_root = Path(__file__).resolve().parents[1]
    code = f"""
from pathlib import Path
from app.locking import LockBusy, exclusive_workspace_lock
p = Path({str(lock_path)!r})
try:
    with exclusive_workspace_lock(p):
        pass
except LockBusy:
    raise SystemExit(17)
raise SystemExit(0)
"""
    env = {**os.environ, "PYTHONPATH": str(source_root)}
    with exclusive_workspace_lock(lock_path):
        child = subprocess.run([sys.executable, "-c", code], env=env, check=False)
    assert child.returncode == 17


@pytest.mark.anyio
async def test_upload_publishes_and_commits_one_atomic_metadata_batch(client_config):
    _app, library = client_config
    response = await post(
        client_config,
        "/upload",
        data=[("titles", "First Book"), ("titles", "Second Book")],
        files=[("files", ("a.pdf", b"%PDF one", "application/pdf")),
               ("files", ("b.PDF", b"%PDF two", "application/pdf"))],
    )
    assert response.status_code == 200
    metadata = json.loads((library / "meta_dt.json").read_text())
    assert len(metadata) == 2
    assert len(list((library / "uploads").glob("*.pdf"))) == 2


@pytest.mark.anyio
async def test_existing_pdf_slug_is_never_overwritten(client_config):
    _app, library = client_config
    uploads = library / "uploads"
    uploads.mkdir()
    original = uploads / "my-book.pdf"
    original.write_bytes(b"original")
    response = await post(
        client_config,
        "/upload",
        data={"titles": "My Book"},
        files={"files": ("book.pdf", b"%PDF replacement", "application/pdf")},
    )
    assert response.status_code == 200
    assert original.read_bytes() == b"original"
    assert response.json()["uploaded"][0]["slug"] != "my-book"


@pytest.mark.anyio
async def test_workspace_lock_returns_busy_with_retry_after(client_config):
    _app, _ = client_config
    with exclusive_workspace_lock(settings.lock_file):
        response = await post(
            client_config,
            "/upload",
            data={"titles": "Busy Book"},
            files={"files": ("book.pdf", b"%PDF data", "application/pdf")},
        )
    assert response.status_code == 409
    assert response.headers["retry-after"] == "15"


@pytest.mark.anyio
async def test_validation_failure_creates_no_final_pdf(client_config):
    _app, library = client_config
    response = await post(
        client_config,
        "/upload",
        data=[("titles", "Valid Book"), ("titles", "Other Book")],
        files=[("files", ("a.pdf", b"%PDF valid", "application/pdf")),
               ("files", ("b.pdf", b"not a pdf", "application/pdf"))],
    )
    assert response.status_code == 400
    assert not (library / "uploads").exists()
    assert not (library / "meta_dt.json").exists()


@pytest.mark.anyio
async def test_multipart_body_limit_applies_before_endpoint_parsing(client_config, monkeypatch):
    _app, _ = client_config
    monkeypatch.setattr(settings, "max_batch_bytes", 8)
    monkeypatch.setattr(settings, "max_multipart_overhead_bytes", 4)
    response = await post(
        client_config,
        "/upload",
        data={"titles": "Small Book"},
        files={"files": ("book.pdf", b"%PDF" + b"x" * 100, "application/pdf")},
    )
    assert response.status_code == 413, response.text
    assert "multipart request" in response.json()["detail"]


@pytest.mark.anyio
async def test_aggregate_pdf_limit_is_enforced_without_partial_batch(client_config, monkeypatch):
    _app, library = client_config
    monkeypatch.setattr(settings, "max_batch_bytes", 10)
    response = await post(
        client_config,
        data=[("titles", "First Book"), ("titles", "Second Book")],
        files=[("files", ("a.pdf", b"%PDFabc", "application/pdf")),
               ("files", ("b.pdf", b"%PDFxyz", "application/pdf"))],
    )
    assert response.status_code == 413
    assert not (library / "uploads").exists()
    assert not (library / "meta_dt.json").exists()


@pytest.mark.anyio
async def test_multipart_stream_limit_applies_without_content_length(client_config, monkeypatch):
    _app, _ = client_config
    monkeypatch.setattr(settings, "max_batch_bytes", 8)
    monkeypatch.setattr(settings, "max_multipart_overhead_bytes", 4)
    response = await post(
        client_config,
        data={"titles": "Large Book"},
        files={"files": ("book.pdf", b"%PDF" + b"x" * 100, "application/pdf")},
        include_content_length=False,
    )
    assert response.status_code == 413, response.text


@pytest.mark.anyio
async def test_file_count_limit_is_enforced(client_config, monkeypatch):
    _app, library = client_config
    monkeypatch.setattr(settings, "max_files", 1)
    response = await post(
        client_config,
        data=[("titles", "First Book"), ("titles", "Second Book")],
        files=[("files", ("a.pdf", b"%PDFabc", "application/pdf")),
               ("files", ("b.pdf", b"%PDFxyz", "application/pdf"))],
    )
    assert response.status_code == 400
    assert not (library / "uploads").exists()


@pytest.mark.anyio
async def test_workspace_ceiling_blocks_upload_that_would_cross_limit(client_config, monkeypatch):
    _app, library = client_config
    monkeypatch.setattr(settings, "max_workspace_bytes", 16)
    response = await post(
        client_config,
        data={"titles": "Storage Book"},
        files={"files": ("book.pdf", b"%PDF12345", "application/pdf")},
    )
    assert response.status_code == 507
    assert not (library / "uploads").exists()
    assert not (library / "meta_dt.json").exists()


@pytest.mark.anyio
async def test_missing_files_returns_client_error(client_config):
    response = await post(client_config, data={"titles": "Book Title"})
    assert response.status_code == 400


@pytest.mark.anyio
async def test_optional_metadata_count_mismatch_is_rejected(client_config):
    response = await post(
        client_config,
        data=[("titles", "First Book"), ("titles", "Second Book"), ("authors", "One Author")],
        files=[("files", ("a.pdf", b"%PDFabc", "application/pdf")),
               ("files", ("b.pdf", b"%PDFxyz", "application/pdf"))],
    )
    assert response.status_code == 400


@pytest.mark.anyio
async def test_publishing_failure_is_not_reported_as_success(client_config, monkeypatch):
    _app, library = client_config
    async def failed_publish(*_):
        raise RuntimeError("failed")
    monkeypatch.setattr(main, "_publish", failed_publish)
    response = await post(
        client_config,
        "/upload",
        data={"titles": "Retained Book"},
        files={"files": ("book.pdf", b"%PDF retained", "application/pdf")},
    )
    assert response.status_code == 503
    assert (library / "meta_dt.json").exists()
    assert len(list((library / "uploads").glob("*.pdf"))) == 1
