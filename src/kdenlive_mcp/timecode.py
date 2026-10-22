"""Frame/timecode conversion helpers for the MLT project format.

Kdenlive/MLT use two different time representations and they are easy to mix up:

``HH:MM:SS.mmm``
    A *clock* value in milliseconds. Used by MLT for ``in``/``out`` on playlist
    entries and for ``length`` on blanks. ``00:00:01.400`` at 25 fps is frame 35.

``HH:MM:SS:FF``
    A *frame* value. Used by Kdenlive for ``kdenlive:duration`` on sequence
    tractors. ``00:00:01:11`` at 25 fps is frame 36.

Everything in this module is frame-based internally; conversion happens only at
the XML boundary.
"""

from __future__ import annotations

import re

Number = int | float

_CLOCK_RE = re.compile(
    r"^\s*(?P<h>\d+):(?P<m>\d{1,2}):(?P<s>\d{1,2})(?:\.(?P<ms>\d{1,3}))?\s*$"
)
_FRAMES_RE = re.compile(
    r"^\s*(?P<h>\d+):(?P<m>\d{1,2}):(?P<s>\d{1,2}):(?P<f>\d{1,3})\s*$"
)


class TimecodeError(ValueError):
    """Raised when a timecode string cannot be parsed."""


def parse_clock(value: str, fps: float) -> int:
    """Parse an MLT clock value (``HH:MM:SS.mmm``) into a frame number.

    The fractional part is milliseconds of wall clock time, so the result
    depends on the project frame rate.
    """
    match = _CLOCK_RE.match(value or "")
    if not match:
        raise TimecodeError(f"Invalid MLT clock timecode: {value!r}")
    total_ms = (
        int(match["h"]) * 3_600_000
        + int(match["m"]) * 60_000
        + int(match["s"]) * 1_000
        + int((match["ms"] or "0").ljust(3, "0"))
    )
    return int(round(total_ms * fps / 1000.0))


def format_clock(frames: int, fps: float) -> str:
    """Render a frame number as an MLT clock value (``HH:MM:SS.mmm``)."""
    if fps <= 0:
        raise TimecodeError("Frame rate must be positive")
    frames = max(0, int(frames))
    total_ms = int(round(frames * 1000.0 / fps))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def parse_frames(value: str, fps: float) -> int:
    """Parse a Kdenlive frame timecode (``HH:MM:SS:FF``) into a frame number."""
    match = _FRAMES_RE.match(value or "")
    if not match:
        raise TimecodeError(f"Invalid frame timecode: {value!r}")
    return (
        int(match["h"]) * 3600 * fps
        + int(match["m"]) * 60 * fps
        + int(match["s"]) * fps
        + int(match["f"])
    )


def format_frames(frames: int, fps: float) -> str:
    """Render a frame number as a Kdenlive frame timecode (``HH:MM:SS:FF``)."""
    if fps <= 0:
        raise TimecodeError("Frame rate must be positive")
    frames = max(0, int(frames))
    total = int(round(frames))
    fps_i = max(1, int(round(fps)))
    hours, rem = divmod(total, 3600 * fps_i)
    minutes, rem = divmod(rem, 60 * fps_i)
    seconds, ff = divmod(rem, fps_i)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{ff:02d}"


def parse_duration(value: str, fps: float) -> int:
    """Parse either a clock or a frame timecode into a frame number.

    ``kdenlive:duration`` has been written in both formats over the years, so
    this sniffs the shape instead of demanding one.
    """
    if not value:
        return 0
    value = value.strip()
    if _FRAMES_RE.match(value):
        return parse_frames(value, fps)
    if _CLOCK_RE.match(value):
        return parse_clock(value, fps)
    raise TimecodeError(f"Unrecognised timecode: {value!r}")


def to_seconds(frames: int, fps: float) -> float:
    """Convert a frame number to seconds."""
    if fps <= 0:
        raise TimecodeError("Frame rate must be positive")
    return frames / fps


def from_seconds(seconds: float, fps: float) -> int:
    """Convert seconds to the nearest frame number."""
    if fps <= 0:
        raise TimecodeError("Frame rate must be positive")
    return int(round(seconds * fps))


def human_duration(frames: int, fps: float) -> str:
    """A short human readable duration such as ``1m 23s``."""
    seconds = to_seconds(frames, fps)
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {int(sec):02d}s"
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours}h {minutes:02d}m {int(sec):02d}s"


def coerce_frames(
    value: Number | str | None,
    fps: float,
    *,
    default: int | None = None,
) -> int:
    """Interpret a user supplied position/duration.

    Accepts an integer number of frames, a float number of seconds, or any
    recognised timecode string. Returns ``default`` when the value is ``None``.
    """
    if value is None:
        if default is None:
            raise TimecodeError("A frame/timecode value is required")
        return default
    if isinstance(value, bool):
        raise TimecodeError(f"Invalid frame value: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return from_seconds(value, fps)
    text = str(value).strip()
    if not text:
        if default is None:
            raise TimecodeError("A frame/timecode value is required")
        return default
    try:
        return int(text)
    except ValueError:
        pass
    return parse_duration(text, fps)
