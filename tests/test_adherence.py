"""Adherence grid, startup counts, and the verified USB export.

These are the read-side safety nets: the grid is how a quietly-missed run of
sessions becomes visible on day 4, and the export is the only way audio
legitimately leaves the laptop. Both must be right the first time.
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence
from unittest import mock

from capture import config
from capture.adherence.tracker import CellStatus, build_grid, startup_summary
from capture.domain.tasks import TakeSlot, iter_slots
from capture.storage.export import copy_to_usb

ALL_SLOTS: Sequence[TakeSlot] = tuple(iter_slots())


class TempDataDirCase(unittest.TestCase):
    """Redirects every data path in config at a throwaway directory."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.master_log = self.data_dir / "master_log.csv"
        self.participants_path = self.data_dir / "participants.json"
        redirected = {
            "DATA_DIR": self.data_dir,
            "CONSENT_DIR": self.data_dir / "consent",
            "MASTER_LOG_PATH": self.master_log,
            "PARTICIPANTS_PATH": self.participants_path,
        }
        for name, value in redirected.items():
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    # --- fixtures ---------------------------------------------------------

    def write_master_log(self, rows: Iterable[Mapping[str, object]]) -> None:
        with self.master_log.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(config.MASTER_LOG_COLUMNS))
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def session_rows(
        self,
        participant: str,
        session_number: int,
        slots: Sequence[TakeSlot] = ALL_SLOTS,
        warn_slots: Sequence[TakeSlot] = (),
        redo: int = 0,
    ) -> list[dict[str, object]]:
        warned = {(s.task.number, s.stem, s.take_n) for s in warn_slots}
        rows: list[dict[str, object]] = []
        for slot in slots:
            key = (slot.task.number, slot.stem, slot.take_n)
            rows.append(
                {
                    "participant": participant,
                    "session_id": f"{session_number:03d}_20260822T060000Z",
                    "session_number": session_number,
                    "task_number": slot.task.number,
                    "task_key": slot.task.key,
                    "stem": slot.stem,
                    "take": slot.take_n,
                    "redo": redo,
                    "filename": f"{slot.task.number:02d}_{slot.stem}.wav",
                    "utc_operator_entered": "2026-08-22T06:00:00+00:00",
                    "device_clock": "1999-03-04T21:11:00+00:00",
                    "monotonic_offset_s": 12.5,
                    "duration_s": 5.0,
                    "qc_status": "warn" if key in warned else "pass",
                    "note": "clipping" if key in warned else "",
                }
            )
        return rows


