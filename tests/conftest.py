"""Shared test fixtures."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def sample_media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Generate two short clips with ffmpeg, skipping the test if absent."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is not installed")
    folder = tmp_path_factory.mktemp("media")
    clips: dict[str, Path] = {}
    for name, source, freq, duration in (
        ("red.mp4", "testsrc", 440, 2),
        ("blue.mp4", "testsrc2", 880, 3),
    ):
        target = folder / name
        result = subprocess.run(
            [
                ffmpeg, "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", f"{source}=size=320x180:rate=25:duration={duration}",
                "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                str(target),
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0 or not target.exists():
            pytest.skip("ffmpeg could not generate the test media")
        clips[name] = target
    return clips


@pytest.fixture
def project(tmp_path: Path) -> str:
    """A fresh, empty project on disk."""
    from kdenlive_mcp import server

    target = tmp_path / "work.kdenlive"
    server.kdenlive_create_project(str(target), "1080p25", "Sequence 1")
    return str(target)
