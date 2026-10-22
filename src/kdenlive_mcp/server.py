"""The Kdenlive MCP server.

The whole point of this server is that it does **not** need Kdenlive to be
running.  A Kdenlive project is an MLT XML document, so a model can read it,
change it and write it back with plain file I/O -- deterministically, on any
operating system, in CI, or inside WSL while the editor runs on Windows.

Only rendering needs an external tool (``melt``), and only *driving a live
editor* needs the optional bridge.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP

from . import effects as effects_module
from . import render as render_module
from . import validate as validate_module
from .binaries import (
    find_ffmpeg,
    find_ffprobe,
    find_melt,
    is_windows,
    is_wsl,
    to_windows_path,
    to_wsl_path,
)
from .bridge import BridgeClient
from .config import backups_enabled, prune_backups, resolve_project
from .errors import KdenliveError, MediaError, ProjectError, ToolError
from .media import probe
from .mltxml import (
    PROFILE_PRESETS,
    MltProject,
    get_property,
    set_property,
)
from .timecode import coerce_frames, format_clock, format_frames, human_duration

mcp = FastMCP(
    name="kdenlive",
    version="0.1.0",
    instructions=(
        "Drive the Kdenlive video editor through its native MLT XML project format.\n\n"
        "A Kdenlive project is a plain XML file, so every tool here works without "
        "the GUI running. Start with `kdenlive_project_info` to understand a "
        "project, then `kdenlive_add_clip` / `kdenlive_add_effect` / "
        "`kdenlive_add_transition` to build a timeline, and `kdenlive_render` to "
        "render it.\n\n"
        "Positions and durations are given in FRAMES by default; pass a timecode "
        "string such as '00:00:05.000' instead if you prefer. Call "
        "`kdenlive_effects_catalog` to look up valid effect names and their "
        "parameters -- do not guess them."
    ),
)

ServerError = KdenliveError

#: A position or duration: a number of frames, or a timecode string such as
#: "00:00:05.000".  Both are accepted everywhere so a model never has to
#: convert between the two.
Frame = int | float | str

#: A reference to a track, clip or sequence: an index, a name or an id.
Ref = int | str


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _load(project_path: str | None) -> MltProject:
    """Load the project, raising a clean error when it is missing."""
    path = resolve_project(project_path)
    if not path.exists():
        raise ProjectError(
            f"Project file not found: {path}. Create one with kdenlive_create_project, "
            "or point at an existing .kdenlive file."
        )
    return MltProject.load(path)


def _save(project: MltProject, *, backup: bool = True) -> Path:
    """Write the project back to disk and prune old backups."""
    path = project.path
    if path is None:
        raise ProjectError("Project has no path to save to")
    project.refresh_timings()
    project.save(backup=backup and backups_enabled())
    prune_backups(path)
    return path


def _ok(**payload: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": True}
    result.update(payload)
    return result


def _fps_context(project: MltProject) -> dict[str, Any]:
    fps = project.fps
    return {"fps": fps, "profile": project.profile.describe()}


# --------------------------------------------------------------------------
# environment / discovery
# --------------------------------------------------------------------------


@mcp.tool
def kdenlive_status(project_path: Annotated[str | None, "Path to a .kdenlive file"] = None) -> dict:
    """Report what this MCP can do on this machine.

    Shows the operating system, whether the WSL bridge is reachable, and which
    optional tools (melt, ffprobe) were found. Useful as a first call.
    """
    melt = find_melt()
    ffprobe = find_ffprobe()
    ffmpeg = find_ffmpeg()
    bridge = BridgeClient().health()

    project_info: dict[str, Any] | None = None
    if project_path:
        try:
            project = MltProject.load(resolve_project(project_path))
            project_info = project.summary()
        except KdenliveError as exc:
            project_info = {"error": str(exc)}

    return _ok(
        platform={
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "is_wsl": is_wsl(),
            "is_windows": is_windows(),
        },
        tools={
            "melt": melt.describe(),
            "ffprobe": ffprobe.describe(),
            "ffmpeg": ffmpeg.describe(),
        },
        bridge=bridge.describe(),
        project=project_info,
    )


@mcp.tool
def kdenlive_validate_project(
    project_path: Annotated[str | None, "Path to a .kdenlive file"] = None,
    check_media: Annotated[bool, "Also check that referenced media files exist"] = True,
) -> dict:
    """Check a project for structural problems.

    Run this after a series of edits to confirm the file is still valid:
    dangling references, producers defined after the elements that use them,
    stale cached durations and missing media are all reported.
    """
    project = _load(project_path)
    report = validate_module.validate(project, check_media=check_media)
    return _ok(**report.describe())


@mcp.tool
def kdenlive_effects_catalog(
    query: Annotated[str | None, "Substring to search for in effect names or descriptions"] = None,
    kind: Annotated[str | None, "'effect', 'transition' or None for both"] = None,
    limit: Annotated[int, "Maximum number of results"] = 50,
) -> dict:
    """List the MLT effects and transitions Kdenlive supports.

    Every entry gives the exact ``mlt_service`` string to write into the
    project and the parameters it accepts. Always look an effect up here
    instead of guessing a service name.
    """
    include_transitions = kind != "effect"
    results = effects_module.search(query or "", include_transitions=include_transitions, limit=limit)
    if kind == "effect":
        results = [r for r in results if r.kind == "effect"]
    elif kind == "transition":
        results = [r for r in results if r.kind == "transition"]
    return _ok(
        count=len(results),
        effects=[item.describe() for item in results],
    )


# --------------------------------------------------------------------------
# project inspection
# --------------------------------------------------------------------------


@mcp.tool
def kdenlive_project_info(project_path: Annotated[str | None, "Path to a .kdenlive file"] = None) -> dict:
    """Describe a project: profile, sequences, tracks and bin clips."""
    project = _load(project_path)
    fps = project.fps
    summary = project.summary()
    active = project.active_sequence()
    if active is not None:
        summary["active_sequence"] = {
            "uuid": active.uuid,
            "name": active.name,
            "tracks": [track.describe() for track in active.tracks],
        }
    summary["duration_human"] = human_duration(
        active.duration_frames if active else 0, fps
    )
    return _ok(**summary)


@mcp.tool
def kdenlive_sequences(project_path: Annotated[str | None, "Path to a .kdenlive file"] = None) -> dict:
    """List every timeline sequence in the project."""
    project = _load(project_path)
    fps = project.fps
    active = project.active_sequence()
    sequences = []
    for index, sequence in enumerate(project.sequences(), start=1):
        data = sequence.describe(fps)
        data["index"] = index
        data["active"] = bool(active and active.uuid == sequence.uuid)
        sequences.append(data)
    return _ok(count=len(sequences), sequences=sequences)


@mcp.tool
def kdenlive_tracks(
    project_path: Annotated[str | None, "Path to a .kdenlive file"] = None,
    sequence: Annotated[Ref | None, "Sequence uuid, name or 1-based index"] = None,
) -> dict:
    """List the tracks of a sequence, including mute/lock state and length."""
    project = _load(project_path)
    seq = project.resolve_sequence(sequence)
    tracks = []
    for index, track in enumerate(seq.tracks, start=1):
        data = track.describe()
        data["position"] = index
        data["clip_count"] = len(project.track_clips(track))
        tracks.append(data)
    return _ok(
        sequence={"uuid": seq.uuid, "name": seq.name},
        count=len(tracks),
        tracks=tracks,
    )


@mcp.tool
def kdenlive_clips(
    project_path: Annotated[str | None, "Path to a .kdenlive file"] = None,
    sequence: Annotated[Ref | None, "Sequence uuid, name or 1-based index"] = None,
    track: Annotated[Ref | None, "Track index (1-based), name or id"] = None,
) -> dict:
    """List the clips on a track (or on every track) with their positions.

    Positions are frames from the start of the track. Use the returned
    ``clip_id`` with the editing tools.
    """
    project = _load(project_path)
    seq = project.resolve_sequence(sequence)
    fps = project.fps

    if track is not None:
        tracks = [project.resolve_track(seq, track)]
    else:
        tracks = seq.tracks

    result = []
    for index, item in enumerate(tracks, start=1):
        clips = []
        for position, clip in enumerate(project.track_clips(item), start=1):
            producer = project.element_by_id(clip.producer_id)
            resource = None
            clip_name = None
            if producer is not None:
                props = {
                    child.get("name", ""): (child.text or "")
                    for child in producer.findall("property")
                }
                resource = props.get("resource")
                clip_name = props.get("kdenlive:clipname")
            data = clip.describe()
            data["index"] = position
            data["name"] = clip_name
            data["resource"] = resource
            # Effects of a timeline clip live in its <entry>; a few
            # Kdenlive versions put them on the producer instead.
            effects = [
                get_property(f, "mlt_service") for f in clip.entry.findall("filter")
            ]
            if producer is not None:
                effects += [
                    get_property(f, "mlt_service") for f in producer.findall("filter")
                ]
            data["effects"] = [name for name in effects if name]
            clips.append(data)
        result.append(
            {
                "track": index,
                "track_id": item.track_id,
                "kind": item.kind,
                "name": item.name,
                "length_frames": item.length,
                "clips": clips,
            }
        )
    return _ok(
        sequence={"uuid": seq.uuid, "name": seq.name},
        fps=fps,
        duration=format_frames(seq.duration_frames, fps),
        tracks=result,
    )


@mcp.tool
def kdenlive_bin(project_path: Annotated[str | None, "Path to a .kdenlive file"] = None) -> dict:
    """List the clips stored in the project bin."""
    project = _load(project_path)
    clips = [clip.describe() for clip in project.bin_clips()]
    return _ok(count=len(clips), clips=clips)


@mcp.tool
def kdenlive_effects_on(
    project_path: Annotated[str | None, "Path to a .kdenlive file"] = None,
    scope: Annotated[str, "'clip', 'track' or 'sequence'"] = "sequence",
    sequence: Annotated[Ref | None, "Sequence uuid, name or 1-based index"] = None,
    track: Annotated[Ref | None, "Track index (1-based), name or id"] = None,
    clip: Annotated[Ref | None, "Clip id (kdenlive:id) or 1-based index on the track"] = None,
) -> dict:
    """List the effects (and transitions) applied to a clip, track or sequence.

    ``scope`` says what to inspect: a clip's own filters, the filters on a
    track, or the master effects and transitions of the sequence.
    """
    project = _load(project_path)
    seq = project.resolve_sequence(sequence)

    def describe_filters(host) -> list[dict]:
        out = []
        for element in host.findall("filter"):
            props = {
                child.get("name", ""): (child.text or "")
                for child in element.findall("property")
            }
            out.append(
                {
                    "filter_id": element.get("id"),
                    "mlt_service": props.get("mlt_service"),
                    "kdenlive_id": props.get("kdenlive_id"),
                    "internal": "internal_added" in props,
                    "parameters": {
                        k: v
                        for k, v in props.items()
                        if k not in ("mlt_service", "kdenlive_id", "internal_added")
                    },
                }
            )
        return out

    if scope == "clip" or (scope == "sequence" and clip is not None):
        target_track = project.resolve_track(seq, track) if track is not None else seq.tracks[0]
        timeline_clip = project.find_clip(target_track, clip)
        producer = project.element_by_id(timeline_clip.producer_id)
        return _ok(
            scope="clip",
            clip_id=timeline_clip.clip_id,
            producer=timeline_clip.producer_id,
            # Effects of a timeline clip live inside its <entry>; a few
            # Kdenlive versions put them on the producer instead, so both
            # are reported.
            filters=describe_filters(timeline_clip.entry)
            + (describe_filters(producer) if producer is not None else []),
        )

    if scope == "track" or (scope == "sequence" and track is not None):
        target_track = project.resolve_track(seq, track)
        return _ok(
            scope="track",
            track=target_track.index + 1,
            # Track effects live on the track tractor.
            filters=describe_filters(target_track.tractor),
        )

    return _ok(
        scope="sequence",
        sequence=seq.uuid,
        filters=describe_filters(seq.tractor),
        transitions=[
            {
                "transition_id": element.get("id"),
                "mlt_service": get_property(element, "mlt_service"),
                "a_track": get_property(element, "a_track"),
                "b_track": get_property(element, "b_track"),
                "in": element.get("in"),
                "out": element.get("out"),
            }
            for element in seq.transitions
        ],
    )


# --------------------------------------------------------------------------
# project creation
# --------------------------------------------------------------------------


@mcp.tool
def kdenlive_create_project(
    project_path: Annotated[str, "Path of the .kdenlive file to create"],
    profile: Annotated[str, "Profile name such as 1080p25, 720p30, 4k25, vertical"] = "1080p25",
    sequence_name: Annotated[str, "Name of the first timeline sequence"] = "Sequence 1",
    width: Annotated[int | None, "Override the profile width"] = None,
    height: Annotated[int | None, "Override the profile height"] = None,
    fps: Annotated[float | None, "Override the frame rate"] = None,
) -> dict:
    """Create a new, empty Kdenlive project.

    The project is created in the modern (23.04+) format with three tracks:
    two video and one audio.
    """
    target = Path(project_path).expanduser()
    if target.exists():
        raise ToolError(f"{target} already exists. Choose another path or delete it first.")

    chosen = PROFILE_PRESETS.get(profile.strip().lower())
    if chosen is None:
        raise ToolError(
            f"Unknown profile {profile!r}. Available: {', '.join(sorted(PROFILE_PRESETS))}"
        )
    if width:
        chosen.width = width
    if height:
        chosen.height = height
    if fps:
        chosen.frame_rate_num = int(round(fps * 1000))
        chosen.frame_rate_den = 1000
        chosen.description = f"{chosen.width}x{chosen.height} @ {fps:g} fps"

    project = MltProject.create(target, chosen, sequence_name=sequence_name)
    payload = project.summary()
    payload.pop("path", None)
    payload.update(_fps_context(project))
    return _ok(path=str(target), **payload)


# --------------------------------------------------------------------------
# timeline editing
# --------------------------------------------------------------------------


@mcp.tool
def kdenlive_add_track(
    project_path: Annotated[str | None, "Path to a .kdenlive file"] = None,
    kind: Annotated[str, "'video' or 'audio'"] = "video",
    name: Annotated[str | None, "Track name"] = None,
    sequence: Annotated[Ref | None, "Sequence uuid, name or 1-based index"] = None,
    position: Annotated[Frame | None, "1-based position, default is append (frames, or a timecode string)"] = None,
) -> dict:
    """Add a video or audio track to a sequence."""
    if kind not in ("video", "audio"):
        raise ToolError("kind must be 'video' or 'audio'")
    project = _load(project_path)
    seq = project.resolve_sequence(sequence)
    fps = project.fps

    index = len(seq.tracks)
    if position is not None:
        try:
            index = int(position) - 1
        except (TypeError, ValueError):
            raise ToolError(f"position must be a number, got {position!r}") from None
    label = name or f"{kind.upper()}{index + 1}"

    tractor = project.make_track_tractor(kind, label)
    tractor_id = tractor.get("id", "")
    project.root.append(tractor)
    project.insert_track_into_sequence(seq, tractor_id, index)

    _save(project)
    return _ok(
        track_id=tractor_id,
        kind=kind,
        name=label,
        position=index + 1,
        sequence=seq.uuid,
        fps=fps,
    )


@mcp.tool
def kdenlive_remove_track(
    project_path: Annotated[str | None, "Path to a .kdenlive file"] = None,
    track: Annotated[Ref | None, "Track index (1-based), name or id"] = None,
    sequence: Annotated[Ref | None, "Sequence uuid, name or 1-based index"] = None,
) -> dict:
    """Remove a track and everything on it."""
    project = _load(project_path)
    seq = project.resolve_sequence(sequence)
    target = project.resolve_track(seq, track if track is not None else 1)

    # Detach the track from the sequence.
    for element in list(seq.tractor.findall("track")):
        if element.get("producer") == target.track_id:
            seq.tractor.remove(element)

    # Drop the track tractor and its playlists from the document.
    for playlist in target.playlists:
        if playlist in list(project.root):
            project.root.remove(playlist)
    if target.tractor in list(project.root):
        project.root.remove(target.tractor)

    set_property(
        seq.tractor,
        "kdenlive:sequenceproperties.tracksCount",
        str(max(0, len(seq.tracks) - 1)),
    )
    _save(project)
    return _ok(removed=target.track_id, remaining_tracks=len(seq.tracks))


@mcp.tool
def kdenlive_add_clip(
    project_path: Annotated[str | None, "Path to a .kdenlive file"] = None,
    media_path: Annotated[str | None, "Path of the media file to add"] = None,
    track: Annotated[Ref | None, "Track index (1-based), name or id"] = 1,
    position: Annotated[Frame, "Start position in frames (frames, or a timecode string)"] = 0,
    sequence: Annotated[Ref | None, "Sequence uuid, name or 1-based index"] = None,
    in_point: Annotated[Frame | None, "Source in point in frames (frames, or a timecode string)"] = None,
    out_point: Annotated[Frame | None, "Source out point in frames (inclusive) (frames, or a timecode string)"] = None,
    duration: Annotated[Frame | None, "Length in frames; defaults to the whole media (frames, or a timecode string)"] = None,
    bin_clip: Annotated[Ref | None, "Clip id (kdenlive:id) or 1-based index on the track"] = None,
    add_to_bin: Annotated[bool, "Also register the clip in the project bin"] = True,
) -> dict:
    """Add a clip to the timeline.

    Provide either ``media_path`` (a file on disk) or ``bin_clip`` (the clip id
    of something already in the project bin). The clip is placed at ``position``
    frames and overwrites whatever is there.
    """
    project = _load(project_path)
    seq = project.resolve_sequence(sequence)
    fps = project.fps
    target_track = project.resolve_track(seq, track if track is not None else 1)
    start = coerce_frames(position, fps, default=0)

    if bin_clip:
        source = None
        for clip in project.bin_clips():
            if clip.clip_id == str(bin_clip) or clip.producer_id == str(bin_clip):
                source = clip
                break
        if source is None:
            raise ToolError(f"No bin clip with id {bin_clip!r}")
        media_length = _producer_length(project, source.element, fps)
        resource = source.resource
        chain = project.create_media_producer(
            resource or "",
            length_frames=media_length,
            clip_name=source.name,
            properties=_copy_properties(source.element),
            before=target_track.playlist,
        )
    else:
        if not media_path:
            raise ToolError("Either media_path or bin_clip is required")
        media = Path(media_path).expanduser()
        if not media.exists():
            raise MediaError(f"Media file not found: {media}")
        info = probe(media)
        media_length = info.duration_frames(fps)
        chain = project.create_media_producer(
            str(media.resolve()),
            length_frames=media_length,
            clip_name=media.name,
            properties={
                "kdenlive:originalurl": str(media.resolve()),
                "meta.media.nb_streams": str(int(info.has_video) + int(info.has_audio)),
                "meta.media.width": str(info.width or ""),
                "meta.media.height": str(info.height or ""),
                "meta.media.frame_rate_num": str(info.fps or ""),
                "meta.media.duration": str(info.duration_seconds),
            },
            before=target_track.playlist,
        )

    chain_id = chain.get("id", "")
    total = _producer_length(project, chain, fps)

    start_in = coerce_frames(in_point, fps, default=0) if in_point is not None else 0
    if duration is not None:
        length = coerce_frames(duration, fps, default=total)
    elif out_point is not None:
        end = coerce_frames(out_point, fps, default=total - 1)
        length = max(1, end - start_in + 1)
    else:
        length = max(1, total - start_in)
    end_in = start_in + length - 1
    if end_in > total - 1:
        end_in = max(0, total - 1)
        length = max(1, end_in - start_in + 1)

    entry = project.make_entry(
        chain_id,
        in_frames=start_in,
        out_frames=end_in,
    )
    project.playlist_insert(target_track.playlist, entry, start)

    if add_to_bin and not bin_clip:
        # A clip that came from the bin is already listed there; adding the
        # new timeline instance again would duplicate it.
        project.add_to_bin(chain_id)

    project.set_property_on_sequence(seq, "kdenlive:duration", format_frames(
        max(project.playlist_length(target_track.playlist), seq.duration_frames), fps
    ))
    _save(project)

    return _ok(
        clip_id=get_property(entry, "kdenlive:id"),
        producer=chain_id,
        track=target_track.index + 1,
        position=start,
        length_frames=length,
        in_point=format_clock(start_in, fps),
        out_point=format_clock(end_in, fps),
        media=media_path or bin_clip,
    )


@mcp.tool
def kdenlive_remove_clip(
    project_path: Annotated[str | None, "Path to a .kdenlive file"] = None,
    clip: Annotated[Ref | None, "Clip id (kdenlive:id) or 1-based index on the track"] = None,
    track: Annotated[Ref | None, "Track index (1-based), name or id"] = 1,
    sequence: Annotated[Ref | None, "Sequence uuid, name or 1-based index"] = None,
) -> dict:
    """Remove a clip from the timeline, leaving a gap behind."""
    project = _load(project_path)
    seq = project.resolve_sequence(sequence)
    target_track = project.resolve_track(seq, track if track is not None else 1)
    timeline_clip = project.find_clip(target_track, clip if clip is not None else 1)

    length = project.playlist_remove_entry(timeline_clip.entry)
    project.merge_blanks(target_track.playlist)
    _save(project)
    return _ok(
        removed=timeline_clip.clip_id,
        position=timeline_clip.position,
        length_frames=length,
    )


@mcp.tool
def kdenlive_move_clip(
    project_path: Annotated[str | None, "Path to a .kdenlive file"] = None,
    clip: Annotated[Ref | None, "Clip id (kdenlive:id) or 1-based index on the track"] = None,
    track: Annotated[Ref | None, "Track index (1-based), name or id"] = 1,
    new_track: Annotated[Ref | None, "Track index (1-based), name or id"] = None,
    new_position: Annotated[Frame, "New start position in frames (frames, or a timecode string)"] = 0,
    sequence: Annotated[Ref | None, "Sequence uuid, name or 1-based index"] = None,
) -> dict:
    """Move a clip to another position, or to another track."""
    project = _load(project_path)
    seq = project.resolve_sequence(sequence)
    fps = project.fps
    source_track = project.resolve_track(seq, track if track is not None else 1)
    timeline_clip = project.find_clip(source_track, clip if clip is not None else 1)
    destination_track = (
        project.resolve_track(seq, new_track) if new_track is not None else source_track
    )
    start = coerce_frames(new_position, fps, default=0)

    entry = timeline_clip.entry
    length = project.playlist_remove_entry(entry)
    project.merge_blanks(source_track.playlist)
    project.playlist_insert(destination_track.playlist, entry, start)

    _save(project)
    return _ok(
        clip_id=timeline_clip.clip_id,
        from_track=source_track.index + 1,
        to_track=destination_track.index + 1,
        position=start,
        length_frames=length,
    )


@mcp.tool
def kdenlive_trim_clip(
    project_path: Annotated[str | None, "Path to a .kdenlive file"] = None,
    clip: Annotated[Ref | None, "Clip id (kdenlive:id) or 1-based index on the track"] = None,
    track: Annotated[Ref | None, "Track index (1-based), name or id"] = 1,
    sequence: Annotated[Ref | None, "Sequence uuid, name or 1-based index"] = None,
    new_in: Annotated[Frame | None, "New source in point in frames (frames, or a timecode string)"] = None,
    new_out: Annotated[Frame | None, "New source out point in frames (inclusive) (frames, or a timecode string)"] = None,
    new_duration: Annotated[Frame | None, "New length in frames (frames, or a timecode string)"] = None,
) -> dict:
    """Trim a clip by changing its in and/or out point."""
    project = _load(project_path)
    seq = project.resolve_sequence(sequence)
    fps = project.fps
    target_track = project.resolve_track(seq, track if track is not None else 1)
    timeline_clip = project.find_clip(target_track, clip if clip is not None else 1)

    start_in = coerce_frames(new_in, fps, default=timeline_clip.in_frames) if new_in is not None else timeline_clip.in_frames
    if new_out is not None:
        end_in = coerce_frames(new_out, fps, default=timeline_clip.out_frames)
    elif new_duration is not None:
        end_in = start_in + coerce_frames(new_duration, fps, default=timeline_clip.length) - 1
    else:
        end_in = timeline_clip.out_frames

    if end_in < start_in:
        raise ToolError("The out point must not be before the in point")

    producer = project.element_by_id(timeline_clip.producer_id)
    total = _producer_length(project, producer, fps) if producer is not None else end_in + 1
    if end_in >= total:
        raise ToolError(
            f"The out point ({end_in}) is beyond the end of the source media ({total - 1} frames)"
        )

    timeline_clip.entry.set("in", format_clock(start_in, fps))
    timeline_clip.entry.set("out", format_clock(end_in, fps))
    _save(project)
    return _ok(
        clip_id=timeline_clip.clip_id,
        in_point=format_clock(start_in, fps),
        out_point=format_clock(end_in, fps),
        length_frames=end_in - start_in + 1,
    )


@mcp.tool
def kdenlive_split_clip(
    project_path: Annotated[str | None, "Path to a .kdenlive file"] = None,
    clip: Annotated[Ref | None, "Clip id (kdenlive:id) or 1-based index on the track"] = None,
    track: Annotated[Ref | None, "Track index (1-based), name or id"] = 1,
    position: Annotated[Frame, "Frame (relative to the clip start) to split at (frames, or a timecode string)"] = 0,
    sequence: Annotated[Ref | None, "Sequence uuid, name or 1-based index"] = None,
) -> dict:
    """Split a clip in two at the given offset."""
    project = _load(project_path)
    seq = project.resolve_sequence(sequence)
    fps = project.fps
    target_track = project.resolve_track(seq, track if track is not None else 1)
    timeline_clip = project.find_clip(target_track, clip if clip is not None else 1)

    offset = coerce_frames(position, fps, default=0)
    if offset <= 0 or offset >= timeline_clip.length:
        raise ToolError(
            f"The split position must be inside the clip (0 < position < {timeline_clip.length})"
        )

    entry = timeline_clip.entry
    split_source = timeline_clip.in_frames + offset
    first_length = offset
    second_length = timeline_clip.length - offset

    # The second half needs its own producer so it can carry its own effects.
    producer = project.element_by_id(timeline_clip.producer_id)
    if producer is not None:
        second = project.create_media_producer(
            get_property(producer, "resource") or "",
            length_frames=_producer_length(project, producer, fps),
            service=get_property(producer, "mlt_service") or "avformat-novalidate",
            clip_name=get_property(producer, "kdenlive:clipname"),
            properties=_copy_properties(producer),
            before=target_track.playlist,
        )
        second_id = second.get("id", "")
    else:
        second_id = timeline_clip.producer_id

    entry.set("out", format_clock(timeline_clip.in_frames + first_length - 1, fps))
    second_entry = project.make_entry(
        second_id,
        in_frames=split_source,
        out_frames=timeline_clip.out_frames,
    )
    # Kdenlive keeps the effects on both halves of a split.
    for existing in entry.findall("filter"):
        import copy as _copy

        second_entry.append(_copy.deepcopy(existing))
    project.playlist_insert(target_track.playlist, second_entry, timeline_clip.position + first_length)
    _save(project)
    return _ok(
        first_clip_id=timeline_clip.clip_id,
        second_clip_id=get_property(second_entry, "kdenlive:id"),
        split_at=timeline_clip.position + first_length,
        first_length=first_length,
        second_length=second_length,
    )


@mcp.tool
def kdenlive_set_clip_property(
    project_path: Annotated[str | None, "Path to a .kdenlive file"] = None,
    clip: Annotated[Ref | None, "Clip id (kdenlive:id) or 1-based index on the track"] = None,
    track: Annotated[Ref | None, "Track index (1-based), name or id"] = 1,
    sequence: Annotated[Ref | None, "Sequence uuid, name or 1-based index"] = None,
    properties: Annotated[dict[str, str], "Property names and values to set"] = {},  # noqa: B006
    scope: Annotated[str, "'clip' writes to the clip's producer, 'entry' to the timeline entry"] = "clip",
) -> dict:
    """Set properties on a clip.

    By default the properties are written to the clip's producer -- that is
    where Kdenlive keeps ``kdenlive:clipname`` and every other clip
    property. Use ``scope="entry"`` to write to the timeline entry instead.
    """
    if not properties:
        raise ToolError("properties must not be empty")
    project = _load(project_path)
    seq = project.resolve_sequence(sequence)
    target_track = project.resolve_track(seq, track if track is not None else 1)
    timeline_clip = project.find_clip(target_track, clip if clip is not None else 1)

    if scope == "entry":
        host = timeline_clip.entry
    elif scope == "clip":
        host = project.element_by_id(timeline_clip.producer_id)
        if host is None:
            raise ProjectError(f"Producer {timeline_clip.producer_id!r} not found in the project")
    else:
        raise ToolError("scope must be 'clip' or 'entry'")

    for key, value in properties.items():
        set_property(host, key, str(value))
    _save(project)
    return _ok(clip_id=timeline_clip.clip_id, scope=scope, updated=properties)


@mcp.tool
def kdenlive_set_track_property(
    project_path: Annotated[str | None, "Path to a .kdenlive file"] = None,
    track: Annotated[Ref | None, "Track index (1-based), name or id"] = 1,
    sequence: Annotated[Ref | None, "Sequence uuid, name or 1-based index"] = None,
    properties: Annotated[dict[str, str], "Property names and values to set"] = {},  # noqa: B006
) -> dict:
    """Set properties on a track.

    Useful keys: ``kdenlive:track_name``, ``kdenlive:locked_track``,
    ``hide`` (1 hides video, 2 hides audio).
    """
    if not properties:
        raise ToolError("properties must not be empty")
    project = _load(project_path)
    seq = project.resolve_sequence(sequence)
    target_track = project.resolve_track(seq, track if track is not None else 1)

    for key, value in properties.items():
        set_property(target_track.tractor, key, str(value))
        if key == "hide":
            set_property(target_track.playlist, "hide", str(value))
    _save(project)
    return _ok(track=target_track.index + 1, updated=properties)


# --------------------------------------------------------------------------
# effects and transitions
# --------------------------------------------------------------------------


@mcp.tool
def kdenlive_add_effect(
    project_path: Annotated[str | None, "Path to a .kdenlive file"] = None,
    effect: Annotated[str, "mlt_service name, for example qtblend or avfilter.gain"] = "",
    scope: Annotated[str, "'clip', 'track' or 'sequence'"] = "clip",
    clip: Annotated[Ref | None, "Clip id (kdenlive:id) or 1-based index on the track"] = None,
    track: Annotated[Ref | None, "Track index (1-based), name or id"] = None,
    sequence: Annotated[Ref | None, "Sequence uuid, name or 1-based index"] = None,
    parameters: Annotated[dict[str, str], "Effect parameters"] = {},  # noqa: B006
) -> dict:
    """Add an effect (MLT filter) to a clip, a track or the whole sequence.

    Look the effect up with ``kdenlive_effects_catalog`` first: the service
    name and the parameter names must match exactly.

    * clip scope -> the filter is attached to the clip's producer,
    * track scope -> the filter is attached to the track's first playlist,
    * sequence scope -> the filter is attached to the sequence tractor.
    """
    if not effect:
        raise ToolError("effect is required")
    info = effects_module.lookup(effect)
    if parameters:
        effects_module.validate_parameters(info.mlt_service, parameters)

    project = _load(project_path)
    seq = project.resolve_sequence(sequence)

    if scope == "clip":
        target_track = project.resolve_track(seq, track if track is not None else 1)
        timeline_clip = project.find_clip(target_track, clip if clip is not None else 1)
        # Kdenlive stores the effects of a timeline clip inside its <entry>,
        # not on the producer: the same producer may be used by several
        # entries and each instance carries its own effects.
        host = timeline_clip.entry
        target = {"clip_id": timeline_clip.clip_id}
    elif scope == "track":
        target_track = project.resolve_track(seq, track if track is not None else 1)
        # Track effects belong to the track's tractor. Only the internally
        # added audio mixer effects live on the tractor as well; everything
        # the user adds is written here.
        host = target_track.tractor
        target = {"track": target_track.index + 1}
    elif scope == "sequence":
        host = seq.tractor
        target = {"sequence": seq.uuid}
    else:
        raise ToolError("scope must be 'clip', 'track' or 'sequence'")

    element = project.add_filter(host, info.mlt_service, parameters)
    _save(project)
    return _ok(
        filter_id=element.get("id"),
        mlt_service=info.mlt_service,
        name=info.name,
        scope=scope,
        parameters=parameters,
        **target,
    )


@mcp.tool
def kdenlive_remove_effect(
    project_path: Annotated[str | None, "Path to a .kdenlive file"] = None,
    filter_id: Annotated[str | None, "Id of the filter to remove"] = None,
    scope: Annotated[str, "'clip', 'track' or 'sequence'"] = "clip",
    clip: Annotated[Ref | None, "Clip id (kdenlive:id) or 1-based index on the track"] = None,
    track: Annotated[Ref | None, "Track index (1-based), name or id"] = None,
    sequence: Annotated[Ref | None, "Sequence uuid, name or 1-based index"] = None,
) -> dict:
    """Remove an effect by its filter id."""
    if not filter_id:
        raise ToolError("filter_id is required")
    project = _load(project_path)
    seq = project.resolve_sequence(sequence)

    if scope == "clip":
        target_track = project.resolve_track(seq, track if track is not None else 1)
        timeline_clip = project.find_clip(target_track, clip if clip is not None else 1)
        host = timeline_clip.entry
    elif scope == "track":
        host = project.resolve_track(seq, track if track is not None else 1).tractor
    elif scope == "sequence":
        host = seq.tractor
    else:
        raise ToolError("scope must be 'clip', 'track' or 'sequence'")

    if host is None:
        raise ProjectError("Could not find the element holding the effect")
    for element in list(host.findall("filter")):
        if element.get("id") == filter_id:
            host.remove(element)
            _save(project)
            return _ok(removed=filter_id, scope=scope)
    raise ToolError(f"No filter with id {filter_id!r} in this {scope}")


@mcp.tool
def kdenlive_add_transition(
    project_path: Annotated[str | None, "Path to a .kdenlive file"] = None,
    transition: Annotated[str, "mlt_service name, for example luma or qtblend"] = "",
    track_a: Annotated[int, "1-based index of the upper track"] = 1,
    track_b: Annotated[int, "1-based index of the lower track"] = 2,
    position: Annotated[Frame, "Position in frames of the transition (frames, or a timecode string)"] = 0,
    duration: Annotated[Frame, "Length of the transition in frames (frames, or a timecode string)"] = 25,
    sequence: Annotated[Ref | None, "Sequence uuid, name or 1-based index"] = None,
    parameters: Annotated[dict[str, str], "Transition parameters"] = {},  # noqa: B006
) -> dict:
    """Add a transition between two tracks.

    ``track_a`` is the upper track and ``track_b`` the lower one, matching the
    way Kdenlive numbers them (video tracks first, then audio tracks).
    """
    if not transition:
        raise ToolError("transition is required")
    info = effects_module.lookup(transition)
    if parameters:
        effects_module.validate_parameters(info.mlt_service, parameters)

    project = _load(project_path)
    seq = project.resolve_sequence(sequence)
    fps = project.fps

    length = coerce_frames(duration, fps, default=25)
    start = coerce_frames(position, fps, default=0)
    if length <= 0:
        raise ToolError("The transition duration must be positive")

    element = project.add_transition(
        seq,
        info.mlt_service,
        a_track=_track_number(track_a),
        b_track=_track_number(track_b),
        in_frames=start,
        out_frames=start + length - 1,
        parameters=parameters,
    )
    _save(project)
    return _ok(
        transition_id=element.get("id"),
        mlt_service=info.mlt_service,
        name=info.name,
        a_track=track_a,
        b_track=track_b,
        in_point=format_clock(start, fps),
        out_point=format_clock(start + length - 1, fps),
        duration_frames=length,
    )


@mcp.tool
def kdenlive_remove_transition(
    project_path: Annotated[str | None, "Path to a .kdenlive file"] = None,
    transition_id: Annotated[str | None, "Id of the transition to remove"] = None,
    sequence: Annotated[Ref | None, "Sequence uuid, name or 1-based index"] = None,
) -> dict:
    """Remove a transition from a sequence."""
    if not transition_id:
        raise ToolError("transition_id is required")
    project = _load(project_path)
    seq = project.resolve_sequence(sequence)
    for element in list(seq.transitions):
        if element.get("id") == transition_id:
            seq.tractor.remove(element)
            _save(project)
            return _ok(removed=transition_id)
    raise ToolError(f"No transition with id {transition_id!r} in sequence {seq.uuid}")


# --------------------------------------------------------------------------
# media
# --------------------------------------------------------------------------


@mcp.tool
def kdenlive_probe_media(media_path: Annotated[str, "Path of the media file"] = "") -> dict:
    """Inspect a media file: duration, frame rate, resolution and streams."""
    if not media_path:
        raise ToolError("media_path is required")
    info = probe(media_path)
    return _ok(**info.describe())


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


@mcp.tool
def kdenlive_render(
    project_path: Annotated[str | None, "Path to a .kdenlive file"] = None,
    output_path: Annotated[str | None, "Output file; defaults to the project name with .mp4"] = None,
    video_codec: Annotated[str, "Video codec, for example libx264 or libx265"] = "libx264",
    audio_codec: Annotated[str, "Audio codec, for example aac"] = "aac",
    crf: Annotated[int | None, "Constant rate factor (lower is better quality)"] = 23,
    preset: Annotated[str | None, "x264 preset, for example fast or slow"] = None,
    bitrate: Annotated[str | None, "Target bitrate, for example 8M"] = None,
    scale: Annotated[str | None, "Output size, for example 1280x720"] = None,
    extra_args: Annotated[list[str] | None, "Extra arguments passed straight to melt"] = None,
) -> dict:
    """Render the project to a video file using the MLT renderer.

    The render runs in the background; poll ``kdenlive_render_status`` with
    the returned ``job_id``.
    """
    project = _load(project_path)
    if project.path is None:
        raise ProjectError("Project has no path")

    output = Path(output_path).expanduser() if output_path else project.path.with_suffix(".mp4")
    render_module.ensure_output_dir(output)

    request = render_module.RenderRequest(
        project=project.path,
        output=output,
        video_codec=video_codec,
        audio_codec=audio_codec,
        crf=crf,
        preset=preset,
        bitrate=bitrate,
        scale=scale,
        extra_args=list(extra_args or []),
    )
    job = render_module.manager().start(request)
    return _ok(**job.describe())


@mcp.tool
def kdenlive_render_status(job_id: Annotated[str, "Job id returned by kdenlive_render"] = "") -> dict:
    """Check the progress of a render job."""
    if not job_id:
        raise ToolError("job_id is required")
    job = render_module.manager().get(job_id)
    return _ok(**job.describe())


@mcp.tool
def kdenlive_render_cancel(job_id: Annotated[str, "Job id returned by kdenlive_render"] = "") -> dict:
    """Cancel a running render job."""
    if not job_id:
        raise ToolError("job_id is required")
    job = render_module.manager().cancel(job_id)
    return _ok(**job.describe())


# --------------------------------------------------------------------------
# bridge (WSL / remote Kdenlive)
# --------------------------------------------------------------------------


@mcp.tool
def kdenlive_bridge_status() -> dict:
    """Check whether a Kdenlive bridge is reachable.

    The bridge runs on the machine that actually has Kdenlive installed. It is
    how the MCP drives a Kdenlive running on Windows while the MCP itself runs
    inside WSL.
    """
    return _ok(**BridgeClient().health().describe())


@mcp.tool
def kdenlive_bridge_projects(directory: Annotated[str, "Directory to scan for .kdenlive files"] = "") -> dict:
    """List Kdenlive projects on the machine running the bridge."""
    client = BridgeClient()
    client.resolve()
    return _ok(projects=client.list_projects(directory))


@mcp.tool
def kdenlive_bridge_open(project_path: Annotated[str, "Path of the project on the bridge machine"] = "") -> dict:
    """Open a project in the Kdenlive GUI running on the bridge machine."""
    if not project_path:
        raise ToolError("project_path is required")
    client = BridgeClient()
    client.resolve()
    return _ok(**client.open_in_kdenlive(project_path))


@mcp.tool
def kdenlive_bridge_render(
    project_path: Annotated[str, "Path of the project on the bridge machine"] = "",
    output_path: Annotated[str | None, "Output file on the bridge machine"] = None,
    video_codec: Annotated[str, "Video codec"] = "libx264",
    audio_codec: Annotated[str, "Audio codec"] = "aac",
    crf: Annotated[int | None, "Constant rate factor"] = 23,
) -> dict:
    """Render a project on the machine running the bridge."""
    if not project_path:
        raise ToolError("project_path is required")
    client = BridgeClient()
    client.resolve()
    payload = client.render(
        project_path,
        output_path or "",
        video_codec=video_codec,
        audio_codec=audio_codec,
        crf=crf,
    )
    return _ok(**payload)


@mcp.tool
def kdenlive_path_translate(
    path: Annotated[str, "Path to translate"] = "",
    direction: Annotated[str, "'to_windows', 'to_wsl' or 'native'"] = "native",
) -> dict:
    """Translate a path between WSL, Windows and the local filesystem.

    Useful when the MCP runs inside WSL but the project lives on a Windows
    drive: ``/mnt/c/Videos/p.kdenlive`` <-> ``C:\\Videos\\p.kdenlive``.
    """
    if not path:
        raise ToolError("path is required")
    if direction == "to_windows":
        result = to_windows_path(path)
    elif direction == "to_wsl":
        result = to_wsl_path(path)
    elif direction == "native":
        client = BridgeClient()
        if not client.health().reachable:
            raise ToolError("No bridge is reachable, so the native path is unknown")
        result = client.native_path(path)
    else:
        raise ToolError("direction must be 'to_windows', 'to_wsl' or 'native'")
    return _ok(path=path, direction=direction, translated=result)


# --------------------------------------------------------------------------
# internal helpers
# --------------------------------------------------------------------------


def _track_number(value: object) -> int:
    """A track index as an integer.

    Transitions reference their tracks by number, so a name has to be
    resolved elsewhere; only plain numbers are accepted here.
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ToolError(f"Track numbers must be numeric, got {value!r}") from None


def _producer_length(project: MltProject, producer, fps: float) -> int:
    """Length in frames of a producer/chain element."""
    if producer is None:
        return 1
    length = get_property(producer, "length")
    if length and length.strip().isdigit():
        return max(1, int(length.strip()))
    out = producer.get("out")
    if out:
        from .timecode import parse_clock

        return max(1, parse_clock(out, fps) + 1)
    return 1


def _copy_properties(producer) -> dict[str, str]:
    """Copy the non-structural properties of a producer."""
    skip = {
        "mlt_service",
        "resource",
        "length",
        "eof",
        "seekable",
        "kdenlive:id",
        "kdenlive:folderid",
    }
    return {
        child.get("name", ""): (child.text or "")
        for child in producer.findall("property")
        if child.get("name", "") not in skip
    }


def main() -> None:  # pragma: no cover - entry point
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
