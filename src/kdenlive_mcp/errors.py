"""Exception types raised by the Kdenlive MCP server."""

from __future__ import annotations


class KdenliveError(Exception):
    """Base class for every error raised by this package."""


class ProjectError(KdenliveError):
    """The project file is missing, malformed or does not contain what was asked for."""


class MediaError(KdenliveError):
    """A media file could not be found, probed or added."""


class RenderError(KdenliveError):
    """A render job failed or could not be started."""


class ToolError(KdenliveError):
    """A tool was called with invalid arguments."""


class BridgeError(KdenliveError):
    """The optional Kdenlive bridge (remote/WSL) is unreachable or returned an error."""
