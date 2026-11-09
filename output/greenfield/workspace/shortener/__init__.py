"""Standard-library URL shortener. Run with python -m shortener.server."""

from .app import create_app

__all__ = ["create_app"]
