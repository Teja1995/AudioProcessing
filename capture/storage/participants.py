"""Participant registry — pseudonyms and their fixed connected-speech passages.

Stored as a JSON list at config.PARTICIPANTS_PATH:

    [
      {"pseudonym": "P01", "passage_text": "..."},
      {"pseudonym": "P02", "passage_text": null}
    ]

Two rules this module exists to enforce:

- **Pseudonyms only.** A real name must never reach this file (CLAUDE.md,
  Consent and data protection). The name<->pseudonym key lives outside data/
  entirely and is not handled here.
- **The passage is fixed.** Task 6 requires the participant to read the same
  passage, in their own native language, every session for the whole mission.
  Once set it is only ever changed deliberately, never wiped by a re-register.

Every pseudonym becomes a directory name under data/, so it is validated as
one: no separators, no "..", nothing Windows will silently mangle.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final

from capture import config
from capture.domain.models import Participant
from capture.storage.metadata import atomic_write_json

log = logging.getLogger("capture.storage.participants")

MAX_PSEUDONYM_LENGTH: Final = 64

# Characters Windows forbids in a path component, plus the POSIX separator.
_ILLEGAL_CHARS: Final = frozenset("<>:\"/\\|?*")

# Windows refuses these as file/directory names regardless of extension.
_RESERVED_NAMES: Final = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)


def validate_pseudonym(pseudonym: str) -> str:
    """Return the pseudonym unchanged, or raise ValueError explaining why not.

    Deliberately does not normalise: silently trimming " P01" to "P01" would
    let one participant end up with two identities. The operator gets told.
    """
    if not isinstance(pseudonym, str):
        raise ValueError(f"Pseudonym must be a string, got {type(pseudonym).__name__}")
    if not pseudonym:
        raise ValueError("Pseudonym must not be empty")
    if len(pseudonym) > MAX_PSEUDONYM_LENGTH:
        raise ValueError(
            f"Pseudonym is longer than {MAX_PSEUDONYM_LENGTH} characters: {pseudonym!r}"
        )
    if pseudonym != pseudonym.strip():
        raise ValueError(
            f"Pseudonym must not start or end with whitespace: {pseudonym!r}"
        )
    if not pseudonym.strip():
        raise ValueError("Pseudonym must not be blank")
    if pseudonym in (".", ".."):
        raise ValueError(f"Pseudonym must not be a path shorthand: {pseudonym!r}")
    if ".." in pseudonym:
        raise ValueError(f"Pseudonym must not contain '..': {pseudonym!r}")
    illegal = sorted(_ILLEGAL_CHARS.intersection(pseudonym))
    if illegal:
        raise ValueError(
            f"Pseudonym must not contain {''.join(illegal)!r}: {pseudonym!r}"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in pseudonym):
        raise ValueError(
            f"Pseudonym must not contain control characters: {pseudonym!r}"
        )
    if pseudonym.endswith(".") or pseudonym.endswith(" "):
        raise ValueError(
            "Pseudonym must not end with a dot or space (Windows strips them): "
            f"{pseudonym!r}"
        )
    if pseudonym.split(".")[0].lower() in _RESERVED_NAMES:
        raise ValueError(
            f"Pseudonym is a reserved device name on Windows: {pseudonym!r}"
        )
    return pseudonym


def list_participants() -> list[Participant]:
    """Every registered participant, sorted by pseudonym."""
    return [_to_participant(entry) for entry in _load_entries()]


def get_participant(pseudonym: str) -> Participant | None:
    validate_pseudonym(pseudonym)
    for entry in _load_entries():
        if entry["pseudonym"] == pseudonym:
            return _to_participant(entry)
    return None


def upsert_participant(pseudonym: str, passage_text: str | None) -> Participant:
    """Register a participant, or update their fixed passage.

    ``passage_text=None`` means "leave the passage as it is" — registering an
    existing participant again can never wipe the passage they have already
    been reading all mission. Passing new text replaces it and is logged: the
    same passage every session is a protocol requirement, so a change is worth
    seeing in the log afterwards.
    """
    validate_pseudonym(pseudonym)
    if passage_text is not None and not isinstance(passage_text, str):
        raise ValueError(
            f"passage_text must be a string or None, got {type(passage_text).__name__}"
        )

    entries = _load_entries()
    for entry in entries:
        if entry["pseudonym"] != pseudonym:
            continue
        existing = entry.get("passage_text")
        if passage_text is None:
            log.info(
                "Participant %s already registered; passage left unchanged", pseudonym
            )
            return _to_participant(entry)
        if existing is not None and existing != passage_text:
            log.warning(
                "Connected-speech passage CHANGED for %s — it is supposed to be "
                "identical every session (CLAUDE.md task 6)",
                pseudonym,
            )
        entry["passage_text"] = passage_text
        _write_entries(entries)
        return _to_participant(entry)

    entry = {"pseudonym": pseudonym, "passage_text": passage_text}
    entries.append(entry)
    _write_entries(entries)
    log.info("Registered participant %s", pseudonym)
    return _to_participant(entry)


def remove_participant(pseudonym: str) -> bool:
    """Drop a participant from the registry. Used by GDPR withdrawal only.

    Returns True if an entry was removed.
    """
    validate_pseudonym(pseudonym)
    entries = _load_entries()
    remaining = [entry for entry in entries if entry["pseudonym"] != pseudonym]
    if len(remaining) == len(entries):
        return False
    _write_entries(remaining)
    return True


# --- File I/O ---------------------------------------------------------------


def _load_entries() -> list[dict[str, Any]]:
    """Raw registry entries, sorted by pseudonym. Missing file means empty."""
    path = config.PARTICIPANTS_PATH
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a JSON list, got {type(raw).__name__}")

    entries: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"{path}: entry {index} is not a JSON object")
        pseudonym = item.get("pseudonym")
        if not isinstance(pseudonym, str):
            raise ValueError(f"{path}: entry {index} has no 'pseudonym' string")
        validate_pseudonym(pseudonym)
        passage = item.get("passage_text")
        if passage is not None and not isinstance(passage, str):
            raise ValueError(f"{path}: entry {index} has a non-string 'passage_text'")
        entries.append({"pseudonym": pseudonym, "passage_text": passage})

    known = [entry["pseudonym"] for entry in entries]
    if len(known) != len(set(known)):
        raise ValueError(f"{path} lists the same pseudonym twice: {known}")
    entries.sort(key=lambda entry: entry["pseudonym"])
    return entries


def _write_entries(entries: list[dict[str, Any]]) -> None:
    ordered = sorted(entries, key=lambda entry: entry["pseudonym"])
    atomic_write_json(config.PARTICIPANTS_PATH, ordered)


def _to_participant(entry: dict[str, Any]) -> Participant:
    return Participant(
        pseudonym=entry["pseudonym"], passage_text=entry.get("passage_text")
    )
