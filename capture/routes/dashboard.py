"""Read-side reporting: adherence grid, startup summary, device status.

All three endpoints are pure reads. In particular there is no gain or volume
setter here or anywhere else in the app: the microphone gain is set once
physically, taped and photographed, and the software must never touch it
(CLAUDE.md, Hard audio requirements; ARCHITECTURE.md §2).
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from capture import config
from capture.adherence import tracker
from capture.audio import devices, selection
from capture.storage import paths
from capture.errors import DeviceError
from fastapi import HTTPException
from capture.routes.session import http_errors
from capture.session_service import service

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
    # A data directory inside OneDrive/Dropbox/etc uploads every take
    # automatically, which CLAUDE.md forbids and GDPR takes seriously.
    result["cloud_sync_warning"] = paths.cloud_sync_warning()
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
    result = dict(report)
    # The level meter's scale must come from config, not a copy in the JS:
    # app.js falls back to a hardcoded -60 when this key is absent, which
    # would silently drift from config.METER_FLOOR_DBFS.
    result["meter_floor_dbfs"] = config.METER_FLOOR_DBFS
    return result


class SelectDeviceRequest(BaseModel):
    """Which input device to record from, by its current PortAudio index.

    The index is only a handle for this request: the server stores the
    device's name and host API instead, because PortAudio renumbers devices
    whenever anything is plugged in or unplugged.
    """

    index: int


# A device problem the operator caused or can fix is not a server fault, so
# these do not go through http_errors() (which maps every DeviceError to 500).
_DEVICE_ERROR_STATUS: dict[str, int] = {
    "session_active": 409,  # finish the session, then change microphone
    "device_not_found": 409,  # list is stale — refresh and choose again
    "device_unsuitable": 400,  # this device cannot record the study format
    "selected_device_missing": 409,  # chosen microphone is unplugged
}


def _device_http(exc: DeviceError) -> HTTPException:
    return HTTPException(
        status_code=_DEVICE_ERROR_STATUS.get(exc.code, 500),
        detail=exc.message,
        headers={"X-Capture-Error-Code": exc.code},
    )


def _refuse_while_recording() -> None:
    """Swapping microphones mid-session would make that session's takes
    incomparable with each other, so it is refused outright."""
    if service.has_active:
        raise _device_http(
            DeviceError(
                "session_active",
                "A session is in progress. Finish it before changing the "
                "microphone — swapping devices mid-session would make its "
                "takes incomparable with each other.",
            )
        )


@router.get("/devices/inputs")
async def list_inputs() -> dict[str, object]:
    """Every connected input, probed at the study's capture format.

    Each entry carries `supports_capture`, `rate_is_native` and a list of
    plain-language `warnings`, so the operator can see BEFORE recording that
    Windows would resample a device, rather than discovering it in the audio
    afterwards.
    """
    try:
        found = selection.list_input_devices()
    except DeviceError as exc:
        raise _device_http(exc) from exc
    groups = selection.group_microphones(found)
    return {
        # One entry per real microphone, host-API aliases collapsed. The UI
        # shows these; `devices` stays for anything wanting the raw list.
        "groups": selection.groups_as_dicts(groups),
        "devices": selection.as_dicts(found),
        "selected": selection.load_selection(),
        "required": {
            "sample_rate_hz": config.SAMPLE_RATE_HZ,
            "channels": config.CHANNELS,
            "subtype": config.SOUNDFILE_SUBTYPE,
        },
        "session_active": service.has_active,
    }


@router.post("/devices/select")
async def select_input(body: SelectDeviceRequest) -> dict[str, object]:
    """Choose the microphone to record from.

    Refuses a device that cannot deliver the study format, and refuses to
    change anything while a session is running.
    """
    _refuse_while_recording()
    try:
        chosen = selection.save_selection(body.index)
    except DeviceError as exc:
        raise _device_http(exc) from exc
    return {
        "selected": {"name": chosen.name, "host_api": chosen.host_api},
        "warnings": list(chosen.warnings),
        "recommended": chosen.recommended,
    }


@router.post("/devices/select/clear")
async def clear_input_selection() -> dict[str, object]:
    """Fall back to whichever device Windows treats as the default."""
    _refuse_while_recording()
    selection.clear_selection()
    return {"selected": None}


@router.get("/demos")
async def demo_availability() -> dict[str, object]:
    """Which spoken-example files actually exist on disk.

    The UI asks before offering a "play the example" control, so a missing
    file becomes a calm instruction to demonstrate out loud rather than a
    failed playback the operator discovers mid-task.

    Vowel tasks never appear here. Playing a model of /a/, /i/ or maximum
    phonation time would anchor the participant's pitch, and fundamental
    frequency is one of the study's measures (CLAUDE.md, task battery).
    """
    from capture.domain.tasks import TASKS

    available: dict[str, bool] = {}
    for task in TASKS:
        if not task.spoken_demo:
            continue
        path = config.STATIC_DIR / "audio" / f"demo_{task.key}.wav"
        available[task.key] = path.is_file()
    return {"demos": available}
