"""meta.json writer and master_log.csv appender — the two ledgers.

- meta.json: authoritative per-session record, updated as each task
  completes (never buffered until session end). Update pattern: serialize,
  write to a temp sibling, os.replace onto meta.json — atomic on the same
  filesystem, so a kill mid-update leaves the previous complete version.
  Fields are only ever added; nothing is rewritten away.
- master_log.csv: one row per completed task, appended IMMEDIATELY with a
  flush — never at session end. Header written once on creation, columns
  from config.MASTER_LOG_COLUMNS. Both clocks appear in every row, in full
  (ARCHITECTURE.md §5).

"Not available" is a first-class value and survives the round trip intact
(CLAUDE.md, Metadata; ARCHITECTURE.md §6):

    None   -> JSON null  -> None    the field was never asked
    "n/a"  -> JSON "n/a" -> "n/a"   the operator marked it not available

Those two are DIFFERENT records of what happened and are never collapsed
into one another, in either direction.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import tempfile
from dataclasses import fields
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from capture import config
from capture.domain.models import (
    Covariates,
    QCResult,
    ReferenceMeasures,
    SessionInfo,
    SessionMeta,
    TakeRecord,
)
from capture.errors import StorageWriteError
from capture.storage import paths

log = logging.getLogger("capture.storage.metadata")

# Written into every meta.json so a later reader can tell which layout it is
# looking at. Module-level rather than in config.py: config.py is off-limits
# to this module (this is a file-format tag, not an operator-tunable knob).
META_SCHEMA: Final = "capture.meta/1"


# --- Atomic write primitives -------------------------------------------------
#
# Shared by every ledger in this package. The pattern is always the same:
# write a temp sibling in the SAME directory (so os.replace stays on one
# filesystem and is therefore atomic), fsync it, then replace. A kill at any
# instant leaves either the old complete file or the new complete file —
# never a half-written one.


def atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` atomically. Raises StorageWriteError."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",  # write the text exactly as given, no line-ending magic
            dir=str(path.parent),
            prefix=f"{path.name}.tmp-",
            delete=False,
        )
    except OSError as exc:
        raise StorageWriteError(
            "atomic_write_failed", f"Could not open a temp file beside {path}: {exc}"
        ) from exc

    tmp_path = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        # Never leave a half-written temp file lying next to real data.
        tmp_path.unlink(missing_ok=True)
        raise StorageWriteError(
            "atomic_write_failed", f"Could not write {path}: {exc}"
        ) from exc


def atomic_write_json(path: Path, payload: object) -> None:
    """Serialize and atomically replace ``path``. UTF-8, human-readable."""
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    atomic_write_text(path, text + "\n")


def append_csv_row(
    path: Path, columns: Sequence[str], row: Mapping[str, object]
) -> None:
    """Append exactly one row, writing the header only when creating the file.

    Opened, written, flushed, fsynced and closed on every call — the handle is
    never held open between takes, so a kill can never lose a buffered row.
    A row that does not match ``columns`` exactly is a bug and raises rather
    than writing a misaligned line.
    """
    missing = [name for name in columns if name not in row]
    if missing:
        raise ValueError(f"{path.name} row is missing columns: {missing}")
    unknown = [name for name in row if name not in columns]
    if unknown:
        raise ValueError(f"{path.name} row has unknown columns: {unknown}")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists() or path.stat().st_size == 0
        if not write_header:
            _verify_csv_header(path, columns)
        with open(path, "a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(columns))
            if write_header:
                writer.writeheader()
            writer.writerow({name: row[name] for name in columns})
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise StorageWriteError(
            "csv_append_failed", f"Could not append to {path}: {exc}"
        ) from exc


def _verify_csv_header(path: Path, columns: Sequence[str]) -> None:
    """Refuse to append under a header that is not the one we are writing.

    Appending a 15-field row under a 14-field header would silently shift
    every value one column left in the analysis dataset. Loud failure instead.
    """
    with open(path, "r", encoding="utf-8", newline="") as handle:
        first_line = handle.readline()
    existing = next(csv.reader([first_line]), [])
    if tuple(existing) != tuple(columns):
        raise StorageWriteError(
            "csv_schema_mismatch",
            f"{path} has header {existing!r}, expected {list(columns)!r}. "
            "Refusing to append misaligned rows.",
        )


# --- meta.json ---------------------------------------------------------------


def write_meta(meta: SessionMeta) -> None:
    """Atomic-replace update of this session's meta.json."""
    path = paths.meta_path(meta.info.participant, meta.info.session_id)
    payload = {
        "schema": META_SCHEMA,
        "info": _dataclass_to_json(meta.info),
        "reference_measures": _dataclass_to_json(meta.reference_measures),
        "covariates": _dataclass_to_json(meta.covariates),
        "takes": [_take_to_json(take) for take in meta.takes],
    }
    atomic_write_json(path, payload)


