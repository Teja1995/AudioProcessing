"""Metadata ledgers, participant registry, consent and GDPR withdrawal.

Every test redirects the config paths into a throwaway temp directory. The
real ``data/`` holds the only copy of a dataset that cannot be recollected —
no test is ever allowed to touch it, and ``setUp`` asserts the redirection
took effect before anything is written.
"""

from __future__ import annotations

import csv
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capture import config
from capture.domain.models import (
    Covariates,
    Participant,
    QCResult,
    ReferenceMeasures,
    SessionInfo,
    SessionMeta,
    TakeRecord,
)
from capture.errors import StorageWriteError
from capture.storage import consent_store, metadata, participants, paths, withdrawal


class StorageTestCase(unittest.TestCase):
    """Base case: config paths point into a temp dir for the whole test."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "data"
        self.consent_dir = self.data / "consent"
        self.consent_dir.mkdir(parents=True)

        redirected = {
            "DATA_DIR": self.data,
            "CONSENT_DIR": self.consent_dir,
            "MASTER_LOG_PATH": self.data / "master_log.csv",
            "PARTICIPANTS_PATH": self.data / "participants.json",
            "WITHDRAWAL_LOG_PATH": self.data / "withdrawals.csv",
        }
        for name, value in redirected.items():
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        # Belt and braces: never write into the mission's real data directory.
        self.assertEqual(config.DATA_DIR, self.data)
        self.assertTrue(str(config.MASTER_LOG_PATH).startswith(str(self.root)))

        # These modules log loudly on purpose; keep it out of the test output.
        capture_log = logging.getLogger("capture")
        silencer = logging.NullHandler()
        capture_log.addHandler(silencer)
        self.addCleanup(capture_log.removeHandler, silencer)

    # --- fixtures ---------------------------------------------------------

    def make_meta(self, pseudonym: str = "P01", sid: str = "001_20260822T073000Z") -> SessionMeta:
        info = SessionInfo(
            participant=pseudonym,
            session_number=1,
            session_id=sid,
            utc_operator_entered_iso="2026-08-22T07:30:00+00:00",
            device_clock_iso="2019-03-11T22:14:09.123456+00:00",
            input_device_name="Digital Microphone (Cirrus Logic)",
            sample_rate_hz=48_000,
            bit_depth=24,
            os_gain_reading="75",
            mouth_to_mic_cm=15.0,
            room_label="lab module",
        )
        return SessionMeta(info=info)

    def write_session_on_disk(self, pseudonym: str, sid: str) -> Path:
        """A session directory with a meta.json and one fake take file."""
        session_dir = paths.session_dir(pseudonym, sid)
        session_dir.mkdir(parents=True)
        (session_dir / "01_silence.wav").write_bytes(b"RIFF-not-really-audio")
        meta = self.make_meta(pseudonym, sid)
        metadata.write_meta(meta)
        return session_dir

    def master_rows(self) -> list[list[str]]:
        with open(config.MASTER_LOG_PATH, "r", encoding="utf-8", newline="") as handle:
            return [row for row in csv.reader(handle) if row]


def sample_row(participant: str = "P01", **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "participant": participant,
        "session_id": "001_20260822T073000Z",
        "session_number": 1,
        "task_number": 3,
        "task_key": "sustained_a",
        "stem": "sustained_a",
        "take": 1,
        "redo": 0,
        "filename": "03_sustained_a_take1.wav",
        "utc_operator_entered": "2026-08-22T07:30:00+00:00",
        "device_clock": "2019-03-11T22:14:09+00:00",
        "monotonic_offset_s": 12.5,
        "duration_s": 5.02,
        "qc_status": "pass",
        "note": "",
    }
    row.update(overrides)
    return row


# --- meta.json ---------------------------------------------------------------


class MetaRoundTripTests(StorageTestCase):
    def test_round_trip_is_lossless(self) -> None:
        meta = self.make_meta()
        meta.reference_measures = ReferenceMeasures(
            urine_specific_gravity=1.021,
            urine_colour_1_to_8="n/a",  # operator marked it not available
            body_mass_kg=71.4,
            fluid_intake_ml=None,  # never asked
        )
        meta.covariates = Covariates(
            minutes_since_exercise=45,
            breathing_route="nasal",
            caffeine_since_last=True,
            alcohol_since_last=False,
            speaking_load_estimate="n/a",
            hours_slept=6.5,
            upper_respiratory_symptoms=False,
            medication=None,
            menstrual_cycle_phase="n/a",
            habitat_temperature_c=22.5,
            habitat_humidity_pct=41.0,
        )
        meta.takes = [
            TakeRecord(
                filename="01_silence.wav",
                task_number=1,
                task_key="silence",
                stem="silence",
                take_n=1,
                redo_n=0,
                device_clock_iso="2019-03-11T22:14:09+00:00",
                monotonic_offset_s=0.0,
                duration_s=3.0,
                qc=QCResult(
                    clipped=False,
                    rms_dbfs=-61.2,
                    peak_dbfs=-48.0,
                    rms_in_range=None,  # no baseline yet — not a warning
                    voicing_detected=False,
                    duration_ok=True,
                    warnings=(),
                ),
                kept=True,
                borg_cr10=None,
            ),
            TakeRecord(
                filename="03_sustained_a_take1.wav",
                task_number=3,
                task_key="sustained_a",
                stem="sustained_a",
                take_n=1,
                redo_n=1,
                device_clock_iso="2019-03-11T22:15:41+00:00",
                monotonic_offset_s=92.125,
                duration_s=5.02,
                qc=QCResult(
                    clipped=True,
                    rms_dbfs=-12.0,
                    peak_dbfs=0.0,
                    rms_in_range=False,
                    voicing_detected=True,
                    duration_ok=True,
                    warnings=("clipping detected", "RMS out of range"),
                ),
                kept=False,
                borg_cr10="n/a",
            ),
            TakeRecord(
                filename="04_sustained_i_take1.wav",
                task_number=4,
                task_key="sustained_i",
                stem="sustained_i",
                take_n=1,
                redo_n=0,
                device_clock_iso="2019-03-11T22:16:02+00:00",
                monotonic_offset_s=113.0,
                duration_s=4.8,
                qc=None,  # QC not run (yet) — distinct from "QC passed"
                kept=True,
                borg_cr10=3.5,
            ),
        ]

        metadata.write_meta(meta)
        loaded = metadata.load_meta_if_exists("P01", "001_20260822T073000Z")

        self.assertIsNotNone(loaded)
        assert loaded is not None  # narrows for the type checker
        self.assertEqual(loaded.info, meta.info)
        self.assertEqual(loaded.reference_measures, meta.reference_measures)
        self.assertEqual(loaded.covariates, meta.covariates)
        self.assertEqual(loaded.takes, meta.takes)
        self.assertEqual(loaded, meta)

    def test_none_and_na_stay_distinct_through_the_round_trip(self) -> None:
        """None means "never asked", "n/a" means "operator marked it missing".

        Collapsing them would erase the difference between an accidental skip
        and a deliberate one, which is exactly what the encoding exists for.
        """
        meta = self.make_meta()
        meta.reference_measures = ReferenceMeasures(
            urine_specific_gravity=None,
            urine_colour_1_to_8="n/a",
            body_mass_kg=None,
            fluid_intake_ml="n/a",
        )
        metadata.write_meta(meta)

        raw = json.loads(paths.meta_path("P01", meta.info.session_id).read_text("utf-8"))
        measures = raw["reference_measures"]
        self.assertIsNone(measures["urine_specific_gravity"])
        self.assertEqual(measures["urine_colour_1_to_8"], "n/a")
        self.assertIsNone(measures["body_mass_kg"])
        self.assertEqual(measures["fluid_intake_ml"], "n/a")

        loaded = metadata.load_meta_if_exists("P01", meta.info.session_id)
        assert loaded is not None
        self.assertIsNone(loaded.reference_measures.urine_specific_gravity)
        self.assertEqual(loaded.reference_measures.urine_colour_1_to_8, "n/a")
        self.assertIsNone(loaded.reference_measures.body_mass_kg)
        self.assertEqual(loaded.reference_measures.fluid_intake_ml, "n/a")

    def test_qc_warnings_come_back_as_a_tuple(self) -> None:
        meta = self.make_meta()
        meta.takes = [
            TakeRecord(
                filename="09_mpt_take1.wav",
                task_number=9,
                task_key="mpt",
                stem="mpt",
                take_n=1,
                redo_n=0,
                device_clock_iso="2019-03-11T22:20:00+00:00",
                monotonic_offset_s=400.0,
                duration_s=1.4,
                qc=QCResult(
                    clipped=False,
                    rms_dbfs=-30.0,
                    peak_dbfs=-10.0,
                    rms_in_range=True,
                    voicing_detected=True,
                    duration_ok=False,
                    warnings=("much shorter than expected",),
                ),
            )
        ]
        metadata.write_meta(meta)
        loaded = metadata.load_meta_if_exists("P01", meta.info.session_id)
        assert loaded is not None
        qc = loaded.takes[0].qc
        assert qc is not None
        self.assertIsInstance(qc.warnings, tuple)
        self.assertEqual(qc.warnings, ("much shorter than expected",))
        self.assertEqual(qc.status, "warn")

    def test_missing_meta_returns_none(self) -> None:
        self.assertIsNone(metadata.load_meta_if_exists("P01", "001_20260822T073000Z"))

    def test_update_replaces_atomically_and_leaves_no_temp_files(self) -> None:
        meta = self.make_meta()
        metadata.write_meta(meta)
        first = paths.meta_path("P01", meta.info.session_id).read_text("utf-8")

        meta.takes.append(
            TakeRecord(
                filename="01_silence.wav",
                task_number=1,
                task_key="silence",
                stem="silence",
                take_n=1,
                redo_n=0,
                device_clock_iso="2019-03-11T22:14:09+00:00",
                monotonic_offset_s=0.0,
                duration_s=3.0,
            )
        )
        metadata.write_meta(meta)

        session_dir = paths.session_dir("P01", meta.info.session_id)
        self.assertEqual([p.name for p in session_dir.iterdir()], ["meta.json"])
        second = (session_dir / "meta.json").read_text("utf-8")
        self.assertNotEqual(first, second)

        loaded = metadata.load_meta_if_exists("P01", meta.info.session_id)
        assert loaded is not None
        self.assertEqual(len(loaded.takes), 1)

    def test_previous_version_survives_a_failed_update(self) -> None:
        """A kill (or a failure) mid-update must leave the last complete
        meta.json in place — never a truncated one."""
        meta = self.make_meta()
        metadata.write_meta(meta)
        path = paths.meta_path("P01", meta.info.session_id)
        original = path.read_text("utf-8")

        meta.covariates = Covariates(hours_slept=6.0)
        with mock.patch(
            "capture.storage.metadata.os.replace", side_effect=OSError("disk gone")
        ):
            with self.assertRaises(StorageWriteError):
                metadata.write_meta(meta)

        self.assertEqual(path.read_text("utf-8"), original)
        self.assertEqual(
            [p.name for p in paths.session_dir("P01", meta.info.session_id).iterdir()],
            ["meta.json"],
        )
        loaded = metadata.load_meta_if_exists("P01", meta.info.session_id)
        assert loaded is not None
        self.assertIsNone(loaded.covariates.hours_slept)

    def test_damaged_meta_is_logged_loudly_and_read_as_none(self) -> None:
        """The QC-baseline read must not let yesterday's damaged file abort
        today's takes — but it must never be quiet about it either."""
        meta = self.make_meta()
        metadata.write_meta(meta)
        path = paths.meta_path("P01", meta.info.session_id)
        path.write_text("{ not json", "utf-8")

        with self.assertLogs("capture.storage.metadata", level="ERROR") as logged:
            self.assertIsNone(metadata.load_meta_if_exists("P01", meta.info.session_id))
        self.assertIn("Damaged session metadata", logged.output[0])
        # The damaged file is left exactly as found, never "repaired".
        self.assertEqual(path.read_text("utf-8"), "{ not json")

    def test_strict_read_raises_on_damaged_meta(self) -> None:
        meta = self.make_meta()
        metadata.write_meta(meta)
        paths.meta_path("P01", meta.info.session_id).write_text("{ not json", "utf-8")
        with self.assertRaises(ValueError):
            metadata.read_meta_strict("P01", meta.info.session_id)

    def test_strict_read_raises_on_a_missing_required_field(self) -> None:
        meta = self.make_meta()
        metadata.write_meta(meta)
        path = paths.meta_path("P01", meta.info.session_id)
        raw = json.loads(path.read_text("utf-8"))
        del raw["info"]["input_device_name"]
        path.write_text(json.dumps(raw), "utf-8")
        with self.assertRaises(ValueError):
            metadata.read_meta_strict("P01", meta.info.session_id)

    def test_strict_read_returns_none_when_the_file_is_simply_absent(self) -> None:
        self.assertIsNone(metadata.read_meta_strict("P01", "001_20260822T073000Z"))


