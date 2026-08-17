from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


ALLOWED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png"})
MAX_FILES = 20
MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_TOTAL_SIZE = 200 * 1024 * 1024
MIN_WIDTH = 470
MIN_HEIGHT = 470
MAX_WIDTH = 8192
MAX_HEIGHT = 8192
MAX_PIXELS = 33_554_432


@dataclass(frozen=True, slots=True)
class MediaAsset:
    path: Path
    sha256: str
    size_bytes: int
    width: int
    height: int
    codec: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
