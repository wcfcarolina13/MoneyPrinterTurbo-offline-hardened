from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Sequence
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


def resolve_under(
    root: Path,
    candidate: Path,
    *,
    must_exist: bool = True,
) -> Path:
    resolved_root = Path(root).resolve(strict=True)
    unresolved = Path(candidate)
    if not unresolved.is_absolute():
        unresolved = resolved_root / unresolved
    resolved = unresolved.resolve(strict=must_exist)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"path escapes approved root: {candidate}") from exc
    return resolved


def publish_file_no_replace(source: Path, destination: Path) -> None:
    """Atomically publish a same-filesystem file without replacing a destination."""
    source_path = Path(source).resolve(strict=True)
    destination_path = Path(destination).absolute()
    if not source_path.is_file() or not destination_path.parent.is_dir():
        raise ValueError("publication paths must reference a file and directory")
    source_fd = os.open(source_path, os.O_RDONLY)
    try:
        os.fsync(source_fd)
    finally:
        os.close(source_fd)
    published = False
    try:
        os.link(source_path, destination_path)
        published = True
        directory_fd = os.open(destination_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        source_path.unlink()
    except Exception:
        if published:
            destination_path.unlink(missing_ok=True)
        raise


def _probe_document(path: Path, ffprobe_bin: str) -> dict[str, object]:
    completed = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        document = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"ffprobe returned invalid JSON for {path.name}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"ffprobe returned invalid JSON for {path.name}")
    return document


def probe_image(path: Path, *, ffprobe_bin: str = "ffprobe") -> MediaAsset:
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"media asset is not a regular file: {path}")
    streams = _probe_document(resolved, ffprobe_bin).get("streams")
    if not isinstance(streams, list) or len(streams) != 1:
        raise ValueError("image must contain exactly one video stream")
    stream = streams[0]
    if not isinstance(stream, dict) or stream.get("codec_type") != "video":
        raise ValueError("image probe did not return a video stream")
    codec = stream.get("codec_name")
    expected = {".jpg": "mjpeg", ".jpeg": "mjpeg", ".png": "png"}
    if codec != expected.get(resolved.suffix.lower()):
        raise ValueError(f"unsupported image codec: {codec!r}")
    try:
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("image probe omitted valid dimensions") from exc
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        raise ValueError(
            f"image dimensions must be at least {MIN_WIDTH}x{MIN_HEIGHT}: "
            f"got {width}x{height}"
        )
    if width > MAX_WIDTH or height > MAX_HEIGHT:
        raise ValueError(
            f"image dimensions must not exceed {MAX_WIDTH}x{MAX_HEIGHT}: "
            f"got {width}x{height}"
        )
    if width * height > MAX_PIXELS:
        raise ValueError(f"image exceeds the {MAX_PIXELS} total-pixels limit")
    return MediaAsset(
        path=resolved,
        sha256=sha256_file(resolved),
        size_bytes=resolved.stat().st_size,
        width=width,
        height=height,
        codec=codec,
    )


def _validated_candidates(input_root: Path, names: Sequence[str]) -> list[Path]:
    root = Path(input_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"input root is not a directory: {input_root}")
    if not names:
        raise ValueError("at least one input asset is required")
    if len(names) > MAX_FILES:
        raise ValueError(f"no more than {MAX_FILES} input files are allowed")
    candidates = []
    seen = set()
    total_size = 0
    for name in names:
        if not isinstance(name, str) or not name:
            raise ValueError("input names must be non-empty strings")
        candidate = resolve_under(root, root / name)
        if candidate in seen:
            raise ValueError(f"duplicate input asset: {name}")
        seen.add(candidate)
        if candidate.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise ValueError(f"unsupported input extension: {candidate.suffix}")
        if not candidate.is_file():
            raise ValueError(f"input asset is not a regular file: {name}")
        size = candidate.stat().st_size
        if size > MAX_FILE_SIZE:
            raise ValueError(f"input file exceeds 50 MiB: {name}")
        total_size += size
        candidates.append(candidate)
    if total_size > MAX_TOTAL_SIZE:
        raise ValueError("input files exceed the 200 MiB aggregate limit")
    return candidates


def validate_inputs(
    input_root: Path,
    names: Sequence[str],
    *,
    ffprobe_bin: str = "ffprobe",
) -> list[MediaAsset]:
    """Validate still images from a caller-owned, immutable input directory."""
    assets = []
    for candidate in _validated_candidates(input_root, names):
        expected_size = candidate.stat().st_size
        asset = probe_image(candidate, ffprobe_bin=ffprobe_bin)
        if asset.size_bytes != expected_size:
            raise ValueError(f"input changed during validation: {candidate.name}")
        assets.append(asset)
    return assets
