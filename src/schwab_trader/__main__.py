"""Entry point so the app can be run as ``python -m schwab_trader``."""

from __future__ import annotations

from schwab_trader.cli import app


def main() -> None:
    """Run the Typer CLI application."""
    app()


if __name__ == "__main__":
    main()
