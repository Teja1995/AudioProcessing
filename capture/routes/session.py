"""Session flow: setup, reference measures, covariates, take lifecycle, Borg.

Request models encode the "n/a is a first-class value" rule directly in
their types: a field is either a value, the literal "n/a" (operator marked
not available), or omitted/None (never asked). Nothing here blocks a session
on a missing measure.

Every endpoint is a thin adapter over ``capture.session_service.service`` —
the one place that owns state, audio and storage ordering. Routes decide
nothing: they translate HTTP into a service call, and the service's answer
back into JSON.

This module also holds the SINGLE error mapping used by every route module
(ARCHITECTURE.md §14) — see ``http_errors`` below. It lives here, next to the
busiest router; the other route modules import it from here.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from capture.domain.models import Covariates, ReferenceMeasures
from capture.domain.state import IllegalTransition
from capture.domain.tasks import BY_NUMBER
from capture.errors import SessionFatalError
from capture.session_service import ActiveSession, service
from capture.storage import consent_store, participants

log = logging.getLogger("capture.routes.session")

router = APIRouter(prefix="/api/sessions", tags=["session"])


# --- The one error mapping (ARCHITECTURE.md §14) --------------------------
#
# Every detail string below is shown to the operator verbatim, so it has to
# read as plain English at 3 a.m. Nothing is ever swallowed into a 200: an
# exception either maps to a status code here, or reaches the unhandled-error
# handler installed in capture.__main__ and becomes a readable 500.


@contextmanager
def http_errors() -> Iterator[None]:
    """Translate domain exceptions into HTTP status codes.

    409 illegal transition  — the request does not fit the session state
    500 session-fatal       — recording cannot safely continue
    404 not found           — missing file, participant or session directory
    409 already exists      — refusing to overwrite an existing record
    400 bad value           — e.g. an unparseable operator UTC entry

    An ``HTTPException`` raised inside the block passes straight through.
    """
    try:
        yield
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SessionFatalError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"{exc.message} (error code: {exc.code})",
            headers={"X-Capture-Error-Code": exc.code},
        ) from exc
    except FileNotFoundError as exc:
        target = exc.filename or exc.strerror or str(exc)
        raise HTTPException(status_code=404, detail=f"Not found: {target}") from exc
    except FileExistsError as exc:
        target = exc.filename or exc.strerror or str(exc)
        raise HTTPException(
            status_code=409,
            detail=f"Already exists, refusing to overwrite: {target}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- Pseudonym validation -------------------------------------------------
#
# A pseudonym becomes a directory name under data/. Anything that could
# escape the data directory, or be unopenable on Windows, is rejected before
# a single byte is written. (Conceptually config; kept as module-level
# constants here because config.py is not this module's to edit.)

_PSEUDONYM_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
_WINDOWS_RESERVED: Final = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def require_pseudonym(pseudonym: str) -> str:
    """Return the cleaned pseudonym, or raise 400 with a readable reason."""
    name = pseudonym.strip()
    unusable = (
        _PSEUDONYM_RE.match(name) is None
        or name.endswith((" ", "."))
        or name.lower() in _WINDOWS_RESERVED
    )
    if unusable:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{pseudonym!r} is not a usable participant pseudonym. Use "
                "letters, digits, spaces, dot, dash or underscore, starting "
                "with a letter or digit, up to 64 characters."
            ),
        )
    return name


# --- Request models -------------------------------------------------------


class SessionStartRequest(BaseModel):
    participant: str
    # Operator-entered UTC from a trusted external source (watch on UTC /
    # GPS). Parsed by capture.domain.time.parse_operator_utc; the device
    # clock is captured server-side at the same instant, alongside — never
    # instead (ARCHITECTURE.md §5).
    utc_operator_entered: str
    mouth_to_mic_cm: float | Literal["n/a"] | None = None
    room_label: str | Literal["n/a"] | None = None


class ReferenceMeasuresRequest(BaseModel):
    """Criterion measures — entered at the same moment as recording."""

    urine_specific_gravity: float | Literal["n/a"] | None = None
    urine_colour_1_to_8: Annotated[int, Field(ge=1, le=8)] | Literal["n/a"] | None = None
    body_mass_kg: float | Literal["n/a"] | None = None
    fluid_intake_ml: float | Literal["n/a"] | None = None


class CovariatesRequest(BaseModel):
    minutes_since_exercise: int | Literal["n/a"] | None = None
    breathing_route: Literal["nasal", "oral", "n/a"] | None = None
    caffeine_since_last: bool | Literal["n/a"] | None = None
    alcohol_since_last: bool | Literal["n/a"] | None = None
    speaking_load_estimate: Annotated[int, Field(ge=0, le=10)] | Literal["n/a"] | None = None
    hours_slept: float | Literal["n/a"] | None = None
    upper_respiratory_symptoms: bool | Literal["n/a"] | None = None
    medication: str | Literal["n/a"] | None = None
    menstrual_cycle_phase: str | Literal["n/a"] | None = None
    habitat_temperature_c: float | Literal["n/a"] | None = None
    habitat_humidity_pct: float | Literal["n/a"] | None = None


class BorgRequest(BaseModel):
    # Borg CR-10 allows half steps; "n/a" never blocks the session.
    rating: Annotated[float, Field(ge=0, le=10)] | Literal["n/a"]


# --- Snapshots the browser reflects ---------------------------------------


def _state_or_none() -> dict[str, object] | None:
    """The state machine's own view, or None once the session is closed."""
    return service.active.state.snapshot() if service.has_active else None


