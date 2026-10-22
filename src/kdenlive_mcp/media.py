"""Reading real media properties with ``ffprobe``.

Kdenlive stores the duration, frame rate and stream layout of a clip inside
the project file.  When the MCP adds a clip it needs those numbers, and
``ffprobe`` is the reliable way to get them.  When ``ffprobe`` is missing the
caller can still supply a duration explicitly.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .binaries import find_ffprobe
from .errors import MediaError


@dataclass(slots=True)
class MediaInfo:
    """Everything the MCP needs to know about a media file."""

    path: str
    exists: bool = False
    duration_seconds: float = 0.0
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    has_video: bool = False
    has_audio: bool = False
    video_codec: str | None = None
    audio_codec: str | None = None
    audio_channels: int | None = None
    sample_rate: int | None = None
    format_name: str | None = None
    bit_rate: int | None = None
    rotation: int = 0
    raw: dict = field(default_factory=dict)

    def duration_frames(self, fps: float) -> int:
        """Length of the media in frames at ``fps``."""
        return max(1, int(round(self.duration_seconds * fps)))

    def describe(self, fps: float | None = None) -> dict[str, object]:
        data: dict[str, object] = {
            "path": self.path,
            "exists": self.exists,
            "duration_seconds": round(self.duration_seconds, 3),
            "has_video": self.has_video,
            "has_audio": self.has_audio,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "audio_channels": self.audio_channels,
            "sample_rate": self.sample_rate,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "format": self.format_name,
            "bit_rate": self.bit_rate,
            "rotation": self.rotation,
        }
        if fps:
            data["duration_frames"] = self.duration_frames(fps)
        return data


def _parse_fps(value: str | None) -> float | None:
    if not value:
        return None
    try:
        number = float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None
    return number if number > 0 else None


def probe(path: str | Path, *, timeout: int = 30) -> MediaInfo:
    """Probe a media file and return its properties.

    Raises :class:`MediaError` when the file does not exist or ``ffprobe`` is
    not installed.
    """
    target = Path(path).expanduser()
    if not target.exists():
        raise MediaError(f"Media file not found: {target}")
    if target.is_dir():
        raise MediaError(f"Expected a media file but got a directory: {target}")

    ffprobe = find_ffprobe()
    if not ffprobe.available or not ffprobe.path:
        raise MediaError(
            "ffprobe is not available. Install FFmpeg (ffprobe ships with it) or pass an "
            "explicit duration when adding clips."
        )

    command = [
        ffprobe.path,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(target),
    ]
    try:
        result = subprocess.run(  # noqa: S603
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"ffprobe timed out after {timeout}s on {target}") from exc
    except OSError as exc:
        raise MediaError(f"Could not run ffprobe: {exc}") from exc

    if result.returncode != 0:
        raise MediaError(f"ffprobe failed on {target}: {(result.stderr or '').strip()[:400]}")

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe returned invalid JSON for {target}") from exc

    return _from_payload(str(target), payload)


def _from_payload(path: str, payload: dict) -> MediaInfo:
    info = MediaInfo(path=path, exists=True, raw=payload)
    fmt = payload.get("format") or {}
    info.format_name = fmt.get("format_long_name") or fmt.get("format_name")
    try:
        info.duration_seconds = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        info.duration_seconds = 0.0
    try:
        info.bit_rate = int(fmt.get("bit_rate"))
    except (TypeError, ValueError):
        info.bit_rate = None

    for stream in payload.get("streams") or []:
        kind = stream.get("codec_type")
        if kind == "video" and not info.has_video:
            info.has_video = True
            info.video_codec = stream.get("codec_name")
            info.width = stream.get("width")
            info.height = stream.get("height")
            info.fps = _parse_fps(stream.get("avg_frame_rate")) or _parse_fps(
                stream.get("r_frame_rate")
            )
            tags = stream.get("tags") or {}
            rotation = tags.get("rotate")
            if rotation:
                try:
                    info.rotation = int(float(rotation))
                except (TypeError, ValueError):
                    info.rotation = 0
            if not info.duration_seconds:
                try:
                    info.duration_seconds = float(stream.get("duration") or 0.0)
                except (TypeError, ValueError):
                    pass
        elif kind == "audio" and not info.has_audio:
            info.has_audio = True
            info.audio_codec = stream.get("codec_name")
            info.audio_channels = stream.get("channels")
            info.sample_rate = stream.get("sample_rate")
            if info.sample_rate:
                try:
                    info.sample_rate = int(info.sample_rate)
                except (TypeError, ValueError):
                    info.sample_rate = None

    return info


def probe_or_none(path: str | Path) -> MediaInfo | None:
    """Probe a file, returning ``None`` instead of raising on failure."""
    try:
        return probe(path)
    except MediaError:
        return None
