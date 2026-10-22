"""Server configuration, read from the environment.

Everything is optional: the MCP works out of the box with no configuration at
all, and each setting only adds capability.

===============================  ===============================================
``KDENLIVE_MCP_PROJECT``         Default ``.kdenlive`` project to work on.
``KDENLIVE_MCP_MELT``            Path to the ``melt`` renderer.
``KDENLIVE_MCP_FFPROBE``         Path to ``ffprobe``.
``KDENLIVE_MCP_BRIDGE_URL``      Base URL of a Kdenlive bridge (WSL/remote).
``KDENLIVE_MCP_BACKUP``          ``0`` disables the ``.bak`` file on save.
``KDENLIVE_MCP_MAX_BACKUPS``     How many ``.bak`` files to keep (default 3).
===============================  ===============================================
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import ToolError


def default_project() -> Path | None:
    """The project configured via ``KDENLIVE_MCP_PROJECT``, if any."""
    value = os.environ.get("KDENLIVE_MCP_PROJECT", "").strip()
    if not value:
        return None
    return Path(value).expanduser()


def backups_enabled() -> bool:
    """Whether a ``.bak`` copy is written when a project is saved."""
    return os.environ.get("KDENLIVE_MCP_BACKUP", "1").strip().lower() not in ("0", "false", "no")


def max_backups() -> int:
    """How many ``.bak`` generations to keep."""
    try:
        value = int(os.environ.get("KDENLIVE_MCP_MAX_BACKUPS", "3"))
    except ValueError:
        return 3
    return max(0, value)


def resolve_project(path: str | os.PathLike[str] | None) -> Path:
    """Resolve the project to operate on.

    Falls back to ``KDENLIVE_MCP_PROJECT`` when no path is given, and raises a
    helpful error when neither is available.
    """
    if path:
        return Path(path).expanduser()
    configured = default_project()
    if configured:
        return configured
    raise ToolError(
        "No project given. Pass the path to a .kdenlive file, or set the "
        "KDENLIVE_MCP_PROJECT environment variable."
    )


def prune_backups(project: Path, keep: int | None = None) -> list[Path]:
    """Remove old ``.kdenlive.bak`` files, keeping the newest ``keep``."""
    keep = max_backups() if keep is None else keep
    if keep <= 0:
        return []
    candidates = [p for p in project.parent.glob(project.name + ".bak*") if p.is_file()]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed: list[Path] = []
    for stale in candidates[keep:]:
        try:
            stale.unlink()
            removed.append(stale)
        except OSError:
            pass
    return removed