def load_meta_if_exists(pseudonym: str, sid: str) -> SessionMeta | None:
    """For QC baselines and crash recovery; None when absent.

    Exact inverse of write_meta: what write_meta wrote comes back identical,
    "n/a" and None included.

    A file that exists but is damaged is reported at ERROR level (path and
    reason) and read as None rather than raised. This is deliberate and is
    the one place in the storage layer that degrades instead of stopping:
    this function is called once per PRIOR session every time a take is
    stopped, purely to build a QC baseline. Raising would let one damaged
    file from a previous day abort every remaining take of the live session —
    trading a nice-to-have baseline for the sample itself. QC already handles
    a missing baseline ("no baseline", never a false warning).

    Use read_meta_strict() where the truth about a damaged file matters.
    """
    path = paths.meta_path(pseudonym, sid)
    try:
        return read_meta_strict(pseudonym, sid)
    except (ValueError, OSError, StorageWriteError) as exc:
        log.error(
            "Damaged session metadata at %s: %s — ignored for this read. "
            "The file is left exactly as it is; inspect it after the session.",
            path,
            exc,
        )
        return None


def read_meta_strict(pseudonym: str, sid: str) -> SessionMeta | None:
    """Same read, but a damaged file raises instead of reading as None.

    None still means "no meta.json there at all".
    """
    path = paths.meta_path(pseudonym, sid)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StorageWriteError(
            "meta_read_failed", f"Could not read {path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a JSON object, got {type(raw).__name__}")

    info_raw = raw.get("info")
    if not isinstance(info_raw, dict):
        raise ValueError(f"{path}: missing the 'info' block")
    takes_raw = raw.get("takes", [])
    if not isinstance(takes_raw, list):
        raise ValueError(f"{path}: 'takes' must be a list")

    return SessionMeta(
        info=_info_from_json(info_raw, path),
        reference_measures=ReferenceMeasures(
            **_optional_values(ReferenceMeasures, _block(raw, "reference_measures", path))
        ),
        covariates=Covariates(
            **_optional_values(Covariates, _block(raw, "covariates", path))
        ),
        takes=[_take_from_json(item, path, i) for i, item in enumerate(takes_raw)],
    )


def append_master_row(row: Mapping[str, object]) -> None:
    """Append one row (config.MASTER_LOG_COLUMNS order) and flush."""
    append_csv_row(config.MASTER_LOG_PATH, config.MASTER_LOG_COLUMNS, row)


# --- Serialization -----------------------------------------------------------


def _scalar(field_name: str, value: Any) -> Any:
    """Pass a metadata value through untouched.

    A non-finite float (a silent take gives RMS -inf) is written as-is rather
    than dropped — losing the record is worse than writing a value only
    Python's json reads back — but it is shouted about in the log.
    """
    if isinstance(value, float) and not math.isfinite(value):
        log.warning("Non-finite value for %s: %r — written as-is", field_name, value)
    return value


def _dataclass_to_json(obj: Any) -> dict[str, Any]:
    """Flat {field: value} dict. Every field of these dataclasses is a JSON
    scalar, None, the "n/a" sentinel, or a tuple of strings."""
    out: dict[str, Any] = {}
    for spec in fields(obj):
        value = getattr(obj, spec.name)
        if isinstance(value, tuple):
            out[spec.name] = [_scalar(spec.name, item) for item in value]
        else:
            out[spec.name] = _scalar(spec.name, value)
    return out


def _take_to_json(take: TakeRecord) -> dict[str, Any]:
    out = _dataclass_to_json(take)
    out["qc"] = _dataclass_to_json(take.qc) if take.qc is not None else None
    return out


# --- Deserialization ---------------------------------------------------------


def _block(raw: Mapping[str, Any], key: str, where: Path) -> Mapping[str, Any]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{where}: '{key}' must be a JSON object")
    return value


def _require(raw: Mapping[str, Any], key: str, where: Path) -> Any:
    if key not in raw:
        raise ValueError(f"{where}: missing required field {key!r}")
    return raw[key]


