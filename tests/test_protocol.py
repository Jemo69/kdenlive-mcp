"""Tests that exercise the server through the real MCP protocol.

These catch the class of problem that only shows up over the wire: a tool
whose schema does not accept the values the documentation promises.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from kdenlive_mcp.server import mcp


@pytest.fixture
async def client():
    async with Client(mcp) as instance:
        yield instance


async def _call(client, tool: str, **arguments):
    result = await client.call_tool(tool, arguments)
    return result.structured_content or result.data


async def test_lists_its_tools(client):
    tools = await client.list_tools()
    assert len(tools) >= 30
    names = {tool.name for tool in tools}
    for expected in (
        "kdenlive_project_info",
        "kdenlive_add_clip",
        "kdenlive_add_effect",
        "kdenlive_add_transition",
        "kdenlive_render",
        "kdenlive_validate_project",
        "kdenlive_effects_catalog",
    ):
        assert expected in names


async def test_status_over_the_protocol(client):
    data = await _call(client, "kdenlive_status")
    assert data["ok"] is True
    assert data["platform"]["system"]
    assert "melt" in data["tools"]


async def test_create_and_edit_with_integer_arguments(client, tmp_path: Path):
    project = str(tmp_path / "p.kdenlive")
    data = await _call(
        client, "kdenlive_create_project", project_path=project, profile="1080p25"
    )
    assert data["ok"] is True

    # An integer track index must be accepted (the media is missing, so the
    # call fails -- but it fails on the media, not on the argument types).
    with pytest.raises(ToolError, match="Media file not found"):
        await _call(
            client,
            "kdenlive_add_clip",
            project_path=project,
            media_path=str(tmp_path / "nope.mp4"),
            track=1,
            position=0,
        )


async def test_track_and_clip_references_accept_integers_and_strings(
    client, tmp_path: Path, sample_media: dict[str, Path]
):
    project = str(tmp_path / "p.kdenlive")
    await _call(client, "kdenlive_create_project", project_path=project)
    await _call(
        client,
        "kdenlive_add_clip",
        project_path=project,
        media_path=str(sample_media["red.mp4"]),
        track=1,
        position=0,
    )

    # Integer track reference.
    data = await _call(client, "kdenlive_clips", project_path=project, track=1)
    assert data["ok"] is True
    assert len(data["tracks"][0]["clips"]) == 1

    # String timecode instead of a frame count.
    data = await _call(
        client,
        "kdenlive_add_clip",
        project_path=project,
        media_path=str(sample_media["blue.mp4"]),
        track="2",
        position="00:00:01.000",
    )
    assert data["ok"] is True
    assert data["position"] == 25

    # String clip id.
    data = await _call(
        client,
        "kdenlive_effects_on",
        project_path=project,
        scope="clip",
        track="1",
        clip="1",
    )
    assert data["ok"] is True


async def test_effect_catalog_over_the_protocol(client):
    data = await _call(client, "kdenlive_effects_catalog", query="qtblend")
    assert data["ok"] is True
    assert any(item["mlt_service"] == "qtblend" for item in data["effects"])


async def test_validate_over_the_protocol(client, tmp_path: Path):
    project = str(tmp_path / "p.kdenlive")
    await _call(client, "kdenlive_create_project", project_path=project)
    data = await _call(client, "kdenlive_validate_project", project_path=project)
    assert data["ok"] is True
    assert data["error_count"] == 0
