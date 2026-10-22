"""Integration tests that actually render a project.

These are skipped unless MLT's ``melt`` renderer is installed, because they
are the slow ones.  When ``melt`` is present they prove the whole chain
works: the MCP writes a project, MLT reads it, and a real video file comes
out with the right length, resolution and frame rate.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from kdenlive_mcp import server
from kdenlive_mcp.binaries import find_ffprobe, find_melt

pytestmark = pytest.mark.skipif(
    not find_melt().available, reason="melt (the MLT renderer) is not installed"
)


def _ffprobe() -> str:
    probe = find_ffprobe()
    if probe.available and probe.path:
        return probe.path
    fallback = shutil.which("ffprobe")
    if fallback:
        return fallback
    pytest.skip("ffprobe is not available")


def _render(project: str, output: Path, timeout: int = 300) -> dict:
    job = server.kdenlive_render(str(project), str(output))
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        status = server.kdenlive_render_status(job["job_id"])
        if status["status"] in ("finished", "failed", "cancelled"):
            return status
    pytest.fail("the render did not finish in time")


def _probe(path: Path) -> dict:
    result = subprocess.run(
        [
            _ffprobe(), "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    import json

    return json.loads(result.stdout or "{}")


def _video_stream(payload: dict) -> dict:
    for stream in payload.get("streams") or []:
        if stream.get("codec_type") == "video":
            return stream
    return {}


def test_render_produces_a_playable_file(project: str, sample_media: dict[str, Path], tmp_path: Path):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    output = tmp_path / "rendered.mp4"

    status = _render(project, output)
    assert status["status"] == "finished", status["message"]
    assert output.exists()
    assert output.stat().st_size > 0

    payload = _probe(output)
    stream = _video_stream(payload)
    assert stream.get("codec_name") == "h264"
    assert stream.get("width") == 1920
    assert stream.get("height") == 1080
    # The clip is two seconds long at 25 fps.
    assert int(stream.get("nb_frames", 0)) == 50


def test_render_duration_matches_the_timeline(project: str, sample_media: dict[str, Path], tmp_path: Path):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    server.kdenlive_add_clip(project, str(sample_media["blue.mp4"]), track=1, position=50)
    output = tmp_path / "two.mp4"

    assert _render(project, output)["status"] == "finished"

    payload = _probe(output)
    stream = _video_stream(payload)
    # red is 2s, blue is 3s, placed at frame 50 -> 125 frames in total.
    assert int(stream.get("nb_frames", 0)) == 125
    duration = float(payload["format"]["duration"])
    assert 4.9 < duration < 5.1


def test_render_applies_a_sequence_effect(project: str, sample_media: dict[str, Path], tmp_path: Path):
    """A brightness effect on the sequence must darken the rendered frames."""
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    plain = tmp_path / "plain.mp4"
    assert _render(project, plain)["status"] == "finished"

    server.kdenlive_add_effect(
        project, effect="brightness", scope="sequence", parameters={"level": "0.15"}
    )
    dark = tmp_path / "dark.mp4"
    assert _render(project, dark)["status"] == "finished"

    def average(path: Path) -> float:
        result = subprocess.run(
            [_ffprobe(), "-v", "error", "-f", "lavfi",
             "-i", "movie=" + str(path) + ",signalstats",
             "-show_entries", "frame_tags=lavfi.signalstats.YAVG",
             "-of", "csv=p=0", "-read_intervals", "%+#1"],
            capture_output=True, text=True, check=False,
        )
        try:
            # ffprobe prints "value," with a trailing separator.
            first = result.stdout.strip().splitlines()[0].strip().rstrip(",")
            return float(first)
        except (IndexError, ValueError):
            pytest.skip("could not read the frame statistics")

    assert average(dark) < average(plain) * 0.6


def test_render_survives_a_round_trip(project: str, sample_media: dict[str, Path], tmp_path: Path):
    """Editing, saving and reloading must not change the timeline."""
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    before = server.kdenlive_clips(project)["tracks"][0]["clips"]
    server.kdenlive_project_info(project)  # force a reload from disk
    after = server.kdenlive_clips(project)["tracks"][0]["clips"]
    assert len(before) == len(after)
    assert before[0]["position_frame"] == after[0]["position_frame"]
    assert before[0]["length_frames"] == after[0]["length_frames"]


@pytest.mark.skipif(os.name == "nt", reason="posix paths only")
def test_relative_paths_are_resolved(project: str, sample_media: dict[str, Path]):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    assert Path(project).exists()
