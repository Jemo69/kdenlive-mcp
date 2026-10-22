"""A pure-python model of the Kdenlive project format.

Kdenlive stores a project as MLT XML (``.kdenlive``). The file is a plain XML
document, which is exactly what makes it scriptable: everything the editor
knows about a project -- clips, tracks, effects, transitions, timing -- is
expressible without a GUI, without IPC and without a running application.

This module implements the *modern* (generation 5, Kdenlive 23.04+) layout
described in ``dev-docs/fileformat.md`` of the upstream repository:

.. code-block:: text

    <mlt producer="main_bin" ...>
      <profile .../>                     <!-- frame size / rate -->
      <chain id="chain0" .../>           <!-- bin clips + clip instances -->
      <playlist id="playlist0">         <!-- track content: entries + blanks -->
        <blank length="00:00:01.000"/>
        <entry producer="chain0" in="..." out="..."/>
      </playlist>
      <tractor id="tractor0">           <!-- one Kdenlive track -->
        <track hide="audio" producer="playlist0"/>
        <track hide="audio" producer="playlist1"/>
      </tractor>
      <tractor id="{uuid}">             <!-- a timeline sequence -->
        <property name="kdenlive:uuid">...</property>
        <track producer="black_track"/>
        <track producer="tractor0"/>
        <transition id="transition0">...</transition>
      </tractor>
      <playlist id="main_bin">          <!-- project bin + doc properties -->
        <property name="kdenlive:docproperties.version">1.1</property>
      </playlist>
      <tractor id="tractor9">           <!-- project tractor: always last -->
        <property name="kdenlive:projectTractor">1</property>
        <track producer="{uuid}"/>
      </tractor>
    </mlt>
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid as uuid_mod
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import KdenliveError, ProjectError
from .timecode import format_clock, format_frames, parse_clock, parse_duration

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

MAIN_BIN_ID = "main_bin"
PROJECT_TRACTOR_PROP = "kdenlive:projectTractor"
SEQUENCE_UUID_PROP = "kdenlive:uuid"
CLIP_ID_PROP = "kdenlive:id"
DOC_VERSION_PROP = "kdenlive:docproperties.version"
DOC_PATCH_VERSION_PROP = "kdenlive:docproperties.patchversion"

#: Current Kdenlive document version (see ``src/doc/kdenlivedoc.cpp``).
DOCUMENT_VERSION = "1.1"
DOCUMENT_PATCH_VERSION = "1"

#: Property that marks a filter as one Kdenlive added itself.
INTERNAL_ADDED = "internal_added"

#: Service used for the background / "black" track.
BACKGROUND_SERVICE = "color"


# --------------------------------------------------------------------------
# profiles
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Profile:
    """Video profile: frame size, frame rate and aspect information."""

    width: int = 1920
    height: int = 1080
    frame_rate_num: int = 25
    frame_rate_den: int = 1
    sample_aspect_num: int = 1
    sample_aspect_den: int = 1
    display_aspect_num: int = 16
    display_aspect_den: int = 9
    colorspace: int = 709
    progressive: int = 1
    description: str = "HD 1080p 25 fps"

    @property
    def fps(self) -> float:
        """Effective frame rate in frames per second."""
        if not self.frame_rate_den:
            return float(self.frame_rate_num)
        return self.frame_rate_num / self.frame_rate_den

    def to_attributes(self) -> dict[str, str]:
        return {
            "width": str(self.width),
            "height": str(self.height),
            "frame_rate_num": str(self.frame_rate_num),
            "frame_rate_den": str(self.frame_rate_den),
            "sample_aspect_num": str(self.sample_aspect_num),
            "sample_aspect_den": str(self.sample_aspect_den),
            "display_aspect_num": str(self.display_aspect_num),
            "display_aspect_den": str(self.display_aspect_den),
            "colorspace": str(self.colorspace),
            "progressive": str(self.progressive),
            "description": self.description,
        }

    @classmethod
    def from_attributes(cls, attrs: dict[str, str]) -> Profile:
        def as_int(key: str, default: int) -> int:
            try:
                return int(attrs.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            width=as_int("width", 1920),
            height=as_int("height", 1080),
            frame_rate_num=as_int("frame_rate_num", 25),
            frame_rate_den=as_int("frame_rate_den", 1),
            sample_aspect_num=as_int("sample_aspect_num", 1),
            sample_aspect_den=as_int("sample_aspect_den", 1),
            display_aspect_num=as_int("display_aspect_num", 16),
            display_aspect_den=as_int("display_aspect_den", 9),
            colorspace=as_int("colorspace", 709),
            progressive=as_int("progressive", 1),
            description=attrs.get("description", ""),
        )

    def describe(self) -> str:
        return (
            f"{self.width}x{self.height} @ {self.fps:g} fps "
            f"({self.description or 'custom profile'})"
        )


#: Ready made profiles, keyed by a friendly name.
PROFILE_PRESETS: dict[str, Profile] = {
    "1080p25": Profile(
        width=1920, height=1080, frame_rate_num=25, frame_rate_den=1,
        display_aspect_num=16, display_aspect_den=9, colorspace=709, progressive=1,
        description="HD 1080p 25 fps",
    ),
    "1080p30": Profile(
        width=1920, height=1080, frame_rate_num=30, frame_rate_den=1,
        display_aspect_num=16, display_aspect_den=9, colorspace=709, progressive=1,
        description="HD 1080p 30 fps",
    ),
    "1080p60": Profile(
        width=1920, height=1080, frame_rate_num=60, frame_rate_den=1,
        display_aspect_num=16, display_aspect_den=9, colorspace=709, progressive=1,
        description="HD 1080p 60 fps",
    ),
    "720p25": Profile(
        width=1280, height=720, frame_rate_num=25, frame_rate_den=1,
        display_aspect_num=16, display_aspect_den=9, colorspace=709, progressive=1,
        description="HD 720p 25 fps",
    ),
    "720p30": Profile(
        width=1280, height=720, frame_rate_num=30, frame_rate_den=1,
        display_aspect_num=16, display_aspect_den=9, colorspace=709, progressive=1,
        description="HD 720p 30 fps",
    ),
    "4k25": Profile(
        width=3840, height=2160, frame_rate_num=25, frame_rate_den=1,
        display_aspect_num=16, display_aspect_den=9, colorspace=709, progressive=1,
        description="UHD 4K 25 fps",
    ),
    "4k30": Profile(
        width=3840, height=2160, frame_rate_num=30, frame_rate_den=1,
        display_aspect_num=16, display_aspect_den=9, colorspace=709, progressive=1,
        description="UHD 4K 30 fps",
    ),
    "pal_dvd": Profile(
        width=720, height=576, frame_rate_num=25, frame_rate_den=1,
        sample_aspect_num=16, sample_aspect_den=15,
        display_aspect_num=4, display_aspect_den=3, colorspace=601, progressive=0,
        description="DV/DVD PAL",
    ),
    "square": Profile(
        width=1080, height=1080, frame_rate_num=25, frame_rate_den=1,
        display_aspect_num=1, display_aspect_den=1, colorspace=709, progressive=1,
        description="Square 1080x1080 25 fps",
    ),
    "vertical": Profile(
        width=1080, height=1920, frame_rate_num=25, frame_rate_den=1,
        display_aspect_num=9, display_aspect_den=16, colorspace=709, progressive=1,
        description="Vertical 1080x1920 25 fps",
    ),
}


# --------------------------------------------------------------------------
# small XML helpers
# --------------------------------------------------------------------------


def get_property(element: ET.Element | None, name: str) -> str | None:
    """Return the text of a child ``<property name=...>`` or ``None``."""
    if element is None:
        return None
    for child in element.findall("property"):
        if child.get("name") == name:
            return child.text if child.text is not None else ""
    return None


def set_property(element: ET.Element, name: str, value: str | None) -> ET.Element:
    """Set (or create) a child ``<property name=...>``."""
    for child in element.findall("property"):
        if child.get("name") == name:
            child.text = value
            return child
    prop = ET.SubElement(element, "property")
    prop.set("name", name)
    prop.text = value
    return prop


def remove_property(element: ET.Element, name: str) -> bool:
    """Remove a child ``<property>``; returns ``True`` when one was removed."""
    for child in list(element.findall("property")):
        if child.get("name") == name:
            element.remove(child)
            return True
    return False


def properties_of(element: ET.Element) -> dict[str, str]:
    """All child properties of an element as a dict."""
    return {
        child.get("name", ""): (child.text or "")
        for child in element.findall("property")
    }


# --------------------------------------------------------------------------
# value objects
# --------------------------------------------------------------------------


@dataclass(slots=True)
class TimelineClip:
    """A clip placed on the timeline (an ``<entry>`` inside a ``<playlist>``)."""

    entry: ET.Element
    producer_id: str
    position: int
    length: int
    in_frames: int
    out_frames: int
    fps: float

    @property
    def clip_id(self) -> str | None:
        """The Kdenlive clip id (``kdenlive:id``) if present."""
        return get_property(self.entry, CLIP_ID_PROP)

    @property
    def properties(self) -> dict[str, str]:
        return properties_of(self.entry)

    @property
    def start_timecode(self) -> str:
        return format_clock(self.position, self.fps)

    @property
    def duration_timecode(self) -> str:
        return format_clock(self.length, self.fps)

    def describe(self) -> dict[str, object]:
        return {
            "clip_id": self.clip_id,
            "producer": self.producer_id,
            "position_frame": self.position,
            "start": self.start_timecode,
            "length_frames": self.length,
            "duration": self.duration_timecode,
            "in": format_clock(self.in_frames, self.fps),
            "out": format_clock(self.out_frames, self.fps),
        }


@dataclass(slots=True)
class Track:
    """A Kdenlive timeline track.

    A track is a ``<tractor>`` holding one or two ``<playlist>`` elements. The
    second playlist only exists when the track contains "mixes" (same track
    transitions) and can be ignored for basic editing.
    """

    tractor: ET.Element
    index: int
    kind: str  # "video" | "audio"
    name: str
    playlists: list[ET.Element] = field(default_factory=list)
    project: MltProject | None = None

    @property
    def playlist(self) -> ET.Element:
        """The primary playlist holding the track's clips."""
        if not self.playlists:
            raise ProjectError(f"Track {self.index} has no playlist")
        return self.playlists[0]

    @property
    def track_id(self) -> str:
        return self.tractor.get("id", "")

    @property
    def length(self) -> int:
        """Length of the track in frames."""
        if self.project is None or not self.playlists:
            return 0
        return self.project.playlist_length(self.playlist)

    @property
    def is_muted(self) -> bool:
        """``True`` when the track's audio is hidden."""
        hide = self.tractor.get("hide")
        if hide is None:
            hide = get_property(self.playlist, "hide") or ""
        return "audio" in hide or hide.strip() == "2"

    @property
    def is_hidden(self) -> bool:
        hide = self.tractor.get("hide")
        if hide is None:
            hide = get_property(self.playlist, "hide") or ""
        return "video" in hide or hide.strip() == "1"

    @property
    def is_locked(self) -> bool:
        return (get_property(self.tractor, "kdenlive:locked_track") or "").strip() not in ("", "0")

    @property
    def properties(self) -> dict[str, str]:
        return properties_of(self.tractor)

    def describe(self) -> dict[str, object]:
        return {
            "index": self.index,
            "track_id": self.track_id,
            "kind": self.kind,
            "name": self.name,
            "muted": self.is_muted,
            "hidden": self.is_hidden,
            "locked": self.is_locked,
            "length_frames": self.length,
        }


