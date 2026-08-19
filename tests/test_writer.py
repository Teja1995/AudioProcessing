"""TakeWriter — the .partial-then-rename guarantee, without hardware.

These tests are the executable form of the promise CLAUDE.md's acceptance
criterion 4 makes: killing the process mid-session leaves every completed
take intact. That holds only if a file at its final name is, by
construction, a finished file — so what is checked here is exactly when the
final name appears, and that it never appears any other way.
"""

from __future__ import annotations

import gc
import os
import tempfile
import unittest
import warnings
from pathlib import Path

import numpy as np
import soundfile as sf

from capture import config
from capture.errors import StorageWriteError
from capture.storage.writer import TakeWriter

# The device delivers 24-bit samples left-justified in int32, so the low byte
# is zero and PCM_24 stores every significant bit (confirmed by the de-risk
# spike, ARCHITECTURE.md §8). Test data mirrors that: values whose low byte
# is clear must survive the round trip untouched.
BLOCK = (
    np.array(
        [0, 1, -1, 0x7FFFFF, -0x800000, 0x123456, -0x123456, 0x00ABCD],
        dtype=np.int32,
    )
    * 256
).reshape(-1, 1)


class WriterTestCase(unittest.TestCase):
    """Each test gets its own directory; nothing touches the real data/."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.final = self.dir / "03_sustained_a_take1.wav"
        self.partial = self.dir / ("03_sustained_a_take1.wav" + config.PARTIAL_SUFFIX)
        self.addCleanup(self._tmp.cleanup)


class PartialThenRenameTests(WriterTestCase):
    def test_only_the_partial_exists_while_recording(self) -> None:
        writer = TakeWriter(self.final)
        writer.open()
        self.addCleanup(writer.abort)

        self.assertTrue(self.partial.exists(), "no .partial while recording")
        self.assertFalse(
            self.final.exists(), "the final name must not exist mid-recording"
        )

        for _ in range(3):
            writer.write(BLOCK)
            self.assertFalse(
                self.final.exists(), "the final name appeared before finalize()"
            )
        self.assertTrue(self.partial.exists())

    def test_finalize_moves_partial_to_final(self) -> None:
        writer = TakeWriter(self.final)
        writer.open()
        writer.write(BLOCK)

        returned = writer.finalize()

        self.assertEqual(returned, self.final)
        self.assertTrue(self.final.exists(), "finalize() did not produce the take")
        self.assertFalse(
            self.partial.exists(), "the .partial must be gone after a clean finalize"
        )

    def test_frames_written_counts_frames_not_bytes(self) -> None:
        writer = TakeWriter(self.final)
        writer.open()
        writer.write(BLOCK)
        writer.write(BLOCK)
        self.assertEqual(writer.frames_written, 2 * len(BLOCK))
        writer.finalize()
        self.assertEqual(sf.info(self.final).frames, 2 * len(BLOCK))

    def test_a_killed_process_leaves_no_final_file(self) -> None:
        """Simulates the kill: open, write, and never finalize. Whatever the
        operator finds on disk must not look like a completed take."""
        writer = TakeWriter(self.final)
        writer.open()
        writer.write(BLOCK)
        with warnings.catch_warnings():
            # Real process death closes the handle for us; here the garbage
            # collector does it, and warns about it. That is the simulation,
            # not a leak.
            warnings.simplefilter("ignore", ResourceWarning)
            del writer  # no finalize(), no abort() — as if the process vanished
            gc.collect()

        self.assertFalse(self.final.exists())
        self.assertTrue(
            self.partial.exists(), "the in-progress recording should still be on disk"
        )


class AbortTests(WriterTestCase):
    def test_abort_never_produces_a_final_file(self) -> None:
        writer = TakeWriter(self.final)
        writer.open()
        writer.write(BLOCK)

        writer.abort()

        self.assertFalse(self.final.exists(), "abort() must never rename to final")

    def test_abort_keeps_the_partial_as_evidence(self) -> None:
        writer = TakeWriter(self.final)
        writer.open()
        writer.write(BLOCK)

        writer.abort()

        self.assertTrue(
            self.partial.exists(),
            "the partial recording is evidence of what went wrong — keep it",
        )
        self.assertGreater(self.partial.stat().st_size, 0)

    def test_abort_never_raises_past_itself(self) -> None:
        """abort() runs when things have already failed; a second exception
        on top of the first would hide the reason."""
        writer = TakeWriter(self.final)
        writer.open()
        writer.abort()
        writer.abort()  # already closed
        TakeWriter(self.final).abort()  # never opened at all

    def test_write_after_abort_fails_loudly(self) -> None:
        writer = TakeWriter(self.final)
        writer.open()
        writer.abort()
        with self.assertRaises(StorageWriteError):
            writer.write(BLOCK)


class NeverOverwriteTests(WriterTestCase):
    def test_open_refuses_when_the_final_take_already_exists(self) -> None:
        self.final.write_bytes(b"an irreplaceable take")
        writer = TakeWriter(self.final)

        with self.assertRaises(StorageWriteError):
            writer.open()

        self.assertEqual(self.final.read_bytes(), b"an irreplaceable take")

    def test_finalize_never_overwrites_a_take_that_appeared_meanwhile(self) -> None:
        """os.replace clobbers silently, so finalize() checks first. The
        recording that could not be renamed must still be on disk."""
        writer = TakeWriter(self.final)
        writer.open()
        writer.write(BLOCK)

        # Something else claims the final name after we opened.
        self.final.write_bytes(b"an irreplaceable take")

        with self.assertRaises(StorageWriteError):
            writer.finalize()

        self.assertEqual(
            self.final.read_bytes(),
            b"an irreplaceable take",
            "finalize() overwrote an existing take",
        )
        self.assertTrue(
            writer.partial_path.exists(),
            "the take that could not be renamed must not be lost",
        )
        self.assertGreater(writer.partial_path.stat().st_size, 0)

    def test_a_leftover_partial_is_kept_and_stepped_around(self) -> None:
        """A .partial from a previous crash is evidence too. Recording must
        neither truncate it nor refuse to start."""
        self.partial.write_bytes(b"yesterday's aborted take")
        writer = TakeWriter(self.final)
        writer.open()
        writer.write(BLOCK)
        final_path = writer.finalize()

        self.assertEqual(final_path, self.final)
        self.assertEqual(
            self.partial.read_bytes(),
            b"yesterday's aborted take",
            "the leftover .partial was destroyed",
        )
        self.assertEqual(sf.info(self.final).frames, len(BLOCK))


class FormatRoundTripTests(WriterTestCase):
    def test_file_is_48khz_24bit_mono_pcm_wav(self) -> None:
        writer = TakeWriter(self.final)
        writer.open()
        writer.write(BLOCK)
        writer.finalize()

        info = sf.info(self.final)
        self.assertEqual(info.samplerate, config.SAMPLE_RATE_HZ)
        self.assertEqual(info.samplerate, 48_000)
        self.assertEqual(info.channels, config.CHANNELS)
        self.assertEqual(info.channels, 1)
        self.assertEqual(info.subtype, config.SOUNDFILE_SUBTYPE)
        self.assertEqual(info.subtype, "PCM_24")
        self.assertEqual(info.format, "WAV")

    def test_samples_round_trip_bit_for_bit(self) -> None:
        """No gain, no normalisation, no filtering, no trimming: what went in
        is what comes out."""
        writer = TakeWriter(self.final)
        writer.open()
        writer.write(BLOCK)
        writer.write(BLOCK * -1)
        writer.finalize()

        back, samplerate = sf.read(self.final, dtype="int32", always_2d=True)

        self.assertEqual(samplerate, config.SAMPLE_RATE_HZ)
        expected = np.concatenate([BLOCK, BLOCK * -1], axis=0)
        np.testing.assert_array_equal(back, expected)

    def test_blocks_are_appended_in_order_not_overwritten(self) -> None:
        first = np.full((16, 1), 0x111100, dtype=np.int32)
        second = np.full((16, 1), 0x222200, dtype=np.int32)
        writer = TakeWriter(self.final)
        writer.open()
        writer.write(first)
        writer.write(second)
        writer.finalize()

        back, _ = sf.read(self.final, dtype="int32", always_2d=True)
        np.testing.assert_array_equal(back, np.concatenate([first, second], axis=0))

    def test_written_file_survives_a_read_after_finalize(self) -> None:
        """finalize() fsyncs before renaming, so the bytes on disk are the
        whole file — header included, not a header claiming zero frames."""
        writer = TakeWriter(self.final)
        writer.open()
        for _ in range(10):
            writer.write(BLOCK)
        writer.finalize()

        # 24-bit mono: 3 bytes per frame, plus a WAV header.
        payload = 10 * len(BLOCK) * 3
        self.assertGreaterEqual(os.path.getsize(self.final), payload)
        self.assertEqual(sf.info(self.final).frames, 10 * len(BLOCK))


class MisuseTests(WriterTestCase):
    def test_write_before_open_fails_loudly(self) -> None:
        with self.assertRaises(StorageWriteError):
            TakeWriter(self.final).write(BLOCK)

    def test_finalize_before_open_fails_loudly(self) -> None:
        with self.assertRaises(StorageWriteError):
            TakeWriter(self.final).finalize()

    def test_open_twice_fails_loudly(self) -> None:
        writer = TakeWriter(self.final)
        writer.open()
        self.addCleanup(writer.abort)
        with self.assertRaises(StorageWriteError):
            writer.open()

    def test_open_creates_missing_parent_directories(self) -> None:
        nested = self.dir / "P01" / "001_20260822T073000Z" / "01_silence.wav"
        writer = TakeWriter(nested)
        writer.open()
        writer.write(BLOCK)
        self.assertEqual(writer.finalize(), nested)
        self.assertTrue(nested.exists())


if __name__ == "__main__":
    unittest.main()
