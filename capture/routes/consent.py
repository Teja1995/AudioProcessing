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
    Omitting it (null) keeps whatever is already stored — re-registering a
    participant can never wipe the passage they have been reading all week.
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
    # None reaches the registry as "leave the passage alone"; anything sent is
    # trimmed, because trailing whitespace in a read-aloud passage is noise.
    passage = None if body.passage_text is None else body.passage_text.strip()
    with http_errors():
        stored = participants.upsert_participant(pseudonym, passage)
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
        summary = await asyncio.to_thread(
            withdrawal.withdraw, pseudonym, entered.isoformat()
        )
    log.warning(
        "WITHDRAWAL: all data for %s deleted at operator UTC %s",
        pseudonym,
        entered.isoformat(),
    )
    # The store's own summary is passed through in full: the operator gets a
    # concrete count of what was erased, not a bare "done".
    return {
        **summary,
        "ok": True,
        "message": (
            f"All audio and metadata for {pseudonym} have been deleted. The "
            "fact of the withdrawal is recorded; the recordings are gone."
        ),
    }


@router.delete("/{pid}")
async def delete_participant(pid: str) -> dict[str, object]:
    """Remove a participant from the register.

    For correcting a mistyped pseudonym, not for removing someone from the
    study. If any session has been recorded the registry entry is NOT the
    only thing that exists, so this refuses and points at withdrawal, which
    deletes the audio and metadata too and records that it happened. Quietly
    dropping the registry row would orphan real recordings and lose the
    consent trail with them.
    """
    pseudonym = require_pseudonym(pid)
    directory = paths.participant_dir(pseudonym)
    sessions = (
        sorted(child.name for child in directory.iterdir() if child.is_dir())
        if directory.exists()
        else []
    )
    if sessions:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{pseudonym} has {len(sessions)} recorded session(s), so this "
                "would leave the audio behind with nothing describing it. Use "
                "Withdraw on the dashboard instead: it deletes the recordings "
                "and metadata as well and records that the withdrawal "
                "happened."
            ),
        )

    with http_errors():
        existed = participants.remove_participant(pseudonym)
        # A registered-but-never-recorded participant may still have consented.
        # That record describes a person with no data, so it goes too.
        consent_removed = consent_store.remove_consent(pseudonym)

    if not existed:
        raise HTTPException(status_code=404, detail=f"No participant {pseudonym}")
    log.info("Participant %s removed from the register (no recordings)", pseudonym)
    return {"removed": pseudonym, "consent_removed": consent_removed}
