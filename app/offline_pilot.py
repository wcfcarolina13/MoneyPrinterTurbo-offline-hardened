from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path

from app.offline_media import (
    ALLOWED_EXTENSIONS,
    MAX_FILES,
    MAX_FILE_SIZE,
    MAX_TOTAL_SIZE,
    publish_file_no_replace,
    resolve_under,
    sha256_file,
    validate_inputs,
)
from app.offline_render import RenderPlan, render_video


MAX_FONT_SIZE = 20 * 1024 * 1024
MAX_PLAN_SIZE = 8 * 1024
DEFAULT_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
EXIT_FAILURE = 1


def _read_root_file(root: Path, name: str, *, limit: int, label: str) -> bytes:
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise OSError("secure input reads require openat and O_NOFOLLOW support")
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError(f"{label} path must be a root-level filename")
    resolved_root = Path(root).resolve(strict=True)
    root_fd = os.open(resolved_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"{label} is not a regular file: {name}")
            if before.st_size > limit:
                raise ValueError(f"{label} exceeds the {limit}-byte limit")
            chunks = []
            size = 0
            while chunk := os.read(descriptor, min(64 * 1024, limit + 1)):
                size += len(chunk)
                if size > limit:
                    raise ValueError(f"{label} exceeds the {limit}-byte limit")
                chunks.append(chunk)
            after = os.fstat(descriptor)
            stable = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) == (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if not stable or size != before.st_size:
                raise ValueError(f"{label} changed while being read")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(root_fd)


def load_plan(input_root: Path, name: str) -> RenderPlan:
    raw = _read_root_file(
        input_root,
        name,
        limit=MAX_PLAN_SIZE,
        label="render plan",
    )
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("render plan must be valid UTF-8 JSON") from exc
    if not isinstance(document, dict) or set(document) != {"slug", "lines"}:
        raise ValueError("render plan must contain exactly slug and lines")
    lines = document["lines"]
    if not isinstance(lines, list):
        raise ValueError("render plan lines must be a JSON array")
    return RenderPlan(slug=document["slug"], lines=tuple(lines))


def _copy_descriptor(
    source_fd: int,
    destination: Path,
    *,
    limit: int,
    label: str,
) -> int:
    destination_fd = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o400,
    )
    try:
        copied = 0
        while chunk := os.read(source_fd, 1024 * 1024):
            copied += len(chunk)
            if copied > limit:
                raise ValueError(f"{label} exceeds its size limit")
            view = memoryview(chunk)
            while view:
                view = view[os.write(destination_fd, view) :]
        os.fsync(destination_fd)
        return copied
    finally:
        os.close(destination_fd)


def _snapshot_inputs(input_root: Path, names: Sequence[str], snapshot: Path) -> None:
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise OSError("secure input snapshots require openat and O_NOFOLLOW support")
    if not names or len(names) > MAX_FILES:
        raise ValueError(f"between 1 and {MAX_FILES} input files are required")
    root = Path(input_root).resolve(strict=True)
    root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    total_size = 0
    seen = set()
    try:
        for name in names:
            if not isinstance(name, str) or Path(name).name != name or name in seen:
                raise ValueError("image names must be unique root-level filenames")
            seen.add(name)
            if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
                raise ValueError(f"unsupported input extension: {Path(name).suffix}")
            source_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                before = os.fstat(source_fd)
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError(f"input is not a regular file: {name}")
                if before.st_size > MAX_FILE_SIZE:
                    raise ValueError(f"input file exceeds 50 MiB: {name}")
                total_size += before.st_size
                if total_size > MAX_TOTAL_SIZE:
                    raise ValueError("input files exceed the 200 MiB aggregate limit")
                copied = _copy_descriptor(
                    source_fd,
                    snapshot / name,
                    limit=MAX_FILE_SIZE,
                    label=f"input file {name}",
                )
                after = os.fstat(source_fd)
                stable = (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) == (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
                if not stable or copied != before.st_size:
                    raise ValueError(f"input changed while snapshotting: {name}")
            finally:
                os.close(source_fd)
    finally:
        os.close(root_fd)


def _snapshot_font(source: Path, snapshot: Path) -> Path:
    resolved = Path(source).resolve(strict=True)
    descriptor = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"font is not a regular file: {source}")
        if before.st_size > MAX_FONT_SIZE:
            raise ValueError("font exceeds the 20 MiB limit")
        destination = snapshot / "font.ttf"
        copied = _copy_descriptor(
            descriptor,
            destination,
            limit=MAX_FONT_SIZE,
            label="font",
        )
        after = os.fstat(descriptor)
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if not stable or copied != before.st_size:
            raise ValueError("font changed while snapshotting")
        return destination.resolve(strict=True)
    finally:
        os.close(descriptor)


