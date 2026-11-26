"""The helper key: the only thing kept on disk."""

from __future__ import annotations

import os
from pathlib import Path

KEY_PREFIX = "sor_"


class KeyStore:
    def __init__(self, data_dir: str):
        self.dir = Path(data_dir)
        self.path = self.dir / "key"

    def load(self) -> str | None:
        try:
            key = self.path.read_text(encoding="ascii").strip()
        except (FileNotFoundError, UnicodeDecodeError):
            return None
        if not key.startswith(KEY_PREFIX) or not key.isascii() or not key.isprintable():
            return None
        return key

    def save(self, key: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name("key.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, key.encode("ascii") + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)
