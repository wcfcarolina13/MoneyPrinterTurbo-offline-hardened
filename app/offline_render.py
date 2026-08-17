from __future__ import annotations

import json
import re
import string
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.offline_media import (
    ALLOWED_EXTENSIONS,
    MAX_FILES,
    MAX_FILE_SIZE,
    MAX_HEIGHT,
    MAX_PIXELS,
    MAX_TOTAL_SIZE,
    MAX_WIDTH,
    MIN_HEIGHT,
    MIN_WIDTH,
    MediaAsset,
    publish_file_no_replace,
    sha256_file,
)


_CODECS_BY_EXTENSION = {".jpg": "mjpeg", ".jpeg": "mjpeg", ".png": "png"}
_SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_CLIP_DURATION = 2.0
_TRANSITION_DURATION = 0.4
_FRAME_RATE = 30
_RENDER_TIMEOUT = 120
_DEFAULT_FONT_SIZE = 52
_LONG_COPY_FONT_SIZE = 40
_LONG_COPY_THRESHOLD = 18


@dataclass(frozen=True, slots=True)
class RenderPlan:
    slug: str
    lines: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.slug, str) or not _SLUG_PATTERN.fullmatch(self.slug):
            raise ValueError("render slug must use lowercase letters, digits, and hyphens")
        if len(self.slug) > 64:
            raise ValueError("render slug must not exceed 64 characters")
        if not isinstance(self.lines, tuple) or not 1 <= len(self.lines) <= 3:
            raise ValueError("render copy must contain between one and three lines")
        for line in self.lines:
            if not isinstance(line, str) or not line or line != line.strip():
                raise ValueError("render copy lines must be non-empty trimmed strings")
            if len(line) > 80 or not line.isprintable():
                raise ValueError("render copy lines must be printable and at most 80 characters")


def _check_assets(assets: Sequence[MediaAsset]) -> list[Path]:
    if not assets:
        raise ValueError("at least one validated asset is required")
    if len(assets) > MAX_FILES:
        raise ValueError(f"no more than {MAX_FILES} assets are allowed")
    paths = []
    total_size = 0
    for asset in assets:
        path = asset.path.resolve(strict=True)
        if path != asset.path or not path.is_file():
            raise ValueError(f"asset path is not canonical: {asset.path}")
        suffix = path.suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise ValueError(f"asset extension is not approved: {suffix}")
        if asset.codec != _CODECS_BY_EXTENSION[suffix]:
            raise ValueError(f"asset codec does not match extension: {path.name}")
        if not MIN_WIDTH <= asset.width <= MAX_WIDTH:
            raise ValueError(f"asset width is outside the approved range: {path.name}")
        if not MIN_HEIGHT <= asset.height <= MAX_HEIGHT:
            raise ValueError(f"asset height is outside the approved range: {path.name}")
        if asset.width * asset.height > MAX_PIXELS:
            raise ValueError(f"asset has too many pixels: {path.name}")
        if not 0 <= asset.size_bytes <= MAX_FILE_SIZE:
            raise ValueError(f"asset size is outside the approved limit: {path.name}")
        if path.stat().st_size != asset.size_bytes:
            raise ValueError(f"asset size changed after validation: {path.name}")
        if len(asset.sha256) != 64 or any(
            character not in string.hexdigits for character in asset.sha256
        ):
            raise ValueError(f"asset SHA-256 is invalid: {path.name}")
        total_size += asset.size_bytes
        paths.append(path)
    if total_size > MAX_TOTAL_SIZE:
        raise ValueError("assets exceed the approved aggregate size")
    return paths


def _fixed_font(font_file: Path | None) -> Path:
    if font_file is None:
        raise ValueError("a fixed font file is required")
    try:
        font = Path(font_file).resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"fixed font file does not exist: {font_file}") from exc
    if not font.is_file():
        raise ValueError(f"font is not a regular file: {font_file}")
    return font


def _font_identity(font: Path) -> dict[str, object]:
    return {
        "path": str(font),
        "sha256": sha256_file(font),
        "size_bytes": font.stat().st_size,
    }


