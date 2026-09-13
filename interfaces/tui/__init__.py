def run(mode: str) -> None:
    """Load Textual only when the TUI interface is started."""
    from .app import run as run_app

    run_app(mode)

__all__ = ["run"]