# --- master_log.csv ----------------------------------------------------------


class MasterLogTests(StorageTestCase):
    def test_header_written_once_then_rows_appended(self) -> None:
        metadata.append_master_row(sample_row())
        metadata.append_master_row(sample_row(task_number=4, task_key="sustained_i"))
        metadata.append_master_row(sample_row(participant="P02"))

        rows = self.master_rows()
        self.assertEqual(rows[0], list(config.MASTER_LOG_COLUMNS))
        self.assertEqual(len(rows), 4)  # header + 3 rows
        self.assertNotIn("participant", [row[0] for row in rows[1:]])

    def test_values_land_in_config_column_order(self) -> None:
        # Deliberately shuffled input mapping — the file order is the config's.
        row = dict(reversed(list(sample_row().items())))
        metadata.append_master_row(row)
        rows = self.master_rows()
        header, written = rows[0], rows[1]
        self.assertEqual(header, list(config.MASTER_LOG_COLUMNS))
        self.assertEqual(written[header.index("participant")], "P01")
        self.assertEqual(written[header.index("filename")], "03_sustained_a_take1.wav")
        self.assertEqual(written[header.index("duration_s")], "5.02")

    def test_missing_or_unknown_column_raises(self) -> None:
        incomplete = sample_row()
        del incomplete["note"]
        with self.assertRaises(ValueError):
            metadata.append_master_row(incomplete)

        extra = sample_row()
        extra["participant_real_name"] = "definitely not allowed"
        with self.assertRaises(ValueError):
            metadata.append_master_row(extra)

        self.assertFalse(config.MASTER_LOG_PATH.exists())

    def test_refuses_to_append_under_a_foreign_header(self) -> None:
        config.MASTER_LOG_PATH.write_text("a,b,c\n", encoding="utf-8")
        with self.assertRaises(StorageWriteError):
            metadata.append_master_row(sample_row())

    def test_the_file_handle_is_never_held_open(self) -> None:
        """Windows refuses to rename a file that is still open, so this
        renaming proves the appender closed it — a row left in a buffer is a
        row lost to a kill."""
        metadata.append_master_row(sample_row())
        moved = config.MASTER_LOG_PATH.with_suffix(".moved")
        config.MASTER_LOG_PATH.rename(moved)
        self.assertTrue(moved.exists())

    def test_na_and_none_survive_the_csv(self) -> None:
        metadata.append_master_row(sample_row(note=None, qc_status="n/a"))
        rows = self.master_rows()
        header, written = rows[0], rows[1]
        self.assertEqual(written[header.index("note")], "")
        self.assertEqual(written[header.index("qc_status")], "n/a")


