"""The fixed 9-task battery, parsed from config data into typed specs.

Order matters and is validated here: maximal-effort tasks go last. Vowel
tasks must never carry a spoken demo (pitch anchoring — fundamental
frequency is one of the measures); that rule is enforced at import time so a
config edit cannot silently break it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Iterator

from capture import config


class StopMode(StrEnum):
    AUTO = "auto"  # fixed duration, stops itself
    MANUAL = "manual"  # operator stops it (SAFETY_CAP_S backstop applies)


@dataclass(frozen=True, slots=True)
class TaskSpec:
    number: int  # 1-9, battery order == filename prefix
    key: str
    title: str
    instruction: str  # short, language-simple, shown on screen
    stems: tuple[str, ...]  # file stems; task 8 records two (/s/ and /z/)
    takes_per_stem: int
    stop: StopMode
    target_s: float | None  # None = max-effort / guidance-only, no ceiling
    qc_floor_s: float  # below this duration the take is implausible
    take_suffix: bool  # False for tasks 1-2, matching CLAUDE.md's layout
    spoken_demo: bool
    is_vowel: bool
    borg: bool  # collect Borg CR-10 after this task


@dataclass(frozen=True, slots=True)
class TakeSlot:
    """One plannable recording unit: (task, stem, take number)."""

    task: TaskSpec
    stem: str
    take_n: int


def _build() -> tuple[TaskSpec, ...]:
    specs = tuple(
        TaskSpec(
            number=raw["number"],
            key=raw["key"],
            title=raw["title"],
            instruction=raw["instruction"],
            stems=tuple(raw["stems"]),
            takes_per_stem=raw["takes_per_stem"],
            stop=StopMode(raw["stop"]),
            target_s=raw["target_s"],
            qc_floor_s=raw["qc_floor_s"],
            take_suffix=raw["take_suffix"],
            spoken_demo=raw["spoken_demo"],
            is_vowel=raw["is_vowel"],
            borg=raw["borg"],
        )
        for raw in config.TASK_BATTERY
    )

    # Fail loudly at import if the battery data is malformed.
    if len(specs) != 9:
        raise ValueError(f"Task battery must have exactly 9 tasks, got {len(specs)}")
    if [t.number for t in specs] != list(range(1, 10)):
        raise ValueError("Task numbers must be 1..9 in battery order — do not reorder")
    anchored = [t.key for t in specs if t.is_vowel and t.spoken_demo]
    if anchored:
        raise ValueError(
            f"Vowel tasks must never have a spoken demo (pitch anchoring): {anchored}"
        )
    all_stems = [s for t in specs for s in t.stems]
    if len(all_stems) != len(set(all_stems)):
        raise ValueError("File stems must be unique across the battery")
    return specs


TASKS: Final[tuple[TaskSpec, ...]] = _build()
BY_NUMBER: Final[dict[int, TaskSpec]] = {t.number: t for t in TASKS}


def iter_slots() -> Iterator[TakeSlot]:
    """All planned recordings of one session, in battery order."""
    for task in TASKS:
        for stem in task.stems:
            for take_n in range(1, task.takes_per_stem + 1):
                yield TakeSlot(task=task, stem=stem, take_n=take_n)
