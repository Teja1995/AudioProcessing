"""Filename and directory builders, plus the never-overwrite guard.

Layout (CLAUDE.md, exactly):

    data/
      <participant_pseudonym>/
        <session_id>/
          meta.json
          01_silence.wav
          03_sustained_a_take1.wav
          ...
      master_log.csv
      consent/
        <participant_pseudonym>.json

Session IDs sort chronologically and never depend on the untrusted device
clock: zero-padded counter + operator-entered UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone
from itertools import count
from pathlib import Path

from capture import config
from capture.domain.tasks import TaskSpec


def session_id(session_number: int, utc_operator_entered: datetime) -> str:
    """E.g. ``001_20260822T073000Z``. Requires an aware datetime."""
    if utc_operator_entered.tzinfo is None:
        raise ValueError("session_id needs an aware (UTC) datetime")
    stamp = utc_operator_entered.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{session_number:03d}_{stamp}"


def participant_dir(pseudonym: str) -> Path:
    return config.DATA_DIR / pseudonym


def session_dir(pseudonym: str, sid: str) -> Path:
    return participant_dir(pseudonym) / sid


def meta_path(pseudonym: str, sid: str) -> Path:
    return session_dir(pseudonym, sid) / "meta.json"


def consent_path(pseudonym: str) -> Path:
    return config.CONSENT_DIR / f"{pseudonym}.json"


def take_filename(task: TaskSpec, stem: str, take_n: int, redo_n: int = 0) -> str:
    """``03_sustained_a_take2.wav``; redo adds ``_redo1`` (redo never deletes,
    never overwrites). Tasks 1-2 carry no take suffix, per the layout."""
    if stem not in task.stems:
        raise ValueError(f"Stem {stem!r} does not belong to task {task.key}")
    if not 1 <= take_n <= task.takes_per_stem:
        raise ValueError(
            f"take {take_n} out of range for {task.key} (1..{task.takes_per_stem})"
        )
    name = f"{task.number:02d}_{stem}"
    if task.take_suffix:
        name += f"_take{take_n}"
    if redo_n > 0:
        name += f"_redo{redo_n}"
    return name + ".wav"


def partial_path(final: Path) -> Path:
    """The in-progress sibling: ``x.wav`` -> ``x.wav.partial``."""
    return final.with_name(final.name + config.PARTIAL_SUFFIX)


def next_free_path(path: Path) -> Path:
    """Never-overwrite last resort. If ``path`` exists (it should not — redo
    numbering already avoids planned collisions), return a ``_dupN`` sibling.
    The caller must warn loudly whenever the returned path differs."""
    if not path.exists():
        return path
    for n in count(1):
        candidate = path.with_name(f"{path.stem}_dup{n}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise AssertionError("unreachable")


def ensure_data_dirs() -> None:
    """Create data/ and data/consent/ if missing. Safe to run every startup."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.CONSENT_DIR.mkdir(parents=True, exist_ok=True)