def _describe(session: ActiveSession) -> dict[str, object]:
    """Identity block plus state for one active session.

    ``state`` is exactly the payload the WebSocket pushes as ``task_state``
    (minus its ``type`` key), so the browser renders one shape either way.
    Both clocks appear in full — neither ever stands in for the other.
    """
    info = session.meta.info
    return {
        "session_id": session.session_id,
        "participant": session.participant,
        "session_number": session.session_number,
        "utc_operator_entered": info.utc_operator_entered_iso,
        "device_clock_at_entry": info.device_clock_iso,
        "input_device_name": info.input_device_name,
        "sample_rate_hz": info.sample_rate_hz,
        "bit_depth": info.bit_depth,
        "os_gain_reading": info.os_gain_reading,
        "mouth_to_mic_cm": info.mouth_to_mic_cm,
        "room_label": info.room_label,
        "takes_recorded": len(session.meta.takes),
        # Non-null once recording has failed: the UI must block on this.
        "fatal": session.fatal,
        "state": session.state.snapshot(),
    }


def _require_current_slot(
    session: ActiveSession, task_number: int, take_n: int, stem: str | None
) -> None:
    """Reject a take command that does not address the slot the server is on.

    The server holds the truth; a stale tab, a back button or a double click
    must never be able to record into the wrong file.
    """
    slot = session.state.current_slot()
    mismatch = (
        slot.task.number != task_number
        or slot.take_n != take_n
        or (stem is not None and slot.stem != stem)
    )
    if mismatch:
        asked = f"task {task_number} take {take_n}"
        if stem is not None:
            asked += f" ({stem})"
        raise IllegalTransition(
            f"The session is at task {slot.task.number} ({slot.stem}) take "
            f"{slot.take_n}, but the request was for {asked}. Reload the page: "
            "the server holds the true session state."
        )


def _resolve_stem(task_number: int, stem: str | None) -> str:
    """Which file stem of the task is meant. Task 8 records two (/s/, /z/)."""
    task = BY_NUMBER.get(task_number)
    if task is None:
        raise HTTPException(
            status_code=404, detail=f"No task numbered {task_number} in the battery."
        )
    if stem is None:
        if len(task.stems) == 1:
            return task.stems[0]
        raise HTTPException(
            status_code=400,
            detail=(
                f"Task {task.number} records {list(task.stems)}. Say which one "
                "to redo with ?stem=..."
            ),
        )
    if stem not in task.stems:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Task {task.number} has no stem {stem!r}. Expected one of "
                f"{list(task.stems)}."
            ),
        )
    return stem


# --- Endpoints ------------------------------------------------------------


@router.get("/active")
async def active_session() -> dict[str, object] | None:
    """The session currently open on the server, or null.

    A browser refresh re-syncs from here: the server holds the state, the
    browser only reflects it (ARCHITECTURE.md §4).
    """
    if not service.has_active:
        return None
    return _describe(service.active)


