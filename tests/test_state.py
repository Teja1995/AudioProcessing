"""Session phase legality — the browser can never force an illegal move."""

from __future__ import annotations

import unittest

from capture.domain.state import (
    IllegalTransition,
    Phase,
    SessionStateMachine,
    TakeState,
)


def battery_ready() -> SessionStateMachine:
    """A machine sitting at the start of the task battery."""
    return SessionStateMachine(phase=Phase.TASK_BATTERY)


class PhaseTransitionTests(unittest.TestCase):
    def test_happy_path(self) -> None:
        sm = SessionStateMachine()
        for phase in (
            Phase.SESSION_SETUP,  # already-consented participant
            Phase.REFERENCE_MEASURES,
            Phase.COVARIATES,
            Phase.TASK_BATTERY,
            Phase.QC_REVIEW,
            Phase.COMPLETE,
        ):
            sm.transition(phase)
        self.assertIs(sm.phase, Phase.COMPLETE)

    def test_first_time_participant_goes_through_consent(self) -> None:
        sm = SessionStateMachine()
        sm.transition(Phase.CONSENT)
        sm.transition(Phase.SESSION_SETUP)
        self.assertIs(sm.phase, Phase.SESSION_SETUP)

    def test_cannot_skip_to_battery(self) -> None:
        sm = SessionStateMachine()
        with self.assertRaises(IllegalTransition):
            sm.transition(Phase.TASK_BATTERY)

    def test_qc_review_can_reopen_battery(self) -> None:
        sm = SessionStateMachine(phase=Phase.QC_REVIEW)
        sm.transition(Phase.TASK_BATTERY)  # redo a flagged take
        self.assertIs(sm.phase, Phase.TASK_BATTERY)

    def test_complete_is_terminal(self) -> None:
        sm = SessionStateMachine(phase=Phase.COMPLETE)
        with self.assertRaises(IllegalTransition):
            sm.transition(Phase.TASK_BATTERY)

    def test_require_guard(self) -> None:
        sm = SessionStateMachine(phase=Phase.COVARIATES)
        sm.require(Phase.COVARIATES, Phase.REFERENCE_MEASURES)  # no raise
        with self.assertRaises(IllegalTransition):
            sm.require(Phase.TASK_BATTERY)


class TakeFlowTests(unittest.TestCase):
    def test_take_lifecycle_advances_one_slot(self) -> None:
        sm = battery_ready()
        first = sm.current_slot()
        sm.arm_current()
        self.assertIs(sm.take_state, TakeState.ARMED)
        sm.mark_recording()
        sm.mark_saved()
        self.assertEqual(sm.slot_index, 1)
        self.assertIsNot(sm.current_slot(), first)

    def test_cannot_record_without_arming(self) -> None:
        sm = battery_ready()
        with self.assertRaises(IllegalTransition):
            sm.mark_recording()

    def test_cannot_save_a_take_that_never_recorded(self) -> None:
        sm = battery_ready()
        sm.arm_current()
        with self.assertRaises(IllegalTransition):
            sm.mark_saved()

    def test_battery_end_hands_over_to_qc_review(self) -> None:
        sm = battery_ready()
        for _ in range(len(sm.slots)):
            sm.arm_current()
            sm.mark_recording()
            sm.mark_saved()
        self.assertTrue(sm.battery_finished)
        self.assertIs(sm.phase, Phase.QC_REVIEW)

    def test_aborted_take_retries_the_same_slot(self) -> None:
        # A device failure must not silently skip a take.
        sm = battery_ready()
        slot = sm.current_slot()
        sm.arm_current()
        sm.mark_recording()
        sm.abort_take()
        self.assertEqual(sm.slot_index, 0)
        self.assertIs(sm.current_slot(), slot)
        self.assertIs(sm.take_state, TakeState.IDLE)

    def test_arming_past_the_end_is_rejected(self) -> None:
        sm = battery_ready()
        sm.slot_index = len(sm.slots)
        with self.assertRaises(IllegalTransition):
            sm.current_slot()


class RedoTests(unittest.TestCase):
    def test_redo_returns_to_qc_review_not_the_rest_of_the_battery(self) -> None:
        sm = SessionStateMachine(phase=Phase.QC_REVIEW)
        sm.slot_index = len(sm.slots)  # battery already finished
        sm.reopen_for_redo(3, "sustained_a", 2)
        self.assertIs(sm.phase, Phase.TASK_BATTERY)
        self.assertTrue(sm.redo_mode)
        sm.arm_current()
        sm.mark_recording()
        sm.mark_saved()
        self.assertIs(sm.phase, Phase.QC_REVIEW)
        self.assertFalse(sm.redo_mode)

    def test_redo_targets_the_named_slot(self) -> None:
        sm = SessionStateMachine(phase=Phase.QC_REVIEW)
        sm.reopen_for_redo(9, "mpt", 2)
        slot = sm.current_slot()
        self.assertEqual(slot.task.number, 9)
        self.assertEqual(slot.take_n, 2)

    def test_unknown_slot_is_rejected(self) -> None:
        sm = SessionStateMachine(phase=Phase.QC_REVIEW)
        with self.assertRaises(IllegalTransition):
            sm.reopen_for_redo(3, "sustained_a", 99)

    def test_redo_only_from_qc_review(self) -> None:
        sm = battery_ready()
        with self.assertRaises(IllegalTransition):
            sm.reopen_for_redo(3, "sustained_a", 1)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_describes_the_current_task(self) -> None:
        sm = battery_ready()
        snap = sm.snapshot()
        self.assertEqual(snap["phase"], "task_battery")
        task = snap["task"]
        assert isinstance(task, dict)
        self.assertEqual(task["number"], 1)
        self.assertEqual(task["key"], "silence")
        self.assertEqual(task["takes_total"], 1)

    def test_snapshot_task_is_none_when_battery_finished(self) -> None:
        sm = battery_ready()
        sm.slot_index = len(sm.slots)
        self.assertIsNone(sm.snapshot()["task"])

    def test_snapshot_never_offers_a_demo_for_a_vowel_task(self) -> None:
        # Playing a tonal model would anchor the participant's pitch.
        sm = battery_ready()
        for _ in range(len(sm.slots)):
            snap = sm.snapshot()
            task = snap["task"]
            assert isinstance(task, dict)
            if task["key"] in ("sustained_a", "sustained_i", "mpt"):
                self.assertFalse(task["spoken_demo"], task["key"])
            sm.arm_current()
            sm.mark_recording()
            sm.mark_saved()


if __name__ == "__main__":
    unittest.main()
