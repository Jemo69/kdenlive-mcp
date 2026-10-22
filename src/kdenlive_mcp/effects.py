"""The catalogue of MLT effects and transitions Kdenlive knows about.

The catalogue is generated from Kdenlive's own ``data/effects`` and
``data/transitions`` definitions (see ``tools/generate_catalog.py``), so the
``mlt_service`` strings and parameter names exposed here are exactly the ones
Kdenlive itself will accept.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._catalog import EFFECTS, TRANSITIONS
from .errors import ToolError


@dataclass(slots=True)
class EffectInfo:
    """Description of one effect or transition."""

    mlt_service: str
    name: str
    description: str
    kind: str
    parameters: dict[str, dict[str, object]]

    def describe(self) -> dict[str, object]:
        return {
            "mlt_service": self.mlt_service,
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "parameters": {
                key: {
                    "type": value.get("type"),
                    "name": value.get("name"),
                    "default": value.get("default"),
                    "min": value.get("min"),
                    "max": value.get("max"),
                }
                for key, value in self.parameters.items()
            },
        }


def _to_info(service: str, record: dict) -> EffectInfo:
    return EffectInfo(
        mlt_service=record["mlt_service"],
        name=record["name"],
        description=record["description"],
        kind=record["kind"],
        parameters=record["parameters"],
    )


def all_effects() -> dict[str, EffectInfo]:
    """Every known effect, keyed by ``mlt_service``."""
    return {key: _to_info(key, record) for key, record in EFFECTS.items()}


def all_transitions() -> dict[str, EffectInfo]:
    """Every known transition, keyed by ``mlt_service``."""
    return {key: _to_info(key, record) for key, record in TRANSITIONS.items()}


def lookup(service: str) -> EffectInfo:
    """Look up an effect or transition by its ``mlt_service`` name.

    Raises :class:`ToolError` with a short list of close matches when the name
    is unknown, which is far more useful to a model than a bare "not found".
    """
    key = (service or "").strip()
    if key in EFFECTS:
        return _to_info(key, EFFECTS[key])
    if key in TRANSITIONS:
        return _to_info(key, TRANSITIONS[key])
    lowered = key.lower()
    for name, record in EFFECTS.items():
        if name.lower() == lowered:
            return _to_info(name, record)
    for name, record in TRANSITIONS.items():
        if name.lower() == lowered:
            return _to_info(name, record)
    suggestions = _suggest(key)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise ToolError(
        f"Unknown effect/transition {service!r}.{hint} "
        "Use the effects_catalog tool to list valid names."
    )


def _suggest(service: str, limit: int = 5) -> list[str]:
    """Cheap fuzzy suggestions based on substring matching."""
    lowered = service.lower()
    if not lowered:
        return []
    scored: list[tuple[int, str]] = []
    for name in list(EFFECTS) + list(TRANSITIONS):
        candidate = name.lower()
        if lowered in candidate or candidate in lowered:
            scored.append((abs(len(candidate) - len(lowered)), name))
    scored.sort()
    return [name for _, name in scored[:limit]]


def search(query: str, *, include_transitions: bool = True, limit: int = 50) -> list[EffectInfo]:
    """Search the catalogue by name, description or parameter name."""
    needle = (query or "").strip().lower()
    pool = dict(EFFECTS)
    if include_transitions:
        pool.update(TRANSITIONS)
    results: list[EffectInfo] = []
    for service, record in pool.items():
        if not needle:
            results.append(_to_info(service, record))
            continue
        haystack = " ".join(
            [
                service,
                record.get("name", ""),
                record.get("description", ""),
                " ".join(record.get("parameters", {}).keys()),
            ]
        ).lower()
        if needle in haystack:
            results.append(_to_info(service, record))
        if len(results) >= limit:
            break
    return results[:limit]


def validate_parameters(service: str, parameters: dict[str, str]) -> dict[str, str]:
    """Check parameters against the catalogue, returning the accepted set.

    Unknown parameter names are *not* rejected -- MLT filters accept extra
    properties and rejecting them would be too strict -- but they are reported
    so the caller can be told about a likely typo.
    """
    info = lookup(service)
    unknown = [key for key in parameters if key not in info.parameters]
    if unknown:
        valid = ", ".join(sorted(info.parameters)) or "(none)"
        raise ToolError(
            f"Unknown parameter(s) for {service!r}: {', '.join(unknown)}. "
            f"Valid parameters: {valid}"
        )
    return parameters
