"""Web board: FastAPI JSON API plus the vanilla-JS UI in ``web/static`` (also exported as a static site)."""

from __future__ import annotations

__all__ = ["create_app"]


def create_app(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201 - thin lazy re-export
    """Lazy alias of :func:`jobsweep.web.app.create_app` (keeps ``import jobsweep.web`` cheap)."""
    from .app import create_app as _create_app

    return _create_app(*args, **kwargs)
