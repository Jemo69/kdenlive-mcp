"""Tests for the MLT XML project model."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from kdenlive_mcp.errors import ProjectError
from kdenlive_mcp.mltxml import MltProject, Profile
from kdenlive_mcp.timecode import format_clock, format_frames, parse_clock, parse_frames

# --------------------------------------------------------------------------
# timecode
# --------------------------------------------------------------------------


def test_clock_roundtrip():
    assert parse_clock("00:00:01.400", 25) == 35
    assert parse_clock("00:00:00.160", 25) == 4
    assert format_clock(35, 25) == "00:00:01.400"
    assert format_clock(4, 25) == "00:00:00.160"


def test_frames_roundtrip():
    assert parse_frames("00:00:01:11", 25) == 36
    assert format_frames(36, 25) == "00:00:01:11"


def test_clock_and_frames_agree_at_25fps():
    # Kdenlive writes '00:00:04.960' for 124 frames and '00:00:04:24' for 125.
    assert parse_clock("00:00:04.960", 25) == 124
    assert parse_frames("00:00:04:24", 25) == 124


# --------------------------------------------------------------------------
# reading real Kdenlive projects
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "av.kdenlive",
        "clip-ids.kdenlive",
        "missing-proxy.kdenlive",
        "test-keyframes.kdenlive",
        "test-missing.kdenlive",
        "test-mix.kdenlive",
        "test-nesting-effects.kdenlive",
        "test-nesting.kdenlive",
    ],
)
def test_loads_real_kdenlive_projects(fixtures: Path, name: str):
    project = MltProject.load(fixtures / name)
    assert project.profile.width > 0
    assert project.fps > 0
    # Every real project has at least one sequence.
    assert project.sequences()


def test_reads_nested_sequences(fixtures: Path):
    project = MltProject.load(fixtures / "test-nesting.kdenlive")
    sequences = project.sequences()
    assert len(sequences) == 3
    names = sorted(s.name for s in sequences)
    assert names == ["Sequence 1", "Sequence 2", "Sequence 3"]
    # The project tractor points at the first sequence.
    assert project.active_sequence() is not None


def test_reads_tracks_and_clips(fixtures: Path):
    project = MltProject.load(fixtures / "clip-ids.kdenlive")
    sequence = project.active_sequence()
    assert sequence is not None
    assert len(sequence.tracks) == 4
    # Kdenlive marks audio tracks with kdenlive:audio_track=1.
    assert [t.kind for t in sequence.tracks] == ["audio", "audio", "video", "video"]
    clips = project.track_clips(sequence.tracks[0])
    assert len(clips) == 4
    # The fixture starts with a blank, so the first clip starts after it.
    blanks = [c for c in sequence.tracks[0].playlist if c.tag == "blank"]
    assert blanks
    assert clips[0].position == project._blank_length(blanks[0], project.fps)
    # Clip ids must be unique and present.
    assert all(clip.clip_id for clip in clips)
    assert len({clip.clip_id for clip in clips}) == len(clips)


def test_reads_bin_clips(fixtures: Path):
    project = MltProject.load(fixtures / "clip-ids.kdenlive")
    clips = project.bin_clips()
    assert clips
    assert all(clip.resource for clip in clips)


def test_reads_effects(fixtures: Path):
    project = MltProject.load(fixtures / "test-nesting-effects.kdenlive")
    sequence = project.active_sequence()
    assert sequence is not None
    # The sequence tractor carries the master effects.
    services = [
        child.findtext("property[@name='mlt_service']")
        for child in sequence.tractor.findall("filter")
    ]
    assert services


def test_rejects_non_mlt_xml(tmp_path: Path):
    bad = tmp_path / "bad.kdenlive"
    bad.write_text("<html><body>nope</body></html>")
    with pytest.raises(ProjectError):
        MltProject.load(bad)


# --------------------------------------------------------------------------
# creating projects
# --------------------------------------------------------------------------


def test_create_project(tmp_path: Path):
    target = tmp_path / "new.kdenlive"
    MltProject.create(target, Profile(frame_rate_num=30, frame_rate_den=1))
    assert target.exists()

    # It must parse back cleanly.
    reloaded = MltProject.load(target)
    assert reloaded.fps == 30
    assert len(reloaded.sequences()) == 1
    assert reloaded.active_sequence() is not None
    assert len(reloaded.active_sequence().tracks) == 3

    # The project tractor has to be the last element of the document.
    root = ET.parse(target).getroot()
    assert root[-1].tag == "tractor"
    assert root[-1].findtext("property[@name='kdenlive:projectTractor']") == "1"


def test_created_project_has_main_bin(tmp_path: Path):
    target = tmp_path / "new.kdenlive"
    MltProject.create(target)
    project = MltProject.load(target)
    assert project.main_bin is not None
    version = project.main_bin.findtext("property[@name='kdenlive:docproperties.version']")
    assert version == "1.1"


def test_producers_are_defined_before_references(tmp_path: Path):
    """MLT resolves references in document order, so order matters."""
    target = tmp_path / "ordered.kdenlive"
    project = MltProject.create(target)
    chain = project.create_media_producer(
        "/tmp/does-not-exist.mp4", length_frames=100, before=project.active_sequence().tracks[0].playlist
    )
    project.save()

    root = ET.parse(target).getroot()
    positions = {child.get("id"): index for index, child in enumerate(root)}
    assert positions[chain.get("id")] < positions[
        project.active_sequence().tracks[0].playlist.get("id")
    ]


# --------------------------------------------------------------------------
# playlist editing
# --------------------------------------------------------------------------


def _new_project(tmp_path: Path) -> MltProject:
    return MltProject.create(tmp_path / "edit.kdenlive")


def test_insert_entry_at_position(tmp_path: Path):
    project = _new_project(tmp_path)
    track = project.active_sequence().tracks[0]
    entry = project.make_entry("chainX", in_frames=0, out_frames=24)
    project.playlist_insert(track.playlist, entry, 0)
    assert project.playlist_length(track.playlist) == 25


def test_insert_entry_splits_a_blank(tmp_path: Path):
    project = _new_project(tmp_path)
    track = project.active_sequence().tracks[0]
    first = project.make_entry("chainA", in_frames=0, out_frames=24)
    project.playlist_insert(track.playlist, first, 0)
    second = project.make_entry("chainB", in_frames=0, out_frames=24)
    project.playlist_insert(track.playlist, second, 50)

    clips = project.track_clips(track)
    assert len(clips) == 2
    assert clips[0].position == 0
    assert clips[1].position == 50
    # There must be a blank between them.
    assert project.playlist_length(track.playlist) == 75


def test_remove_entry_leaves_a_gap(tmp_path: Path):
    project = _new_project(tmp_path)
    track = project.active_sequence().tracks[0]
    entry = project.make_entry("chainA", in_frames=0, out_frames=24)
    project.playlist_insert(track.playlist, entry, 0)
    length = project.playlist_remove_entry(entry)
    assert length == 25
    assert project.playlist_length(track.playlist) == 25
    assert project.track_clips(track) == []


def test_merge_blanks(tmp_path: Path):
    project = _new_project(tmp_path)
    track = project.active_sequence().tracks[0]
    entry = project.make_entry("chainA", in_frames=0, out_frames=24)
    project.playlist_insert(track.playlist, entry, 0)
    project.playlist_remove_entry(entry)
    project.merge_blanks(track.playlist)
    blanks = [c for c in track.playlist if c.tag == "blank"]
    assert len(blanks) == 1


def test_refresh_timings_updates_duration(tmp_path: Path):
    project = _new_project(tmp_path)
    sequence = project.active_sequence()
    entry = project.make_entry("chainA", in_frames=0, out_frames=99)
    project.playlist_insert(sequence.tracks[0].playlist, entry, 0)
    project.refresh_timings()

    assert sequence.duration_frames == 100
    assert sequence.tractor.get("out") == "00:00:03.960"
    duration = sequence.tractor.findtext("property[@name='kdenlive:duration']")
    assert duration == format_frames(100, project.fps)


def test_save_is_atomic_and_backs_up(tmp_path: Path):
    target = tmp_path / "backup.kdenlive"
    MltProject.create(target)
    project = MltProject.load(target)
    project.save()
    assert target.with_suffix(".kdenlive.bak").exists()
