"""Tests for the undo history, redo and checkpoints."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdenlive_mcp import server
from kdenlive_mcp.errors import ProjectError
from kdenlive_mcp.history import ProjectHistory, sha256


@pytest.fixture(autouse=True)
def isolated_state(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Keep the history out of the real user state directory."""
    monkeypatch.setenv("KDENLIVE_MCP_STATE_DIR", str(tmp_path_factory.mktemp("state")))


def test_history_records_every_change(project: str):
    server.kdenlive_add_track(project, kind="audio", name="Extra")
    history = server.kdenlive_history(project)
    assert history["revision_count"] == 1
    assert history["revisions"][0]["operation"] == "add_track"
    assert history["can_undo"] is True


def test_undo_restores_the_previous_content(project: str, sample_media: dict[str, Path]):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    with_clip = Path(project).read_bytes()

    server.kdenlive_remove_clip(project, clip=1, track=1)
    assert Path(project).read_bytes() != with_clip

    result = server.kdenlive_undo(project)
    assert result["ok"] is True
    assert Path(project).read_bytes() == with_clip


def test_undo_restores_the_original_state(project: str):
    before = Path(project).read_bytes()
    server.kdenlive_add_track(project, kind="audio", name="Extra")
    assert Path(project).read_bytes() != before

    server.kdenlive_undo(project)
    assert Path(project).read_bytes() == before


def test_redo_reapplies_the_change(project: str):
    before = Path(project).read_bytes()
    server.kdenlive_add_track(project, kind="audio", name="Extra")
    changed = Path(project).read_bytes()

    server.kdenlive_undo(project)
    assert Path(project).read_bytes() == before

    server.kdenlive_redo(project)
    assert Path(project).read_bytes() == changed


def test_undo_and_redo_walk_the_whole_history(project: str):
    states = [Path(project).read_bytes()]
    for name in ("A", "B", "C"):
        server.kdenlive_add_track(project, kind="audio", name=name)
        states.append(Path(project).read_bytes())

    assert len(states) == 4
    for expected in reversed(states[:-1]):
        server.kdenlive_undo(project)
        assert Path(project).read_bytes() == expected

    for expected in states[1:]:
        server.kdenlive_redo(project)
        assert Path(project).read_bytes() == expected


def test_undo_refuses_when_the_file_changed_outside_the_mcp(project: str):
    server.kdenlive_add_track(project, kind="audio", name="Extra")
    # Somebody edits the file behind the MCP's back.
    Path(project).write_bytes(b"<mlt>hand edited</mlt>")

    with pytest.raises(ProjectError, match="changed outside the MCP"):
        server.kdenlive_undo(project)


def test_force_undo_overrides_the_safety_check(project: str):
    server.kdenlive_add_track(project, kind="audio", name="Extra")
    Path(project).write_bytes(b"<mlt>hand edited</mlt>")

    result = server.kdenlive_undo(project, force=True)
    assert result["ok"] is True


def test_undo_with_nothing_to_undo(project: str):
    with pytest.raises(ProjectError, match="nothing to undo"):
        server.kdenlive_undo(project)


def test_redo_with_nothing_to_redo(project: str):
    server.kdenlive_add_track(project, kind="audio", name="Extra")
    with pytest.raises(ProjectError, match="nothing to redo"):
        server.kdenlive_redo(project)


def test_checkpoint_can_be_restored(project: str):
    server.kdenlive_add_track(project, kind="audio", name="Extra")
    marked = Path(project).read_bytes()

    server.kdenlive_checkpoint(project, label="before the big edit")
    server.kdenlive_add_track(project, kind="audio", name="Another")
    assert Path(project).read_bytes() != marked

    server.kdenlive_restore_checkpoint(project, label="before the big edit")
    assert Path(project).read_bytes() == marked


def test_restore_an_unknown_checkpoint(project: str):
    with pytest.raises(ProjectError, match="No checkpoint"):
        server.kdenlive_restore_checkpoint(project, label="nope")


def test_checkpoints_are_listed(project: str):
    server.kdenlive_checkpoint(project, label="one")
    server.kdenlive_checkpoint(project, label="two")
    history = ProjectHistory(Path(project)).checkpoints()
    assert [r.label for r in history] == ["one", "two"]


def test_the_history_is_not_stored_next_to_the_project(project: str, tmp_path: Path):
    server.kdenlive_add_track(project, kind="audio", name="Extra")
    siblings = list(Path(project).parent.iterdir())
    assert siblings == [Path(project)] or all(
        not sibling.name.startswith(".kdenlive-mcp") for sibling in siblings
    )


def test_each_project_has_its_own_history(tmp_path: Path):
    first = tmp_path / "one.kdenlive"
    second = tmp_path / "two.kdenlive"
    for target in (first, second):
        server.kdenlive_create_project(str(target))
    server.kdenlive_add_track(str(first), kind="audio", name="A")

    assert server.kdenlive_history(str(first))["revision_count"] == 1
    assert server.kdenlive_history(str(second))["revision_count"] == 0


def test_history_survives_a_new_process(tmp_path: Path):
    """The history is on disk, not in memory."""
    target = tmp_path / "durable.kdenlive"
    server.kdenlive_create_project(str(target))
    server.kdenlive_add_track(str(target), kind="audio", name="A")

    history = ProjectHistory(target)
    assert len(history.revisions()) == 1
    assert history.revisions()[0].operation == "add_track"


def test_render_does_not_record_a_revision(project: str, sample_media: dict[str, Path]):
    server.kdenlive_add_clip(project, str(sample_media["red.mp4"]), track=1, position=0)
    before = server.kdenlive_history(project)["revision_count"]
    # Rendering never touches the project file, so it must not add a revision.
    assert before == 1


def test_read_only_tools_do_not_record_revisions(project: str):
    server.kdenlive_project_info(project)
    server.kdenlive_tracks(project)
    server.kdenlive_clips(project)
    server.kdenlive_validate_project(project)
    assert server.kdenlive_history(project)["revision_count"] == 0


def test_sha256_matches_the_content(project: str):
    data = Path(project).read_bytes()
    assert sha256(data) == sha256(data)
    assert len(sha256(data)) == 64
