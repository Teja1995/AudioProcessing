"""FastAPI app factory: routers + static mount + the /ws push channel.

API docs endpoints are disabled on purpose — Swagger UI pulls its assets
from a CDN, and this app must run with no network at all.
"""

from __future__ import annotations

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from capture import config
from capture.routes import consent, dashboard, export, session
from capture.ws.hub import hub


def create_app() -> FastAPI:
    app = FastAPI(
        title="SPACE READY_4 Voice Capture",
        docs_url=None,  # offline: no CDN-served Swagger/ReDoc
        redoc_url=None,
        openapi_url=None,
    )

    app.include_router(session.router)
    app.include_router(consent.router)
    app.include_router(dashboard.router)
    app.include_router(export.router)
    app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(config.STATIC_DIR / "index.html")

    @app.get("/dashboard")
    async def dashboard_page() -> FileResponse:
        return FileResponse(config.STATIC_DIR / "dashboard.html")

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        # Push-only channel: we park on receive to notice the disconnect;
        # anything a client sends is ignored — commands travel as HTTP.
        await hub.register(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            hub.unregister(ws)

    return app
