"""Typed errors shared across layers. No bare excepts anywhere catch these.

Two severities, matching ARCHITECTURE.md §14:

- ``SessionFatalError`` — the session cannot safely continue (device lost,
  write failed, disk full, dropped audio blocks). The current take is
  aborted without ever being renamed to its final name, and the UI shows a
  blocking error rather than letting the operator record into the void.
- everything else — recoverable; surfaced to the operator, session continues.
"""

from __future__ import annotations


class CaptureError(Exception):
    """Base for every error this application raises deliberately."""


class SessionFatalError(CaptureError):
    """Recording cannot safely continue. Always surfaced, never swallowed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class DeviceError(SessionFatalError):
    """Input device lost, unreadable, or delivering bad status flags."""


class StorageWriteError(SessionFatalError):
    """A take could not be written or finalized on disk."""
