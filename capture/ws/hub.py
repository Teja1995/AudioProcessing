"""Connection registry + broadcast for the live meter and task state.

Message shapes (ARCHITECTURE.md §13), all dicts with a "type" key:

    {"type": "level", "rms_dbfs": -23.1, "peak_dbfs": -9.8, "clipping": false}
    {"type": "task_state", "task": 3, "stem": "sustained_a", "take": 2,
     "status": "recording"}
    {"type": "error", "code": "device_lost", "message": "..."}

Single event loop only — register/broadcast are never called from other
threads (the engine marshals level updates via call_soon_threadsafe first),
so no locking is needed here.
"""

from __future__ import annotations

from fastapi import WebSocket


class Hub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def register(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def unregister(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, message: dict[str, object]) -> None:
        """Best-effort push to every connected browser. A dead socket is
        dropped; UI push must never stall the recording path."""
        dead: list[WebSocket] = []
        for ws in self._clients:
            try:
                await ws.send_json(message)
            except Exception:  # noqa: BLE001 — not the recording path; a
                # failed UI push only means this client is gone.
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)


hub = Hub()
