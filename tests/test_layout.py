"""Tests that the project layout matches what Kdenlive itself writes.

The reference is the ``.kdenlive`` files in ``tests/fixtures``, which are
Kdenlive's own test dataset, plus the upstream source:

* clip effects live inside the ``<entry>`` (not on the producer),
* track effects live inside the track's ``<tractor>``,
* master/sequence effects live inside the sequence's ``<tractor>``,
* the sequence tractor's element id is its uuid,
* producers are defined before the elements that reference them.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from kdenlive_mcp import server
from kdenlive_mcp.mltxml import MltProject


def _parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    return {child: parent for parent in root.iter() for child in parent}


def test_fixture_layout_matches_our_conventions(fixtures: Path):
    """The upstream test data must obey the same rules we follow."""
    parents = {}
    for path in fixtures.glob("*.kdenlive"):
        root = ET.parse(path).getroot()
        parents[path.name] = _parent_map(root)
        # Every clip effect is inside an <entry>.
        for element in root.iter("filter"):
            service = element.findtext("property[@name='mlt_service']")
            if service in ("volume", "panner", "audiolevel"):
                continue  # internally added audio mixer effects
            parent = parents[path.name].get(element)
            assert parent is not None
            assert parent.tag in ("entry", "tractor"), (
                f"{path.name}: user effect {service} sits inside <{parent.tag}>, "
                "expected <entry> or <tractor>"
            )


def test_clip_effect_goes_inside_the_entry(project: str, sample_media: dict[str, Path]):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    server.kdenlive_add_effect(project, effect="avfilter.negate", scope="clip", clip=1, track=1)

    root = ET.parse(project).getroot()
    entries = [e for e in root.iter("entry") if e.findall("filter")]
    assert entries, "the clip effect was not written into an <entry>"
    for entry in entries:
        service = entry.find("filter/property[@name='mlt_service']").text
        assert service == "avfilter.negate"


def test_track_effect_goes_inside_the_track_tractor(project: str):
    server.kdenlive_add_effect(project, effect="avfilter.negate", scope="track", track=1)
    root = ET.parse(project).getroot()
    parents = _parent_map(root)
    found = False
    for element in root.iter("filter"):
        parent = parents.get(element)
        if parent is not None and parent.tag == "tractor":
            found = True
    assert found, "the track effect was not written into the track tractor"


def test_sequence_effect_goes_inside_the_sequence_tractor(project: str):
    server.kdenlive_add_effect(project, effect="avfilter.negate", scope="sequence")
    root = ET.parse(project).getroot()
    parents = _parent_map(root)
    sequence_tractors = {
        t for t in root.iter("tractor")
        if t.find("property[@name='kdenlive:uuid']") is not None
    }
    hosts = {
        parents.get(element)
        for element in root.iter("filter")
        if element.findtext("property[@name='mlt_service']") == "avfilter.negate"
    }
    assert hosts & sequence_tractors, "the sequence effect was not written into the sequence tractor"


def test_sequence_tractor_id_is_its_uuid(project: str):
    project_model = MltProject.load(project)
    sequence = project_model.active_sequence()
    assert sequence is not None
    assert sequence.tractor.get("id") == sequence.uuid


def test_project_tractor_is_the_last_element(project: str):
    root = ET.parse(project).getroot()
    assert root[-1].tag == "tractor"
    assert root[-1].findtext("property[@name='kdenlive:projectTractor']") == "1"


def test_producers_precede_the_playlists_that_use_them(project: str, sample_media: dict[str, Path]):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    root = ET.parse(project).getroot()
    order = {child.get("id"): index for index, child in enumerate(root) if child.get("id")}
    for playlist in root.iter("playlist"):
        for entry in playlist.findall("entry"):
            producer = entry.get("producer")
            if producer in order:
                assert order[producer] < order[playlist.get("id")], (
                    f"{producer} is defined after the playlist that references it"
                )


def test_split_clip_copies_the_effects(project: str, sample_media: dict[str, Path]):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    server.kdenlive_add_effect(project, effect="avfilter.negate", scope="clip", clip=1, track=1)
    server.kdenlive_split_clip(project, clip=1, track=1, position=10)

    clips = server.kdenlive_clips(project)["tracks"][0]["clips"]
    assert len(clips) == 2
    for clip in clips:
        assert "avfilter.negate" in clip["effects"]
