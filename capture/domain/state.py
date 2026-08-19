"""SessionStateMachine — the ONLY place a session transition is legal.

The state lives entirely server-side; the browser reflects it, never holds
it. A refresh, back-button, or double-click cannot desynchronize a session
because any action that does not match the current legal state is rejected
here (ARCHITECTURE.md §4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from capture.domain.tasks import TakeSlot, iter_slots


class Phase(StrEnum):
    PARTICIPANT_SELECT = "participant_select"
    CONSENT = "consent"
    SESSION_SETUP = "session_setup"  # UTC entry, mic distance, room label
    REFERENCE_MEASURES = "reference_measures"
    COVARIATES = "covariates"
    TASK_BATTERY = "task_battery"
    QC_REVIEW = "qc_review"
    COMPLETE = "complete"


class TakeState(StrEnum):
    IDLE = "idle"
    ARMED = "armed"
    RECORDING = "recording"
    SAVED = "saved"


_LEGAL: Final[dict[Phase, frozenset[Phase]]] = {
    Phase.PARTICIPANT_SELECT: frozenset({Phase.CONSENT, Phase.SESSION_SETUP}),
    Phase.CONSENT: frozenset({Phase.SESSION_SETUP}),
    Phase.SESSION_SETUP: frozenset({Phase.REFERENCE_MEASURES}),
    Phase.REFERENCE_MEASURES: frozenset({Phase.COVARIATES}),
    Phase.COVARIATES: frozenset({Phase.TASK_BATTERY}),
    Phase.TASK_BATTERY: frozenset({Phase.QC_REVIEW}),
    # QC review may send the operator back to redo a flagged take.
    Phase.QC_REVIEW: frozenset({Phase.TASK_BATTERY, Phase.COMPLETE}),
    Phase.COMPLETE: frozenset(),
}


class IllegalTransition(RuntimeError):
    """Raised (never swallowed) when a request does not fit the session state."""


@dataclass(slots=True)
class SessionStateMachine:
    """Tracks phase plus position inside the fixed task battery."""

    phase: Phase = Phase.PARTICIPANT_SELECT
    slot_index: int = 0  # index into the planned slot list, battery order
    take_state: TakeState = TakeState.IDLE
    slots: list[TakeSlot] = field(default_factory=lambda: list(iter_slots()))
    # True while re-recording one specific slot from QC review, so finishing
    # it returns to QC review instead of replaying the rest of the battery.
    redo_mode: bool = False

    def transition(self, to: Phase) -> None:
        """Move between phases; anything not in the legal map raises."""
        if to not in _LEGAL[self.phase]:
            raise IllegalTransition(f"Cannot go {self.phase} -> {to}")
        self.phase = to

    def require(self, *phases: Phase) -> None:
        """Guard used by routes: the request is only legal in these phases."""
        if self.phase not in phases:
            raise IllegalTransition(
                f"Action requires phase in {[p.value for p in phases]}, "
                f"session is in {self.phase.value}"
            )

    # --- Task-battery flow ------------------------------------------------

    @property
    def battery_finished(self) -> bool:
        return self.slot_index >= len(self.slots)

    def current_slot(self) -> TakeSlot:
        if self.battery_finished:
            raise IllegalTransition("Task battery is finished — no current slot")
        return self.slots[self.slot_index]

    def find_slot_index(self, task_number: int, stem: str, take_n: int) -> int:
        for i, slot in enumerate(self.slots):
            if (
                slot.task.number == task_number
                and slot.stem == stem
                and slot.take_n == take_n
            ):
                return i
        raise IllegalTransition(
            f"No such slot: task {task_number}, stem {stem!r}, take {take_n}"
        )

    def arm_current(self) -> None:
        """IDLE/SAVED -> ARMED for the current slot.

        Battery order is fixed: there is deliberately no way to arm an
        arbitrary slot mid-battery. Only QC review reopens a specific take,
        via reopen_for_redo().
        """
        self.require(Phase.TASK_BATTERY)
        if self.take_state not in (TakeState.IDLE, TakeState.SAVED):
            raise IllegalTransition(f"Cannot arm while take is {self.take_state}")
        self.current_slot()  # raises if the battery is already finished
        self.take_state = TakeState.ARMED

    def mark_recording(self) -> None:
        if self.take_state is not TakeState.ARMED:
            raise IllegalTransition(f"Cannot start recording from {self.take_state}")
        self.take_state = TakeState.RECORDING

    def mark_saved(self) -> None:
        """A take finished cleanly: advance, or hand back to QC review."""
        if self.take_state is not TakeState.RECORDING:
            raise IllegalTransition(f"Cannot save a take that is {self.take_state}")
        self.take_state = TakeState.SAVED
        if self.redo_mode:
            self.redo_mode = False
            self.transition(Phase.QC_REVIEW)
            return
        self.slot_index += 1
        if self.battery_finished:
            self.transition(Phase.QC_REVIEW)

    def abort_take(self) -> None:
        """Session-fatal path: the take did not complete. Position is
        unchanged, so the same slot is retried rather than skipped."""
        self.take_state = TakeState.IDLE

    def reopen_for_redo(self, task_number: int, stem: str, take_n: int) -> None:
        """From QC review: reopen one specific slot under a redo suffix."""
        self.require(Phase.QC_REVIEW)
        self.slot_index = self.find_slot_index(task_number, stem, take_n)
        self.redo_mode = True
        self.take_state = TakeState.IDLE
        self.transition(Phase.TASK_BATTERY)

    def snapshot(self) -> dict[str, object]:
        """What the browser is told; it renders this, it never derives it."""
        slot = None if self.battery_finished else self.current_slot()
        return {
            "phase": self.phase.value,
            "take_state": self.take_state.value,
            "slot_index": self.slot_index,
            "total_slots": len(self.slots),
            "redo_mode": self.redo_mode,
            "task": None
            if slot is None
            else {
                "number": slot.task.number,
                "key": slot.task.key,
                "title": slot.task.title,
                "instruction": slot.task.instruction,
                "stem": slot.stem,
                "take": slot.take_n,
                "takes_total": slot.task.takes_per_stem,
                "stop": slot.task.stop.value,
                "target_s": slot.task.target_s,
                "spoken_demo": slot.task.spoken_demo,
                "borg": slot.task.borg,
            },
        }
