"""Content model checks derived from MLT's own XML DTD.

MLT ships a DTD (``src/modules/xml/mlt-xml.dtd``) that describes the shape of
a project document.  Rather than depending on an XML validator -- and on the
DTD file being present -- the relevant content models are encoded here and
checked directly.  That keeps the MCP dependency free while still catching the
mistakes that actually break Kdenlive: a property written after an entry, a
filter in the wrong place inside a tractor, a blank without a length.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .mltxml import MltProject

#: Content model of every element, as declared by MLT's DTD.
#:
#: Each model is a list of groups.  Every group lists the tags it accepts and
#: the minimum number of children that group needs.  Children have to appear
#: group by group, in order; a child of a later group may not be followed by a
#: child of an earlier one.
CONTENT_MODELS: dict[str, tuple[tuple[tuple[str, ...], int], ...]] = {
    "producer": ((("property", "properties", "filter"), 0),),
    "chain": ((("property", "properties", "filter", "link"), 0),),
    "link": ((("property", "properties"), 0),),
    "filter": ((("property", "properties"), 0),),
    "transition": ((("property", "properties"), 0),),
    "consumer": ((("property", "properties"), 0),),
    "entry": (
        (("property", "properties"), 0),
        (("producer", "playlist", "tractor", "multitrack", "filter", "transition", "chain"), 0),
    ),
    "playlist": (
        (("property", "properties"), 0),
        (("entry", "blank", "producer", "playlist", "tractor", "multitrack", "chain"), 0),
    ),
    "tractor": (
        (("property", "properties"), 0),
        (("multitrack", "track"), 1),
        (("filter", "transition"), 0),
    ),
    "track": (
        (("producer", "playlist", "tractor", "multitrack", "filter", "transition", "chain"), 0),
    ),
}

#: Elements allowed directly below ``<mlt>``.
#:
#: The published DTD predates MLT's ``<chain>`` element, which modern
#: Kdenlive projects use for every clip, so it is accepted here.
ROOT_ELEMENTS = (
    "profile",
    "producer",
    "playlist",
    "tractor",
    "multitrack",
    "consumer",
    "chain",
)

#: Attributes ``<profile>`` must carry.
REQUIRED_PROFILE_ATTRIBUTES = (
    "width",
    "height",
    "frame_rate_num",
    "frame_rate_den",
    "sample_aspect_num",
    "sample_aspect_den",
    "progressive",
)

#: Accepted values of the ``hide`` attribute on ``<track>``.
HIDE_VALUES = ("video", "audio", "both")


def check(project: MltProject) -> list[tuple[str, str, str]]:
    """Check the document against MLT's content models.

    Returns a list of ``(code, message, location)`` tuples; empty when the
    document is well formed.
    """
    problems: list[tuple[str, str, str]] = []
    root = project.root

    for child in root:
        if child.tag not in ROOT_ELEMENTS:
            problems.append(
                (
                    "unexpected_root_child",
                    f"<{child.tag}> is not allowed directly below <mlt>",
                    child.get("id", child.tag),
                )
            )

    profile = root.find("profile")
    if profile is None:
        problems.append(("missing_profile", "The document has no <profile>", "mlt"))
    else:
        for attribute in REQUIRED_PROFILE_ATTRIBUTES:
            if attribute not in profile.attrib:
                problems.append(
                    (
                        "incomplete_profile",
                        f"<profile> is missing the required {attribute!r} attribute",
                        "profile",
                    )
                )

    _check_element(root, problems)
    return problems


def _check_element(element: ET.Element, problems: list[tuple[str, str, str]]) -> None:
    """Recursively check one element and its children."""
    tag = element.tag
    model = CONTENT_MODELS.get(tag)

    if model is not None:
        group_index = 0
        counts = [0] * len(model)
        for child in element:
            while group_index < len(model) and child.tag not in model[group_index][0]:
                group_index += 1
            if group_index >= len(model):
                problems.append(
                    (
                        "invalid_child",
                        f"<{child.tag}> is not allowed at this position inside <{tag}>",
                        element.get("id", tag),
                    )
                )
                continue
            counts[group_index] += 1
        for index, (_tags, minimum) in enumerate(model):
            if counts[index] < minimum:
                problems.append(
                    (
                        "missing_required_child",
                        f"<{tag}> requires at least {minimum} child element(s) of type "
                        + "/".join(_tags),
                        element.get("id", tag),
                    )
                )

    if tag == "blank" and "length" not in element.attrib:
        problems.append(
            ("blank_without_length", "<blank> must carry a 'length' attribute", "blank")
        )

    if tag == "track":
        hide = element.get("hide")
        if hide is not None and hide not in HIDE_VALUES:
            problems.append(
                (
                    "invalid_hide",
                    f"The 'hide' attribute of <track> must be one of {HIDE_VALUES}, got {hide!r}",
                    element.get("producer", "track"),
                )
            )

    for child in element:
        _check_element(child, problems)
