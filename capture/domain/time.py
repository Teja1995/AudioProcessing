"""Dual-clock capture. The ONLY module allowed to read any clock.

Habitat device clocks are deliberately scrambled; the laptop clock cannot be
trusted. Every record carries BOTH the operator-entered UTC and the device
clock, plus a monotonic offset for intra-session ordering. No code path ever
derives, corrects, or substitutes one clock from another
(ARCHITECTURE.md §5).

If a review ever finds a ``datetime.now()`` call outside this module, that
is a bug.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class DualClock:
    """The clocks anchored at session start."""

    utc_operator_entered: datetime  # aware, UTC — from a trusted external source
    device_clock_at_entry: datetime  # aware, UTC — untrusted, recorded for drift
    monotonic_anchor: float  # time.monotonic() at the same instant


def read_device_clock() -> datetime:
    """The single permitted device-clock read in the codebase."""
    return datetime.now(timezone.utc)


def monotonic_now() -> float:
    return time.monotonic()


def parse_operator_utc(text: str) -> datetime:
    """Parse the operator's typed UTC entry. Strict; fails loudly.

    Accepts ISO 8601 (``2026-08-22T07:30`` or with seconds/offset). A naive
    entry is taken as UTC by definition — the operator is reading a UTC
    source. An entry with a non-UTC offset is rejected rather than converted,
    so a typo cannot masquerade as a timezone.
    """
    parsed = datetime.fromisoformat(text.strip())
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    if parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"Operator UTC entry must be UTC, got offset: {text!r}")
    return parsed.astimezone(timezone.utc)


def anchor_session(operator_entry: str) -> DualClock:
    """Capture all three clocks at (as close as possible) the same instant."""
    return DualClock(
        utc_operator_entered=parse_operator_utc(operator_entry),
        device_clock_at_entry=read_device_clock(),
        monotonic_anchor=monotonic_now(),
    )


def offset_since(clock: DualClock) -> float:
    """Seconds elapsed since session start, immune to any wall-clock lie."""
    return monotonic_now() - clock.monotonic_anchor