@router.post("")
async def start_session(body: SessionStartRequest) -> dict[str, object]:
    """Create the session: anchor clocks, allocate session_id, open the
    InputStream, write the meta.json header."""
    participant = require_pseudonym(body.participant)

    # GDPR: no recording without a consent record on disk. Checked BEFORE any
    # session directory or audio stream exists (CLAUDE.md, Consent).
    with http_errors():
        consented = consent_store.has_consent(participant)
    if not consented:
        raise HTTPException(
            status_code=409,
            detail=(
                f"No consent record for {participant}. Go through the consent "
                "screen with them first — nothing may be recorded until then."
            ),
        )

    with http_errors():
        session = service.start_session(
            participant=participant,
            utc_operator_entered=body.utc_operator_entered,
            mouth_to_mic_cm=body.mouth_to_mic_cm,
            room_label=body.room_label,
        )
    log.info(
        "Session %s started for %s (operator UTC %s)",
        session.session_id,
        participant,
        session.meta.info.utc_operator_entered_iso,
    )
    return _describe(session)


@router.post("/{sid}/reference-measures")
async def submit_reference_measures(
    sid: str, body: ReferenceMeasuresRequest
) -> dict[str, object]:
    measures = ReferenceMeasures(
        urine_specific_gravity=body.urine_specific_gravity,
        urine_colour_1_to_8=body.urine_colour_1_to_8,
        body_mass_kg=body.body_mass_kg,
        fluid_intake_ml=body.fluid_intake_ml,
    )
    with http_errors():
        service.submit_reference_measures(sid, measures)
        return _describe(service.active)


@router.post("/{sid}/covariates")
async def submit_covariates(sid: str, body: CovariatesRequest) -> dict[str, object]:
    covariates = Covariates(
        minutes_since_exercise=body.minutes_since_exercise,
        breathing_route=body.breathing_route,
        caffeine_since_last=body.caffeine_since_last,
        alcohol_since_last=body.alcohol_since_last,
        speaking_load_estimate=body.speaking_load_estimate,
        hours_slept=body.hours_slept,
        upper_respiratory_symptoms=body.upper_respiratory_symptoms,
        medication=body.medication,
        menstrual_cycle_phase=body.menstrual_cycle_phase,
        habitat_temperature_c=body.habitat_temperature_c,
        habitat_humidity_pct=body.habitat_humidity_pct,
    )
    with http_errors():
        service.submit_covariates(sid, covariates)
        return _describe(service.active)


@router.post("/{sid}/tasks/{tid}/takes/{n}/start")
async def start_take(
    sid: str, tid: int, n: int, stem: str | None = None
) -> dict[str, object]:
    """Arm the engine at the slot's collision-checked path; begin writing."""
    with http_errors():
        session = service.require_session(sid)
        _require_current_slot(session, tid, n, stem)
        result = await service.start_take(sid)
        return {**result, "state": _state_or_none()}


@router.post("/{sid}/tasks/{tid}/takes/{n}/stop")
async def stop_take(
    sid: str, tid: int, n: int, stem: str | None = None
) -> dict[str, object]:
    """Finalize (.partial -> final), run QC, append the master_log row."""
    with http_errors():
        session = service.require_session(sid)
        _require_current_slot(session, tid, n, stem)
        result = await service.stop_take(sid)
        return {**result, "state": _state_or_none()}


@router.post("/{sid}/tasks/{tid}/takes/{n}/redo")
async def redo_take(
    sid: str, tid: int, n: int, stem: str | None = None
) -> dict[str, object]:
    """Rearm the same slot under the next _redoN suffix. Never deletes."""
    resolved = _resolve_stem(tid, stem)
    with http_errors():
        service.require_session(sid)
        result = await service.redo_take(sid, tid, resolved, n)
        return {**result, "state": _state_or_none()}


@router.post("/{sid}/tasks/{tid}/borg")
async def submit_borg(sid: str, tid: int, body: BorgRequest) -> dict[str, object]:
    with http_errors():
        service.submit_borg(sid, tid, body.rating)
    return {
        "ok": True,
        "task_number": tid,
        "rating": body.rating,
        "state": _state_or_none(),
    }


@router.get("/{sid}/qc-summary")
async def qc_summary(sid: str) -> dict[str, object]:
    """Pass/warn list for the QC review screen."""
    with http_errors():
        return service.qc_summary(sid)


@router.post("/{sid}/complete")
async def complete_session(sid: str) -> dict[str, object]:
    """Close the InputStream, final meta.json update, back to idle."""
    with http_errors():
        result = service.complete_session(sid)
    log.info("Session %s closed out", sid)
    return {**result, "ok": True, "state": _state_or_none()}
