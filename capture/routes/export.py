"""One-click USB export with per-file verification.

Copies data/ to the destination, then re-walks BOTH trees and compares the
file set and every file's byte size — per file, not totals, so offsetting
mismatches cannot cancel. Any mismatch is a blocking, loud failure.

A failed verification comes back as HTTP 500 with the full report in the
body: there is no way for the UI to mistake it for a successful copy.

The name<->pseudonym key file is deliberately NOT exported.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from capture import config
from capture.routes.session import http_errors
from capture.storage import export as export_store

log = logging.getLogger("capture.routes.export")

router = APIRouter(prefix="/api/export", tags=["export"])


class UsbExportRequest(BaseModel):
    destination: str  # e.g. "E:\\space_ready_backup"


def _resolve_destination(raw: str) -> Path:
    """Turn the typed destination into a path, or refuse with a reason."""
    text = raw.strip().strip('"')
    if not text:
        raise HTTPException(
            status_code=400,
            detail="Enter the destination folder on the USB drive, e.g. E:\\space_ready_capture",
        )
    destination = Path(text).resolve()
    data_root = config.DATA_DIR.resolve()
    if destination == data_root or destination.is_relative_to(data_root):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{destination} is inside the data directory. Choose a folder "
                "on the USB drive instead — copying data into itself would "
                "corrupt the export."
            ),
        )
    if not destination.exists() and not destination.parent.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                f"{destination} does not exist and neither does "
                f"{destination.parent}. Is the USB drive plugged in?"
            ),
        )
    return destination


@router.post("/usb")
async def export_usb(body: UsbExportRequest) -> JSONResponse:
    destination = _resolve_destination(body.destination)
    log.info("USB export starting: %s -> %s", config.DATA_DIR, destination)

    with http_errors():
        # Copying gigabytes is blocking and slow; keep the event loop free so
        # the UI stays responsive and can show progress or errors.
        report = await asyncio.to_thread(export_store.copy_to_usb, destination)

    # str() so a mismatch is readable whatever shape the report uses.
    mismatches = [str(mismatch) for mismatch in report.mismatches]
    payload: dict[str, object] = {
        "ok": bool(report.ok),
        "destination": str(destination),
        "files_copied": report.files_copied,
        "bytes_copied": report.bytes_copied,
        "source_file_count": report.source_file_count,
        "destination_file_count": report.destination_file_count,
        "mismatches": mismatches,
    }

    if report.ok:
        payload["detail"] = (
            f"Export verified: {report.files_copied} files "
            f"({report.bytes_copied} bytes) copied to {destination}, and every "
            "file matched on name and byte size."
        )
        log.info("%s", payload["detail"])
        return JSONResponse(status_code=200, content=payload)

    payload["detail"] = (
        f"EXPORT FAILED VERIFICATION — {len(mismatches)} mismatch(es) between "
        f"{config.DATA_DIR} and {destination}. Do NOT treat this USB drive as a "
        "backup and do not delete anything from the laptop. Check the drive has "
        "free space, then run the export again."
    )
    log.error("%s Mismatches: %s", payload["detail"], mismatches)
    return JSONResponse(status_code=500, content=payload)
