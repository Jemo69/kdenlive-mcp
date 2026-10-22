"""Kdenlive MCP -- drive the Kdenlive video editor through its MLT project format.

The package is deliberately split so that every layer can be used on its own:

``mltxml``
    A pure-python model of the ``.kdenlive`` (MLT XML) project format.
``effects``
    The catalogue of MLT effects and transitions Kdenlive supports.
``media``
    Media inspection through ``ffprobe``.
``render``
    Headless rendering through MLT's ``melt``.
``bridge``
    Talking to a Kdenlive running on another machine (WSL <-> Windows).
``server``
    The FastMCP tool surface.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__", "main"]


def main() -> None:  # pragma: no cover - thin entry point
    """Run the MCP server over stdio."""
    from .server import main as _main

    _main()