class AdherenceGridTests(TempDataDirCase):
    def test_complete_flagged_and_missing_in_one_grid(self) -> None:
        rows: list[dict[str, object]] = []
        rows += self.session_rows("P01", 1)  # all takes, all pass
        rows += self.session_rows("P01", 2, slots=ALL_SLOTS[:5])  # short session
        rows += self.session_rows("P01", 3, warn_slots=[ALL_SLOTS[0]])  # a warn
        self.write_master_log(rows)

        grid = build_grid()
        cells = grid.cells["P01"]

        self.assertEqual(cells[0], CellStatus.COMPLETE)
        self.assertEqual(cells[1], CellStatus.FLAGGED)  # incomplete
        self.assertEqual(cells[2], CellStatus.FLAGGED)  # QC warning
        self.assertEqual(cells[3], CellStatus.MISSING)  # never happened
        self.assertTrue(all(c is CellStatus.MISSING for c in cells[3:]))

    def test_grid_spans_the_whole_mission(self) -> None:
        self.write_master_log(self.session_rows("P01", 1))
        grid = build_grid()
        self.assertEqual(grid.expected_sessions, config.EXPECTED_SESSIONS_PER_PARTICIPANT)
        self.assertEqual(len(grid.cells["P01"]), config.EXPECTED_SESSIONS_PER_PARTICIPANT)

    def test_expected_take_count_comes_from_the_battery(self) -> None:
        # One take short of the battery is not complete — whatever the
        # battery's length happens to be. Nothing here hardcodes 13.
        self.write_master_log(
            self.session_rows("P01", 1, slots=ALL_SLOTS[:-1])
            + self.session_rows("P02", 1, slots=ALL_SLOTS)
        )
        grid = build_grid()
        self.assertEqual(grid.cells["P01"][0], CellStatus.FLAGGED)
        self.assertEqual(grid.cells["P02"][0], CellStatus.COMPLETE)

    def test_a_redone_take_does_not_break_completeness(self) -> None:
        # A redo appends a second row for the same slot. The session still
        # covered every planned take, so it must not read as incomplete.
        rows = self.session_rows("P01", 1)
        rows += self.session_rows("P01", 1, slots=[ALL_SLOTS[2]], redo=1)
        self.write_master_log(rows)
        self.assertEqual(build_grid().cells["P01"][0], CellStatus.COMPLETE)

    def test_participants_are_the_union_of_roster_and_log(self) -> None:
        self.participants_path.write_text(
            '{"P01": "passage one", "P02": "passage two"}', encoding="utf-8"
        )
        self.write_master_log(self.session_rows("P03", 1))
        grid = build_grid()
        self.assertEqual(grid.participants, ("P01", "P02", "P03"))
        # A rostered participant who has never recorded is all-missing, which
        # is the whole point of the screen.
        self.assertTrue(all(c is CellStatus.MISSING for c in grid.cells["P01"]))
        self.assertEqual(grid.cells["P03"][0], CellStatus.COMPLETE)

    def test_roster_as_a_list_of_objects(self) -> None:
        self.participants_path.write_text(
            '[{"pseudonym": "P07", "passage_text": "x"}]', encoding="utf-8"
        )
        self.assertEqual(build_grid().participants, ("P07",))

    def test_missing_log_is_day_one_not_an_error(self) -> None:
        self.assertFalse(self.master_log.exists())
        grid = build_grid()
        self.assertEqual(grid.participants, ())
        self.assertEqual(grid.cells, {})
        self.assertEqual(grid.expected_sessions, config.EXPECTED_SESSIONS_PER_PARTICIPANT)

    def test_empty_log_file_is_day_one_too(self) -> None:
        self.master_log.write_text("", encoding="utf-8")
        self.participants_path.write_text('{"P01": "passage"}', encoding="utf-8")
        grid = build_grid()
        self.assertEqual(grid.participants, ("P01",))
        self.assertEqual(len(grid.cells["P01"]), config.EXPECTED_SESSIONS_PER_PARTICIPANT)
        self.assertTrue(all(c is CellStatus.MISSING for c in grid.cells["P01"]))

    def test_header_only_log_is_valid_and_empty(self) -> None:
        self.write_master_log([])
        self.assertEqual(build_grid().participants, ())

    def test_malformed_header_fails_loudly(self) -> None:
        self.master_log.write_text("participant,session_id\nP01,001\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            build_grid()

    def test_unreadable_row_never_reads_as_complete(self) -> None:
        rows = self.session_rows("P01", 1)
        rows[4]["take"] = "not-a-number"
        self.write_master_log(rows)
        self.assertEqual(build_grid().cells["P01"][0], CellStatus.FLAGGED)

    def test_out_of_range_session_number_is_skipped_not_crashed(self) -> None:
        rows = self.session_rows("P01", config.EXPECTED_SESSIONS_PER_PARTICIPANT + 5)
        rows += self.session_rows("P01", 1)
        self.write_master_log(rows)
        grid = build_grid()
        self.assertEqual(len(grid.cells["P01"]), config.EXPECTED_SESSIONS_PER_PARTICIPANT)
        self.assertEqual(grid.cells["P01"][0], CellStatus.COMPLETE)


class StartupSummaryTests(TempDataDirCase):
    def make_take(self, participant: str, session: str, filename: str) -> Path:
        directory = self.data_dir / participant / session
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        path.write_bytes(b"RIFF-not-really-audio")
        return path

    def test_counts_sessions_takes_and_partials(self) -> None:
        self.make_take("P01", "001_20260822T060000Z", "01_silence.wav")
        self.make_take("P01", "001_20260822T060000Z", "02_reference_tone.wav")
        self.make_take("P01", "002_20260822T120000Z", "01_silence.wav")
        self.make_take("P02", "001_20260822T063000Z", "01_silence.wav")
        # A take that died mid-write: never renamed, must be reported.
        self.make_take("P02", "001_20260822T063000Z", "03_sustained_a_take1.wav.partial")
        # Sidecars and the consent store must not be mistaken for takes.
        (self.data_dir / "P01" / "001_20260822T060000Z" / "meta.json").write_text(
            "{}", encoding="utf-8"
        )
        (self.data_dir / "consent").mkdir()
        (self.data_dir / "consent" / "P01.json").write_text("{}", encoding="utf-8")

        summary = startup_summary()
        self.assertEqual(summary["sessions"], 3)
        self.assertEqual(summary["takes"], 4)
        self.assertEqual(summary["partials"], 1)

    def test_frontend_keys_are_present(self) -> None:
        # static/js/app.js reads exactly data.sessions and data.takes.
        summary = startup_summary()
        self.assertIn("sessions", summary)
        self.assertIn("takes", summary)
        self.assertIn("partials", summary)

    def test_empty_data_dir_is_all_zeroes(self) -> None:
        self.assertEqual(
            startup_summary(), {"sessions": 0, "takes": 0, "partials": 0}
        )

    def test_absent_data_dir_is_all_zeroes(self) -> None:
        with mock.patch.object(config, "DATA_DIR", self.root / "nothing-here"):
            self.assertEqual(
                startup_summary(), {"sessions": 0, "takes": 0, "partials": 0}
            )


def _matches(src: Path, relative: str) -> bool:
    """Fake copies are keyed by relative path, not basename — two sessions
    legitimately hold files with the same name."""
    return Path(src).as_posix().endswith(relative)


def resizing_copy(sizes: Mapping[str, int]) -> Callable[[Path, Path], Path]:
    """A copy that writes named files at a wrong length — a bad USB write."""

    def _copy(src: Path, dst: Path) -> Path:
        data = Path(src).read_bytes()
        for relative, wanted in sizes.items():
            if _matches(src, relative):
                data = data[:wanted].ljust(wanted, b"\x00")
                break
        Path(dst).write_bytes(data)
        return Path(dst)

    return _copy


def skipping_copy(relatives: Sequence[str]) -> Callable[[Path, Path], Path]:
    """A copy that silently drops named files — a file that never arrives."""

    def _copy(src: Path, dst: Path) -> Path:
        if not any(_matches(src, relative) for relative in relatives):
            Path(dst).write_bytes(Path(src).read_bytes())
        return Path(dst)

    return _copy


def failing_copy(relatives: Sequence[str]) -> Callable[[Path, Path], Path]:
    """A copy that raises on named files — a drive that fills up mid-export."""

    def _copy(src: Path, dst: Path) -> Path:
        if any(_matches(src, relative) for relative in relatives):
            raise OSError(28, "No space left on device")
        Path(dst).write_bytes(Path(src).read_bytes())
        return Path(dst)

    return _copy


class UsbExportTests(TempDataDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.destination = self.root / "usb"
        self.source_files = {
            "P01/001_20260822T060000Z/meta.json": b'{"info": {}}',
            "P01/001_20260822T060000Z/01_silence.wav": b"silence-take" * 40,
            "P01/001_20260822T060000Z/03_sustained_a_take1.wav": b"vowel-take" * 60,
            "P02/001_20260822T063000Z/01_silence.wav": b"another-take" * 30,
            "consent/P01.json": b'{"agreed": true}',
            "master_log.csv": b"participant,session_id\n",
        }
        for relative, payload in self.source_files.items():
            path = self.data_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        # The name<->pseudonym key lives OUTSIDE data/ by design (§11).
        self.key_file = self.root / "space_ready_pseudonym_key.json"
        self.key_file.write_bytes(b'{"Ravi": "P01"}')

    def total_source_bytes(self) -> int:
        return sum(len(payload) for payload in self.source_files.values())

    def test_round_trip_verifies_clean(self) -> None:
        report = copy_to_usb(self.destination)
        self.assertTrue(report.ok, report.mismatches)
        self.assertEqual(report.mismatches, ())
        self.assertEqual(report.files_copied, len(self.source_files))
        self.assertEqual(report.bytes_copied, self.total_source_bytes())
        self.assertEqual(report.source_file_count, len(self.source_files))
        self.assertEqual(report.destination_file_count, len(self.source_files))
        for relative, payload in self.source_files.items():
            self.assertEqual((self.destination / relative).read_bytes(), payload)

    def test_pseudonym_key_file_is_never_exported(self) -> None:
        copy_to_usb(self.destination)
        exported = [p.name for p in self.destination.rglob("*") if p.is_file()]
        self.assertNotIn(self.key_file.name, exported)

    def test_truncated_destination_file_is_caught(self) -> None:
        target = "P01/001_20260822T060000Z/03_sustained_a_take1.wav"
        with mock.patch(
            "capture.storage.export.shutil.copy2", new=resizing_copy({target: 10})
        ):
            report = copy_to_usb(self.destination)

        self.assertFalse(report.ok)
        self.assertEqual(len(report.mismatches), 1)
        self.assertIn("SIZE MISMATCH", report.mismatches[0])
        self.assertIn(target, report.mismatches[0])
        # The file set matched perfectly — only a per-file size check finds it.
        self.assertEqual(report.source_file_count, report.destination_file_count)

    def test_offsetting_size_errors_do_not_cancel_out(self) -> None:
        shorter = "P01/001_20260822T060000Z/01_silence.wav"
        longer = "P01/001_20260822T060000Z/03_sustained_a_take1.wav"
        # One file loses 100 bytes, another gains 100: the totals agree, so an
        # aggregate byte check would pass this corrupted copy.
        with mock.patch(
            "capture.storage.export.shutil.copy2",
            new=resizing_copy(
                {
                    shorter: len(self.source_files[shorter]) - 100,
                    longer: len(self.source_files[longer]) + 100,
                }
            ),
        ):
            report = copy_to_usb(self.destination)

        self.assertFalse(report.ok)
        self.assertEqual(len(report.mismatches), 2)
        self.assertTrue(all("SIZE MISMATCH" in m for m in report.mismatches))
        destination_bytes = sum(
            p.stat().st_size for p in self.destination.rglob("*") if p.is_file()
        )
        self.assertEqual(destination_bytes, self.total_source_bytes())

    def test_file_that_never_arrives_is_reported(self) -> None:
        missing = "P01/001_20260822T060000Z/01_silence.wav"
        with mock.patch(
            "capture.storage.export.shutil.copy2", new=skipping_copy([missing])
        ):
            report = copy_to_usb(self.destination)

        self.assertFalse(report.ok)
        # A copy that quietly writes nothing is caught twice over: the size
        # check straight after the copy, and the verification re-walk.
        self.assertIn(f"MISSING at destination: {missing}", report.mismatches)
        self.assertTrue(any(m.startswith("COPY FAILED:") for m in report.mismatches))
        self.assertEqual(report.files_copied, len(self.source_files) - 1)
        self.assertEqual(report.destination_file_count, len(self.source_files) - 1)

    def test_copy_failure_is_reported_and_the_rest_still_copies(self) -> None:
        with mock.patch(
            "capture.storage.export.shutil.copy2",
            new=failing_copy(["P01/001_20260822T060000Z/03_sustained_a_take1.wav"]),
        ):
            report = copy_to_usb(self.destination)

        self.assertFalse(report.ok)
        self.assertEqual(report.files_copied, len(self.source_files) - 1)
        self.assertTrue(any(m.startswith("COPY FAILED:") for m in report.mismatches))
        self.assertTrue(any(m.startswith("MISSING at destination:") for m in report.mismatches))

    def test_extra_destination_file_is_reported_and_never_deleted(self) -> None:
        stray = self.destination / "P01" / "001_20260822T060000Z" / "old_take.wav"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"from a previous export")

        report = copy_to_usb(self.destination)

        self.assertFalse(report.ok)
        self.assertEqual(len(report.mismatches), 1)
        self.assertIn("EXTRA at destination", report.mismatches[0])
        self.assertIn("old_take.wav", report.mismatches[0])
        self.assertTrue(stray.exists(), "the export must never delete at the destination")

    def test_re_export_over_a_previous_copy_is_clean(self) -> None:
        self.assertTrue(copy_to_usb(self.destination).ok)
        second = copy_to_usb(self.destination)
        self.assertTrue(second.ok, second.mismatches)
        self.assertEqual(second.files_copied, len(self.source_files))

    def test_refuses_a_destination_inside_the_data_dir(self) -> None:
        with self.assertRaises(ValueError):
            copy_to_usb(self.data_dir / "backup")
        with self.assertRaises(ValueError):
            copy_to_usb(self.data_dir)
        with self.assertRaises(ValueError):
            copy_to_usb(self.root)  # would contain data/ itself

    def test_absent_data_dir_reports_loudly_instead_of_succeeding(self) -> None:
        with mock.patch.object(config, "DATA_DIR", self.root / "nothing-here"):
            report = copy_to_usb(self.destination)
        self.assertFalse(report.ok)
        self.assertEqual(report.files_copied, 0)
        self.assertEqual(len(report.mismatches), 1)

    def test_destination_that_is_a_file_reports_loudly(self) -> None:
        blocker = self.root / "not-a-directory"
        blocker.write_bytes(b"x")
        report = copy_to_usb(blocker)
        self.assertFalse(report.ok)
        self.assertEqual(report.files_copied, 0)


if __name__ == "__main__":
    unittest.main()
