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
PURGE_ACTION: Final = "purged"


def withdraw(pseudonym: str, utc_operator_entered_iso: str) -> dict[str, object]:
    """Withdraw a participant: their data leaves the study immediately.

    The audio and consent record are MOVED to config.WITHDRAWN_DIR rather
    than deleted. From the study's point of view the effect is the same --
    the log rows and registry entry go, the adherence grid forgets them, and
    the USB export no longer carries them. What differs is that a misclick by
    a tired operator at 3am no longer destroys a participant's entire week.

    Permanent erasure is ``purge_withdrawn()``, a separate deliberate act.

    GDPR note: the archived copy is STILL personal data. Withdrawal is not
    complete until it is purged, and the archive must not travel on the USB
    export. Purge before the dataset leaves the habitat.
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
    log.warning(
        "Withdrawal recorded for %s — moving their data out of the study "
        "now. It is archived, not erased: purging is a separate act.",
        pseudonym,
    )

    # (2) All audio and per-session metadata, MOVED out of the study.
    sessions, files, total_bytes = _measure_tree(participant_dir)
    archive_dir = _archive_dir(pseudonym, device_clock_iso)
    audio_removed = participant_dir.exists()
    if audio_removed:
        archive_dir.mkdir(parents=True, exist_ok=True)
        try:
            # A move within one filesystem is atomic and cannot half-copy a
            # take, unlike a copy-then-delete.
            shutil.move(str(participant_dir), str(archive_dir / "sessions"))
        except OSError as exc:
            raise StorageWriteError(
                "withdrawal_archive_failed",
                f"Could not move {participant_dir} to {archive_dir}: {exc}. "
                "The tombstone is already written; move it by hand.",
            ) from exc

    # (3) The consent record travels with them.
    consent_path = paths.consent_path(pseudonym)
    consent_removed = consent_path.exists()
    if consent_removed:
        archive_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(consent_path), str(archive_dir / consent_path.name))
        except OSError as exc:
            raise StorageWriteError(
                "withdrawal_archive_failed",
                f"Could not move {consent_path}: {exc}",
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
        "archived_to": str(archive_dir),
        "archived": audio_removed or consent_removed,
    }
    log.warning(
        "Withdrawal complete for %s. Data moved to %s and is pending purge: %s",
        pseudonym,
        archive_dir,
        summary,
    )
    return summary


def _archive_dir(pseudonym: str, device_clock_iso: str) -> Path:
    """Where a withdrawn participant's data waits for purging.

    Stamped with the device clock only to keep repeated withdrawals of the
    same pseudonym apart. It is not a trustworthy time and nothing reads it
    as one; the tombstone carries the operator-entered UTC.
    """
    stamp = device_clock_iso.replace(":", "").replace("-", "").replace(".", "")[:15]
    base = config.WITHDRAWN_DIR / f"{pseudonym}_{stamp}"
    # Two withdrawals of the same pseudonym within one second would otherwise
    # collide, and shutil.move into an existing directory nests rather than
    # renames — quietly burying one archive inside the other.
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}_{suffix}")
        suffix += 1
    return candidate


def list_withdrawn() -> list[dict[str, object]]:
    """Archived participants still awaiting permanent erasure."""
    root = config.WITHDRAWN_DIR
    if not root.exists():
        return []
    out: list[dict[str, object]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        sessions, files, total_bytes = _measure_tree(entry)
        pseudonym = entry.name.rsplit("_", 1)[0]
        out.append(
            {
                "archive": entry.name,
                "pseudonym": pseudonym,
                "sessions": sessions,
                "files": files,
                "bytes": total_bytes,
            }
        )
    return out


def purge_withdrawn(archive_name: str, utc_operator_entered_iso: str) -> dict[str, object]:
    """Permanently erase one archived withdrawal. This cannot be undone.

    Separate from withdraw() on purpose: the first act takes someone out of
    the study, which is urgent and should be easy; this one destroys the
    recordings, which is final and should not be reachable by a misclick.
    """
    if not isinstance(utc_operator_entered_iso, str) or not utc_operator_entered_iso.strip():
        raise ValueError(
            "Purging needs the operator-entered UTC — the laptop clock is "
            "scrambled and cannot stand in for it"
        )
    if "/" in archive_name or "\\" in archive_name or ".." in archive_name:
        raise ValueError(f"Not an archive name: {archive_name!r}")

    target = (config.WITHDRAWN_DIR / archive_name).resolve()
    root = config.WITHDRAWN_DIR.resolve()
    if root not in target.parents:
        raise ValueError(f"{archive_name!r} is not inside the withdrawn archive")
    if not target.is_dir():
        raise FileNotFoundError(f"No withdrawn archive named {archive_name}")

    sessions, files, total_bytes = _measure_tree(target)
    pseudonym = archive_name.rsplit("_", 1)[0]
    device_clock_iso = clock.read_device_clock().isoformat()

    # Tombstone the purge BEFORE deleting, same reasoning as the withdrawal:
    # the record of what happened must outlive the data it describes.
    append_csv_row(
        config.WITHDRAWAL_LOG_PATH,
        WITHDRAWAL_LOG_COLUMNS,
        {
            "pseudonym": pseudonym,
            "utc_operator_entered": utc_operator_entered_iso,
            "device_clock": device_clock_iso,
            "action": PURGE_ACTION,
        },
    )
    try:
        shutil.rmtree(target)
    except OSError as exc:
        raise StorageWriteError(
            "purge_failed",
            f"Could not delete {target}: {exc}. The purge is already recorded; "
            "finish the deletion by hand.",
        ) from exc

    summary = {
        "archive": archive_name,
        "pseudonym": pseudonym,
        "sessions_deleted": sessions,
        "files_deleted": files,
        "bytes_deleted": total_bytes,
        "utc_operator_entered": utc_operator_entered_iso,
        "device_clock": device_clock_iso,
    }
    log.warning("PURGE complete, data permanently erased: %s", summary)
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
