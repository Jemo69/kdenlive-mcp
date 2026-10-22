"""Locating the external tools this MCP can use, on any operating system.

Kdenlive projects are plain XML, so everything in this package works with
nothing but Python installed.  Two optional external tools unlock more:

``melt``
    The MLT command line renderer.  Ships with Kdenlive.  Used to render a
    project to a file without opening the GUI.

``ffprobe``
    Part of FFmpeg.  Used to read the real duration, frame rate and stream
    layout of a media file so clips can be placed with exact timings.

Both are looked up in a way that works on Linux, macOS, native Windows and
WSL, where a Windows ``melt.exe`` may be reachable through WSL interop.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"


def is_windows() -> bool:
    """``True`` when running natively on Windows (not under WSL)."""
    return IS_WINDOWS


def is_wsl() -> bool:
    """``True`` when running inside the Windows Subsystem for Linux."""
    if IS_WINDOWS:
        return False
    try:
        release = platform.uname().release.lower()
    except Exception:  # pragma: no cover - extremely defensive
        return False
    if "microsoft" in release or "wsl" in release:
        return True
    return Path("/proc/sys/kernel/osrelease").is_file() and "microsoft" in (
        Path("/proc/sys/kernel/osrelease").read_text().lower()
    )


def _candidate_names(base: str) -> list[str]:
    names = [base]
    if IS_WINDOWS:
        names.insert(0, f"{base}.exe")
    else:
        # Under WSL the Windows binary may also be on PATH through interop.
        names.append(f"{base}.exe")
    return names


def _search_paths(base: str) -> list[Path]:
    """Platform specific install locations for a tool."""
    home = Path.home()
    candidates: list[Path] = []

    if IS_MACOS:
        candidates += [
            Path(f"/Applications/Kdenlive.app/Contents/MacOS/{base}"),
            Path(f"/opt/homebrew/bin/{base}"),
            Path(f"/usr/local/bin/{base}"),
            home / f"Applications/Kdenlive.app/Contents/MacOS/{base}",
        ]
    elif IS_WINDOWS:
        for drive in ("C:", "D:", "E:"):
            candidates += [
                Path(f"{drive}/Program Files/Kdenlive/bin/{base}.exe"),
                Path(f"{drive}/Program Files (x86)/Kdenlive/bin/{base}.exe"),
            ]
        candidates.append(home / "scoop/shims" / f"{base}.exe")
    else:
        candidates += [
            Path(f"/usr/bin/{base}"),
            Path(f"/usr/local/bin/{base}"),
            Path(f"/snap/bin/{base}"),
            Path(f"/var/lib/flatpak/exports/bin/{base}"),
            home / f".local/bin/{base}",
        ]
    return candidates


@dataclass(slots=True)
class ToolInfo:
    """A discovered external tool."""

    name: str
    path: str | None
    available: bool
    version: str | None = None
    detail: str = ""

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "available": self.available,
            "path": self.path,
            "version": self.version,
            "detail": self.detail,
        }


def _tool_version(path: str, args: list[str]) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603
            [path, *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or result.stderr or "").strip()
    if not output:
        return None
    return output.splitlines()[0][:200]


def find_tool(name: str, version_args: list[str] | None = None) -> ToolInfo:
    """Find an executable, preferring an explicit environment override."""
    override = os.environ.get(f"KDENLIVE_MCP_{name.upper()}")
    if override:
        path = Path(override).expanduser()
        if path.exists():
            return ToolInfo(
                name=name,
                path=str(path),
                available=True,
                version=_tool_version(str(path), version_args or ["-version"]),
                detail="configured via environment variable",
            )
        return ToolInfo(
            name=name,
            path=None,
            available=False,
            detail=f"KDENLIVE_MCP_{name.upper()}={override} does not exist",
        )

    for candidate in _candidate_names(name):
        found = shutil.which(candidate)
        if found:
            return ToolInfo(
                name=name,
                path=found,
                available=True,
                version=_tool_version(found, version_args or ["-version"]),
                detail="found on PATH",
            )

    for candidate in _search_paths(name):
        if candidate.exists():
            return ToolInfo(
                name=name,
                path=str(candidate),
                available=True,
                version=_tool_version(str(candidate), version_args or ["-version"]),
                detail="found in a standard install location",
            )

    return ToolInfo(name=name, path=None, available=False, detail="not found on this system")


def find_melt() -> ToolInfo:
    """Locate the MLT ``melt`` renderer."""
    return find_tool("melt", ["-version"])


def find_ffprobe() -> ToolInfo:
    """Locate FFmpeg's ``ffprobe``."""
    return find_tool("ffprobe", ["-version"])


def find_ffmpeg() -> ToolInfo:
    """Locate FFmpeg's ``ffmpeg``."""
    return find_tool("ffmpeg", ["-version"])


# --------------------------------------------------------------------------
# WSL path translation
# --------------------------------------------------------------------------


def to_windows_path(path: str | os.PathLike[str]) -> str:
    """Translate a path into Windows form (``C:\\dir\\file``).

    Outside WSL the input is returned unchanged.
    """
    text = str(path)
    if IS_WINDOWS:
        return text
    if not is_wsl():
        return text
    if len(text) > 2 and text[1] == ":" and text[2] in "\\/":
        return text.replace("/", "\\")
    match = os.path.abspath(text)
    # /mnt/c/... -> C:\...
    if match.startswith("/mnt/") and len(match) > 6 and match[5] == "/":
        letter = match[5].upper()
        remainder = match[6:].replace("/", "\\")
        return f"{letter}:\\{remainder}" if remainder else f"{letter}:\\"
    # Anything else (e.g. /home/...) lives inside the Linux filesystem and has
    # no Windows equivalent; the caller has to copy it over.
    return text


def to_wsl_path(path: str | os.PathLike[str]) -> str:
    """Translate a Windows path into WSL form (``/mnt/c/dir/file``)."""
    text = str(path)
    if len(text) > 2 and text[1] == ":" and text[2] in "\\/":
        letter = text[0].lower()
        remainder = text[2:].replace("\\", "/")
        return f"/mnt/{letter}{remainder}"
    return text


def is_on_windows_filesystem(path: str | os.PathLike[str]) -> bool:
    """``True`` when a path lives on a Windows drive mounted into WSL."""
    if IS_WINDOWS or not is_wsl():
        return False
    return str(path).startswith("/mnt/")


# --------------------------------------------------------------------------
# running Windows binaries from WSL
# --------------------------------------------------------------------------


def run_interop(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run a command, transparently handling WSL interop.

    When the MCP runs inside WSL but the target binary lives on the Windows
    side, the Windows binary is invoked directly: WSL interop makes Windows
    executables runnable from Linux, and arguments that look like Linux paths
    are translated to their Windows equivalents first.
    """
    return subprocess.run(  # noqa: S603
        argv,
        capture_output=True,
        text=True,
        check=False,
        **kwargs,  # type: ignore[arg-type]
    )
