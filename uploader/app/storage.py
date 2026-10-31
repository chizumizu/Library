import os
import secrets
from pathlib import Path
from typing import Any

from slugify import slugify

from app.config import atomic_write_json

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


def finalize_pdf(staged: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    # A hard link is atomic and fails if the destination already exists.
    os.link(staged, target)


def write_batch_metadata(
    meta_path: Path,
    meta: dict[str, Any],
    entries: list[tuple[str, str, str | None, str | None]],
) -> None:
    for slug, title, description, author in entries:
        meta[slug] = {
            "slug": slug,
            "title": title,
            "description": description or "",
            "author": author or "",
            "tag": "book",
        }
    atomic_write_json(meta_path, meta)
