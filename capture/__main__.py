"""Entry point: ``python -m capture`` — checks, serve, open the browser.

Startup sequence (ARCHITECTURE.md §8):

1. Ensure data directories exist.
2. Configure logging to the console AND a rotating file under data/, so the
   device and gain readings survive a crash and travel with the dataset.
3. Device report: input name + OS gain logged — the drift anchor CLAUDE.md
   asks for. Enhancement flags are not readable in v1, so the operator
   confirms the manual checklist in the UI.
4. Data summary: sessions/takes on disk, so a silent failure yesterday is
   visible today.
5. Serve on 127.0.0.1 only and open the browser.

Steps 3 and 4 are reported loudly but never fatal: a dead terminal tells the
operator nothing, whereas a running UI shows them exactly what is wrong.
"""

from __future__ import annotations

import importlib.util
import logging
import logging.handlers
import sys
import threading
import webbrowser
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from capture import __version__, config
from capture.adherence import tracker
from capture.app import create_app
from capture.audio import devices
from capture.session_service import service
from capture.storage.paths import ensure_data_dirs

log = logging.getLogger("capture.startup")

# Startup-only knobs. Conceptually config, kept here because config.py is not
# this module's to edit.
LOG_FILENAME: Final = "capture.log"
LOG_MAX_BYTES: Final = 2_000_000
LOG_BACKUP_COUNT: Final = 10
LOG_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
BROWSER_OPEN_DELAY_S: Final = 1.0
RULE: Final = "=" * 72

# uvicorn only speaks WebSocket if one of these is importable. Without them
# /ws answers 404 and the level meter is dead (see check_websocket_support).
WEBSOCKET_LIBRARIES: Final = ("websockets", "wsproto")


def loud(title: str, lines: list[str]) -> None:
    """Print an unmissable block to the terminal and record it in the log."""
    print(RULE, file=sys.stderr, flush=True)
    print(f"  {title}", file=sys.stderr, flush=True)
    for line in lines:
        print(f"  {line}", file=sys.stderr, flush=True)
    print(RULE, file=sys.stderr, flush=True)
    log.error("%s", title)
    for line in lines:
        log.error("  %s", line)


