"""Per-project undo history.

Every mutating tool call rewrites the whole project file, so the undo unit has
to be the file itself rather than an inverse operation -- computing the
inverse of an XML edit is where undo systems usually go wrong.

The history is a linear list of revisions.  A revision records the bytes
before and after the change, so undoing is "put the previous bytes back" and
redoing is "put the next bytes back".  A cursor marks where in that list the
project currently sits; it is stored on disk next to the history rather than
inferred from the content, because inference cannot tell "the file is at
revision 3" from "the file happens to look like revision 3".

Before undoing, the current file is hashed and compared with the revision
that produced it.  If somebody edited the file outside the MCP in the
meantime, the undo is refused rather than silently discarding their work.

Checkpoints are named snapshots.  They are kept apart from the revision
chain because restoring one is a jump, not a step.

Storage lives outside the project folder.  A ``.kdenlive`` file usually lives
in a folder that gets synced to cloud storage or handed to a render farm, and
a directory of full file snapshots next to it is exactly the kind of thing
that should not be there.  The state directory follows the platform
convention and can be overridden with ``KDENLIVE_MCP_STATE_DIR``.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ProjectError


def sha256(data: bytes) -> str:
    """The hash used to detect edits made outside the MCP."""
    return hashlib.sha256(data).hexdigest()


def state_directory() -> Path:
    """Where the history is kept."""
    override = os.environ.get("KDENLIVE_MCP_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    elif os.sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_STATE_HOME", "").strip()
        root = Path(base).expanduser() if base else Path.home() / ".local" / "state"

    return root / "kdenlive-mcp"


def _project_key(project: Path) -> str:
    """A stable, filesystem-safe key for a project path."""
    resolved = str(project.expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    name = project.expanduser().name.replace(" ", "_")[:40]
    return f"{name}-{digest}"


def _encode(data: bytes | None) -> str | None:
    if data is None:
        return None
    return base64.b64encode(data).decode("ascii")


def _decode(value: str | None) -> bytes | None:
    if not value:
        return None
    return base64.b64decode(value)


@dataclass(slots=True)
class Revision:
    """One recorded change to a project file."""

    seq: int
    operation: str
    timestamp: float
    before: bytes | None
    after: bytes | None
    before_sha: str | None
    after_sha: str | None
    label: str = ""

    def describe(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "operation": self.operation,
            "label": self.label,
            "timestamp": self.timestamp,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp)),
            "before_sha": self.before_sha,
            "after_sha": self.after_sha,
            "has_before": self.before is not None,
        }


@dataclass(slots=True)
class ProjectHistory:
    """The undo history of a single project."""

    project: Path
    root: Path = field(init=False)

    def __init__(self, project: Path, root: Path | None = None) -> None:
        self.project = project.expanduser()
        self.root = (root or state_directory()) / "history" / _project_key(self.project)

    # -- paths --------------------------------------------------------------

    @property
    def _revisions_dir(self) -> Path:
        return self.root / "revisions"

    @property
    def _checkpoints_dir(self) -> Path:
        return self.root / "checkpoints"

    @property
    def _state_path(self) -> Path:
        return self.root / "state.json"

    def _revision_path(self, seq: int) -> Path:
        return self._revisions_dir / f"{seq:06d}.rev.gz"

    # -- storage ------------------------------------------------------------

    def _read_revision(self, seq: int) -> Revision | None:
        path = self._revision_path(seq)
        if not path.exists():
            return None
        try:
            payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        except (OSError, ValueError, gzip.BadGzipFile):
            return None
        return Revision(
            seq=int(payload["seq"]),
            operation=str(payload.get("operation", "edit")),
            timestamp=float(payload.get("timestamp", 0.0)),
            before=_decode(payload.get("before")),
            after=_decode(payload.get("after")),
            before_sha=payload.get("before_sha"),
            after_sha=payload.get("after_sha"),
            label=str(payload.get("label", "")),
        )

    def _write_revision(self, revision: Revision) -> None:
        self._revisions_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "seq": revision.seq,
            "operation": revision.operation,
            "timestamp": revision.timestamp,
            "label": revision.label,
            "before_sha": revision.before_sha,
            "after_sha": revision.after_sha,
            "before": _encode(revision.before),
            "after": _encode(revision.after),
        }
        blob = gzip.compress(json.dumps(payload).encode("utf-8"))
        target = self._revision_path(revision.seq)
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(blob)
        os.replace(temporary, target)

    def _read_checkpoint(self, label: str) -> Revision | None:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in label)[:80]
        path = self._checkpoints_dir / f"{safe}.ckpt.gz"
        if not path.exists():
            return None
        try:
            payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        except (OSError, ValueError, gzip.BadGzipFile):
            return None
        content = _decode(payload.get("content"))
        return Revision(
            seq=int(payload.get("seq", 0)),
            operation="checkpoint",
            timestamp=float(payload.get("timestamp", 0.0)),
            before=None,
            after=content,
            before_sha=payload.get("sha"),
            after_sha=payload.get("sha"),
            label=str(payload.get("label", label)),
        )

    def _write_checkpoint(self, label: str, content: bytes) -> Revision:
        self._checkpoints_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in label)[:80]
        payload = {
            "label": label,
            "timestamp": time.time(),
            "sha": sha256(content),
            "content": _encode(content),
        }
        blob = gzip.compress(json.dumps(payload).encode("utf-8"))
        target = self._checkpoints_dir / f"{safe}.ckpt.gz"
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(blob)
        os.replace(temporary, target)
        return Revision(
            seq=0,
            operation="checkpoint",
            timestamp=payload["timestamp"],
            before=None,
            after=content,
            before_sha=payload["sha"],
            after_sha=payload["sha"],
            label=label,
        )

    # -- reading ------------------------------------------------------------

    def revisions(self) -> list[Revision]:
        """Every recorded revision, oldest first."""
        if not self._revisions_dir.exists():
            return []
        result: list[Revision] = []
        for path in sorted(self._revisions_dir.glob("*.rev.gz")):
            try:
                seq = int(path.name.split(".", 1)[0])
            except ValueError:
                continue
            revision = self._read_revision(seq)
            if revision is not None:
                result.append(revision)
        result.sort(key=lambda r: r.seq)
        return result

    def checkpoints(self) -> list[Revision]:
        """The named checkpoints, oldest first."""
        if not self._checkpoints_dir.exists():
            return []
        found: list[Revision] = []
        for path in sorted(self._checkpoints_dir.glob("*.ckpt.gz")):
            try:
                payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
            except (OSError, ValueError, gzip.BadGzipFile):
                continue
            content = _decode(payload.get("content"))
            found.append(
                Revision(
                    seq=0,
                    operation="checkpoint",
                    timestamp=float(payload.get("timestamp", 0.0)),
                    before=None,
                    after=content,
                    before_sha=payload.get("sha"),
                    after_sha=payload.get("sha"),
                    label=str(payload.get("label", "")),
                )
            )
        found.sort(key=lambda r: r.timestamp)
        return found

    def _current_bytes(self) -> bytes | None:
        try:
            return self.project.read_bytes()
        except OSError:
            return None

    def cursor(self) -> int:
        """The index of the last applied revision, or -1 for the original state."""
        revisions = self.revisions()
        if not revisions:
            return -1
        if self._state_path.exists():
            try:
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                cursor = int(data.get("cursor", -1))
            except (OSError, ValueError):
                cursor = -1
            if -1 <= cursor <= len(revisions) - 1:
                return cursor
        # No cursor recorded -- fall back to matching the content.
        current = self._current_bytes()
        if current is not None:
            digest = sha256(current)
            for index, revision in enumerate(revisions):
                if revision.after_sha == digest:
                    return index
        return -1

    def _expected_sha(self, cursor: int) -> str | None:
        """The hash the file should have for a given cursor position.

        This is what tells "the MCP made more changes since" apart from
        "somebody edited the file behind our back".
        """
        revisions = self.revisions()
        if not revisions:
            return None
        if cursor < 0:
            return revisions[0].before_sha
        if cursor >= len(revisions):
            return None
        return revisions[cursor].after_sha

    def _externally_changed(self) -> bool:
        """``True`` when the file no longer matches what the MCP last wrote."""
        current = self._current_bytes()
        if current is None:
            return False
        expected = self._expected_sha(self.cursor())
        if not expected:
            return False
        return sha256(current) != expected

    def _save_cursor(self, cursor: int) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"cursor": int(cursor)}), encoding="utf-8")
        os.replace(temporary, self._state_path)

    # -- writing ------------------------------------------------------------

    def record(
        self,
        operation: str,
        before: bytes | None,
        after: bytes | None,
        *,
        label: str = "",
    ) -> Revision:
        """Record a change that has just been written to disk."""
        revisions = self.revisions()
        seq = (revisions[-1].seq + 1) if revisions else 0
        revision = Revision(
            seq=seq,
            operation=operation,
            timestamp=time.time(),
            before=before,
            after=after,
            before_sha=sha256(before) if before is not None else None,
            after_sha=sha256(after) if after is not None else None,
            label=label,
        )
        self._write_revision(revision)
        self._save_cursor(seq)
        return revision

    # -- undo / redo --------------------------------------------------------

    def undo(self, *, force: bool = False) -> tuple[Revision, Path]:
        """Restore the state before the most recent revision."""
        revisions = self.revisions()
        if not revisions:
            raise ProjectError("There is nothing to undo -- no changes have been recorded")

        cursor = self.cursor()
        if cursor < 0:
            raise ProjectError(
                "There is nothing to undo -- the project is already at its original state"
            )

        revision = revisions[cursor]
        current = self._current_bytes()
        if not force and current is not None and revision.after_sha:
            if sha256(current) != revision.after_sha:
                raise ProjectError(
                    "The project file was changed outside the MCP since the last recorded "
                    "change, so undoing would discard that work. Close the project in "
                    "Kdenlive, or pass force=True to undo anyway."
                )

        if revision.before is None:
            raise ProjectError("The original content of this project is not in the history")

        self.project.write_bytes(revision.before)
        self._save_cursor(cursor - 1)
        return revision, self.project

    def redo(self) -> tuple[Revision, Path]:
        """Re-apply the revision that was most recently undone."""
        revisions = self.revisions()
        if not revisions:
            raise ProjectError("There is nothing to redo")

        cursor = self.cursor()
        if cursor + 1 >= len(revisions):
            raise ProjectError("There is nothing to redo -- the project is at its newest state")

        revision = revisions[cursor + 1]
        if revision.after is None:
            raise ProjectError("The content of that revision is not in the history")

        self.project.write_bytes(revision.after)
        self._save_cursor(cursor + 1)
        return revision, self.project

    # -- checkpoints --------------------------------------------------------

    def checkpoint(self, label: str) -> Revision:
        """Name the current state so it can be restored later."""
        current = self._current_bytes()
        if current is None:
            raise ProjectError(f"Cannot checkpoint a project that does not exist: {self.project}")
        return self._write_checkpoint(label, current)

    def restore_checkpoint(self, label: str, *, force: bool = False) -> tuple[Revision, Path]:
        """Restore a named checkpoint."""
        checkpoint = self._read_checkpoint(label)
        if checkpoint is None:
            available = ", ".join(c.label for c in self.checkpoints()) or "(none)"
            raise ProjectError(f"No checkpoint named {label!r}. Available: {available}")
        if checkpoint.after is None:
            raise ProjectError("The content of that checkpoint is not in the history")

        # The checkpoint may be older than the current state -- that is the
        # point of a checkpoint.  What must not happen is the file having been
        # changed *outside* the MCP, because restoring would throw that away.
        if not force and self._externally_changed():
            raise ProjectError(
                "The project file was changed outside the MCP, so restoring the "
                "checkpoint would discard that work. Pass force=True to restore "
                "it anyway."
            )

        self.project.write_bytes(checkpoint.after)
        # Put the cursor back where the checkpoint came from.
        revisions = self.revisions()
        digest = checkpoint.after_sha
        cursor = -1
        for index, revision in enumerate(revisions):
            if revision.after_sha == digest:
                cursor = index
        self._save_cursor(cursor)
        return checkpoint, self.project

    def clear(self) -> int:
        """Delete the whole history. Returns the number of revisions removed."""
        revisions = self.revisions()
        for directory in (self._revisions_dir, self._checkpoints_dir):
            if not directory.exists():
                continue
            for path in directory.glob("*.gz"):
                try:
                    path.unlink()
                except OSError:
                    pass
        try:
            self._state_path.unlink()
        except OSError:
            pass
        return len(revisions)
