"""Participants, consent, withdrawal.

Everything here speaks pseudonyms only. The name<->pseudonym key lives
outside data/ and outside this app entirely (ARCHITECTURE.md §11), so no
route can read it, log it, or export it.

Consent is a precondition for recording, not a formality: POST
/api/sessions refuses to start a session for a pseudonym with no consent
record on disk.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from capture import config
from capture.domain import time as clock
from capture.domain.models import Participant
from capture.routes.session import http_errors, require_pseudonym
from capture.session_service import service
from capture.storage import consent_store, participants, paths, withdrawal

log = logging.getLogger("capture.routes.consent")

router = APIRouter(prefix="/api/participants", tags=["consent"])


class ParticipantRequest(BaseModel):
    """Create or update a participant and their fixed passage.

    ``passage_text`` is the connected-speech text for task 6, in the
    participant's own native language, reused identically every session.
    Omitting it (null) keeps whatever is already stored; sending an empty
    string clears it deliberately.
    """

    pseudonym: str
    passage_text: str | None = None


class ConsentRequest(BaseModel):
    # The participant confirmed all five points on screen: what is recorded,
    # why, retention, who sees it, right to withdraw. One flag — the screen
    # does not allow partial consent.
    agreed: bool
    utc_operator_entered: str


class WithdrawRequest(BaseModel):
    # Deliberate friction for an irreversible action: the operator retypes
    # the pseudonym to confirm.
    confirm_pseudonym: str
    utc_operator_entered: str


def _describe(participant: Participant) -> dict[str, object]:
    """One row of the participant-select screen."""
    return {
        "pseudonym": participant.pseudonym,
        "has_consent": consent_store.has_consent(participant.pseudonym),
        "passage_set": bool(participant.passage_text),
        # Shown verbatim on the connected-speech screen; identical every
        # session for this participant (CLAUDE.md task 6).
        "passage_text": participant.passage_text,
        # Counter-based, never derived from the untrusted device clock.
        "next_session_number": service.next_session_number(participant.pseudonym),
        "expected_sessions": config.EXPECTED_SESSIONS_PER_PARTICIPANT,
    }


def _exists_anywhere(pseudonym: str) -> bool:
    """Is there anything at all on disk under this pseudonym?"""
    if paths.participant_dir(pseudonym).exists():
        return True
    if paths.consent_path(pseudonym).exists():
        return True
    return participants.get_participant(pseudonym) is not None


@router.get("/consent-text")
async def consent_text() -> dict[str, object]:
    """The exact wording the consent screen must show.

    Served from config so the screen and the stored record can never drift
    apart (CLAUDE.md, Consent and data protection).
    """
    return {
        "version": config.CONSENT_VERSION,
        "points": list(config.CONSENT_POINTS),
    }


@router.get("")
async def list_participants() -> list[dict[str, object]]:
    """Pseudonyms + consent status + passage-configured flag."""
    with http_errors():
        known = participants.list_participants()
        return [_describe(participant) for participant in known]


@router.post("")
async def upsert_participant(body: ParticipantRequest) -> dict[str, object]:
    """Create a participant, or update their fixed passage."""
    pseudonym = require_pseudonym(body.pseudonym)
    with http_errors():
        existing = participants.get_participant(pseudonym)
        if body.passage_text is None:
            # Not supplied: never silently erase a passage already in use.
            passage = existing.passage_text if existing is not None else None
        else:
            stripped = body.passage_text.strip()
            passage = stripped or None
        participants.upsert_participant(
            Participant(pseudonym=pseudonym, passage_text=passage)
        )
        stored = participants.get_participant(pseudonym)
        if stored is None:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"{pseudonym} was not saved. Check that {config.DATA_DIR} "
                    "is writable before recording anything."
                ),
            )
        log.info(
            "Participant %s saved (passage %s)",
            pseudonym,
            "set" if stored.passage_text else "NOT set",
        )
        return _describe(stored)


@router.post("/{pid}/consent")
async def record_consent(pid: str, body: ConsentRequest) -> dict[str, object]:
    pseudonym = require_pseudonym(pid)
    if not body.agreed:
        raise HTTPException(
            status_code=400,
            detail=(
                "Consent was not given, so nothing was recorded. A participant "
                "who does not consent cannot take part."
            ),
        )
    with http_errors():
        # Operator-entered UTC from the trusted external source; a typo is a
        # 400 here rather than a wrong timestamp on a GDPR record.
        entered = clock.parse_operator_utc(body.utc_operator_entered)
        if consent_store.has_consent(pseudonym):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Consent is already recorded for {pseudonym}. Re-consent "
                    "is a protocol event: keep the existing record and note it "
                    "in the mission log."
                ),
            )
        consent_store.record_consent(pseudonym, entered.isoformat())
    log.info("Consent recorded for %s at operator UTC %s", pseudonym, entered.isoformat())
    return {
        "ok": True,
        "pseudonym": pseudonym,
        "consent_utc_operator_entered": entered.isoformat(),
        "consent_version": config.CONSENT_VERSION,
    }


@router.post("/{pid}/withdraw")
async def withdraw(pid: str, body: WithdrawRequest) -> dict[str, object]:
    """Delete all audio + metadata for the participant, write the tombstone."""
    pseudonym = require_pseudonym(pid)
    confirm = body.confirm_pseudonym.strip()
    if confirm != pseudonym:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Nothing was deleted. To withdraw {pseudonym}, type that "
                f"pseudonym exactly to confirm (got {confirm!r})."
            ),
        )
    with http_errors():
        entered = clock.parse_operator_utc(body.utc_operator_entered)
    if not _exists_anywhere(pseudonym):
        raise HTTPException(
            status_code=404,
            detail=(
                f"Nothing to withdraw: no recordings, consent record or "
                f"participant entry exists for {pseudonym}."
            ),
        )

    # Release the microphone and drop the in-flight session first: a session
    # whose data is about to be erased must not keep recording, and on
    # Windows an open WAV handle would block the delete outright.
    if service.has_active and service.active.participant == pseudonym:
        log.warning(
            "Withdrawal for %s abandons active session %s",
            pseudonym,
            service.active.session_id,
        )
        service.abandon_active_session()

    with http_errors():
        # Deleting a whole participant tree is slow; keep the event loop free
        # so the UI (and the WebSocket) stay responsive while it runs.
        await asyncio.to_thread(withdrawal.withdraw, pseudonym, entered.isoformat())
    log.warning(
        "WITHDRAWAL: all data for %s deleted at operator UTC %s",
        pseudonym,
        entered.isoformat(),
    )
    return {
        "ok": True,
        "pseudonym": pseudonym,
        "withdrawn_utc_operator_entered": entered.isoformat(),
        "message": (
            f"All audio and metadata for {pseudonym} have been deleted. The "
            "fact of the withdrawal is recorded; the recordings are gone."
        ),
    }