@dataclass(slots=True)
class Sequence:
    """A timeline sequence (a ``<tractor>`` carrying a ``kdenlive:uuid``)."""

    tractor: ET.Element
    uuid: str
    name: str
    tracks: list[Track] = field(default_factory=list)
    project: MltProject | None = None
    _duration_frames: int = 0

    @property
    def transitions(self) -> list[ET.Element]:
        return self.tractor.findall("transition")

    @property
    def properties(self) -> dict[str, str]:
        return properties_of(self.tractor)

    @property
    def duration_frames(self) -> int:
        """Length of the sequence in frames.

        Read from the tractor so the value stays correct even when the
        project has been edited since the sequence was handed out.
        """
        stored = get_property(self.tractor, "kdenlive:duration")
        if stored:
            try:
                fps = self.project.fps if self.project else 25.0
                return parse_duration(stored, fps)
            except Exception:  # noqa: BLE001 - fall back to the cached value
                pass
        return self._duration_frames

    @duration_frames.setter
    def duration_frames(self, value: int) -> None:
        self._duration_frames = int(value)

    def describe(self, fps: float) -> dict[str, object]:
        return {
            "uuid": self.uuid,
            "name": self.name,
            "duration_frames": self.duration_frames,
            "duration": format_frames(self.duration_frames, fps),
            "tracks": [track.describe() for track in self.tracks],
            "transition_count": len(self.transitions),
        }


