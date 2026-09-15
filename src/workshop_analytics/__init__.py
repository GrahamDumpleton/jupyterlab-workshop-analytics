"""The jupyterlab-workshop analytics service.

Receives the progress events that workshops report, stores them,
projects them into sessions, shows them live, and answers questions
about them.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("jupyterlab-workshop-analytics")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = ["__version__"]
