import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

UPLOADER_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(UPLOADER_DIR / ".env"), env_prefix="UPLOADER_", extra="ignore"
    )

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    max_pdf_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    max_batch_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_multipart_overhead_bytes: int = Field(default=2 * 1024 * 1024, gt=0)
    max_files: int = Field(default=5, ge=1, le=5)
    max_workspace_bytes: int = Field(default=1024 * 1024 * 1024, gt=0)
    workspace_reserve_bytes: int = Field(default=8 * 1024 * 1024, ge=0)
    cleanup_threshold_bytes: int = Field(default=300 * 1024 * 1024, gt=0)
    remote_url: str = "git@github.com:chizumizu/Library.git"
    target_branch: str = "main"
    lock_file: Path | None = None
    library_dir: Path = UPLOADER_DIR.parent

    @field_validator("library_dir")
    @classmethod
    def resolve_library_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else (UPLOADER_DIR / value).resolve()

    @field_validator("lock_file")
    @classmethod
    def resolve_lock_file(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        return value if value.is_absolute() else (UPLOADER_DIR / value).resolve()


settings = Settings()


def shared_lock_path() -> Path:
    path = settings.lock_file or settings.library_dir.parent / ".library-uploader.lock"
    path = path.resolve()
    library = settings.library_dir.resolve()
    if path == library or library in path.parents:
        raise ValueError("lock file must be outside the replaceable library checkout")
    return path


def workspace_size(path: Path | None = None) -> int:
    root = path or settings.library_dir
    total = 0
    for item in root.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except FileNotFoundError:
            continue
    return total


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(path.parent), prefix=".meta-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_meta(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
        raise ValueError("Metadata must be an object of book records")
    return data
