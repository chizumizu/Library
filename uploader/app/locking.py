from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class LockBusy(Exception):
    pass


@contextmanager
def exclusive_workspace_lock(path: Path, *, wait: bool = False) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        flags = fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError as exc:
            raise LockBusy from exc
        yield
    finally:
        os.close(fd)
