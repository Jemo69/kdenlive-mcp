"""Keeping the server inside the directories it is allowed to touch.

An MCP server is driven by a model, which means the paths it receives may be
hallucinated, mis-typed, or taken from an untrusted document.  Without a
boundary, a single mistaken argument can write over a file that had nothing
to do with the project.

``KDENLIVE_MCP_ALLOWED_ROOTS`` names the directories the server may read and
write.  When it is set, every project path, media path and output path is
resolved -- following symlinks -- and has to land inside one of those roots.
Paths that escape through a symlink or through ``..`` are rejected, because
the check runs on the resolved path rather than on the text that was typed.

When the variable is not set the server is unrestricted.  That keeps the
zero-configuration promise, and :func:`describe_policy` reports the state so
the caller can see it rather than having to guess.
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import ToolError

ENV_VAR = "KDENLIVE_MCP_ALLOWED_ROOTS"


def allowed_roots() -> list[Path]:
    """The configured roots, resolved. Empty when the server is unrestricted."""
    raw = os.environ.get(ENV_VAR, "").strip()
    if not raw:
        return []
    roots: list[Path] = []
    for item in raw.split(os.pathsep):
        item = item.strip()
        if not item:
            continue
        try:
            resolved = Path(item).expanduser().resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    return roots


def _normcase(path: Path) -> str:
    return os.path.normcase(str(path))


def is_within(roots: list[Path], candidate: Path) -> bool:
    """Whether ``candidate`` sits inside one of ``roots``."""
    if not roots:
        return True
    target = _normcase(candidate)
    for root in roots:
        base = _normcase(root)
        if target == base:
            return True
        if target.startswith(base.rstrip(os.sep) + os.sep):
            return True
    return False


def resolve_within(path: str | os.PathLike[str], *, kind: str = "path") -> Path:
    """Resolve a path and check it against the allowed roots.

    The path is resolved through symlinks before the check, so a symlink
    pointing outside a root is rejected just like a literal ``..`` would be.
    For a path that does not exist yet, the closest existing parent is
    resolved instead, which is what matters for an output file.
    """
    candidate = Path(path).expanduser()
    try:
        # resolve() is non-strict: it resolves symlinks in the path even when
        # the target does not exist, which is what stops a dangling symlink
        # from being used to step outside a root.
        resolved = candidate.resolve()
    except OSError as exc:
        raise ToolError(f"Could not resolve {kind} {candidate}: {exc}") from exc

    roots = allowed_roots()
    if roots and not is_within(roots, resolved):
        listing = ", ".join(str(root) for root in roots)
        raise ToolError(
            f"The {kind} {resolved} is outside the allowed roots ({listing}). "
            f"Set {ENV_VAR} to change the policy."
        )
    return resolved


def describe_policy() -> dict[str, object]:
    """A description of the current containment policy."""
    roots = allowed_roots()
    return {
        "restricted": bool(roots),
        "allowed_roots": [str(root) for root in roots],
        "environment_variable": ENV_VAR,
        "note": (
            "Every project, media and output path is resolved and checked against "
            "the allowed roots."
            if roots
            else "No roots are configured: the server may read and write any path "
            "it is given. Set " + ENV_VAR + " to restrict it."
        ),
    }