def write_manifest(path: Path, payload: Mapping[str, object]) -> None:
    destination = Path(path)
    destination.parent.resolve(strict=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        publish_file_no_replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def _resolve_tool(name: str) -> Path:
    location = shutil.which(name)
    if not location:
        raise FileNotFoundError(f"required executable is unavailable: {name}")
    path = Path(location).resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"required executable is not runnable: {path}")
    return path


def _executable_identity(path: Path) -> dict[str, object]:
    resolved = Path(path).resolve(strict=True)
    before = sha256_file(resolved)
    completed = subprocess.run(
        [str(resolved), "-version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    after = sha256_file(resolved)
    if before != after:
        raise ValueError(f"executable identity changed during version probe: {resolved}")
    version = next((line.strip() for line in completed.stdout.splitlines() if line), "")
    if not version:
        raise ValueError(f"tool did not report a version: {resolved}")
    return {
        "path": str(resolved),
        "name": resolved.name,
        "version": version,
        "sha256": after,
    }


def _require_ffmpeg_filters(ffmpeg: Path) -> None:
    completed = subprocess.run(
        [str(ffmpeg), "-filters"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    available = set()
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            available.add(fields[1])
    required = {"crop", "drawtext", "scale", "xfade", "zoompan"}
    missing = sorted(required - available)
    if missing:
        raise ValueError(f"required FFmpeg filters unavailable: {', '.join(missing)}")


def _git_commit(root: Path) -> str:
    git = root / ".git"
    try:
        if git.is_file():
            git = (root / git.read_text().strip().removeprefix("gitdir: ")).resolve()
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head
        reference = head.removeprefix("ref: ")
        loose = git / reference
        if loose.is_file():
            return loose.read_text().strip()
        for line in (git / "packed-refs").read_text().splitlines():
            if line.endswith(f" {reference}"):
                return line.split()[0]
    except OSError:
        pass
    return "unknown"


def _application_identity() -> dict[str, object]:
    root = Path(__file__).resolve().parents[1]
    version = os.environ.get("OFFLINE_APP_VERSION")
    if not version:
        try:
            version = tomllib.loads((root / "pyproject.toml").read_text())["project"][
                "version"
            ]
        except (OSError, KeyError, tomllib.TOMLDecodeError):
            version = "unknown"
    configured_commit = os.environ.get("OFFLINE_APP_COMMIT")
    if os.environ.get("OFFLINE_REQUIRE_APP_COMMIT") == "1":
        if not configured_commit:
            raise ValueError("OFFLINE_APP_COMMIT is required in container mode")
        if len(configured_commit) != 40 or any(
            character not in "0123456789abcdef" for character in configured_commit
        ):
            raise ValueError(
                "OFFLINE_APP_COMMIT must be a lowercase 40-character Git SHA"
            )
    return {
        "name": "offline-media-pilot",
        "version": version,
        "commit": configured_commit or _git_commit(root),
        "module_sha256": {
            "offline_media.py": sha256_file(Path(__file__).with_name("offline_media.py")),
            "offline_pilot.py": sha256_file(Path(__file__)),
            "offline_render.py": sha256_file(Path(__file__).with_name("offline_render.py")),
        },
    }


def _public_tool_identity(identity: Mapping[str, object]) -> dict[str, object]:
    return {
        "name": identity["name"],
        "version": identity["version"],
        "sha256": identity["sha256"],
    }


def _portable_render(
    render: Mapping[str, object],
    *,
    output_name: str,
    font_identity: Mapping[str, object],
) -> dict[str, object]:
    operations = dict(render["operations"])
    operations["font"] = dict(font_identity)
    output = dict(render["output"])
    output["path"] = output_name
    return {
        "slug": render["slug"],
        "copy": list(render["copy"]),
        "sources": list(render["sources"]),
        "operations": operations,
        "output": output,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render a deterministic offline edit from local still images"
    )
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--plan", required=True, help="root-level JSON render plan")
    parser.add_argument("--output", help="MP4 filename beneath the output root")
    parser.add_argument("--font-file", type=Path, default=DEFAULT_FONT)
    parser.add_argument("images", nargs="+", help="root-level image filenames")
    return parser


def _run(argv: Sequence[str] | None) -> int:
    arguments = _parser().parse_args(argv)
    input_root = Path(arguments.input_root).resolve(strict=True)
    output_root = Path(arguments.output_root).resolve(strict=True)
    if not input_root.is_dir() or not output_root.is_dir():
        raise ValueError("approved input and output roots must be directories")
    plan = load_plan(input_root, arguments.plan)
    output_name = arguments.output or f"{plan.slug}.mp4"
    if Path(output_name).name != output_name:
        raise ValueError("output path must be a root-level filename")
    if Path(output_name).suffix.lower() != ".mp4":
        raise ValueError("output must use the .mp4 extension")
    output_path = resolve_under(output_root, output_root / output_name, must_exist=False)
    manifest_path = resolve_under(
        output_root,
        output_path.with_suffix(".manifest.json"),
        must_exist=False,
    )
    if output_path.exists() or manifest_path.exists():
        raise ValueError("output and manifest destinations must not already exist")
    font = Path(arguments.font_file).resolve(strict=True)
    if not font.is_file():
        raise ValueError(f"font is not a regular file: {font}")
    ffmpeg = _resolve_tool("ffmpeg")
    ffprobe = _resolve_tool("ffprobe")
    if ffprobe != ffmpeg.with_name("ffprobe"):
        raise ValueError("ffmpeg and ffprobe must come from the same installation")
    tools = {
        "ffmpeg": _executable_identity(ffmpeg),
        "ffprobe": _executable_identity(ffprobe),
    }
    _require_ffmpeg_filters(ffmpeg)
    application = _application_identity()

    with tempfile.TemporaryDirectory(prefix="offline-input-snapshot-") as directory:
        snapshot = Path(directory)
        _snapshot_inputs(input_root, arguments.images, snapshot)
        snapshot_font = _snapshot_font(font, snapshot)
        assets = validate_inputs(snapshot, arguments.images, ffprobe_bin=str(ffprobe))
        sources = []
        for name, asset in zip(arguments.images, assets, strict=True):
            source = asdict(asset)
            source["name"] = name
            source.pop("path")
            sources.append(source)
        font_identity = {
            "sha256": sha256_file(snapshot_font),
            "size_bytes": snapshot_font.stat().st_size,
        }
        transaction_complete = False
        try:
            render = render_video(
                assets,
                output_path,
                plan,
                ffmpeg_bin=str(ffmpeg),
                font_file=snapshot_font,
            )
            final_tools = {
                "ffmpeg": _executable_identity(ffmpeg),
                "ffprobe": _executable_identity(ffprobe),
            }
            if final_tools != tools:
                raise ValueError("executable identity changed during rendering")
            portable_render = _portable_render(
                render,
                output_name=output_path.name,
                font_identity=font_identity,
            )
            payload = {
                "schema_version": 1,
                "plan": {"slug": plan.slug, "lines": list(plan.lines)},
                "features": {
                    "network": False,
                    "stock_media": False,
                    "ai_media_generation": False,
                    "llm_scripting": False,
                    "tts": False,
                    "generated_music": False,
                    "publication": False,
                },
                "identity": {
                    "application": application,
                    "tools": {
                        name: _public_tool_identity(identity)
                        for name, identity in tools.items()
                    },
                    "font": font_identity,
                },
                "sources": sources,
                "render": portable_render,
                "output": portable_render["output"],
            }
            write_manifest(manifest_path, payload)
            transaction_complete = True
        finally:
            if not transaction_complete:
                output_path.unlink(missing_ok=True)
                manifest_path.unlink(missing_ok=True)
    return 0


def _failure_message(error: BaseException) -> str:
    if isinstance(error, subprocess.CalledProcessError) and error.stderr:
        return error.stderr.strip().splitlines()[-1]
    return str(error).strip() or error.__class__.__name__


def main(argv: Sequence[str] | None = None) -> int:
    """Return 0 on success or 1 for expected validation and render failures."""
    try:
        return _run(argv)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"offline-pilot: {_failure_message(error)}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
