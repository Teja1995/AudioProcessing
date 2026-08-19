"""Read-side reporting: adherence grid, startup summary, device status.

All three endpoints are pure reads. In particular there is no gain or volume
setter here or anywhere else in the app: the microphone gain is set once
physically, taped and photographed, and the software must never touch it
(CLAUDE.md, Hard audio requirements; ARCHITECTURE.md §2).
"""

from __future__ import annotations

from fastapi import APIRouter

from capture import config
from capture.adherence import tracker
from capture.audio import devices
from capture.routes.session import http_errors

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/adherence")
async def adherence() -> dict[str, object]:
    """Participants x expected sessions, each cell complete/missing/flagged.

    This is the screen that makes six quietly-missed sessions visible on
    day 4 instead of day 7.
    """
    with http_errors():
        grid = tracker.build_grid()
    return {
        "participants": list(grid.participants),
        "expected_sessions": grid.expected_sessions,
        # str() rather than .value: CellStatus is a StrEnum, so this is the
        # cell status either way.
        "cells": {
            pseudonym: [str(cell) for cell in cells]
            for pseudonym, cells in grid.cells.items()
        },
    }


@router.get("/summary")
async def summary() -> dict[str, object]:
    """Sessions and takes on disk so far — shown at startup so a silent
    failure yesterday is visible today."""
    with http_errors():
        counts = tracker.startup_summary()
    result: dict[str, object] = dict(counts)
    result["data_dir"] = str(config.DATA_DIR)
    return result


@router.get("/devices")
async def device_report() -> dict[str, object]:
    """Input device name, OS gain reading, enhancement status. Read-only —
    no setter exists anywhere in this app, by design.

    The gain reading is the drift anchor: it is logged at startup and shown
    here so the operator can confirm nothing moved during the week.
    """
    with http_errors():
        report = devices.startup_report()
    return dict(report)
