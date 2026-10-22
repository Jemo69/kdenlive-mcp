"""End to end tests of the MCP tool surface."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from kdenlive_mcp import server
from kdenlive_mcp.errors import MediaError, ToolError

# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


def test_status_reports_the_environment():
    result = server.kdenlive_status()
    assert result["ok"] is True
    assert "melt" in result["tools"]
    assert "bridge" in result
    assert result["platform"]["system"]


def test_effects_catalog_lists_real_services():
    result = server.kdenlive_effects_catalog(query="gain")
    assert result["count"] > 0
    services = [item["mlt_service"] for item in result["effects"]]
    assert any("gain" in service for service in services)


def test_effects_catalog_includes_qtblend():
    result = server.kdenlive_effects_catalog(query="qtblend")
    services = [item["mlt_service"] for item in result["effects"]]
    assert "qtblend" in services


def test_effects_catalog_can_filter_by_kind():
    result = server.kdenlive_effects_catalog(query="luma", kind="transition")
    assert all(item["kind"] == "transition" for item in result["effects"])


def test_unknown_effect_is_rejected_with_a_suggestion():
    with pytest.raises(ToolError) as exc:
        server.kdenlive_add_effect("/tmp/does-not-exist.kdenlive", "totallyMadeUp")
    assert "totallyMadeUp" in str(exc.value)


# --------------------------------------------------------------------------
# project lifecycle
# --------------------------------------------------------------------------


def test_create_project_writes_a_valid_file(project: str):
    assert Path(project).exists()
    root = ET.parse(project).getroot()
    assert root.tag == "mlt"
    assert root.get("producer") == "main_bin"


def test_create_project_refuses_to_overwrite(tmp_path: Path):
    target = tmp_path / "exists.kdenlive"
    server.kdenlive_create_project(str(target))
    with pytest.raises(ToolError):
        server.kdenlive_create_project(str(target))


def test_create_project_rejects_unknown_profile(tmp_path: Path):
    with pytest.raises(ToolError):
        server.kdenlive_create_project(str(tmp_path / "x.kdenlive"), "notAProfile")


def test_project_info_describes_the_project(project: str):
    result = server.kdenlive_project_info(project)
    assert result["profile"]["fps"] == 25
    assert len(result["sequences"]) == 1
    assert len(result["active_sequence"]["tracks"]) == 3


def test_validate_project_accepts_a_fresh_project(project: str):
    result = server.kdenlive_validate_project(project)
    assert result["ok"] is True, result["issues"]
    assert result["error_count"] == 0


def test_validate_project_reports_a_dangling_reference(project: str):
    root = ET.parse(project).getroot()
    playlist = root.find("playlist")
    entry = ET.SubElement(playlist, "entry")
    entry.set("producer", "chainDoesNotExist")
    entry.set("in", "00:00:00.000")
    entry.set("out", "00:00:01.000")
    ET.ElementTree(root).write(project, encoding="utf-8", xml_declaration=True)

    result = server.kdenlive_validate_project(project)
    assert result["ok"] is False
    codes = {issue["code"] for issue in result["issues"]}
    assert "dangling_entry" in codes


# --------------------------------------------------------------------------
# tracks
# --------------------------------------------------------------------------


def test_add_and_remove_a_track(project: str):
    before = server.kdenlive_tracks(project)["count"]
    server.kdenlive_add_track(project, kind="audio", name="Extra Audio")
    after = server.kdenlive_tracks(project)
    assert after["count"] == before + 1
    assert any(track["name"] == "Extra Audio" for track in after["tracks"])

    server.kdenlive_remove_track(project, track=after["count"])
    assert server.kdenlive_tracks(project)["count"] == before


def test_add_track_rejects_a_bad_kind(project: str):
    with pytest.raises(ToolError):
        server.kdenlive_add_track(project, kind="subtitles")


def test_set_track_property(project: str):
    server.kdenlive_set_track_property(project, track=1, properties={"kdenlive:track_name": "Main"})
    tracks = server.kdenlive_tracks(project)["tracks"]
    assert tracks[0]["name"] == "Main"


# --------------------------------------------------------------------------
# clips
# --------------------------------------------------------------------------


def test_add_clip_places_media_on_the_timeline(project: str, sample_media: dict[str, Path]):
    result = server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    assert result["length_frames"] > 0

    clips = server.kdenlive_clips(project)["tracks"][0]["clips"]
    assert len(clips) == 1
    assert clips[0]["resource"].endswith("red.mp4")


def test_add_clip_at_an_offset_creates_a_gap(project: str, sample_media: dict[str, Path]):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=25)
    clips = server.kdenlive_clips(project)["tracks"][0]["clips"]
    assert clips[0]["position_frame"] == 25


def test_add_clip_on_two_tracks(project: str, sample_media: dict[str, Path]):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    server.kdenlive_add_clip(project, str(sample_media["blue.mp4"]), track=2, position=0)
    tracks = server.kdenlive_clips(project)["tracks"]
    assert len(tracks[0]["clips"]) == 1
    assert len(tracks[1]["clips"]) == 1


def test_add_missing_media_raises(project: str, tmp_path: Path):
    with pytest.raises(MediaError):
        server.kdenlive_add_clip(project, str(tmp_path / "nope.mp4"), track=1)


def test_remove_clip(project: str, sample_media: dict[str, Path]):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    clip_id = server.kdenlive_clips(project)["tracks"][0]["clips"][0]["clip_id"]
    server.kdenlive_remove_clip(project, clip=clip_id, track=1)
    assert server.kdenlive_clips(project)["tracks"][0]["clips"] == []


def test_move_clip_to_another_track(project: str, sample_media: dict[str, Path]):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    clip_id = server.kdenlive_clips(project)["tracks"][0]["clips"][0]["clip_id"]
    server.kdenlive_move_clip(project, clip=clip_id, track=1, new_track=2, new_position=10)

    tracks = server.kdenlive_clips(project)["tracks"]
    assert tracks[0]["clips"] == []
    assert len(tracks[1]["clips"]) == 1
    assert tracks[1]["clips"][0]["position_frame"] == 10


def test_trim_clip(project: str, sample_media: dict[str, Path]):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    clip_id = server.kdenlive_clips(project)["tracks"][0]["clips"][0]["clip_id"]
    result = server.kdenlive_trim_clip(project, clip=clip_id, track=1, new_in=0, new_out=24)
    assert result["length_frames"] == 25


def test_trim_clip_beyond_the_source_is_rejected(project: str, sample_media: dict[str, Path]):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    clip_id = server.kdenlive_clips(project)["tracks"][0]["clips"][0]["clip_id"]
    with pytest.raises(ToolError):
        server.kdenlive_trim_clip(project, clip=clip_id, track=1, new_in=0, new_out=99999)


def test_split_clip(project: str, sample_media: dict[str, Path]):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    clip_id = server.kdenlive_clips(project)["tracks"][0]["clips"][0]["clip_id"]
    result = server.kdenlive_split_clip(project, clip=clip_id, track=1, position=10)
    assert result["first_length"] == 10
    clips = server.kdenlive_clips(project)["tracks"][0]["clips"]
    assert len(clips) == 2
    assert clips[1]["position_frame"] == 10


def test_set_clip_property(project: str, sample_media: dict[str, Path]):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    clip_id = server.kdenlive_clips(project)["tracks"][0]["clips"][0]["clip_id"]
    server.kdenlive_set_clip_property(
        project, clip=clip_id, track=1, properties={"kdenlive:clipname": "Hero shot"}
    )
    clips = server.kdenlive_clips(project)["tracks"][0]["clips"]
    assert clips[0]["name"] == "Hero shot"


# --------------------------------------------------------------------------
# effects and transitions
# --------------------------------------------------------------------------


def test_add_effect_to_a_clip(project: str, sample_media: dict[str, Path]):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    clip_id = server.kdenlive_clips(project)["tracks"][0]["clips"][0]["clip_id"]
    result = server.kdenlive_add_effect(
        project, effect="avfilter.negate", scope="clip", clip=clip_id, track=1,
        parameters={"av.negate_alpha": "1"},
    )
    assert result["mlt_service"] == "avfilter.negate"

    listed = server.kdenlive_effects_on(project, scope="clip", clip=clip_id, track=1)
    assert "avfilter.negate" in [f["mlt_service"] for f in listed["filters"]]


def test_add_effect_rejects_unknown_parameters(project: str, sample_media: dict[str, Path]):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    clip_id = server.kdenlive_clips(project)["tracks"][0]["clips"][0]["clip_id"]
    with pytest.raises(ToolError) as exc:
        server.kdenlive_add_effect(
            project, effect="avfilter.negate", scope="clip", clip=clip_id, track=1,
            parameters={"not_a_parameter": "1"},
        )
    assert "not_a_parameter" in str(exc.value)


def test_add_effect_to_a_track(project: str):
    result = server.kdenlive_add_effect(project, effect="qtblend", scope="track", track=1)
    assert result["scope"] == "track"
    listed = server.kdenlive_effects_on(project, scope="track", track=1)
    assert "qtblend" in [f["mlt_service"] for f in listed["filters"]]


def test_add_effect_to_a_sequence(project: str):
    server.kdenlive_add_effect(project, effect="avfilter.negate", scope="sequence")
    listed = server.kdenlive_effects_on(project, scope="sequence")
    assert "avfilter.negate" in [f["mlt_service"] for f in listed["filters"]]


def test_remove_effect(project: str):
    added = server.kdenlive_add_effect(project, effect="qtblend", scope="sequence")
    server.kdenlive_remove_effect(project, filter_id=added["filter_id"], scope="sequence")
    listed = server.kdenlive_effects_on(project, scope="sequence")
    assert listed["filters"] == []


def test_add_transition(project: str):
    result = server.kdenlive_add_transition(
        project, transition="luma", track_a=1, track_b=2, position=25, duration=25
    )
    assert result["mlt_service"] == "luma"
    listed = server.kdenlive_effects_on(project, scope="sequence")
    assert len(listed["transitions"]) == 1


def test_remove_transition(project: str):
    added = server.kdenlive_add_transition(project, transition="luma")
    server.kdenlive_remove_transition(project, transition_id=added["transition_id"])
    listed = server.kdenlive_effects_on(project, scope="sequence")
    assert listed["transitions"] == []


# --------------------------------------------------------------------------
# bin
# --------------------------------------------------------------------------


def test_clips_are_registered_in_the_bin(project: str, sample_media: dict[str, Path]):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    clips = server.kdenlive_bin(project)["clips"]
    assert clips
    assert clips[0]["resource"].endswith("red.mp4")


def test_a_bin_clip_can_be_placed_on_the_timeline(project: str, sample_media: dict[str, Path]):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    bin_clip = server.kdenlive_bin(project)["clips"][0]
    server.kdenlive_add_clip(project, track=2, position=0, bin_clip=bin_clip["clip_id"])
    tracks = server.kdenlive_clips(project)["tracks"]
    assert len(tracks[1]["clips"]) == 1


# --------------------------------------------------------------------------
# media
# --------------------------------------------------------------------------


def test_probe_media(sample_media: dict[str, Path]):
    result = server.kdenlive_probe_media(str(sample_media["red.mp4"]))
    assert result["has_video"] is True
    assert result["has_audio"] is True
    assert result["duration_seconds"] > 0
    assert result["width"] == 320


def test_probe_missing_media_raises(tmp_path: Path):
    with pytest.raises(MediaError):
        server.kdenlive_probe_media(str(tmp_path / "nope.mp4"))


# --------------------------------------------------------------------------
# rendering (skipped without melt)
# --------------------------------------------------------------------------


def test_render_without_melt_fails_clearly(project: str, monkeypatch):
    from kdenlive_mcp.binaries import ToolInfo

    monkeypatch.setattr(
        "kdenlive_mcp.render.find_melt",
        lambda: ToolInfo(name="melt", path=None, available=False, detail="not found"),
    )
    with pytest.raises(Exception) as exc:
        server.kdenlive_render(project, str(Path(project).with_suffix(".mp4")))
    assert "melt" in str(exc.value)


def test_generated_ids_are_unique(project: str, sample_media: dict[str, Path]):
    """Ids must be unique across the whole document, not just at the root."""
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    clip_id = server.kdenlive_clips(project)["tracks"][0]["clips"][0]["clip_id"]
    server.kdenlive_add_effect(project, effect="avfilter.negate", scope="sequence")
    server.kdenlive_add_effect(project, effect="qtblend", scope="track", track=1)
    server.kdenlive_add_transition(project, transition="luma")

    root = ET.parse(project).getroot()
    ids = [element.get("id") for element in root.iter() if element.get("id")]
    assert len(ids) == len(set(ids)), "duplicate ids in the document"
    assert clip_id