def _optional_values(cls: type, raw: Mapping[str, Any]) -> dict[str, Any]:
    """Every field of ``cls`` pulled from ``raw``; absent means None, which is
    exactly what None encodes here — "never asked"."""
    return {spec.name: raw.get(spec.name) for spec in fields(cls)}


def _info_from_json(raw: Mapping[str, Any], where: Path) -> SessionInfo:
    return SessionInfo(
        participant=_require(raw, "participant", where),
        session_number=_require(raw, "session_number", where),
        session_id=_require(raw, "session_id", where),
        utc_operator_entered_iso=_require(raw, "utc_operator_entered_iso", where),
        device_clock_iso=_require(raw, "device_clock_iso", where),
        input_device_name=_require(raw, "input_device_name", where),
        sample_rate_hz=_require(raw, "sample_rate_hz", where),
        bit_depth=_require(raw, "bit_depth", where),
        os_gain_reading=raw.get("os_gain_reading"),
        mouth_to_mic_cm=raw.get("mouth_to_mic_cm"),
        room_label=raw.get("room_label"),
    )


def _qc_from_json(raw: Mapping[str, Any], where: Path) -> QCResult:
    warnings = raw.get("warnings", [])
    if not isinstance(warnings, list):
        raise ValueError(f"{where}: qc.warnings must be a list")
    return QCResult(
        clipped=_require(raw, "clipped", where),
        rms_dbfs=_require(raw, "rms_dbfs", where),
        peak_dbfs=_require(raw, "peak_dbfs", where),
        # None is meaningful: no baseline yet (first session), not a warning.
        rms_in_range=raw.get("rms_in_range"),
        voicing_detected=_require(raw, "voicing_detected", where),
        duration_ok=_require(raw, "duration_ok", where),
        warnings=tuple(warnings),
        # Both default to None for takes recorded before these were measured,
        # so older meta.json files still load. None is meaningful in each
        # case: "not measurable" and "floor not yet known".
        effective_bits=raw.get("effective_bits"),
        noise_floor_dbfs=raw.get("noise_floor_dbfs"),
    )


def _take_from_json(raw: Any, where: Path, index: int) -> TakeRecord:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: takes[{index}] must be a JSON object")
    qc_raw = raw.get("qc")
    if qc_raw is not None and not isinstance(qc_raw, dict):
        raise ValueError(f"{where}: takes[{index}].qc must be an object or null")
    return TakeRecord(
        filename=_require(raw, "filename", where),
        task_number=_require(raw, "task_number", where),
        task_key=_require(raw, "task_key", where),
        stem=_require(raw, "stem", where),
        take_n=_require(raw, "take_n", where),
        redo_n=_require(raw, "redo_n", where),
        device_clock_iso=_require(raw, "device_clock_iso", where),
        monotonic_offset_s=_require(raw, "monotonic_offset_s", where),
        duration_s=_require(raw, "duration_s", where),
        qc=_qc_from_json(qc_raw, where) if qc_raw is not None else None,
        kept=raw.get("kept", True),
        borg_cr10=raw.get("borg_cr10"),
    )


# --- Ledger maintenance (used only by storage.withdrawal) --------------------


def rewrite_csv_without(path: Path, column: str, value: str) -> int:
    """Rewrite ``path`` without the rows whose ``column`` equals ``value``.

    The ONE permitted rewrite of an append-only ledger, reserved for GDPR
    erasure (ARCHITECTURE.md §11). Header preserved, temp + atomic replace,
    and the file is left untouched when nothing matches. Returns how many
    rows were removed.
    """
    if not path.exists():
        return 0
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
    except OSError as exc:
        raise StorageWriteError(
            "csv_read_failed", f"Could not read {path}: {exc}"
        ) from exc

    if not rows:
        return 0
    header = rows[0]
    if column not in header:
        raise ValueError(
            f"{path} has no {column!r} column (header: {header!r}); "
            "cannot prove which rows belong to whom"
        )
    index = header.index(column)

    kept: list[list[str]] = []
    removed = 0
    for row in rows[1:]:
        if not row:  # blank line carries no data
            continue
        if len(row) <= index:
            raise ValueError(
                f"{path}: a data row is shorter than its header "
                f"({len(row)} fields); cannot decide whether it must be erased"
            )
        if row[index] == value:
            removed += 1
        else:
            kept.append(row)

    if removed == 0:
        return 0

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(kept)
    atomic_write_text(path, buffer.getvalue())
    return removed
