"""Filename/ID builders and the never-overwrite guard."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

from capture.domain.tasks import BY_NUMBER
from capture.storage.paths import (
    cloud_sync_warning,
    next_free_path,
    partial_path,
    session_id,
    take_filename,
)


class SessionIdTests(unittest.TestCase):
    def test_format_counter_plus_entered_utc(self) -> None:
        utc = datetime(2026, 8, 22, 7, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(session_id(1, utc), "001_20260822T073000Z")

    def test_sorts_chronologically_by_counter(self) -> None:
        utc = datetime(2026, 8, 22, 7, 30, 0, tzinfo=timezone.utc)
        ids = [session_id(n, utc) for n in (1, 2, 10, 21)]
        self.assertEqual(ids, sorted(ids))

    def test_rejects_naive_datetime(self) -> None:
        with self.assertRaises(ValueError):
            session_id(1, datetime(2026, 8, 22, 7, 30, 0))


class TakeFilenameTests(unittest.TestCase):
    def test_layout_matches_claude_md(self) -> None:
        # Tasks 1-2: no take suffix. Others: _takeN. Exactly the documented
        # layout.
        self.assertEqual(
            take_filename(BY_NUMBER[1], "silence", 1), "01_silence.wav"
        )
        self.assertEqual(
            take_filename(BY_NUMBER[3], "sustained_a", 2),
            "03_sustained_a_take2.wav",
        )
        self.assertEqual(
            take_filename(BY_NUMBER[8], "sustained_z", 1),
            "08_sustained_z_take1.wav",
        )

    def test_redo_suffix_never_collides_with_original(self) -> None:
        original = take_filename(BY_NUMBER[3], "sustained_a", 2)
        redo = take_filename(BY_NUMBER[3], "sustained_a", 2, redo_n=1)
        self.assertEqual(redo, "03_sustained_a_take2_redo1.wav")
        self.assertNotEqual(original, redo)

    def test_rejects_wrong_stem_or_take(self) -> None:
        with self.assertRaises(ValueError):
            take_filename(BY_NUMBER[3], "sustained_i", 1)
        with self.assertRaises(ValueError):
            take_filename(BY_NUMBER[3], "sustained_a", 4)  # only 3 takes


class NeverOverwriteTests(unittest.TestCase):
    def test_partial_path(self) -> None:
        self.assertEqual(
            partial_path(Path("x/01_silence.wav")).name, "01_silence.wav.partial"
        )

    def test_next_free_path_suffixes_instead_of_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "03_sustained_a_take1.wav"
            self.assertEqual(next_free_path(target), target)  # free: unchanged
            target.write_bytes(b"existing take - must never be overwritten")
            dup1 = next_free_path(target)
            self.assertEqual(dup1.name, "03_sustained_a_take1_dup1.wav")
            dup1.write_bytes(b"second")
            self.assertEqual(
                next_free_path(target).name, "03_sustained_a_take1_dup2.wav"
            )




class CloudSyncDetectionTests(unittest.TestCase):
    """Recordings inside a synced folder are uploaded automatically, which
    CLAUDE.md forbids and GDPR takes seriously. Detect it, loudly."""

    def test_onedrive_path_is_flagged(self) -> None:
        with mock.patch.dict(
            os.environ, {"OneDrive": r"C:\Users\someone\OneDrive"}, clear=False
        ):
            warning = cloud_sync_warning(
                Path(r"C:\Users\someone\OneDrive\Desktop\study\data")
            )
        self.assertIsNotNone(warning)
        self.assertIn("OneDrive", warning)

    def test_named_sync_folders_are_flagged_without_an_env_var(self) -> None:
        for folder in ("Dropbox", "Google Drive", "iCloud Drive", "Nextcloud"):
            warning = cloud_sync_warning(Path("C:/Users/x") / folder / "study" / "data")
            self.assertIsNotNone(warning, folder)

    def test_a_plain_local_path_is_not_flagged(self) -> None:
        self.assertIsNone(cloud_sync_warning(Path(r"C:\space_ready_data")))
        self.assertIsNone(cloud_sync_warning(Path(r"D:\mission\data")))

    def test_a_folder_merely_mentioning_a_service_is_not_flagged(self) -> None:
        # "my-dropbox-notes" is not a Dropbox sync root; only a whole path
        # component counts, so this must not cry wolf.
        self.assertIsNone(cloud_sync_warning(Path(r"C:\work\my-dropbox-notes\data")))




class TakeAudioPathGuardTests(unittest.TestCase):
    """The listen-back endpoint serves files by name from a session folder.

    A crafted name must not be able to walk out of that folder, and only
    finished takes may be served: meta.json, the master log, and anything
    outside the session are all off limits.
    """

    def _served(self, session_dir: Path, filename: str) -> bool:
        # Exactly the guard in routes.session.take_audio.
        candidate = (session_dir / filename).resolve()
        directory = session_dir.resolve()
        return directory in candidate.parents and candidate.suffix.lower() == ".wav"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.session = Path(self._tmp.name) / "P01" / "001_20260822T073000Z"
        self.session.mkdir(parents=True)

    def test_a_normal_take_is_served(self) -> None:
        self.assertTrue(self._served(self.session, "03_sustained_a_take1.wav"))

    def test_parent_traversal_is_refused(self) -> None:
        for name in ("../../master_log.csv", r"..\..\master_log.csv", "../../../secrets.wav"):
            self.assertFalse(self._served(self.session, name), name)

    def test_absolute_paths_are_refused(self) -> None:
        for name in ("C:/Windows/win.ini", "/etc/passwd"):
            self.assertFalse(self._served(self.session, name), name)

    def test_non_wav_files_in_the_session_are_refused(self) -> None:
        # meta.json lives right next to the takes and must not be servable.
        for name in ("meta.json", "01_silence.wav.partial"):
            self.assertFalse(self._served(self.session, name), name)

    def test_a_path_that_normalises_back_inside_is_allowed(self) -> None:
        self.assertTrue(self._served(self.session, "sub/../01_silence.wav"))


if __name__ == "__main__":
    unittest.main()