# --- participants.json -------------------------------------------------------


class ParticipantRegistryTests(StorageTestCase):
    def test_upsert_creates_then_get_and_list_find_it(self) -> None:
        created = participants.upsert_participant("P02", "Passage in Polish.")
        self.assertEqual(created, Participant("P02", "Passage in Polish."))
        self.assertEqual(participants.get_participant("P02"), created)

        participants.upsert_participant("P01", None)
        self.assertEqual(
            [p.pseudonym for p in participants.list_participants()], ["P01", "P02"]
        )

    def test_registry_is_a_json_list_on_disk(self) -> None:
        participants.upsert_participant("P01", "Passage.")
        raw = json.loads(config.PARTICIPANTS_PATH.read_text("utf-8"))
        self.assertEqual(raw, [{"pseudonym": "P01", "passage_text": "Passage."}])

    def test_unknown_participant_is_none(self) -> None:
        self.assertIsNone(participants.get_participant("P09"))

    def test_reregistering_never_wipes_the_fixed_passage(self) -> None:
        participants.upsert_participant("P01", "Fixed passage, same every session.")
        again = participants.upsert_participant("P01", None)
        self.assertEqual(again.passage_text, "Fixed passage, same every session.")
        self.assertEqual(len(participants.list_participants()), 1)

    def test_passage_can_be_changed_deliberately(self) -> None:
        participants.upsert_participant("P01", "First.")
        with self.assertLogs("capture.storage.participants", level="WARNING"):
            updated = participants.upsert_participant("P01", "Second.")
        self.assertEqual(updated.passage_text, "Second.")

    def test_remove_participant(self) -> None:
        participants.upsert_participant("P01", None)
        self.assertTrue(participants.remove_participant("P01"))
        self.assertFalse(participants.remove_participant("P01"))
        self.assertEqual(participants.list_participants(), [])

    def test_unsafe_pseudonyms_are_rejected(self) -> None:
        for bad in (
            "",
            "   ",
            " P01",
            "P01 ",
            "..",
            "../P02",
            "a/b",
            "a\\b",
            "C:evil",
            "P01.",
            "con",
            "NUL.json",
            "bad\x00name",
            "x" * 65,
        ):
            with self.subTest(pseudonym=bad):
                with self.assertRaises(ValueError):
                    participants.validate_pseudonym(bad)
                with self.assertRaises(ValueError):
                    participants.upsert_participant(bad, None)

    def test_unicode_pseudonym_is_allowed(self) -> None:
        # Pseudonyms are operator-chosen; non-ASCII is fine as long as it is
        # a safe directory name.
        created = participants.upsert_participant("Załoga-3", None)
        self.assertEqual(created.pseudonym, "Załoga-3")

    def test_corrupt_registry_fails_loudly(self) -> None:
        config.PARTICIPANTS_PATH.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            participants.list_participants()


