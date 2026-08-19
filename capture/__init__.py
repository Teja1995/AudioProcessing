"""Voice Capture Tool — SPACE READY_4 analog mission.

Offline recording app for standardised voice samples. See CLAUDE.md for
requirements and ARCHITECTURE.md for the design this package follows.

This package captures audio and metadata. It performs no analysis and never
modifies the captured signal.
"""

from __future__ import annotations

from typing import NoReturn

__version__ = "0.1.0-skeleton"


def todo(step: int, what: str) -> NoReturn:
    """Loud placeholder for not-yet-implemented behaviour.

    The skeleton never stubs silently: anything unfinished raises with a
    pointer to its build-order step in ARCHITECTURE.md §15.
    """
    raise NotImplementedError(
        f"Not implemented yet: {what} (build-order step {step}, ARCHITECTURE.md §15)"
    )
