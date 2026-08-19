"""Domain dataclasses shared across the app.

"Not available" handling (CLAUDE.md, Metadata): every reference measure and
covariate can be recorded as explicitly-not-available instead of a value, and
no form blocks the session on a missing field. The encoding distinguishes:

    None   -> the field was never asked / never shown (accidental skip,
              detectable afterwards)
    "n/a"  -> the operator explicitly marked it not available

Use ``NAable[T]`` for any such field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal, TypeAlias, TypeVar, Union

T = TypeVar("T")

NA: Final = "n/a"
NAable: TypeAlias = Union[T, Literal["n/a"], None]


@dataclass(frozen=True, slots=True)
class Participant:
    """A crew member, referenced ONLY by pseudonym inside this app.

    The name<->pseudonym mapping is never stored under data/ — see
    ARCHITECTURE.md §11.
    """

    pseudonym: str
    # Fixed connected-speech passage in the participant's native language,
    # reused identically every session (CLAUDE.md task 6).
    passage_text: str | None = None


@dataclass(frozen=True, slots=True)
class ReferenceMeasures:
    """Criterion measures, entered at the same moment as recording.

    Simultaneity matters — these are what the voice data is tested against.
    """

    urine_specific_gravity: NAable[float] = None
    urine_colour_1_to_8: NAable[int] = None
    body_mass_kg: NAable[float] = None
    fluid_intake_ml: NAable[float] = None


@dataclass(frozen=True, slots=True)
class Covariates:
    """Per-session covariate checklist (CLAUDE.md, Metadata)."""

    minutes_since_exercise: NAable[int] = None
    breathing_route: NAable[Literal["nasal", "oral"]] = None
    caffeine_since_last: NAable[bool] = None
    alcohol_since_last: NAable[bool] = None
    speaking_load_estimate: NAable[int] = None  # 0-10 self-estimate
    hours_slept: NAable[float] = None
    upper_respiratory_symptoms: NAable[bool] = None
    medication: NAable[str] = None
    menstrual_cycle_phase: NAable[str] = None  # where applicable
    habitat_temperature_c: NAable[float] = None
    habitat_humidity_pct: NAable[float] = None


@dataclass(frozen=True, slots=True)
class QCResult:
    """Immediate integrity checks on one finished take (ARCHITECTURE.md §9).

    These are sanity checks, not acoustic analysis.
    """

    clipped: bool
    rms_dbfs: float
    peak_dbfs: float
    # None = no baseline yet (participant's first session) — reported as
    # "no baseline", never as a false warning.
    rms_in_range: bool | None
    voicing_detected: bool
    duration_ok: bool
    warnings: tuple[str, ...] = ()

    @property
    def status(self) -> Literal["pass", "warn"]:
        return "warn" if self.warnings else "pass"


@dataclass(frozen=True, slots=True)
class TakeRecord:
    """One recorded take as written to meta.json / master_log.csv.

    Carries both clocks plus the monotonic offset — never one clock standing
    in for another (ARCHITECTURE.md §5).
    """

    filename: str
    task_number: int
    task_key: str
    stem: str
    take_n: int
    redo_n: int  # 0 = original attempt
    device_clock_iso: str  # laptop clock at take completion
    monotonic_offset_s: float  # since session start
    duration_s: float
    qc: QCResult | None = None
    kept: bool = True  # which file is the kept one for a redone slot
    borg_cr10: NAable[float] = None


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Identity block of one session — the meta.json session header."""

    participant: str
    session_number: int
    session_id: str
    utc_operator_entered_iso: str
    device_clock_iso: str
    input_device_name: str
    sample_rate_hz: int
    bit_depth: int
    os_gain_reading: NAable[str] = None
    mouth_to_mic_cm: NAable[float] = None
    room_label: NAable[str] = None


@dataclass(slots=True)
class SessionMeta:
    """The full, incrementally-built meta.json content for one session."""

    info: SessionInfo
    reference_measures: ReferenceMeasures = field(default_factory=ReferenceMeasures)
    covariates: Covariates = field(default_factory=Covariates)
    takes: list[TakeRecord] = field(default_factory=list)
