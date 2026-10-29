import secrets
from pathlib import Path
from typing import Any

from slugify import slugify

from app.config import atomic_write_json, read_meta, settings

SLUG_SEPARATOR = "-"
SUFFIX_LENGTH = 4
FALLBACK_SLUG = "untitled"


def build_slug(title: str, meta: dict[str, Any]) -> str:
    base = slugify(title, separator=SLUG_SEPARATOR) or FALLBACK_SLUG
    if base not in meta:
        return base
    while True:
        candidate = f"{base}-{secrets.token_hex(2)}"
        if candidate not in meta:
            return candidate


def save_pdf(slug: str, content: bytes) -> Path:
    uploads = settings.library_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    target = uploads / f"{slug}.pdf"
    target.write_bytes(content)
    return target


def register_book(
    slug: str, title: str, description: str | None, author: str | None
) -> None:
    meta_path = settings.library_dir / "meta_dt.json"
    meta = read_meta(meta_path)
    meta[slug] = {
        "slug": slug,
        "title": title,
        "description": description or "",
        "author": author or "",
        "tag": "book",
    }
    atomic_write_json(meta_path, meta)
