"""Tests for the path containment policy."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kdenlive_mcp import server
from kdenlive_mcp.errors import ToolError
from kdenlive_mcp.sandbox import allowed_roots, is_within, resolve_within


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A workspace directory that is the only allowed root."""
    allowed = tmp_path / "workspace"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("KDENLIVE_MCP_ALLOWED_ROOTS", str(allowed))
    return allowed, outside


def test_no_roots_means_unrestricted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KDENLIVE_MCP_ALLOWED_ROOTS", raising=False)
    assert allowed_roots() == []
    # Anything is allowed.
    resolve_within(tmp_path / "anything.kdenlive", kind="project")


def test_a_root_is_accepted(workspace: tuple[Path, Path]):
    allowed, _ = workspace
    resolved = resolve_within(allowed / "project.kdenlive", kind="project")
    assert resolved.parent == allowed.resolve()


def test_a_path_outside_the_root_is_rejected(workspace: tuple[Path, Path]):
    _, outside = workspace
    with pytest.raises(ToolError, match="outside the allowed roots"):
        resolve_within(outside / "project.kdenlive", kind="project")


def test_parent_traversal_is_rejected(workspace: tuple[Path, Path]):
    allowed, _ = workspace
    with pytest.raises(ToolError, match="outside the allowed roots"):
        resolve_within(allowed / ".." / "escape.kdenlive", kind="project")


def test_a_symlink_out_of_the_root_is_rejected(workspace: tuple[Path, Path]):
    allowed, outside = workspace
    link = allowed / "link.kdenlive"
    try:
        link.symlink_to(outside / "secret.kdenlive")
    except OSError:
        pytest.skip("symlinks are not available here")
    with pytest.raises(ToolError, match="outside the allowed roots"):
        resolve_within(link, kind="project")


def test_is_within_handles_the_platform_separator():
    root = Path("/srv/videos")
    assert is_within([root], Path("/srv/videos/a.kdenlive")) is True
    assert is_within([root], Path("/srv/videos")) is True
    assert is_within([root], Path("/srv/videos-other/a.kdenlive")) is False
    assert is_within([root], Path("/etc/passwd")) is False


def test_the_server_refuses_a_project_outside_the_roots(workspace: tuple[Path, Path]):
    _, outside = workspace
    target = outside / "project.kdenlive"
    with pytest.raises(ToolError, match="outside the allowed roots"):
        server.kdenlive_create_project(str(target))


def test_the_server_accepts_a_project_inside_the_roots(workspace: tuple[Path, Path]):
    allowed, _ = workspace
    target = allowed / "project.kdenlive"
    result = server.kdenlive_create_project(str(target))
    assert result["ok"] is True
    assert target.exists()


def test_media_outside_the_roots_is_refused(
    workspace: tuple[Path, Path], sample_media: dict[str, Path]
):
    allowed, _ = workspace
    project = allowed / "project.kdenlive"
    server.kdenlive_create_project(str(project))
    with pytest.raises(Exception, match="outside the allowed roots"):
        server.kdenlive_add_clip(str(project), str(sample_media["red.mp4"]), track=1)


def test_status_reports_the_policy(workspace: tuple[Path, Path]):
    allowed, _ = workspace
    result = server.kdenlive_status()
    assert result["policy"]["restricted"] is True
    assert str(allowed.resolve()) in result["policy"]["allowed_roots"]


def test_status_reports_an_unrestricted_server(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KDENLIVE_MCP_ALLOWED_ROOTS", raising=False)
    result = server.kdenlive_status()
    assert result["policy"]["restricted"] is False
    assert "may read and write any path" in result["policy"]["note"]


def test_several_roots_are_supported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("KDENLIVE_MCP_ALLOWED_ROOTS", os.pathsep.join([str(first), str(second)]))
    assert len(allowed_roots()) == 2
    resolve_within(first / "a.kdenlive", kind="project")
    resolve_within(second / "b.kdenlive", kind="project")
    with pytest.raises(ToolError):
        resolve_within(tmp_path / "c.kdenlive", kind="project")


def test_a_root_that_does_not_exist_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    good = tmp_path / "good"
    good.mkdir()
    monkeypatch.setenv(
        "KDENLIVE_MCP_ALLOWED_ROOTS", os.pathsep.join([str(good), str(tmp_path / "missing")])
    )
    assert allowed_roots() == [good.resolve()]