def _escape_filter_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for character in ("'", ":", ",", ";", "[", "]"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _filter_graph(asset_count: int, lines: Sequence[str], font: Path) -> str:
    filters = []
    for index in range(asset_count):
        filters.append(
            f"[{index}:v]"
            "scale=720:1280:force_original_aspect_ratio=increase,"
            "crop=720:1280,"
            "zoompan=z='min(zoom+0.0005,1.04)':"
            "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d=1:s=720x1280:fps={_FRAME_RATE},"
            f"trim=duration={_CLIP_DURATION:.1f},"
            "setpts=PTS-STARTPTS,setsar=1,format=yuv420p"
            f"[v{index}]"
        )
    current = "v0"
    for index in range(1, asset_count):
        next_label = f"blend{index}"
        offset = index * (_CLIP_DURATION - _TRANSITION_DURATION)
        filters.append(
            f"[{current}][v{index}]xfade=transition=fade:"
            f"duration={_TRANSITION_DURATION:.1f}:offset={offset:.1f}"
            f"[{next_label}]"
        )
        current = next_label
    total = asset_count * _CLIP_DURATION - (asset_count - 1) * _TRANSITION_DURATION
    segment = total / len(lines)
    font_option = f"fontfile='{_escape_filter_value(str(font))}':"
    for index, line in enumerate(lines):
        start = index * segment
        end = min(total, (index + 1) * segment)
        font_size = (
            _LONG_COPY_FONT_SIZE
            if len(line) > _LONG_COPY_THRESHOLD
            else _DEFAULT_FONT_SIZE
        )
        next_label = "outv" if index == len(lines) - 1 else f"title{index}"
        filters.append(
            f"[{current}]drawtext={font_option}"
            f"text='{_escape_filter_value(line)}':expansion=none:fontcolor=white:"
            f"fontsize={font_size}:"
            "x=(w-text_w)/2:y=h*0.74:box=1:boxcolor=black@0.45:boxborderw=18:"
            f"enable='between(t,{start:.2f},{end:.2f})'[{next_label}]"
        )
        current = next_label
    return ";".join(filters)


def build_ffmpeg_command(
    assets: Sequence[MediaAsset],
    output: Path,
    plan: RenderPlan,
    *,
    ffmpeg_bin: str = "ffmpeg",
    font_file: Path | None = None,
) -> list[str]:
    """Build an FFmpeg command for paths already confined by the caller."""
    asset_paths = _check_assets(assets)
    font = _fixed_font(font_file)
    output_path = Path(output).resolve(strict=False)
    if output_path.suffix.lower() != ".mp4":
        raise ValueError("render output must use the .mp4 extension")
    if not output_path.parent.is_dir():
        raise ValueError(f"render output directory does not exist: {output_path.parent}")
    command = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin", "-n"]
    for path in asset_paths:
        command.extend(
            [
                "-loop",
                "1",
                "-framerate",
                str(_FRAME_RATE),
                "-t",
                f"{_CLIP_DURATION:.1f}",
                "-i",
                str(path),
            ]
        )
    command.extend(
        [
            "-filter_complex_threads",
            "1",
            "-filter_complex",
            _filter_graph(len(asset_paths), plan.lines, font),
            "-map",
            "[outv]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(_FRAME_RATE),
            "-s",
            "720x1280",
            "-movflags",
            "+faststart",
            "-map_metadata",
            "-1",
            "-threads",
            "1",
            str(output_path),
        ]
    )
    return command


def _ffprobe_for(ffmpeg_bin: str) -> str:
    path = Path(ffmpeg_bin)
    if path.is_absolute() or path.parent != Path("."):
        return str(path.with_name("ffprobe"))
    return "ffprobe"


def _probe_output(
    path: Path,
    ffprobe_bin: str,
    expected_duration: float,
) -> dict[str, object]:
    completed = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,"
            "avg_frame_rate:format=duration",
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
        streams = document["streams"]
        video = [stream for stream in streams if stream.get("codec_type") == "video"]
        audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
        if len(video) != 1:
            raise ValueError("rendered output must contain exactly one video stream")
        if audio:
            raise ValueError("rendered output must not contain audio")
        stream = video[0]
        metadata = {
            "video_streams": 1,
            "audio_streams": 0,
            "codec": stream["codec_name"],
            "width": int(stream["width"]),
            "height": int(stream["height"]),
            "pixel_format": stream["pix_fmt"],
            "frame_rate": stream.get("avg_frame_rate") or stream["r_frame_rate"],
            "duration_seconds": float(document["format"]["duration"]),
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("rendered output"):
            raise
        raise ValueError("ffprobe returned invalid rendered-output metadata") from exc
    required = (metadata["codec"], metadata["width"], metadata["height"])
    if required != ("h264", 720, 1280):
        raise ValueError(f"rendered output has unexpected video properties: {required}")
    if metadata["pixel_format"] != "yuv420p" or metadata["frame_rate"] != "30/1":
        raise ValueError("rendered output has unexpected pixel format or frame rate")
    if abs(metadata["duration_seconds"] - expected_duration) > 0.15:
        raise ValueError("rendered output duration does not match the deterministic timeline")
    return metadata


def render_video(
    assets: Sequence[MediaAsset],
    output: Path,
    plan: RenderPlan,
    *,
    ffmpeg_bin: str = "ffmpeg",
    font_file: Path | None = None,
) -> dict[str, object]:
    """Render atomically to a destination path already confined by the caller."""
    output_path = Path(output).resolve(strict=False)
    if output_path.exists():
        raise FileExistsError(f"render output already exists: {output_path}")
    font = _fixed_font(font_file)
    asset_paths = _check_assets(assets)
    for asset, path in zip(assets, asset_paths, strict=True):
        if sha256_file(path) != asset.sha256:
            raise ValueError(f"asset changed after validation: {path.name}")
    font_identity = _font_identity(font)
    expected_duration = (
        len(assets) * _CLIP_DURATION
        - (len(assets) - 1) * _TRANSITION_DURATION
    )
    with tempfile.TemporaryDirectory(
        dir=output_path.parent,
        prefix=".offline-render-",
    ) as temporary_directory:
        temporary_output = Path(temporary_directory) / "render.mp4"
        command = build_ffmpeg_command(
            assets,
            temporary_output,
            plan,
            ffmpeg_bin=ffmpeg_bin,
            font_file=font,
        )
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=_RENDER_TIMEOUT,
        )
        if not temporary_output.is_file() or temporary_output.stat().st_size == 0:
            raise RuntimeError("ffmpeg did not produce a non-empty output")
        metadata = _probe_output(
            temporary_output,
            _ffprobe_for(ffmpeg_bin),
            expected_duration,
        )
        for asset, path in zip(assets, asset_paths, strict=True):
            if sha256_file(path) != asset.sha256:
                raise ValueError(f"asset changed during rendering: {path.name}")
        if _font_identity(font) != font_identity:
            raise ValueError("font identity changed during rendering")
        metadata.update(
            {
                "path": str(output_path),
                "sha256": sha256_file(temporary_output),
                "size_bytes": temporary_output.stat().st_size,
            }
        )
        result = {
            "slug": plan.slug,
            "copy": list(plan.lines),
            "sources": [asset.sha256 for asset in assets],
            "operations": {
                "scale": "720x1280:force_original_aspect_ratio=increase",
                "crop": "720x1280",
                "frame_rate": _FRAME_RATE,
                "clip_duration_seconds": _CLIP_DURATION,
                "transition": "xfade:fade",
                "transition_duration_seconds": _TRANSITION_DURATION,
                "motion": "restrained zoompan",
                "text": list(plan.lines),
                "color": "source-preserving",
                "audio": "disabled",
                "font": font_identity,
            },
            "output": metadata,
        }
        publish_file_no_replace(temporary_output, output_path)
    return result
