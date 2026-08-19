"""Per-participant completion grid for the whole mission.

Reads master_log.csv (one flat file — no per-session JSON opens) and renders
participants x the 21 expected sessions, each cell complete / missing /
flagged. This is how the operator notices on day 4 that someone quietly
missed six sessions. Pure reporting: no new storage.

Cell rules (ARCHITECTURE.md §10):

    COMPLETE  every planned take of the session is present and none warned
    FLAGGED   the session started but is short of takes, or a take warned
    MISSING   no rows at all for that session number

"Every planned take" is derived from ``domain.tasks.iter_slots()`` — the same
data the recording flow walks — so changing the battery in config.py changes
what the dashboard demands, with no second copy of the number 13 anywhere.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Mapping

from capture import config
from capture.domain.tasks import iter_slots

log = logging.getLogger("capture.adherence")

# One planned recording: (task_number, stem, take_n). Matches the master log's
# task_number/stem/take columns, and TakeSlot's identity.
SlotKey = tuple[int, str, int]

# The columns the grid is actually built from. The full schema is
# config.MASTER_LOG_COLUMNS; only these are load-bearing here, so a writer
# that adds a column later does not break the dashboard.
_REQUIRED_COLUMNS: Final = (
    "participant",
    "session_number",
    "task_number",
    "stem",
    "take",
    "qc_status",
)

# The qc_status value written for a take with warnings (models.QCResult.status).
_WARN_STATUS: Final = "warn"

# Audio files are always uncompressed PCM WAV (CLAUDE.md, hard requirements) —
# this is the format, not a tunable, so it is not a config constant.
_WAV_SUFFIX: Final = ".wav"


class CellStatus(StrEnum):
    COMPLETE = "complete"
    MISSING = "missing"
    FLAGGED = "flagged"  # present but with QC warnings


@dataclass(frozen=True, slots=True)
class AdherenceGrid:
    participants: tuple[str, ...]
    expected_sessions: int
    # cells[pseudonym][session_number - 1]
    cells: dict[str, tuple[CellStatus, ...]]


def build_grid() -> AdherenceGrid:
    """Scan master_log.csv once and lay every participant out over the mission.

    A missing or empty log is day one, not a failure: the grid comes back
    valid, with every known participant showing all sessions MISSING.
    """
    expected_sessions = config.EXPECTED_SESSIONS_PER_PARTICIPANT
    expected_slots = _expected_slot_keys()

    # (participant, session_number) -> the planned slots seen for it. A key
    # present with an empty set still means "this session exists".
    seen_slots: dict[tuple[str, int], set[SlotKey]] = {}
    flagged: set[tuple[str, int]] = set()
    participants_in_log: set[str] = set()

    for row_number, row in enumerate(_read_master_log(), start=2):  # row 1 = header
        participant = _text(row, "participant")
        if not participant:
            log.warning(
                "master_log.csv row %d: no participant, row skipped",
                row_number,
            )
            continue
        participants_in_log.add(participant)

        session_number = _as_int(row, "session_number")
        if session_number is None:
            log.warning(
                "master_log.csv row %d: unreadable session_number %r for %s: "
                "this take is not represented on the adherence grid",
                row_number,
                row.get("session_number"),
                participant,
            )
            continue
        if not 1 <= session_number <= expected_sessions:
            log.warning(
                "master_log.csv row %d: session_number %d for %s is outside "
                "1..%d, the grid cannot show it",
                row_number,
                session_number,
                participant,
                expected_sessions,
            )
            continue

        cell = (participant, session_number)
        slots = seen_slots.setdefault(cell, set())

        if _text(row, "qc_status").lower() == _WARN_STATUS:
            flagged.add(cell)

        task_number = _as_int(row, "task_number")
        take_n = _as_int(row, "take")
        stem = _text(row, "stem")
        if task_number is None or take_n is None or not stem:
            # A row that cannot be placed cannot count towards completeness.
            # The cell stays short of its slots and therefore reads FLAGGED,
            # which is the loud outcome — never a silent COMPLETE.
            log.warning(
                "master_log.csv row %d: unreadable task_number/stem/take for "
                "%s session %d, counted as an incomplete session",
                row_number,
                participant,
                session_number,
            )
            continue

        slot: SlotKey = (task_number, stem, take_n)
        if slot in expected_slots:
            # Redos repeat a slot; a set means they neither double-count nor
            # hide the fact that the slot was covered.
            slots.add(slot)
        else:
            log.warning(
                "master_log.csv row %d: %s session %d has take %r, which is "
                "not in the current task battery",
                row_number,
                participant,
                session_number,
                slot,
            )

    participants = sorted(set(_read_participants()) | participants_in_log)
    cells: dict[str, tuple[CellStatus, ...]] = {}
    for participant in participants:
        cells[participant] = tuple(
            _cell_status(
                seen_slots.get((participant, n)),
                (participant, n) in flagged,
                expected_slots,
            )
            for n in range(1, expected_sessions + 1)
        )

    return AdherenceGrid(
        participants=tuple(participants),
        expected_sessions=expected_sessions,
        cells=cells,
    )


def startup_summary() -> dict[str, int]:
    """Totals shown on the opening screen: sessions and takes on disk so far
    (CLAUDE.md, Data safety — makes a silent failure yesterday visible).

    Also counts leftover ``.partial`` files. A take only gets its final name
    once it is flushed and closed (ARCHITECTURE.md §2), so a surviving
    ``.partial`` means a take died mid-write and the operator should know.
    """
    summary = {"sessions": 0, "takes": 0, "partials": 0}
    data_dir = config.DATA_DIR
    if not data_dir.is_dir():
        return summary  # nothing recorded yet — not an error

    for participant_dir in sorted(data_dir.iterdir()):
        if not participant_dir.is_dir():
            continue
        if participant_dir == config.CONSENT_DIR:
            continue  # consent JSONs, not sessions
        summary["sessions"] += sum(
            1 for child in participant_dir.iterdir() if child.is_dir()
        )

    # os.walk does not follow symlinks, so a stray link cannot loop the count.
    for _dirpath, _dirnames, filenames in os.walk(data_dir):
        for filename in filenames:
            lowered = filename.lower()
            if lowered.endswith(config.PARTIAL_SUFFIX.lower()):
                summary["partials"] += 1
            elif lowered.endswith(_WAV_SUFFIX):
                summary["takes"] += 1

    return summary


# --- internals ------------------------------------------------------------


def _expected_slot_keys() -> frozenset[SlotKey]:
    """Every planned recording of one complete session, from the battery."""
    return frozenset(
        (slot.task.number, slot.stem, slot.take_n) for slot in iter_slots()
    )


def _cell_status(
    slots: set[SlotKey] | None,
    warned: bool,
    expected_slots: frozenset[SlotKey],
) -> CellStatus:
    if slots is None:
        return CellStatus.MISSING
    if warned or slots != expected_slots:
        # Deliberately conservative: a warned take that was later redone
        # successfully still shows FLAGGED, because master_log.csv is
        # append-only and does not record which take was kept (meta.json
        # does, and the dashboard reads one flat file by design). An extra
        # look at a good session costs a glance; a hidden bad one costs data.
        return CellStatus.FLAGGED
    return CellStatus.COMPLETE


def _read_master_log() -> list[dict[str, str]]:
    """Every row of the master log, read in one pass. [] when there is none."""
    path: Path = config.MASTER_LOG_PATH
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return []  # empty file: header not written yet
        missing = [c for c in _REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise ValueError(
                f"{path} is missing required column(s) {missing}; its header is "
                f"{list(reader.fieldnames)}. The adherence grid cannot be "
                "trusted until this is fixed."
            )
        return [dict(row) for row in reader]


def _read_participants() -> list[str]:
    """Pseudonyms from the operator's roster file (config.PARTICIPANTS_PATH).

    The roster's exact shape belongs to the participant store, so this reader
    accepts the documented forms — a ``{pseudonym: passage_text}`` mapping
    (ARCHITECTURE.md §7), an object wrapping one under "participants", or a
    list of pseudonyms / participant objects — and raises on anything else
    rather than quietly showing an empty dashboard.
    """
    path: Path = config.PARTICIPANTS_PATH
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    raw = json.loads(text)  # a corrupt roster is worth shouting about

    if isinstance(raw, dict):
        inner = raw.get("participants")
        raw = inner if isinstance(inner, (dict, list)) else raw
    if isinstance(raw, dict):
        return [str(key).strip() for key in raw if str(key).strip()]
    if isinstance(raw, list):
        pseudonyms: list[str] = []
        for entry in raw:
            if isinstance(entry, str):
                name = entry.strip()
            elif isinstance(entry, dict) and "pseudonym" in entry:
                name = str(entry["pseudonym"]).strip()
            else:
                raise ValueError(
                    f"{path}: cannot read a pseudonym from entry {entry!r}"
                )
            if name:
                pseudonyms.append(name)
        return pseudonyms

    raise ValueError(f"{path}: expected an object or a list, got {type(raw).__name__}")


def _text(row: Mapping[str, object], column: str) -> str:
    """One CSV cell as trimmed text. Short rows give None; treat that as ''."""
    value = row.get(column)
    return value.strip() if isinstance(value, str) else ""


def _as_int(row: Mapping[str, object], column: str) -> int | None:
    """One CSV cell as an int, or None if it is blank or not a number.
    The caller logs; nothing is skipped without a warning."""
    text = _text(row, column)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None
