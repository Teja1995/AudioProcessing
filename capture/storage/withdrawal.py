"""Participant withdrawal — the GDPR delete-everything function.

Contract (ARCHITECTURE.md §11):
- delete data/<pseudonym>/ entirely (audio + metadata),
- delete data/consent/<pseudonym>.json,
- append a tombstone row (pseudonym, operator UTC, "withdrawn") to
  config.WITHDRAWAL_LOG_PATH so the FACT of withdrawal survives the data,
- never touch the name<->pseudonym key file (operator-managed, elsewhere),
- master_log.csv rows for the participant are also removed — the one
  permitted rewrite of that file, done via temp + atomic replace.

Irreversible; the route must require explicit confirmation.

Order is load-bearing: the tombstone is written FIRST, before anything is
deleted. If the machine dies halfway through the deletion, the record that a
withdrawal was requested still exists and the operator can finish the job.
The reverse order could delete the data and lose the evidence that it was
ever asked for.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Final

from capture import config
from capture.domain import time as clock
from capture.errors import StorageWriteError
from capture.storage import paths, participants
from capture.storage.metadata import append_csv_row, rewrite_csv_without

log = logging.getLogger("capture.storage.withdrawal")

# Tombstone ledger schema; module-level because config.py is off-limits here.
WITHDRAWAL_LOG_COLUMNS: Final = (
    "pseudonym",
    "utc_operator_entered",
    "device_clock",
    "action",
)
WITHDRAWAL_ACTION: Final = "withdrawn"


def withdraw(pseudonym: str, utc_operator_entered_iso: str) -> dict[str, object]:
    """Erase everything recorded for one participant. Irreversible.

    Returns a summary of what was actually removed, so the operator sees a
    concrete count instead of a bare "done".
    """
    participants.validate_pseudonym(pseudonym)
    if not isinstance(utc_operator_entered_iso, str) or not utc_operator_entered_iso.strip():
        raise ValueError(
            "Withdrawal needs the operator-entered UTC — the laptop clock is "
            "scrambled and cannot stand in for it"
        )

    participant_dir = paths.participant_dir(pseudonym)
    _assert_inside_data_dir(participant_dir)

    # (1) Tombstone first: the fact of withdrawal must outlive the data even
    #     if the deletion below is interrupted.
    device_clock_iso = clock.read_device_clock().isoformat()
    append_csv_row(
        config.WITHDRAWAL_LOG_PATH,
        WITHDRAWAL_LOG_COLUMNS,
        {
            "pseudonym": pseudonym,
            "utc_operator_entered": utc_operator_entered_iso,
            "device_clock": device_clock_iso,
            "action": WITHDRAWAL_ACTION,
        },
    )
    log.warning("Withdrawal recorded for %s — deleting their data now", pseudonym)

    # (2) All audio and per-session metadata.
    sessions, files, total_bytes = _measure_tree(participant_dir)
    audio_removed = participant_dir.exists()
    if audio_removed:
        try:
            shutil.rmtree(participant_dir)
        except OSError as exc:
            raise StorageWriteError(
                "withdrawal_delete_failed",
                f"Could not delete {participant_dir}: {exc}. The tombstone is "
                "already written; finish the deletion by hand.",
            ) from exc

    # (3) The consent record itself.
    consent_path = paths.consent_path(pseudonym)
    consent_removed = consent_path.exists()
    if consent_removed:
        try:
            consent_path.unlink()
        except OSError as exc:
            raise StorageWriteError(
                "withdrawal_delete_failed",
                f"Could not delete {consent_path}: {exc}",
            ) from exc

    # (4) Their rows in master_log.csv — the one permitted rewrite.
    rows_removed = rewrite_csv_without(
        config.MASTER_LOG_PATH, "participant", pseudonym
    )

    # (5) The registry entry (pseudonym + their fixed passage text).
    registry_removed = participants.remove_participant(pseudonym)

    summary: dict[str, object] = {
        "pseudonym": pseudonym,
        "utc_operator_entered": utc_operator_entered_iso,
        "device_clock": device_clock_iso,
        "audio_dir_removed": audio_removed,
        "sessions_removed": sessions,
        "files_removed": files,
        "bytes_removed": total_bytes,
        "consent_removed": consent_removed,
        "master_log_rows_removed": rows_removed,
        "registry_entry_removed": registry_removed,
        "tombstone_path": str(config.WITHDRAWAL_LOG_PATH),
    }
    log.warning("Withdrawal complete for %s: %s", pseudonym, summary)
    return summary


def _measure_tree(directory: Path) -> tuple[int, int, int]:
    """(session directories, files, total bytes) under a participant folder."""
    if not directory.exists():
        return (0, 0, 0)
    sessions = sum(1 for child in directory.iterdir() if child.is_dir())
    files = 0
    total_bytes = 0
    for path in directory.rglob("*"):
        if path.is_file():
            files += 1
            total_bytes += path.stat().st_size
    return (sessions, files, total_bytes)


def _assert_inside_data_dir(directory: Path) -> None:
    """Guard the recursive delete: it may only ever hit a child of data/.

    validate_pseudonym already rejects separators and "..", so this can only
    fire on a programming error — which is exactly when a guard on rmtree
    earns its place.
    """
    resolved = directory.resolve()
    data_root = config.DATA_DIR.resolve()
    if resolved == data_root or data_root not in resolved.parents:
        raise ValueError(
            f"Refusing to delete {resolved}: it is not inside {data_root}"
        )
