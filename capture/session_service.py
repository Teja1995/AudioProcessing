"""SessionService — the one place the recording path, storage and state meet.

Routes are thin adapters over this. It owns exactly one active session at a
time and is responsible for the ordering guarantees that make a take
recoverable:

    arm state -> arm engine -> record -> finalize file -> QC -> meta.json
    -> master_log.csv -> advance state

Nothing is buffered to session end: meta.json is rewritten and a
master_log.csv row appended after every completed take.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from capture import config
from capture.audio import devices, playback, selection
from capture.audio.engine import LevelUpdate, RecordingEngine
from capture.audio.qc import check_take
from capture.domain import time as clock
from capture.domain.models import (
    Covariates,
    ReferenceMeasures,
    SessionInfo,
    SessionMeta,
    TakeRecord,
)
from capture.domain.state import IllegalTransition, Phase, SessionStateMachine
from capture.domain.tasks import BY_NUMBER
from capture.errors import SessionFatalError
from capture.storage import metadata, paths
from capture.ws.hub import hub

log = logging.getLogger("capture.session")

# Slot key: (task_number, stem, take_n)
SlotKey = tuple[int, str, int]


@dataclass(slots=True)
class ActiveSession:
    participant: str
    session_number: int
    session_id: str
    directory: Path
    dual_clock: clock.DualClock
    meta: SessionMeta
    state: SessionStateMachine
    engine: RecordingEngine
    redo_counts: dict[SlotKey, int] = field(default_factory=dict)
    current_take_path: Path | None = None
    current_take_started_s: float | None = None
    fatal: str | None = None  # set once a session-fatal error has occurred


class SessionService:
    """Holds the single active session. All methods raise loudly on misuse."""

    def __init__(self) -> None:
        self._active: ActiveSession | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._watchdog: asyncio.Task[None] | None = None

    # --- Access -----------------------------------------------------------

    @property
    def active(self) -> ActiveSession:
        if self._active is None:
            raise IllegalTransition("No active session")
        return self._active

    @property
    def has_active(self) -> bool:
        return self._active is not None

    def require_session(self, session_id: str) -> ActiveSession:
        session = self.active
        if session.session_id != session_id:
            raise IllegalTransition(
                f"Session {session_id} is not the active session "
                f"({session.session_id})"
            )
        if session.fatal is not None:
            raise SessionFatalError("session_fatal", session.fatal)
        return session

    # --- Broadcasting (thread-safe: audio threads call into here) ---------

    def _broadcast(self, message: dict[str, Any]) -> None:
        """Schedule a WS push from any thread. Never raises into the caller —
        a failed UI push must not disturb the recording path."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(hub.broadcast(message), loop)
        except RuntimeError as exc:  # loop shutting down
            log.warning("Could not schedule broadcast: %s", exc)

    def _on_level(self, level: LevelUpdate) -> None:
        self._broadcast(
            {
                "type": "level",
                "rms_dbfs": level.rms_dbfs,
                "peak_dbfs": level.peak_dbfs,
                "clipping": level.clipping,
            }
        )

    def _on_fatal(self, message: str) -> None:
        """Called from the audio threads when recording cannot continue."""
        log.error("Session-fatal audio error: %s", message)
        if self._active is not None:
            self._active.fatal = message
            self._active.state.abort_take()
        self._broadcast({"type": "error", "code": "recording_failed", "message": message})

    def _push_state(self) -> None:
        session = self._active
        if session is None:
            return
        message: dict[str, Any] = {"type": "task_state"}
        message.update(session.state.snapshot())
        self._broadcast(message)

    def bind_loop(self) -> None:
        """Capture the running event loop for cross-thread broadcasts."""
        self._loop = asyncio.get_running_loop()

    # --- Session lifecycle ------------------------------------------------

    def next_session_number(self, participant: str) -> int:
        """Counter-based, never derived from the untrusted device clock."""
        directory = paths.participant_dir(participant)
        if not directory.exists():
            return 1
        return sum(1 for child in directory.iterdir() if child.is_dir()) + 1

    def start_session(
        self,
        participant: str,
        utc_operator_entered: str,
        mouth_to_mic_cm: Any = None,
        room_label: Any = None,
    ) -> ActiveSession:
        if self._active is not None:
            raise IllegalTransition(
                f"Session {self._active.session_id} is still active — "
                "complete it before starting another"
            )

        dual = clock.anchor_session(utc_operator_entered)
        number = self.next_session_number(participant)
        sid = paths.session_id(number, dual.utc_operator_entered)
        directory = paths.session_dir(participant, sid)
        directory.mkdir(parents=True, exist_ok=True)

        # The operator's chosen microphone, re-resolved by name. Raises if it
        # is not connected rather than quietly recording a different mic.
        device = selection.resolve_capture_device()
        info = SessionInfo(
            participant=participant,
            session_number=number,
            session_id=sid,
            utc_operator_entered_iso=dual.utc_operator_entered.isoformat(),
            device_clock_iso=dual.device_clock_at_entry.isoformat(),
            input_device_name=device.name,
            sample_rate_hz=config.SAMPLE_RATE_HZ,
            bit_depth=24,
            os_gain_reading=devices.read_os_input_gain(),
            mouth_to_mic_cm=mouth_to_mic_cm,
            room_label=room_label,
        )
        meta = SessionMeta(info=info)

        engine = RecordingEngine(
            device_index=device.index,
            on_level=self._on_level,
            on_fatal=self._on_fatal,
        )
        state = SessionStateMachine()
        state.transition(Phase.SESSION_SETUP)
        state.transition(Phase.REFERENCE_MEASURES)

        session = ActiveSession(
            participant=participant,
            session_number=number,
            session_id=sid,
            directory=directory,
            dual_clock=dual,
            meta=meta,
            state=state,
            engine=engine,
        )
        self._active = session

        metadata.write_meta(meta)
        engine.open_session_stream()  # meter runs before the first take
        log.info(
            "Session %s started for %s on device %r",
            sid,
            participant,
            device.name,
        )
        self._push_state()
        return session

    def submit_reference_measures(
        self, session_id: str, measures: ReferenceMeasures
    ) -> None:
        session = self.require_session(session_id)
        session.state.require(Phase.REFERENCE_MEASURES)
        session.meta.reference_measures = measures
        metadata.write_meta(session.meta)
        session.state.transition(Phase.COVARIATES)
        self._push_state()

    def submit_covariates(self, session_id: str, covariates: Covariates) -> None:
        session = self.require_session(session_id)
        session.state.require(Phase.COVARIATES)
        session.meta.covariates = covariates
        metadata.write_meta(session.meta)
        session.state.transition(Phase.TASK_BATTERY)
        self._push_state()

    def complete_session(self, session_id: str) -> dict[str, Any]:
        session = self.require_session(session_id)
        session.state.require(Phase.QC_REVIEW)
        session.state.transition(Phase.COMPLETE)
        metadata.write_meta(session.meta)
        session.engine.close()
        self._active = None
        log.info("Session %s complete: %d takes", session_id, len(session.meta.takes))
        self._broadcast({"type": "task_state", "phase": Phase.COMPLETE.value})
        return {"session_id": session_id, "takes": len(session.meta.takes)}

    def abandon_active_session(self) -> None:
        """Shutdown path: release the device. Completed takes are already
        safe on disk — nothing here can undo them."""
        if self._active is None:
            return
        try:
            self._active.engine.close()
        except Exception:  # noqa: BLE001 — shutdown must not mask the reason
            log.exception("Error closing engine during shutdown")
        self._active = None

    # --- Take lifecycle ---------------------------------------------------

    def _take_path(self, session: ActiveSession) -> Path:
        slot = session.state.current_slot()
        key: SlotKey = (slot.task.number, slot.stem, slot.take_n)
        redo_n = session.redo_counts.get(key, 0)
        name = paths.take_filename(slot.task, slot.stem, slot.take_n, redo_n)
        wanted = session.directory / name
        actual = paths.next_free_path(wanted)
        if actual != wanted:
            log.warning(
                "Path collision: %s already exists, writing %s instead",
                wanted.name,
                actual.name,
            )
            self._broadcast(
                {
                    "type": "error",
                    "code": "path_collision",
                    "message": (
                        f"{wanted.name} already existed; wrote {actual.name}. "
                        "No file was overwritten."
                    ),
                }
            )
        return actual

    async def start_take(self, session_id: str) -> dict[str, Any]:
        session = self.require_session(session_id)
        slot = session.state.current_slot()

        session.state.arm_current()
        path = self._take_path(session)
        session.engine.arm_take(path)
        session.current_take_path = path
        session.current_take_started_s = clock.offset_since(session.dual_clock)
        session.state.mark_recording()
        self._push_state()

        # Auto-stop tasks finish on their own; manual tasks get a watchdog.
        if slot.task.key == "reference_tone":
            await asyncio.to_thread(playback.play_wav, config.REFERENCE_TONE_WAV)
            return await self.stop_take(session_id)
        if slot.task.target_s is not None and slot.task.stop.value == "auto":
            await asyncio.sleep(slot.task.target_s)
            return await self.stop_take(session_id)

        self._arm_safety_cap(session_id)
        return {"status": "recording", "file": path.name}

    def _arm_safety_cap(self, session_id: str) -> None:
        """Backstop against a forgotten stop click (config.SAFETY_CAP_S).
        Not a protocol requirement — see ARCHITECTURE.md §7."""

        async def _cap() -> None:
            await asyncio.sleep(config.SAFETY_CAP_S)
            session = self._active
            if (
                session is not None
                and session.session_id == session_id
                and session.state.take_state.value == "recording"
            ):
                log.warning("Safety cap reached — auto-stopping take")
                await self.stop_take(session_id)

        self._cancel_watchdog()
        self._watchdog = asyncio.create_task(_cap())

    def _cancel_watchdog(self) -> None:
        if self._watchdog is not None and not self._watchdog.done():
            self._watchdog.cancel()
        self._watchdog = None

    async def stop_take(self, session_id: str) -> dict[str, Any]:
        session = self.require_session(session_id)
        self._cancel_watchdog()
        slot = session.state.current_slot()

        result = await asyncio.to_thread(session.engine.stop_take)
        key: SlotKey = (slot.task.number, slot.stem, slot.take_n)
        redo_n = session.redo_counts.get(key, 0)

        qc = await asyncio.to_thread(
            check_take,
            result.final_path,
            slot.task,
            self._rms_history(session.participant, slot.task.key),
        )

        record = TakeRecord(
            filename=result.final_path.name,
            task_number=slot.task.number,
            task_key=slot.task.key,
            stem=slot.stem,
            take_n=slot.take_n,
            redo_n=redo_n,
            device_clock_iso=clock.read_device_clock().isoformat(),
            monotonic_offset_s=session.current_take_started_s or 0.0,
            duration_s=result.duration_s,
            qc=qc,
            kept=True,
        )
        # A redo supersedes the earlier attempt for this slot; the earlier
        # file is kept on disk, just no longer marked as the kept take.
        if redo_n > 0:
            for i, previous in enumerate(session.meta.takes):
                if (
                    previous.task_number == slot.task.number
                    and previous.stem == slot.stem
                    and previous.take_n == slot.take_n
                ):
                    session.meta.takes[i] = replace_kept(previous, False)

        session.meta.takes.append(record)
        metadata.write_meta(session.meta)
        metadata.append_master_row(self._master_row(session, record))

        session.current_take_path = None
        session.state.mark_saved()
        self._push_state()
        return {
            "status": "saved",
            "file": record.filename,
            "duration_s": record.duration_s,
            "qc": qc_to_dict(qc),
        }

    async def redo_take(
        self, session_id: str, task_number: int, stem: str, take_n: int
    ) -> dict[str, Any]:
        """Reopen one slot under the next _redoN suffix. Never deletes."""
        session = self.require_session(session_id)
        key: SlotKey = (task_number, stem, take_n)
        session.redo_counts[key] = session.redo_counts.get(key, 0) + 1
        session.state.reopen_for_redo(task_number, stem, take_n)
        self._push_state()
        return await self.start_take(session_id)

    def submit_borg(self, session_id: str, task_number: int, rating: Any) -> None:
        session = self.require_session(session_id)
        task = BY_NUMBER.get(task_number)
        if task is None:
            raise IllegalTransition(f"No task numbered {task_number}")
        if not task.borg:
            raise IllegalTransition(f"Task {task.key} does not collect a Borg rating")
        updated = False
        for i, record in enumerate(session.meta.takes):
            if record.task_number == task_number and record.kept:
                session.meta.takes[i] = replace_borg(record, rating)
                updated = True
        if not updated:
            raise IllegalTransition(
                f"No completed take for task {task_number} to attach a rating to"
            )
        metadata.write_meta(session.meta)

    # --- QC support -------------------------------------------------------

    def _rms_history(self, participant: str, task_key: str) -> list[float]:
        """This participant's RMS values for this task in PRIOR sessions.

        Empty on their first session, which makes qc.check_take report
        "no baseline" rather than a false out-of-range warning.
        """
        history: list[float] = []
        directory = paths.participant_dir(participant)
        if not directory.exists():
            return history
        active_id = self._active.session_id if self._active else None
        for session_dir in sorted(directory.iterdir()):
            if not session_dir.is_dir() or session_dir.name == active_id:
                continue
            meta = metadata.load_meta_if_exists(participant, session_dir.name)
            if meta is None:
                continue
            for record in meta.takes:
                if record.task_key == task_key and record.kept and record.qc:
                    history.append(record.qc.rms_dbfs)
        return history

    def qc_summary(self, session_id: str) -> dict[str, Any]:
        session = self.require_session(session_id)
        rows = [
            {
                "task_number": record.task_number,
                "task_key": record.task_key,
                "stem": record.stem,
                "take": record.take_n,
                "redo": record.redo_n,
                "file": record.filename,
                "duration_s": round(record.duration_s, 2),
                "kept": record.kept,
                "status": record.qc.status if record.qc else "pass",
                "warnings": list(record.qc.warnings) if record.qc else [],
            }
            for record in session.meta.takes
        ]
        return {
            "session_id": session_id,
            "participant": session.participant,
            "takes": rows,
            "warn_count": sum(1 for r in rows if r["status"] == "warn"),
        }

    def _master_row(
        self, session: ActiveSession, record: TakeRecord
    ) -> dict[str, object]:
        """One master_log.csv row. Both clocks appear in full, every row."""
        return {
            "participant": session.participant,
            "session_id": session.session_id,
            "session_number": session.session_number,
            "task_number": record.task_number,
            "task_key": record.task_key,
            "stem": record.stem,
            "take": record.take_n,
            "redo": record.redo_n,
            "filename": record.filename,
            "utc_operator_entered": session.meta.info.utc_operator_entered_iso,
            "device_clock": record.device_clock_iso,
            "monotonic_offset_s": round(record.monotonic_offset_s, 3),
            "duration_s": round(record.duration_s, 3),
            "qc_status": record.qc.status if record.qc else "pass",
            "note": "; ".join(record.qc.warnings) if record.qc else "",
        }


def replace_kept(record: TakeRecord, kept: bool) -> TakeRecord:
    from dataclasses import replace

    return replace(record, kept=kept)


def replace_borg(record: TakeRecord, rating: Any) -> TakeRecord:
    from dataclasses import replace

    return replace(record, borg_cr10=rating)


def qc_to_dict(qc: Any) -> dict[str, Any] | None:
    if qc is None:
        return None
    return {
        "status": qc.status,
        "clipped": qc.clipped,
        "rms_dbfs": round(qc.rms_dbfs, 2),
        "peak_dbfs": round(qc.peak_dbfs, 2),
        "rms_in_range": qc.rms_in_range,
        "voicing_detected": qc.voicing_detected,
        "duration_ok": qc.duration_ok,
        "warnings": list(qc.warnings),
    }


service = SessionService()
