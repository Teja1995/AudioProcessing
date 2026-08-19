"""HTTP routes — thin adapters between the browser and the domain/audio/storage
layers. Commands are plain POSTs (ordinary status codes, idempotency); live
updates flow back over the WebSocket hub.
"""

from __future__ import annotations

from typing import NoReturn

from fastapi import HTTPException


def todo_http(step: int, what: str) -> NoReturn:
    """Loud 501 for not-yet-implemented endpoints — the HTTP twin of
    capture.todo()."""
    raise HTTPException(
        status_code=501,
        detail=f"Not implemented yet: {what} (build-order step {step}, ARCHITECTURE.md §15)",
    )