def configure_logging() -> Path:
    """Console + rotating file under data/. Returns the log file path.

    This process is the only owner of logging configuration, so it starts
    from a clean set of handlers and is safe to call twice.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.INFO)

    formatter = logging.Formatter(LOG_FORMAT)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    log_path = config.DATA_DIR / LOG_FILENAME
    to_file = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    to_file.setFormatter(formatter)
    root.addHandler(to_file)
    return log_path


def report_devices() -> None:
    """Log the input device name and OS gain reading: the drift anchor.

    Read-only. Nothing in this app sets gain or volume — the physical gain is
    fixed, taped and photographed (CLAUDE.md, Hard audio requirements).
    """
    try:
        report = devices.startup_report()
    except Exception as exc:  # noqa: BLE001 — reported loudly, never silent;
        # the app must still serve so the operator sees this in the browser.
        log.exception("Startup device report failed")
        loud(
            "STARTUP DEVICE CHECK FAILED",
            [
                f"{type(exc).__name__}: {exc}",
                "The input device could not be read.",
                "DO NOT RECORD until this is fixed — check the microphone is",
                "plugged in and selected as the default input device.",
                "The app is still starting so you can see this in the browser.",
            ],
        )
        return

    # startup_report() writes the full block to this same log; one summary
    # line here keeps the two facts that matter easy to find afterwards.
    log.info(
        "input device | %s | OS gain reading: %s",
        report.get("input_device_name"),
        report.get("os_gain_reading") or "UNREADABLE",
    )
    if str(report.get("enhancement_status", "")) != "off":
        log.warning(
            "Audio enhancements are NOT confirmed off (status: %s). The "
            "operator must work through the manual checklist in the UI before "
            "the first session.",
            report.get("enhancement_status"),
        )


def report_data_summary() -> None:
    """Log how much is already on disk, so a silent failure is visible."""
    try:
        counts = tracker.startup_summary()
    except Exception as exc:  # noqa: BLE001 — same reasoning as report_devices
        log.exception("Startup data summary failed")
        loud(
            "STARTUP DATA SUMMARY UNAVAILABLE",
            [
                f"{type(exc).__name__}: {exc}",
                f"Could not count what is already in {config.DATA_DIR}.",
                "Check that directory by hand before recording.",
            ],
        )
        return

    log.info(
        "data on disk | %s",
        ", ".join(f"{name}={value}" for name, value in sorted(counts.items())) or "nothing",
    )
    if not counts or all(value == 0 for value in counts.values()):
        log.warning(
            "No sessions or takes found under %s. If this is not the first "
            "session of the mission, yesterday's data is MISSING — stop and "
            "check before recording anything else.",
            config.DATA_DIR,
        )
    partials = counts.get("partials", 0)
    if partials:
        # A take only gets its final name once it is flushed and closed, so a
        # surviving .partial is a take that died mid-write.
        loud(
            f"{partials} UNFINISHED RECORDING(S) FOUND",
            [
                f"There are {partials} leftover .partial file(s) under {config.DATA_DIR}.",
                "A take died mid-write — the microphone was unplugged, or the",
                "process was killed. Find which session it was and redo that take.",
            ],
        )


def check_websocket_support() -> None:
    """The live meter, task state and errors all ride the WebSocket.

    With no WebSocket library installed, uvicorn answers /ws with 404 and the
    operator gets a dead level meter and no explanation. Say so at startup
    instead, while there is still time to fix it.
    """
    available = [
        name
        for name in WEBSOCKET_LIBRARIES
        if importlib.util.find_spec(name) is not None
    ]
    if available:
        log.info("websocket | %s available — live meter enabled", ", ".join(available))
        return
    loud(
        "NO WEBSOCKET LIBRARY — THE LIVE LEVEL METER WILL NOT WORK",
        [
            "uvicorn cannot serve /ws without one of: "
            + ", ".join(WEBSOCKET_LIBRARIES)
            + ".",
            "Recording still works, but the input level meter, the clipping",
            "indicator and live error messages will not reach the browser.",
            "Fix this BEFORE the mission, while there is still internet:",
            "    pip install websockets",
        ],
    )


def install_lifecycle_hooks(app: FastAPI) -> None:
    """Bind the event loop on startup, release the device on shutdown.

    ``service.bind_loop()`` needs the *running* loop: the audio threads push
    level meter and error messages across to it. Shutdown releases the input
    stream; completed takes are already safe on disk and nothing here can
    undo them.

    app.py owns create_app(), so the hooks are installed on the instance it
    returns rather than inside it.
    """
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        service.bind_loop()
        log.info("Event loop bound — WebSocket push channel is live")
        try:
            async with previous(app):
                yield
        finally:
            service.abandon_active_session()
            log.info("Shutdown: input device released")

    app.router.lifespan_context = lifespan


def install_error_handler(app: FastAPI) -> None:
    """Turn any unmapped exception into readable text for the operator.

    The routes map every expected failure themselves (ARCHITECTURE.md §14).
    Anything left over would otherwise reach the browser as a bare "Internal
    Server Error", which tells a tired operator nothing. Starlette re-raises
    after this handler returns, so the full traceback still reaches the log.
    """

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "detail": (
                    f"Unexpected server error on {request.url.path}: "
                    f"{type(exc).__name__}: {exc}"
                )
            },
        )


def build_app() -> FastAPI:
    app = create_app()
    install_lifecycle_hooks(app)
    install_error_handler(app)
    return app


def main() -> None:
    ensure_data_dirs()
    log_path = configure_logging()

    log.info(RULE)
    log.info("SPACE READY_4 voice capture %s starting", __version__)
    log.info(
        "Every timestamp in this log comes from the UNTRUSTED device clock. "
        "The trusted time is the operator-entered UTC in each session record."
    )
    log.info(
        "audio | %d Hz, %s, %d channel(s), blocksize %d frames",
        config.SAMPLE_RATE_HZ,
        config.SOUNDFILE_SUBTYPE,
        config.CHANNELS,
        config.BLOCKSIZE_FRAMES,
    )
    log.info("paths | data=%s log=%s", config.DATA_DIR, log_path)

    report_devices()
    report_data_summary()
    check_websocket_support()

    url = f"http://{config.HOST}:{config.PORT}/"
    log.info("serving on %s — loopback only, nothing is exposed to a network", url)
    open_browser = threading.Timer(BROWSER_OPEN_DELAY_S, webbrowser.open, args=(url,))
    open_browser.start()

    try:
        uvicorn.run(
            build_app(),
            host=config.HOST,  # 127.0.0.1, never 0.0.0.0
            port=config.PORT,
            log_level="info",
            log_config=None,  # keep our handlers: console + rotating file
        )
    except OSError as exc:
        open_browser.cancel()
        loud(
            "COULD NOT START THE SERVER",
            [
                f"{type(exc).__name__}: {exc}",
                f"Port {config.PORT} may already be in use.",
                "Is the capture tool already running in another window?",
            ],
        )
        raise
    finally:
        open_browser.cancel()


if __name__ == "__main__":
    main()
