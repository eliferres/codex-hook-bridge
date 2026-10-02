"""Run Claude Code hooks under the Codex CLI without rewriting them."""
from __future__ import annotations

__version__ = "0.1.0"

from .cli import main  # noqa: E402  (main needs __version__ defined first)

__all__ = ["main", "__version__"]
