CLI_STORAGE_INTERFACE = "tui"  # Preserve existing local identity and history.


def run(mode: str) -> None:
    """Load the plain terminal interface only when CLI mode is started."""
    from .app import run as run_app

    run_app(mode)

__all__ = ["CLI_STORAGE_INTERFACE", "run"]
