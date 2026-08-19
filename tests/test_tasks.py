"""Battery invariants — these encode CLAUDE.md rules, so a failing test here
means the protocol is broken, not just the code."""

from __future__ import annotations

import unittest

from capture.domain.tasks import TASKS, StopMode, iter_slots


class TaskBatteryTests(unittest.TestCase):
    def test_exactly_nine_tasks_in_order(self) -> None:
        self.assertEqual([t.number for t in TASKS], list(range(1, 10)))

    def test_order_matches_protocol(self) -> None:
        # Maximal-effort tasks last; do not reorder without being asked.
        self.assertEqual(
            [t.key for t in TASKS],
            [
                "silence",
                "reference_tone",
                "sustained_a",
                "sustained_i",
                "soft_pa",
                "connected_speech",
                "pataka",
                "s_z",
                "mpt",
            ],
        )

    def test_no_vowel_task_has_spoken_demo(self) -> None:
        for task in TASKS:
            if task.is_vowel:
                self.assertFalse(
                    task.spoken_demo, f"{task.key}: vowel demo would anchor pitch"
                )

    def test_take_counts(self) -> None:
        by_key = {t.key: t for t in TASKS}
        self.assertEqual(by_key["sustained_a"].takes_per_stem, 3)
        self.assertEqual(by_key["mpt"].takes_per_stem, 2)
        self.assertEqual(by_key["s_z"].stems, ("sustained_s", "sustained_z"))

    def test_max_effort_tasks_have_no_ceiling_but_a_floor(self) -> None:
        for key in ("s_z", "mpt"):
            task = next(t for t in TASKS if t.key == key)
            self.assertIsNone(task.target_s)
            self.assertGreater(task.qc_floor_s, 0)
            self.assertIs(task.stop, StopMode.MANUAL)

    def test_slot_expansion(self) -> None:
        slots = list(iter_slots())
        # 1 + 1 + 3 + 1 + 1 + 1 + 1 + 2 (s,z) + 2 = 13 planned recordings
        self.assertEqual(len(slots), 13)
        self.assertEqual(slots[0].task.key, "silence")
        self.assertEqual(slots[-1].task.key, "mpt")




class InstructionClarityTests(unittest.TestCase):
    """The crew is international and the operator is tired. Every instruction
    has to say which sound to make without relying on phonetic notation."""

    def test_every_instruction_is_a_real_sentence(self) -> None:
        for task in TASKS:
            self.assertGreater(
                len(task.instruction), 40, f"{task.key}: instruction too terse"
            )
            self.assertTrue(
                task.instruction.rstrip().endswith("."),
                f"{task.key}: instruction should read as prose",
            )

    def test_sound_tasks_anchor_to_a_familiar_word(self) -> None:
        # "/a/" means nothing to a non-specialist; "the vowel in FATHER" does.
        # A written anchor conveys WHICH sound without conveying a pitch, so
        # it is safe for the vowel tasks where an audio model never is.
        anchors = {
            "sustained_a": "FATHER",
            "sustained_i": "SEE",
            "soft_pa": "PARK",
            "mpt": "FATHER",
        }
        for key, anchor in anchors.items():
            task = next(t for t in TASKS if t.key == key)
            self.assertIn(anchor, task.instruction, f"{key}: no word anchor")

    def test_no_instruction_uses_slash_phonetic_notation(self) -> None:
        for task in TASKS:
            self.assertNotIn(
                "/a/", task.instruction, f"{task.key}: raw phonetic notation"
            )
            self.assertNotIn(
                "/i/", task.instruction, f"{task.key}: raw phonetic notation"
            )

    def test_effort_tasks_say_what_maximal_means(self) -> None:
        # Task 5 is about minimal effort, 7 about speed, 8 and 9 about
        # duration. Each instruction must carry its own emphasis.
        by_key = {t.key: t.instruction for t in TASKS}
        self.assertIn("QUIETLY", by_key["soft_pa"])
        self.assertIn("FAST", by_key["pataka"])
        self.assertIn("as long as you can", by_key["s_z"])
        self.assertIn("AS LONG AS YOU POSSIBLY CAN", by_key["mpt"])


if __name__ == "__main__":
    unittest.main()