# --- consent -----------------------------------------------------------------


class ConsentTests(StorageTestCase):
    def test_record_then_has_consent(self) -> None:
        self.assertFalse(consent_store.has_consent("P01"))
        consent_store.record_consent("P01", "2026-08-22T07:25:00+00:00")
        self.assertTrue(consent_store.has_consent("P01"))

        stored = json.loads(paths.consent_path("P01").read_text("utf-8"))
        self.assertEqual(stored["pseudonym"], "P01")
        self.assertEqual(stored["consent_version"], config.CONSENT_VERSION)
        self.assertEqual(stored["utc_operator_entered"], "2026-08-22T07:25:00+00:00")
        self.assertEqual(stored["points_confirmed"], list(config.CONSENT_POINTS))
        self.assertIn("device_clock", stored)  # both clocks, never one for the other

    def test_refuses_to_overwrite_an_existing_record(self) -> None:
        consent_store.record_consent("P01", "2026-08-22T07:25:00+00:00")
        before = paths.consent_path("P01").read_text("utf-8")

        with self.assertRaises(consent_store.ConsentAlreadyRecorded):
            consent_store.record_consent("P01", "2026-08-25T18:00:00+00:00")

        self.assertEqual(paths.consent_path("P01").read_text("utf-8"), before)

    def test_blank_operator_utc_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            consent_store.record_consent("P01", "   ")
        self.assertFalse(consent_store.has_consent("P01"))

    def test_unsafe_pseudonym_cannot_probe_the_filesystem(self) -> None:
        with self.assertRaises(ValueError):
            consent_store.has_consent("../../etc/passwd")

    def test_read_consent_returns_none_when_absent(self) -> None:
        self.assertIsNone(consent_store.read_consent("P01"))


