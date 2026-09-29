"""Local HTTP adapter for the existing agent lifecycle."""

from .server import create_app, run

__all__ = ["create_app", "run"]
