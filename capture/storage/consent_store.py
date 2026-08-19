"""Consent records — pseudonymous, GDPR-aware.

Stored at data/consent/<pseudonym>.json with the operator-entered UTC
timestamp and the five points the participant confirmed: what is recorded,
why, retention, who sees it, right to withdraw.

The name<->pseudonym key is NOT handled here beyond holding its
operator-configured path (config.PSEUDONYM_KEY_DEFAULT as fallback). It is
never written under data/ and never part of the USB export.

The exact wording confirmed on screen is copied into each record rather than
referenced, so a later edit to config.CONSENT_POINTS can never rewrite what a
participant actually agreed to. config.CONSENT_VERSION goes in alongside.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final

from capture import config
from capture.domain import time as clock
from capture.errors import CaptureError
from capture.storage import paths
from capture.storage.metadata import atomic_write_json
from capture.storage.participants import validate_pseudonym

log = logging.getLogger("capture.storage.consent")

# Layout tag; module-level because config.py is off-limits to this module.
CONSENT_SCHEMA: Final = "capture.consent/1"


class ConsentAlreadyRecorded(CaptureError):
    """A consent record for this pseudonym already exists.

    Re-consent is a protocol event — a new version of the information sheet, a
    participant re-reading and re-agreeing — and it must be visible, not a
    silent replacement of the original record. The operator resolves it by
    hand (archive the existing file), never the app behind their back.
    """


def has_consent(pseudonym: str) -> bool:
    validate_pseudonym(pseudonym)
    return paths.consent_path(pseudonym).exists()


def record_consent(pseudonym: str, utc_operator_entered_iso: str) -> None:
    """Write the consent JSON. Refuses to overwrite an existing record —
    re-consent would be a protocol event worth surfacing, not silently
    replacing."""
    validate_pseudonym(pseudonym)
    if not isinstance(utc_operator_entered_iso, str) or not utc_operator_entered_iso.strip():
        raise ValueError(
            "Consent needs the operator-entered UTC — the laptop clock is "
            "scrambled and cannot stand in for it"
        )

    path = paths.consent_path(pseudonym)
    if path.exists():
        raise ConsentAlreadyRecorded(
            f"Consent for {pseudonym} was already recorded at {path}. "
            "Re-consent must be handled deliberately, not by overwriting."
        )

    payload = {
        "schema": CONSENT_SCHEMA,
        "pseudonym": pseudonym,
        "consent_version": config.CONSENT_VERSION,
        # Both clocks, never one standing in for the other (ARCHITECTURE.md §5).
        "utc_operator_entered": utc_operator_entered_iso,
        "device_clock": clock.read_device_clock().isoformat(),
        "points_confirmed": list(config.CONSENT_POINTS),
    }
    atomic_write_json(path, payload)
    log.info("Consent recorded for %s (version %s)", pseudonym, config.CONSENT_VERSION)


def read_consent(pseudonym: str) -> dict[str, Any] | None:
    """The stored record, or None if this participant has not consented yet."""
    validate_pseudonym(pseudonym)
    path = paths.consent_path(pseudonym)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a JSON object, got {type(raw).__name__}")
    return raw
