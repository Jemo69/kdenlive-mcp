"""Tests for path translation between Linux and Windows.

The WSL interop is the fiddly part of this server: the MCP may run inside
WSL while the renderer and the media live on the Windows side, so every
path that crosses that boundary has to be translated.  These tests pin down
the translation, because a mistake here shows up as "the renderer cannot
find the project" rather than as an exception.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kdenlive_mcp import binaries
from kdenlive_mcp.render import RenderRequest, build_melt_command


@pytest.fixture
def wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend we are running inside WSL."""
    monkeypatch.setattr(binaries, "IS_WINDOWS", False)
    monkeypatch.setattr(binaries, "is_wsl", lambda: True)


def test_to_windows_path_translates_a_drive_mount(wsl: None) -> None:
    result = binaries.to_windows_path("/mnt/c/Users/Jemo Life/a b.mp4")
    assert result == "C:\\Users\\Jemo Life\\a b.mp4"


def test_to_windows_path_handles_the_bare_drive(wsl: None) -> None:
    assert binaries.to_windows_path("/mnt/c") == "C:\\"


def test_to_windows_path_leaves_linux_paths_alone(wsl: None) -> None:
    assert binaries.to_windows_path("/home/jemo/video.mp4") == "/home/jemo/video.mp4"


def test_to_windows_path_leaves_windows_paths_alone(wsl: None) -> None:
    assert binaries.to_windows_path("C:\\already\\windows") == "C:\\already\\windows"


def test_to_windows_path_does_not_double_the_separator(wsl: None) -> None:
    result = binaries.to_windows_path("/mnt/c/Users/Jemo Life/a b.mp4")
    assert "\\\\" not in result
    assert result == "C:\\Users\\Jemo Life\\a b.mp4"


def test_windows_and_wsl_paths_round_trip(wsl: None) -> None:
    original = "/mnt/c/Users/Jemo Life/Downloads/inguration project/new_project.kdenlive"
    assert binaries.to_wsl_path(binaries.to_windows_path(original)) == original


def test_melt_command_uses_windows_paths_for_a_windows_melt(wsl: None) -> None:
    """A Windows melt cannot read /mnt paths; the command must be translated."""
    project = Path("/mnt/c/Users/Jemo Life/Downloads/inguration project/new_project.kdenlive")
    output = Path("/mnt/c/Users/Jemo Life/Downloads/inguration project/new_project.mp4")
    request = RenderRequest(project=project, output=output, crf=20, preset="fast")

    argv = build_melt_command("C:/Program Files/Kdenlive/bin/melt.exe", request)

    assert argv[1] == "C:\\Users\\Jemo Life\\Downloads\\inguration project\\new_project.kdenlive"
    assert "avformat:C:\\Users\\Jemo Life\\Downloads\\inguration project\\new_project.mp4" in argv


def test_melt_command_keeps_linux_paths_for_a_linux_melt(wsl: None) -> None:
    project = Path("/home/jemo/new_project.kdenlive")
    output = Path("/home/jemo/new_project.mp4")
    request = RenderRequest(project=project, output=output)

    argv = build_melt_command("/usr/bin/melt", request)

    # str(project) rather than a literal, so the expectation holds on a
    # Windows host too, where Path renders the separators differently.
    assert argv[1] == str(project)
    assert f"avformat:{output}" in argv
