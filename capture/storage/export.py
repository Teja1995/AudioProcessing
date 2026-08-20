"""One-click USB export with per-file verification (ARCHITECTURE.md §12).

Copies data/ to the operator's destination, then re-walks BOTH trees and
compares the file set and every file's byte size — per file, never totals, so
two offsetting mismatches cannot cancel each other out. Anything wrong lands
in ExportReport.mismatches with ok=False; the UI must treat that as a
blocking failure, not a toast.

Two things this deliberately never does:

- It never deletes anything at the destination. A file there that is not in
  data/ is reported and left alone — deciding what to remove from the
  operator's drive is not this tool's call.
- It never exports the name<->pseudonym key file. That file lives OUTSIDE
  data/ by design (ARCHITECTURE.md §11), so copying data/ and nothing else is
  exactly what keeps it out of the export. There is no filter here to forget.

.partial files under data/ ARE copied: the export is a faithful byte copy of
what is on the laptop, and a half-written take is still evidence.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from capture import config

log = logging.getLogger("capture.export")


@dataclass(frozen=True, slots=True)
class ExportReport:
    ok: bool
    files_copied: int
    bytes_copied: int
    source_file_count: int
    destination_file_count: int
    mismatches: tuple[str, ...]


def copy_to_usb(destination: Path) -> ExportReport:
    """Copy data/ to ``destination`` and verify it file by file.

    Returns a report; only ``ok=True`` means every file arrived at the right
    size. Raises ValueError if the destination overlaps the data directory —
    that is operator error, caught before anything is written.
    """
    source_root: Path = config.DATA_DIR
    destination = Path(destination)

    if not source_root.is_dir():
        return _failed_before_copy(f"No data directory to export: {source_root}")

    _refuse_overlapping_destination(source_root, destination)

    if destination.exists() and not destination.is_dir():
        return _failed_before_copy(
            f"Export destination is not a directory: {destination}"
        )
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.exception("Export could not open destination %s", destination)
        # Emitted strings stay ASCII: they are read on a Windows console at
        # 3 a.m., where a cp1252 terminal mangles anything else.
        return _failed_before_copy(
            f"Cannot open export destination {destination}: is the drive "
            f"plugged in and writable? ({exc})"
        )

    mismatches: list[str] = []
    files_copied = 0
    bytes_copied = 0

    source_files = _walk_files(source_root)
    for relative in sorted(source_files):
        source_file = source_files[relative]
        target = destination / source_file.relative_to(source_root)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target)
            # Measured at the destination, not assumed from the source.
            bytes_copied += target.stat().st_size
            files_copied += 1
        except OSError as exc:
            # Not swallowed: logged with its traceback, recorded as a
            # mismatch, and ok will be False. A drive that fills up halfway
            # must still produce a report naming every file that did not make
            # it, rather than an exception that hides the ones that did.
            log.exception("Export failed to copy %s", relative)
            mismatches.append(f"COPY FAILED: {relative}: {exc}")

    # Re-walk BOTH trees and compare. The counts reported below come from
    # these same two walks, so they always describe exactly what was verified.
    source_after = _walk_files(source_root)
    destination_after = _walk_files(destination)
    mismatches.extend(_compare(source_after, destination_after))

    report = ExportReport(
        ok=not mismatches,
        files_copied=files_copied,
        bytes_copied=bytes_copied,
        source_file_count=len(source_after),
        destination_file_count=len(destination_after),
        mismatches=tuple(mismatches),
    )
    if report.ok:
        log.info(
            "Export verified: %d files, %d bytes -> %s",
            report.files_copied,
            report.bytes_copied,
            destination,
        )
    else:
        log.error(
            "Export to %s FAILED verification with %d mismatch(es): %s",
            destination,
            len(report.mismatches),
            "; ".join(report.mismatches),
        )
    return report


# --- internals ------------------------------------------------------------


def _compare(
    source_files: dict[str, Path], destination_files: dict[str, Path]
) -> list[str]:
    """Compare two walked trees: the file sets, then every byte size.

    Sizes are compared one file at a time on purpose: a total would let a
    file that lost bytes cancel out against one that gained them.
    """
    mismatches: list[str] = []

    for relative in sorted(set(source_files) - set(destination_files)):
        mismatches.append(f"MISSING at destination: {relative}")

    for relative in sorted(set(destination_files) - set(source_files)):
        mismatches.append(
            "EXTRA at destination (left in place; nothing is ever deleted "
            f"there): {relative}"
        )

    for relative in sorted(set(source_files) & set(destination_files)):
        try:
            source_size = source_files[relative].stat().st_size
            destination_size = destination_files[relative].stat().st_size
        except OSError as exc:
            log.exception("Export could not stat %s during verification", relative)
            mismatches.append(f"UNVERIFIABLE: {relative}: {exc}")
            continue
        if source_size != destination_size:
            mismatches.append(
                f"SIZE MISMATCH: {relative}: source {source_size} bytes, "
                f"destination {destination_size} bytes"
            )

    return mismatches


def _walk_files(root: Path) -> dict[str, Path]:
    """Every file under ``root``, keyed by its POSIX-style relative path.

    os.walk does not follow symlinks, so a stray link cannot send the export
    into a loop or drag in files from outside data/.
    """
    found: dict[str, Path] = {}
    withdrawn = config.WITHDRAWN_DIR.name
    for dirpath, dirnames, filenames in os.walk(root):
        # Withdrawn participants' data is out of the study and awaiting
        # purge; it must never travel on the export. Pruning dirnames in
        # place stops os.walk descending into it at all.
        if Path(dirpath) == root and withdrawn in dirnames:
            dirnames.remove(withdrawn)
        directory = Path(dirpath)
        for filename in filenames:
            path = directory / filename
            found[path.relative_to(root).as_posix()] = path
    return found


def _refuse_overlapping_destination(source_root: Path, destination: Path) -> None:
    """A destination inside data/ (or containing it) would copy the dataset
    into itself. Caught before a single byte is written."""
    source_resolved = source_root.resolve()
    destination_resolved = destination.resolve()
    if destination_resolved == source_resolved:
        raise ValueError(
            f"Export destination {destination} IS the data directory. "
            "Choose a directory on the USB drive."
        )
    if source_resolved in destination_resolved.parents:
        raise ValueError(
            f"Export destination {destination} is inside the data directory "
            f"{source_root}. Choose a directory on the USB drive."
        )
    if destination_resolved in source_resolved.parents:
        raise ValueError(
            f"Export destination {destination} contains the data directory "
            f"{source_root}. Choose a directory on the USB drive."
        )


def _failed_before_copy(message: str) -> ExportReport:
    """A report for a failure that stopped the export before it started —
    loud, and impossible to mistake for a successful empty copy."""
    log.error("Export not attempted: %s", message)
    return ExportReport(
        ok=False,
        files_copied=0,
        bytes_copied=0,
        source_file_count=0,
        destination_file_count=0,
        mismatches=(message,),
    )