# --- withdrawal --------------------------------------------------------------


class WithdrawalTests(StorageTestCase):
    def setUp(self) -> None:
        super().setUp()
        for pseudonym in ("P01", "P02"):
            participants.upsert_participant(pseudonym, f"Passage for {pseudonym}.")
            consent_store.record_consent(pseudonym, "2026-08-22T07:25:00+00:00")
            self.write_session_on_disk(pseudonym, "001_20260822T073000Z")
            self.write_session_on_disk(pseudonym, "002_20260822T133000Z")
            metadata.append_master_row(sample_row(pseudonym))
            metadata.append_master_row(sample_row(pseudonym, task_number=4))

    def test_removes_audio_consent_and_rows_but_keeps_the_tombstone(self) -> None:
        summary = withdrawal.withdraw("P01", "2026-08-24T09:00:00+00:00")

        # Audio + per-session metadata gone.
        self.assertFalse(paths.participant_dir("P01").exists())
        # Consent record gone.
        self.assertFalse(paths.consent_path("P01").exists())
        # Registry entry (and their passage text) gone.
        self.assertIsNone(participants.get_participant("P01"))

        # master_log.csv keeps its header and everyone else's rows.
        rows = self.master_rows()
        self.assertEqual(rows[0], list(config.MASTER_LOG_COLUMNS))
        participant_column = rows[0].index("participant")
        remaining = [row[participant_column] for row in rows[1:]]
        self.assertEqual(remaining, ["P02", "P02"])

        # The FACT of the withdrawal survives the data.
        with open(config.WITHDRAWAL_LOG_PATH, "r", encoding="utf-8", newline="") as fh:
            tombstones = [row for row in csv.reader(fh) if row]
        self.assertEqual(tombstones[0], list(withdrawal.WITHDRAWAL_LOG_COLUMNS))
        self.assertEqual(tombstones[1][0], "P01")
        self.assertEqual(tombstones[1][1], "2026-08-24T09:00:00+00:00")
        self.assertEqual(tombstones[1][3], withdrawal.WITHDRAWAL_ACTION)

        self.assertEqual(summary["pseudonym"], "P01")
        self.assertEqual(summary["sessions_removed"], 2)
        self.assertEqual(summary["files_removed"], 4)  # 2 sessions x (wav + meta)
        self.assertEqual(summary["master_log_rows_removed"], 2)
        self.assertTrue(summary["consent_removed"])
        self.assertTrue(summary["registry_entry_removed"])

    def test_other_participants_are_untouched(self) -> None:
        withdrawal.withdraw("P01", "2026-08-24T09:00:00+00:00")

        self.assertTrue(paths.participant_dir("P02").exists())
        self.assertTrue(paths.consent_path("P02").exists())
        kept = participants.get_participant("P02")
        assert kept is not None
        self.assertEqual(kept.passage_text, "Passage for P02.")
        loaded = metadata.load_meta_if_exists("P02", "001_20260822T073000Z")
        self.assertIsNotNone(loaded)

    def test_master_log_keeps_its_header_when_every_row_goes(self) -> None:
        withdrawal.withdraw("P01", "2026-08-24T09:00:00+00:00")
        withdrawal.withdraw("P02", "2026-08-24T09:10:00+00:00")
        rows = self.master_rows()
        self.assertEqual(rows, [list(config.MASTER_LOG_COLUMNS)])
        # The next session can still append under that header.
        metadata.append_master_row(sample_row("P03"))
        self.assertEqual(len(self.master_rows()), 2)

    def test_withdrawal_with_nothing_left_still_records_the_fact(self) -> None:
        withdrawal.withdraw("P01", "2026-08-24T09:00:00+00:00")
        summary = withdrawal.withdraw("P01", "2026-08-24T09:05:00+00:00")

        with open(config.WITHDRAWAL_LOG_PATH, "r", encoding="utf-8", newline="") as fh:
            tombstones = [row for row in csv.reader(fh) if row]
        self.assertEqual(len(tombstones), 3)  # header + two withdrawals
        self.assertEqual(summary["sessions_removed"], 0)
        self.assertEqual(summary["master_log_rows_removed"], 0)

    def test_blank_operator_utc_is_rejected_before_anything_is_deleted(self) -> None:
        with self.assertRaises(ValueError):
            withdrawal.withdraw("P01", "")
        self.assertTrue(paths.participant_dir("P01").exists())
        self.assertFalse(config.WITHDRAWAL_LOG_PATH.exists())

    def test_unsafe_pseudonym_never_reaches_the_recursive_delete(self) -> None:
        with self.assertRaises(ValueError):
            withdrawal.withdraw("../P02", "2026-08-24T09:00:00+00:00")
        self.assertTrue(paths.participant_dir("P02").exists())
        self.assertFalse(config.WITHDRAWAL_LOG_PATH.exists())


if __name__ == "__main__":
    unittest.main()
