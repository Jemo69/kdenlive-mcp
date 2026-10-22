"""Structural validation of a Kdenlive project.

After the MCP edits a project it is worth checking that the result still
satisfies the invariants Kdenlive relies on.  A validation report is much
more useful to a model than a generic "the file is broken": it says exactly
which reference is dangling or which cached duration is stale.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import schema
from .binaries import to_wsl_path
from .mltxml import (
    INTERNAL_ADDED,
    MAIN_BIN_ID,
    MltProject,
    get_property,
)
from .timecode import format_frames


@dataclass(slots=True)
class Issue:
    """One problem found in a project."""

    severity: str  # "error" | "warning" | "info"
    code: str
    message: str
    location: str | None = None

    def describe(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "location": self.location,
        }


@dataclass(slots=True)
class ValidationReport:
    """The outcome of validating a project."""

    path: str | None
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        """``True`` when nothing of severity ``error`` was found."""
        return not self.errors

    def describe(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "path": self.path,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [issue.describe() for issue in self.issues],
        }


def resolve_media_path(project: MltProject, resource: str) -> Path | None:
    """Resolve a bin resource to a path on the filesystem we can see.

    Kdenlive stores media relative to the project file, and it also stores
    absolute Windows paths (``C:/...``) for anything outside the project
    folder.  Both have to be mapped onto the filesystem this process can
    actually see, otherwise every clip looks missing.

    Returns ``None`` for resources that are not files at all (colour
    producers, generated clips) or that live on a remote host.
    """
    text = resource.strip()
    if not text or text.startswith(("http://", "https://", "#")):
        return None
    # An absolute Windows path only exists for us under WSL.
    if len(text) > 2 and text[1] == ":" and text[2] in "\\/":
        return Path(to_wsl_path(text))
    path = Path(text)
    if path.is_absolute():
        return path
    # Relative resources are relative to the project file.
    if project.path is not None:
        return project.path.parent / path
    return path


def validate(project: MltProject, *, check_media: bool = True) -> ValidationReport:
    """Validate a project and return a report of everything that looks wrong."""
    report = ValidationReport(path=str(project.path) if project.path else None)
    issues = report.issues

    root = project.root
    if root.get("producer") != MAIN_BIN_ID:
        issues.append(
            Issue(
                "warning",
                "root_producer",
                f"The root element should declare producer=\"{MAIN_BIN_ID}\", "
                f"found {root.get('producer')!r}",
                "<mlt>",
            )
        )

    # -- main bin ----------------------------------------------------------
    bin_element = None
    for child in root:
        if child.tag == "playlist" and child.get("id") == MAIN_BIN_ID:
            bin_element = child
            break
    if bin_element is None:
        issues.append(
            Issue("error", "missing_main_bin", "The project has no <playlist id=\"main_bin\">", "main_bin")
        )

    # -- project tractor ---------------------------------------------------
    tractor = project.project_tractor()
    if tractor is None:
        issues.append(
            Issue(
                "error",
                "missing_project_tractor",
                "No tractor carries the 'kdenlive:projectTractor' property; Kdenlive "
                "uses it to know which sequence is open",
                "project tractor",
            )
        )
    elif list(root)[-1] is not tractor:
        issues.append(
            Issue(
                "warning",
                "project_tractor_not_last",
                "The project tractor should be the last element of the document",
                tractor.get("id", "project tractor"),
            )
        )

    # -- reference integrity ----------------------------------------------
    known: set[str] = set()
    for child in root:
        element_id = child.get("id")
        if element_id:
            known.add(element_id)

    for playlist in root.iter("playlist"):
        playlist_id = playlist.get("id", "playlist")
        for entry in playlist.findall("entry"):
            producer = entry.get("producer", "")
            if producer and producer not in known:
                issues.append(
                    Issue(
                        "error",
                        "dangling_entry",
                        f"The entry references producer {producer!r} which is not defined",
                        playlist_id,
                    )
                )

    for tractor in root.iter("tractor"):
        tractor_id = tractor.get("id", "tractor")
        for track in tractor.findall("track"):
            producer = track.get("producer", "")
            if producer and producer not in known:
                issues.append(
                    Issue(
                        "error",
                        "dangling_track",
                        f"The track references producer {producer!r} which is not defined",
                        tractor_id,
                    )
                )

    # -- definition order --------------------------------------------------
    positions = {child.get("id"): index for index, child in enumerate(root) if child.get("id")}
    for child in root:
        if child.tag != "playlist":
            continue
        for entry in child.findall("entry"):
            producer = entry.get("producer", "")
            if producer in positions and positions[producer] > positions[child.get("id", "")]:
                issues.append(
                    Issue(
                        "error",
                        "forward_reference",
                        f"The playlist references {producer!r} which is defined later in "
                        "the document; MLT resolves references in document order",
                        child.get("id", "playlist"),
                    )
                )

    # -- sequences and durations ------------------------------------------
    for sequence in project.sequences():
        stored = get_property(sequence.tractor, "kdenlive:duration")
        longest = max((track.length for track in sequence.tracks), default=0)
        if stored and longest:
            from .timecode import parse_duration

            try:
                declared = parse_duration(stored, project.fps)
            except Exception:  # noqa: BLE001
                declared = longest
            if declared != longest:
                issues.append(
                    Issue(
                        "warning",
                        "stale_duration",
                        f"The sequence duration says {declared} frames but the tracks hold "
                        f"{longest} frames",
                        sequence.name,
                    )
                )
        for track in sequence.tracks:
            if track.kind == "audio":
                if (get_property(track.tractor, "kdenlive:audio_track") or "") != "1":
                    issues.append(
                        Issue(
                            "warning",
                            "audio_track_flag",
                            "An audio track is missing the 'kdenlive:audio_track' property",
                            track.name or track.track_id,
                        )
                    )

    # -- media -------------------------------------------------------------
    if check_media:
        for clip in project.bin_clips():
            resource = clip.resource
            if not resource:
                continue
            if resource.startswith(("http://", "https://")):
                continue
            # Colour and other generated producers have a resource that is a
            # colour name, not a file.
            if get_property(clip.element, "mlt_service") == "color":
                continue
            resolved = resolve_media_path(project, resource)
            if resolved is not None and not resolved.exists():
                issues.append(
                    Issue(
                        "warning",
                        "missing_media",
                        f"The media file {resource!r} does not exist on this machine",
                        clip.name,
                    )
                )

    # -- effects -----------------------------------------------------------
    for element in root.iter("filter"):
        if get_property(element, "mlt_service") is None:
            issues.append(
                Issue(
                    "error",
                    "filter_without_service",
                    "A <filter> has no 'mlt_service' property",
                    element.get("id", "filter"),
                )
            )
        if get_property(element, INTERNAL_ADDED) is None and get_property(element, "kdenlive_id") is None:
            issues.append(
                Issue(
                    "warning",
                    "filter_without_kdenlive_id",
                    "A <filter> has no 'kdenlive_id'; Kdenlive will not show it in the "
                    "effect stack",
                    element.get("id", "filter"),
                )
            )

    # -- content model -----------------------------------------------------
    for code, message, location in schema.check(project):
        issues.append(Issue("error", code, message, location))

    # -- transitions -------------------------------------------------------
    for element in root.iter("transition"):
        if get_property(element, "mlt_service") is None:
            issues.append(
                Issue(
                    "error",
                    "transition_without_service",
                    "A <transition> has no 'mlt_service' property",
                    element.get("id", "transition"),
                )
            )

    return report


def validate_file(path: str | os.PathLike[str], *, check_media: bool = True) -> ValidationReport:
    """Load a project from disk and validate it."""
    return validate(MltProject.load(path), check_media=check_media)


def describe_project(project: MltProject) -> str:
    """A short human readable summary of a project."""
    fps = project.fps
    lines = [
        f"{project.path}",
        f"  profile: {project.profile.describe()}",
    ]
    for sequence in project.sequences():
        lines.append(
            f"  sequence {sequence.name!r}: {len(sequence.tracks)} tracks, "
            f"{format_frames(sequence.duration_frames, fps)}"
        )
        for track in sequence.tracks:
            lines.append(
                f"    track {track.index + 1} ({track.kind}) {track.name!r}: "
                f"{track.length} frames"
            )
    return "\n".join(lines)