@dataclass(slots=True)
class BinClip:
    """A clip stored in the project bin (``main_bin``)."""

    producer_id: str
    element: ET.Element
    name: str
    resource: str | None
    clip_type: str

    @property
    def clip_id(self) -> str | None:
        return get_property(self.element, CLIP_ID_PROP)

    def describe(self) -> dict[str, object]:
        return {
            "clip_id": self.clip_id,
            "producer": self.producer_id,
            "name": self.name,
            "resource": self.resource,
            "clip_type": self.clip_type,
            "duration": get_property(self.element, "length"),
        }


# --------------------------------------------------------------------------
# the project
# --------------------------------------------------------------------------


class MltProject:
    """An in-memory Kdenlive project backed by MLT XML."""

    def __init__(self, root: ET.Element, path: Path | None = None) -> None:
        self.root = root
        self.path = path
        self._dirty = False
        #: The raw bytes the project was loaded from, used by the undo history
        #: to know what the file looked like before an edit.
        self.source_bytes: bytes | None = None

    # -- construction ------------------------------------------------------

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> MltProject:
        """Load a ``.kdenlive`` project from disk."""
        p = Path(path).expanduser()
        if not p.exists():
            raise ProjectError(f"Project file not found: {p}")
        try:
            tree = ET.parse(p)
        except ET.ParseError as exc:
            raise ProjectError(f"{p} is not valid XML: {exc}") from exc
        root = tree.getroot()
        if root.tag != "mlt":
            raise ProjectError(f"{p} is not an MLT project (root element is <{root.tag}>)")
        project = cls(root, p)
        try:
            project.source_bytes = p.read_bytes()
        except OSError:
            project.source_bytes = None
        return project

    @classmethod
    def from_string(cls, text: str, path: Path | None = None) -> MltProject:
        """Parse a project from an XML string."""
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise ProjectError(f"Invalid project XML: {exc}") from exc
        if root.tag != "mlt":
            raise ProjectError(f"Not an MLT project (root element is <{root.tag}>)")
        return cls(root, path)

    @classmethod
    def create(
        cls,
        path: str | os.PathLike[str],
        profile: Profile | None = None,
        *,
        sequence_name: str = "Sequence 1",
    ) -> MltProject:
        """Create a brand new, empty project on disk."""
        profile = profile or PROFILE_PRESETS["1080p25"]
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)

        root = ET.Element("mlt", {"LC_NUMERIC": "C", "producer": MAIN_BIN_ID})
        ET.SubElement(root, "profile", profile.to_attributes())

        project = cls(root, p)
        sequence = project.add_sequence(sequence_name)
        project.set_active_sequence(sequence)
        project.write_bin_properties()
        project.save()
        return project

    # -- persistence -------------------------------------------------------

    def to_string(self) -> str:
        """Serialise the project the way Kdenlive writes it."""
        _indent(self.root)
        body = ET.tostring(self.root, encoding="unicode")
        return "<?xml version='1.0' encoding='utf-8'?>\n" + body + "\n"

    def save(self, path: str | os.PathLike[str] | None = None, *, backup: bool = True) -> Path:
        """Atomically write the project to disk.

        The file is written to a temporary file in the destination directory and
        then moved into place, so an interrupted write can never leave a
        half-written project behind.
        """
        target = Path(path).expanduser() if path else self.path
        if target is None:
            raise ProjectError("No destination path given and the project has no path")
        target.parent.mkdir(parents=True, exist_ok=True)

        if backup and target.exists():
            shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))

        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".kdenlive-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(self.to_string())
            os.replace(tmp_name, target)
        except BaseException:
            with suppress_oserror():
                os.unlink(tmp_name)
            raise
        self.path = target
        self._dirty = False
        return target

    # -- profile -----------------------------------------------------------

    @property
    def profile(self) -> Profile:
        el = self.root.find("profile")
        if el is None:
            raise ProjectError("Project has no <profile> element")
        return Profile.from_attributes(el.attrib)

    @profile.setter
    def profile(self, value: Profile) -> None:
        el = self.root.find("profile")
        if el is None:
            el = ET.Element("profile")
            self.root.insert(0, el)
        el.attrib.clear()
        el.attrib.update(value.to_attributes())

    @property
    def fps(self) -> float:
        return self.profile.fps

    # -- element lookup ----------------------------------------------------

    def element_by_id(self, element_id: str) -> ET.Element | None:
        """Find any element (producer/chain/playlist/tractor) by id."""
        if not element_id:
            return None
        for child in self.root:
            if child.get("id") == element_id:
                return child
        return None

    def _all_ids(self) -> set[str]:
        """Every id used anywhere in the document.

        Filters, transitions and nested producers carry ids too, so the whole
        tree has to be scanned -- otherwise a new filter could be handed an id
        that is already in use.
        """
        return {
            element.get("id", "")
            for element in self.root.iter()
            if element.get("id")
        }

    def next_id(self, prefix: str) -> str:
        """Return an unused id such as ``chain17``."""
        used = self._all_ids()
        index = 0
        while f"{prefix}{index}" in used:
            index += 1
        return f"{prefix}{index}"

    def next_clip_id(self) -> str:
        """Return an unused Kdenlive clip id (a monotonically increasing int)."""
        highest = 0
        for child in self.root.iter():
            value = get_property(child, CLIP_ID_PROP)
            if value and value.isdigit():
                highest = max(highest, int(value))
        return str(highest + 1)

    # -- main bin ----------------------------------------------------------

    @property
    def main_bin(self) -> ET.Element:
        """The ``main_bin`` playlist holding project settings and bin clips."""
        for child in self.root:
            if child.tag == "playlist" and child.get("id") == MAIN_BIN_ID:
                return child
        raise ProjectError("Project has no <playlist id='main_bin'>")

    def ensure_main_bin(self) -> ET.Element:
        for child in self.root:
            if child.tag == "playlist" and child.get("id") == MAIN_BIN_ID:
                return child
        playlist = ET.Element("playlist", {"id": MAIN_BIN_ID})
        self.root.append(playlist)
        return playlist

    def write_bin_properties(self) -> None:
        """Ensure the essential project properties exist in ``main_bin``."""
        bin_element = self.ensure_main_bin()
        defaults = {
            DOC_VERSION_PROP: DOCUMENT_VERSION,
            DOC_PATCH_VERSION_PROP: DOCUMENT_PATCH_VERSION,
            "kdenlive:docproperties.documentid": uuid_mod.uuid4().hex,
            "kdenlive:docproperties.audioChannels": "2",
            "kdenlive:docproperties.enableproxy": "0",
            "kdenlive:docproperties.enableexternalproxy": "0",
            "kdenlive:docproperties.generateimageproxy": "0",
            "kdenlive:docproperties.compositing": "0",
            "kdenlive:docproperties.activeTrack": "0",
        }
        for name, value in defaults.items():
            if get_property(bin_element, name) is None:
                set_property(bin_element, name, value)

    def bin_clips(self) -> list[BinClip]:
        """List the clips stored in the project bin."""
        clips: list[BinClip] = []
        try:
            bin_element = self.main_bin
        except ProjectError:
            return clips
        for entry in bin_element.findall("entry"):
            producer_id = entry.get("producer", "")
            element = self.element_by_id(producer_id)
            if element is None:
                continue
            # Sequence entries point at tractors, not clips.
            if element.tag == "tractor":
                continue
            props = properties_of(element)
            resource = props.get("resource")
            name = props.get("kdenlive:clipname") or element.get("id", producer_id)
            clips.append(
                BinClip(
                    producer_id=producer_id,
                    element=element,
                    name=name,
                    resource=resource,
                    clip_type=props.get("kdenlive:clip_type", "0"),
                )
            )
        return clips

    def add_to_bin(self, producer_id: str) -> None:
        """Register an existing producer as a bin clip."""
        bin_element = self.ensure_main_bin()
        element = self.element_by_id(producer_id)
        if element is None:
            raise ProjectError(f"No producer with id {producer_id!r}")
        out = element.get("out")
        entry = ET.SubElement(bin_element, "entry")
        entry.set("producer", producer_id)
        entry.set("in", "00:00:00.000")
        entry.set("out", out or "00:00:00.000")

    # -- sequences ---------------------------------------------------------

    def sequences(self) -> list[Sequence]:
        """All timeline sequences in the project.

        Modern (23.04+) projects store each sequence as a tractor carrying a
        ``kdenlive:uuid``.  Older projects have a single timeline whose tractor
        holds the track tractors; those are supported too.
        """
        result: list[Sequence] = []
        for child in self.root:
            if child.tag != "tractor":
                continue
            if get_property(child, PROJECT_TRACTOR_PROP) is not None:
                continue
            if get_property(child, SEQUENCE_UUID_PROP) is None:
                continue
            result.append(self._build_sequence(child))
        if not result:
            result = self._legacy_sequences()
        return result

    def _legacy_sequences(self) -> list[Sequence]:
        """Find the timeline of a pre-23.04 project.

        In the old format there is no sequence tractor: the timeline is the
        tractor whose tracks are themselves tractors (the individual tracks).
        """
        candidates: list[ET.Element] = []
        for child in self.root:
            if child.tag != "tractor":
                continue
            if get_property(child, PROJECT_TRACTOR_PROP) is not None:
                continue
            for track in child.findall("track"):
                element = self.element_by_id(track.get("producer", ""))
                if element is not None and element.tag == "tractor":
                    candidates.append(child)
                    break
        if not candidates:
            return []
        # The timeline is the outermost such tractor.
        return [self._build_sequence(candidates[-1])]

    def project_tractor(self) -> ET.Element | None:
        """The ``kdenlive:projectTractor`` tractor (always the last element)."""
        for child in reversed(list(self.root)):
            if child.tag == "tractor" and get_property(child, PROJECT_TRACTOR_PROP) is not None:
                return child
        return None

    def active_sequence(self) -> Sequence | None:
        """The sequence currently opened in the project tractor."""
        tractor = self.project_tractor()
        if tractor is None:
            sequences = self.sequences()
            return sequences[0] if sequences else None
        for track in tractor.findall("track"):
            producer_id = track.get("producer", "")
            element = self.element_by_id(producer_id)
            if element is not None and element.tag == "tractor":
                return self._build_sequence(element)
        sequences = self.sequences()
        return sequences[0] if sequences else None

    def set_active_sequence(self, sequence: Sequence) -> None:
        """Point the project tractor at ``sequence``."""
        tractor = self.project_tractor()
        if tractor is None:
            tractor = ET.Element("tractor", {"id": self.next_id("tractor")})
            set_property(tractor, PROJECT_TRACTOR_PROP, "1")
            self.root.append(tractor)
        for track in list(tractor.findall("track")):
            tractor.remove(track)
        track = ET.SubElement(tractor, "track")
        track.set("producer", sequence.uuid)
        duration = sequence.duration_frames
        tractor.set("in", "00:00:00.000")
        tractor.set("out", format_clock(max(0, duration - 1), self.fps))
        self._dirty = True

    def resolve_sequence(self, reference: str | int | None) -> Sequence:
        """Find a sequence by uuid, name, or 1-based index."""
        sequences = self.sequences()
        if not sequences:
            raise ProjectError("Project contains no timeline sequence")
        if reference is None or reference == "":
            active = self.active_sequence()
            if active is None:
                raise ProjectError("Project contains no timeline sequence")
            return active
        if isinstance(reference, int):
            if 1 <= reference <= len(sequences):
                return sequences[reference - 1]
            raise ProjectError(
                f"Sequence index {reference} out of range (project has {len(sequences)})"
            )
        text = str(reference).strip()
        if text.isdigit():
            index = int(text)
            if 1 <= index <= len(sequences):
                return sequences[index - 1]
            raise ProjectError(
                f"Sequence index {text} out of range (project has {len(sequences)})"
            )
        for sequence in sequences:
            if sequence.uuid == text:
                return sequence
        lowered = text.lower()
        for sequence in sequences:
            if sequence.name.lower() == lowered:
                return sequence
        for sequence in sequences:
            if sequence.uuid.lower() == lowered:
                return sequence
        raise ProjectError(
            f"No sequence matching {reference!r}. Available: "
            + ", ".join(f"{s.name} ({s.uuid})" for s in sequences)
        )

    def _build_sequence(self, tractor: ET.Element) -> Sequence:
        fps = self.fps
        sequence_uuid = get_property(tractor, SEQUENCE_UUID_PROP) or tractor.get("id", "")
        name = get_property(tractor, "kdenlive:clipname") or ""
        if not name:
            name = "Timeline" if not sequence_uuid.startswith("{") else sequence_uuid
        duration = parse_duration(get_property(tractor, "kdenlive:duration") or "", fps)

        tracks: list[Track] = []
        index = 0
        for track_el in tractor.findall("track"):
            producer_id = track_el.get("producer", "")
            element = self.element_by_id(producer_id)
            if element is None:
                continue
            if element.tag == "producer":
                # background / "black" track, not a user visible track
                continue
            if element.tag == "playlist":
                # legacy single playlist track
                kind = "audio" if (get_property(element, "kdenlive:audio_track") or "0") == "1" else "video"
                track_name = get_property(element, "kdenlive:track_name") or ""
                tracks.append(
                    Track(
                        tractor=element,
                        index=index,
                        kind=kind,
                        name=track_name,
                        playlists=[element],
                        project=self,
                    )
                )
                index += 1
                continue
            if element.tag != "tractor":
                continue
            kind = "audio" if (get_property(element, "kdenlive:audio_track") or "0") == "1" else "video"
            track_name = get_property(element, "kdenlive:track_name") or ""
            playlists = [
                self.element_by_id(t.get("producer", "")) or t
                for t in element.findall("track")
            ]
            playlists = [p for p in playlists if p is not None and p.tag == "playlist"]
            tracks.append(
                Track(
                    tractor=element,
                    index=index,
                    kind=kind,
                    name=track_name,
                    playlists=playlists,
                    project=self,
                )
            )
            index += 1

        if duration == 0:
            duration = max((track.length for track in tracks), default=0)

        return Sequence(
            tractor=tractor,
            uuid=sequence_uuid,
            name=name,
            tracks=tracks,
            project=self,
            _duration_frames=duration,
        )

    def add_sequence(self, name: str = "Sequence 1") -> Sequence:
        """Create a new, empty timeline sequence."""
        fps = self.fps
        sequence_uuid = "{" + str(uuid_mod.uuid4()) + "}"
        # Kdenlive gives the sequence tractor the uuid as its element id, and
        # that is what main_bin and the project tractor reference.
        tractor_id = sequence_uuid

        # A black background producer so the timeline has something to render.
        # Kdenlive recognises this track by the literal id "black_track".
        black_id = "black_track"
        if self.element_by_id(black_id) is not None:
            black_id = self.next_id("black_track")
        black = ET.Element("producer", {"id": black_id, "in": "00:00:00.000", "out": "00:00:00.000"})
        set_property(black, "mlt_service", BACKGROUND_SERVICE)
        set_property(black, "kdenlive:track_name", "black_track")
        self.root.append(black)

        # Two video tracks and one audio track, matching a fresh Kdenlive project.
        track_tractors: list[ET.Element] = []
        for track_index, kind in enumerate(("video", "video", "audio")):
            track_tractor = self._make_track_tractor(kind, f"{kind.upper()}{track_index + 1}")
            self.root.append(track_tractor)
            track_tractors.append(track_tractor)

        tractor = ET.Element("tractor", {"id": tractor_id, "in": "00:00:00.000", "out": "00:00:00.000"})
        set_property(tractor, SEQUENCE_UUID_PROP, sequence_uuid)
        set_property(tractor, "kdenlive:clipname", name)
        set_property(tractor, "kdenlive:clip_type", "0")
        set_property(tractor, "kdenlive:producer_type", "17")
        set_property(tractor, CLIP_ID_PROP, self.next_clip_id())
        set_property(tractor, "kdenlive:duration", format_frames(0, fps))
        set_property(tractor, "kdenlive:maxduration", "0")
        set_property(tractor, "kdenlive:sequenceproperties.activeTrack", "0")
        set_property(tractor, "kdenlive:sequenceproperties.tracksCount", str(len(track_tractors)))
        set_property(tractor, "kdenlive:sequenceproperties.documentuuid", sequence_uuid)
        set_property(tractor, "kdenlive:sequenceproperties.hasVideo", "1")
        set_property(tractor, "kdenlive:sequenceproperties.hasAudio", "1")

        track_el = ET.SubElement(tractor, "track")
        track_el.set("producer", black_id)
        for track_tractor in track_tractors:
            track_el = ET.SubElement(tractor, "track")
            track_el.set("producer", track_tractor.get("id", ""))

        self.root.append(tractor)
        self.write_bin_properties()

        entry = ET.SubElement(self.ensure_main_bin(), "entry")
        entry.set("producer", sequence_uuid)
        entry.set("in", "00:00:00.000")
        entry.set("out", "00:00:00.000")

        self._dirty = True
        return self._build_sequence(tractor)

    def _make_track_tractor(self, kind: str, name: str) -> ET.Element:
        """Build a ``<tractor>`` holding the two playlists of a track."""
        tractor_id = self.next_id("tractor")
        tractor = ET.Element("tractor", {"id": tractor_id, "in": "00:00:00.000", "out": "00:00:00.000"})
        set_property(tractor, "kdenlive:track_name", name)
        set_property(tractor, "kdenlive:trackheight", "67")
        set_property(tractor, "kdenlive:timeline_active", "1")
        set_property(tractor, "kdenlive:collapsed", "0")
        set_property(tractor, "kdenlive:thumbs_format", "")
        set_property(tractor, "kdenlive:audio_rec", "")
        if kind == "audio":
            set_property(tractor, "kdenlive:audio_track", "1")
        hide = "video" if kind == "audio" else "audio"
        # Kdenlive keeps two playlists per track so that "mixes" (same track
        # transitions) have somewhere to live.
        for _ in range(2):
            playlist_id = self.next_id("playlist")
            playlist = ET.Element("playlist", {"id": playlist_id})
            set_property(playlist, "kdenlive:audio_track", "1" if kind == "audio" else "0")
            set_property(playlist, "hide", "1" if kind == "audio" else "2")
            self.root.append(playlist)
            track_el = ET.SubElement(tractor, "track")
            track_el.set("hide", hide)
            track_el.set("producer", playlist_id)
        return tractor

    def make_track_tractor(self, kind: str, name: str) -> ET.Element:
        """Public wrapper around the track tractor builder."""
        return self._make_track_tractor(kind, name)

    def make_track_element(self, producer_id: str, *, hide: str | None = None) -> ET.Element:
        """Create a ``<track>`` element referencing a producer."""
        element = ET.Element("track")
        element.set("producer", producer_id)
        if hide:
            element.set("hide", hide)
        return element

    def set_property_on_sequence(self, sequence: Sequence, name: str, value: str) -> None:
        """Set a property on a sequence tractor."""
        set_property(sequence.tractor, name, value)
        self._dirty = True

    def insert_track_into_sequence(self, sequence: Sequence, tractor_id: str, position: int) -> None:
        """Insert a track into a sequence at a 1-based position.

        The background ("black") track always stays first.
        """
        track_elements = sequence.tractor.findall("track")
        background: ET.Element | None = None
        user_tracks = list(track_elements)
        if track_elements:
            candidate = track_elements[0]
            element = self.element_by_id(candidate.get("producer", ""))
            if element is not None and element.tag == "producer":
                background = candidate
                user_tracks = track_elements[1:]

        new_element = self.make_track_element(tractor_id)
        if background is not None:
            index = list(sequence.tractor).index(background) + 1
        elif user_tracks:
            index = list(sequence.tractor).index(user_tracks[0])
        else:
            index = len(list(sequence.tractor))

        # Walk forward to the requested slot among the user visible tracks.
        for offset, element in enumerate(user_tracks):
            if position <= offset:
                break
            index = list(sequence.tractor).index(element) + 1

        sequence.tractor.insert(index, new_element)
        set_property(
            sequence.tractor,
            "kdenlive:sequenceproperties.tracksCount",
            str(len(user_tracks) + 1),
        )
        self._dirty = True

    # -- track helpers -----------------------------------------------------

    def resolve_track(self, sequence: Sequence, reference: str | int) -> Track:
        """Find a track by 1-based index, name, or id."""
        tracks = sequence.tracks
        if not tracks:
            raise ProjectError("Sequence has no tracks")
        if isinstance(reference, int):
            index = reference
        else:
            text = str(reference).strip()
            if text.isdigit():
                index = int(text)
            else:
                for track in tracks:
                    if track.name and track.name.lower() == text.lower():
                        return track
                for track in tracks:
                    if track.track_id == text:
                        return track
                raise ProjectError(
                    f"No track matching {reference!r}. Available: "
                    + ", ".join(f"{i + 1}:{t.name or t.track_id}" for i, t in enumerate(tracks))
                )
        if 1 <= index <= len(tracks):
            return tracks[index - 1]
        raise ProjectError(
            f"Track index {index} out of range (sequence has {len(tracks)} tracks)"
        )

    # -- playlist editing --------------------------------------------------

    def playlist_length(self, playlist: ET.Element) -> int:
        """Total length of a playlist in frames."""
        fps = self.fps
        total = 0
        for child in playlist:
            if child.tag == "entry":
                total += self._entry_length(child, fps)
            elif child.tag == "blank":
                total += self._blank_length(child, fps)
        return total

    def _entry_length(self, entry: ET.Element, fps: float) -> int:
        in_raw = entry.get("in")
        out_raw = entry.get("out")
        if in_raw is not None and out_raw is not None:
            return max(1, parse_clock(out_raw, fps) - parse_clock(in_raw, fps) + 1)
        producer = self.element_by_id(entry.get("producer", ""))
        if producer is not None:
            length = get_property(producer, "length")
            if length and length.isdigit():
                return max(1, int(length))
            out_raw = producer.get("out")
            if out_raw:
                return max(1, parse_clock(out_raw, fps) + 1)
        return 1

    def _blank_length(self, blank: ET.Element, fps: float) -> int:
        length = blank.get("length")
        if length:
            return max(0, parse_clock(length, fps))
        in_raw = blank.get("in")
        out_raw = blank.get("out")
        if in_raw is not None and out_raw is not None:
            return max(0, parse_clock(out_raw, fps) - parse_clock(in_raw, fps) + 1)
        return 0

    def playlist_clips(self, playlist: ET.Element) -> list[TimelineClip]:
        """All clips on a playlist, with their computed timeline positions."""
        fps = self.fps
        clips: list[TimelineClip] = []
        position = 0
        for child in playlist:
            if child.tag == "entry":
                length = self._entry_length(child, fps)
                in_frames = parse_clock(child.get("in") or "00:00:00.000", fps)
                out_frames = parse_clock(child.get("out") or "00:00:00.000", fps)
                clips.append(
                    TimelineClip(
                        entry=child,
                        producer_id=child.get("producer", ""),
                        position=position,
                        length=length,
                        in_frames=in_frames,
                        out_frames=out_frames,
                        fps=fps,
                    )
                )
                position += length
            elif child.tag == "blank":
                position += self._blank_length(child, fps)
        return clips

    def track_clips(self, track: Track) -> list[TimelineClip]:
        return self.playlist_clips(track.playlist)

    def find_clip(self, track: Track, reference: str | int) -> TimelineClip:
        """Find a clip on a track.

        Resolution order:

        * an ``int`` is a 1-based position on the track,
        * a string is matched against the clip's ``kdenlive:id`` first -- those
          ids are numeric, so they have to win over the positional meaning,
        * a string that is not a clip id is then treated as a 1-based position,
        * finally the producer id is tried.

        ``"id:7"`` and ``"index:2"`` force the id or the positional meaning.
        """
        if reference is None:
            raise ProjectError("No clip was given; pass a clip id or an index on the track")
        clips = self.track_clips(track)
        if not clips:
            raise ProjectError("Track is empty")
        if isinstance(reference, int) and not isinstance(reference, bool):
            index = reference
        else:
            text = str(reference).strip()
            lowered = text.lower()
            if lowered.startswith("index:"):
                text = text.split(":", 1)[1].strip()
            elif lowered.startswith("id:"):
                wanted = text.split(":", 1)[1].strip()
                for clip in clips:
                    if clip.clip_id == wanted:
                        return clip
                raise ProjectError(f"No clip with id {wanted!r} on this track")
            else:
                for clip in clips:
                    if clip.clip_id == text:
                        return clip
                if text.isdigit():
                    index = int(text)
                else:
                    for clip in clips:
                        if clip.producer_id == text:
                            return clip
                    raise ProjectError(
                        f"No clip matching {reference!r} on this track. "
                        + "Available clip ids: "
                        + ", ".join(str(c.clip_id) for c in clips if c.clip_id)
                    )
        if 1 <= index <= len(clips):
            return clips[index - 1]
        raise ProjectError(f"Clip index {index} out of range (track has {len(clips)} clips)")

    def _blank(self, length: int, fps: float) -> ET.Element:
        blank = ET.Element("blank")
        blank.set("length", format_clock(max(0, length), fps))
        return blank

    def playlist_insert(self, playlist: ET.Element, new_element: ET.Element, position: int) -> None:
        """Insert an ``<entry>`` into a playlist at ``position`` frames.

        Existing material is overwritten (the Kdenlive default), and any blank
        that is partially covered is split around the new entry.
        """
        fps = self.fps
        position = max(0, int(position))
        new_length = self._entry_length(new_element, fps)

        children = list(playlist)
        cursor = 0
        insert_index = len(children)
        covered: ET.Element | None = None
        offset_in_segment = 0

        for index, child in enumerate(children):
            if child.tag == "entry":
                segment_length = self._entry_length(child, fps)
            elif child.tag == "blank":
                segment_length = self._blank_length(child, fps)
            else:
                continue
            if position < cursor + segment_length:
                insert_index = index
                covered = child
                offset_in_segment = position - cursor
                break
            cursor += segment_length

        if covered is None:
            # Past the end of the playlist: pad with a blank then append.
            if cursor < position:
                playlist.append(self._blank(position - cursor, fps))
            playlist.append(new_element)
            self._dirty = True
            return

        if covered.tag == "entry":
            # Overwrite the tail of an existing clip: truncate it and append.
            remaining = self._entry_length(covered, fps) - offset_in_segment
            if remaining <= 0:
                raise ProjectError("Cannot insert at this position")
            new_length = min(new_length, remaining)
            covered.set("out", format_clock(
                parse_clock(covered.get("in") or "00:00:00.000", fps) + offset_in_segment - 1, fps
            ))
            if new_length < self._entry_length(new_element, fps):
                new_element.set("out", format_clock(
                    parse_clock(new_element.get("in") or "00:00:00.000", fps) + new_length - 1, fps
                ))
            playlist.insert(insert_index + 1, new_element)
            self._dirty = True
            return

        # covered is a blank -> split it around the new entry
        segment_length = self._blank_length(covered, fps)
        head_length = offset_in_segment
        tail_length = segment_length - head_length - new_length
        if tail_length < 0:
            tail_length = 0
        children = list(playlist)
        position_index = children.index(covered)
        playlist.remove(covered)
        insert_at = position_index
        if head_length > 0:
            head = self._blank(head_length, fps)
            playlist.insert(insert_at, head)
            insert_at += 1
        playlist.insert(insert_at, new_element)
        insert_at += 1
        if tail_length > 0:
            playlist.insert(insert_at, self._blank(tail_length, fps))
        self._dirty = True

    def playlist_remove_entry(self, entry: ET.Element) -> int:
        """Remove an entry from its playlist, leaving a blank of equal length."""
        for playlist in self.root.iter("playlist"):
            if entry in list(playlist):
                fps = self.fps
                length = self._entry_length(entry, fps)
                index = list(playlist).index(entry)
                playlist.remove(entry)
                if length > 0:
                    playlist.insert(index, self._blank(length, fps))
                self._dirty = True
                return length
        raise ProjectError("Entry is not part of any playlist")

    def merge_blanks(self, playlist: ET.Element) -> None:
        """Collapse consecutive blanks into a single one."""
        fps = self.fps
        previous_blank: ET.Element | None = None
        for child in list(playlist):
            if child.tag == "blank":
                if previous_blank is not None:
                    total = self._blank_length(previous_blank, fps) + self._blank_length(child, fps)
                    previous_blank.set("length", format_clock(total, fps))
                    playlist.remove(child)
                    continue
                previous_blank = child
            else:
                previous_blank = None
        self._dirty = True

    # -- clip helpers ------------------------------------------------------

    def make_entry(
        self,
        producer_id: str,
        *,
        in_frames: int = 0,
        out_frames: int = 0,
        clip_id: str | None = None,
    ) -> ET.Element:
        """Build an ``<entry>`` element referencing ``producer_id``."""
        fps = self.fps
        entry = ET.Element("entry")
        entry.set("producer", producer_id)
        entry.set("in", format_clock(in_frames, fps))
        entry.set("out", format_clock(out_frames, fps))
        set_property(entry, CLIP_ID_PROP, clip_id or self.next_clip_id())
        return entry

    def create_media_producer(
        self,
        resource: str,
        *,
        length_frames: int,
        service: str = "avformat-novalidate",
        clip_name: str | None = None,
        properties: dict[str, str] | None = None,
        before: ET.Element | None = None,
    ) -> ET.Element:
        """Create a ``<chain>`` element for an external media file.

        ``before`` places the new element immediately before the given element
        in the document.  Kdenlive always defines a producer before the
        playlist that references it, and MLT resolves references in document
        order, so new chains are inserted ahead of the playlist that uses them
        rather than appended at the end.
        """
        fps = self.fps
        chain_id = self.next_id("chain")
        chain = ET.Element("chain", {"id": chain_id, "out": format_clock(max(0, length_frames - 1), fps)})
        set_property(chain, "length", str(max(1, length_frames)))
        set_property(chain, "eof", "pause")
        set_property(chain, "resource", resource)
        set_property(chain, "mlt_service", service)
        set_property(chain, "seekable", "1")
        set_property(chain, "kdenlive:clipname", clip_name or Path(resource).name)
        set_property(chain, "kdenlive:clip_type", "0")
        set_property(chain, "kdenlive:folderid", "-1")
        set_property(chain, "kdenlive:id", self.next_clip_id())
        set_property(chain, "kdenlive:originalurl", resource)
        set_property(chain, "mute_on_pause", "0")
        for key, value in (properties or {}).items():
            set_property(chain, key, value)
        self.insert_element(chain, before)
        self._dirty = True
        return chain

    def insert_element(self, element: ET.Element, before: ET.Element | None = None) -> None:
        """Insert an element into the document, optionally before another one."""
        if before is not None:
            children = list(self.root)
            if before in children:
                self.root.insert(children.index(before), element)
                return
        self.root.append(element)

    # -- effects ------------------------------------------------------------

    def add_filter(
        self,
        host: ET.Element,
        mlt_service: str,
        parameters: dict[str, str] | None = None,
        *,
        kdenlive_id: str | None = None,
        internal: bool = False,
    ) -> ET.Element:
        """Attach an MLT filter to ``host`` (a chain, playlist or tractor)."""
        filter_id = self.next_id("filter")
        element = ET.Element("filter", {"id": filter_id})
        set_property(element, "mlt_service", mlt_service)
        set_property(element, "kdenlive_id", kdenlive_id or mlt_service)
        for key, value in (parameters or {}).items():
            set_property(element, key, value)
        if internal:
            set_property(element, INTERNAL_ADDED, "237")
        if host.tag == "tractor":
            # MLT's content model puts filters and transitions in the same
            # trailing group of a tractor; Kdenlive writes the filters first.
            for index, child in enumerate(list(host)):
                if child.tag == "transition":
                    host.insert(index, element)
                    self._dirty = True
                    return element
        host.append(element)
        self._dirty = True
        return element

    def filters_of(self, host: ET.Element) -> list[ET.Element]:
        return host.findall("filter")

    def add_transition(
        self,
        sequence: Sequence,
        mlt_service: str,
        *,
        a_track: int = 0,
        b_track: int = 1,
        in_frames: int | None = None,
        out_frames: int | None = None,
        parameters: dict[str, str] | None = None,
    ) -> ET.Element:
        """Add a user transition to a sequence tractor."""
        transition_id = self.next_id("transition")
        attrs = {"id": transition_id}
        if in_frames is not None:
            attrs["in"] = format_clock(in_frames, self.fps)
        if out_frames is not None:
            attrs["out"] = format_clock(out_frames, self.fps)
        element = ET.Element("transition", attrs)
        set_property(element, "a_track", str(a_track))
        set_property(element, "b_track", str(b_track))
        set_property(element, "mlt_service", mlt_service)
        set_property(element, "kdenlive_id", mlt_service)
        for key, value in (parameters or {}).items():
            set_property(element, key, value)
        sequence.tractor.append(element)
        self._dirty = True
        return element

    # -- misc ---------------------------------------------------------------

    def refresh_timings(self) -> None:
        """Recompute the cached duration information of the document.

        Kdenlive stores the length of every track, of every sequence and of the
        project itself.  After any edit those numbers are stale, so they are
        recomputed here before the project is written back.
        """
        fps = self.fps

        for sequence in self.sequences():
            longest = 0
            for track in sequence.tracks:
                length = self.playlist_length(track.playlist) if track.playlists else 0
                if length > longest:
                    longest = length
                # A track tractor's out point is the last frame of its content.
                track.tractor.set("out", format_clock(max(0, length - 1), fps))
            sequence.duration_frames = longest
            sequence.tractor.set("out", format_clock(max(0, longest - 1), fps))
            set_property(sequence.tractor, "kdenlive:duration", format_frames(longest, fps))
            set_property(sequence.tractor, "kdenlive:maxduration", str(longest))

        tractor = self.project_tractor()
        if tractor is not None:
            active = self.active_sequence()
            length = active.duration_frames if active is not None else 0
            tractor.set("in", "00:00:00.000")
            tractor.set("out", format_clock(max(0, length - 1), fps))
        self._dirty = True

    def summary(self) -> dict[str, object]:
        """A high level description of the project."""
        fps = self.fps
        sequences = self.sequences()
        active = self.active_sequence()
        return {
            "path": str(self.path) if self.path else None,
            "profile": {
                "width": self.profile.width,
                "height": self.profile.height,
                "fps": fps,
                "description": self.profile.description,
            },
            "sequences": [
                {
                    "uuid": s.uuid,
                    "name": s.name,
                    "tracks": len(s.tracks),
                    "duration_frames": s.duration_frames,
                    "duration": format_frames(s.duration_frames, fps),
                    "active": bool(active and active.uuid == s.uuid),
                }
                for s in sequences
            ],
            "bin_clips": [
                {
                    "clip_id": c.clip_id,
                    "producer": c.producer_id,
                    "name": c.name,
                    "resource": c.resource,
                }
                for c in self.bin_clips()
            ],
        }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def suppress_oserror() -> Any:  # noqa: ANN401 - tiny context manager helper
    import contextlib

    return contextlib.suppress(OSError)


def _indent(element: ET.Element, level: int = 0) -> None:
    """Pretty print in place, the way Kdenlive writes its projects."""
    pad = "\n" + " " * level
    if len(element):
        if not (element.text or "").strip():
            element.text = pad + " "
        for child in element:
            _indent(child, level + 1)
        if not (element[-1].tail or "").strip():
            element[-1].tail = pad
    if level and not (element.tail or "").strip():
        element.tail = pad


def iter_projects(directory: str | os.PathLike[str]) -> Iterator[Path]:
    """Yield every ``.kdenlive`` project inside ``directory``."""
    root = Path(directory).expanduser()
    if not root.exists():
        raise KdenliveError(f"Directory does not exist: {root}")
    if root.is_file():
        if root.suffix == ".kdenlive":
            yield root
        return
    yield from sorted(root.rglob("*.kdenlive"))
