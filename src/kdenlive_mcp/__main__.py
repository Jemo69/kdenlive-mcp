"""``python -m kdenlive_mcp`` -- run the MCP server over stdio."""

from __future__ import annotations


def main() -> None:  # pragma: no cover - entry point
    from .server import main as _main

    _main()


if __name__ == "__main__":  # pragma: no cover
    main()
