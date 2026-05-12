"""Entry point: `python -m bpd_mcp` or `bpd-mcp`."""

from __future__ import annotations


def main() -> None:
    from .server import run

    run()


if __name__ == "__main__":
    main()
