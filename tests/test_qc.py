"""Immediate QC: integrity sanity checks on a finished take.

Every case writes a real WAV in the capture format (48 kHz / 24-bit / mono)
and reads it back through check_take, so these tests exercise the same
soundfile path the mission does - not a mocked one.

The rule these tests exist to protect: the warn list must fire on every real
failure and stay empty otherwise. A list that cries wolf at 3am is a list the
operator stops reading.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from capture import config
from capture.audio.qc import check_take, peak_dbfs, rms_dbfs
from capture.domain.models import QCResult
from capture.domain.tasks import BY_NUMBER, TaskSpec

SILENCE_TASK = BY_NUMBER[1]  # room silence - a quiet take is CORRECT here
REFERENCE_TONE_TASK = BY_NUMBER[2]
SUSTAINED_A_TASK = BY_NUMBER[3]  # target_s = 5.0
MPT_TASK = BY_NUMBER[9]  # target_s = None -> qc_floor_s applies


class QCTestCase(unittest.TestCase):
    """Shared temp directory and signal synthesis."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write_wav(
        self,
        name: str,
        samples: np.ndarray,
        sample_rate: int = config.SAMPLE_RATE_HZ,
        subtype: str = config.SOUNDFILE_SUBTYPE,
    ) -> Path:
        path = self.dir / name
        sf.write(path, samples, sample_rate, subtype=subtype)
        return path

    @staticmethod
    def sine(seconds: float, amplitude: float, hz: float = 150.0) -> np.ndarray:
        frames = int(round(seconds * config.SAMPLE_RATE_HZ))
        t = np.arange(frames, dtype=np.float64) / config.SAMPLE_RATE_HZ
        return amplitude * np.sin(2.0 * np.pi * hz * t)

    @staticmethod
    def quiet(seconds: float) -> np.ndarray:
        frames = int(round(seconds * config.SAMPLE_RATE_HZ))
        return np.zeros(frames, dtype=np.float64)

    def assert_warns_about(self, result: QCResult, fragment: str) -> None:
        joined = " | ".join(result.warnings)
        self.assertIn(fragment, joined)
        self.assertEqual(result.status, "warn")


class CleanTakeTests(QCTestCase):
    def test_clean_take_passes_with_zero_warnings(self) -> None:
        path = self.write_wav("03_sustained_a_take1.wav", self.sine(5.0, 0.1))
        result = check_take(path, SUSTAINED_A_TASK, [])
        self.assertEqual(result.warnings, ())
        self.assertEqual(result.status, "pass")
        self.assertFalse(result.clipped)
        self.assertTrue(result.voicing_detected)
        self.assertTrue(result.duration_ok)

    def test_check_take_does_not_modify_the_file(self) -> None:
        # QC reads a finished take. It must never rewrite, trim or normalise
        # it - CLAUDE.md's "no processing of any kind".
        path = self.write_wav("03_sustained_a_take1.wav", self.sine(5.0, 0.1))
        before = path.read_bytes()
        check_take(path, SUSTAINED_A_TASK, [])
        self.assertEqual(path.read_bytes(), before)

    def test_missing_file_raises_instead_of_reporting_a_pass(self) -> None:
        with self.assertRaises(FileNotFoundError):
            check_take(self.dir / "never_written.wav", SUSTAINED_A_TASK, [])


class ClippingTests(QCTestCase):
    def test_clipped_take_is_flagged(self) -> None:
        # A heavily over-driven sine sits at full scale for long stretches.
        clipped_signal = np.clip(self.sine(5.0, 3.0), -1.0, 1.0)
        path = self.write_wav("03_sustained_a_take1.wav", clipped_signal)
        result = check_take(path, SUSTAINED_A_TASK, [])
        self.assertTrue(result.clipped)
        self.assert_warns_about(result, "CLIPPED")

    def test_clipping_advice_never_tells_the_operator_to_change_gain(self) -> None:
        # The gain is fixed, taped and photographed for the whole mission
        # (CLAUDE.md, "Fixed input gain"). The fix for a clipped take is
        # distance, never level.
        clipped_signal = np.clip(self.sine(5.0, 3.0), -1.0, 1.0)
        path = self.write_wav("03_sustained_a_take1.wav", clipped_signal)
        result = check_take(path, SUSTAINED_A_TASK, [])
        self.assertIn("Do NOT change the gain", " | ".join(result.warnings))

    def test_loud_but_unclipped_take_is_not_flagged(self) -> None:
        path = self.write_wav("03_sustained_a_take1.wav", self.sine(5.0, 0.9))
        result = check_take(path, SUSTAINED_A_TASK, [])
        self.assertFalse(result.clipped)
        self.assertEqual(result.warnings, ())

    def test_isolated_full_scale_samples_are_not_clipping(self) -> None:
        # Fewer than QC_CLIP_CONSECUTIVE samples at full scale is a spike,
        # not a clipped take.
        signal = self.sine(5.0, 0.1)
        signal[1000 : 1000 + config.QC_CLIP_CONSECUTIVE - 1] = 1.0
        path = self.write_wav("03_sustained_a_take1.wav", signal)
        result = check_take(path, SUSTAINED_A_TASK, [])
        self.assertFalse(result.clipped)
        self.assertEqual(result.warnings, ())


class VoicingTests(QCTestCase):
    def test_silent_speech_take_is_flagged(self) -> None:
        path = self.write_wav("03_sustained_a_take1.wav", self.quiet(5.0))
        result = check_take(path, SUSTAINED_A_TASK, [])
        self.assertFalse(result.voicing_detected)
        self.assert_warns_about(result, "NO VOICE RECORDED")

    def test_silent_room_silence_take_is_correct_and_never_flagged(self) -> None:
        # Task 1 characterises the noise floor. Silence is the RESULT, not a
        # failure - warning here would be the tool crying wolf every session.
        path = self.write_wav("01_silence.wav", self.quiet(3.0))
        result = check_take(path, SILENCE_TASK, [])
        self.assertFalse(result.voicing_detected)
        self.assertEqual(result.warnings, ())
        self.assertEqual(result.status, "pass")

    def test_silent_reference_tone_take_names_the_speaker(self) -> None:
        # Task 2 is not speech: the useful instruction is about the speaker,
        # not the participant's voice.
        path = self.write_wav("02_reference_tone.wav", self.quiet(5.0))
        result = check_take(path, REFERENCE_TONE_TASK, [])
        self.assert_warns_about(result, "NO TONE RECORDED")

    def test_empty_file_is_flagged_not_crashed_on(self) -> None:
        # The nightmare case: a take that recorded nothing at all.
        path = self.write_wav("09_mpt_take1.wav", self.quiet(0.0))
        result = check_take(path, MPT_TASK, [])
        self.assertFalse(result.voicing_detected)
        self.assertFalse(result.duration_ok)
        self.assertFalse(result.clipped)
        self.assert_warns_about(result, "NO VOICE RECORDED")
        self.assert_warns_about(result, "TOO SHORT")

    def test_faint_but_audible_take_passes(self) -> None:
        # Just above the voicing floor: still a real take, no warning.
        amplitude = 10.0 ** ((config.QC_VOICING_FLOOR_DBFS + 6.0) / 20.0) * np.sqrt(2.0)
        path = self.write_wav("03_sustained_a_take1.wav", self.sine(5.0, amplitude))
        result = check_take(path, SUSTAINED_A_TASK, [])
        self.assertTrue(result.voicing_detected)
        self.assertEqual(result.warnings, ())


class DurationTests(QCTestCase):
    def test_take_far_below_its_target_is_flagged(self) -> None:
        short_s = SUSTAINED_A_TASK.target_s * config.QC_SHORT_FRACTION * 0.5
        path = self.write_wav("03_sustained_a_take1.wav", self.sine(short_s, 0.1))
        result = check_take(path, SUSTAINED_A_TASK, [])
        self.assertFalse(result.duration_ok)
        self.assert_warns_about(result, "TOO SHORT")

    def test_slightly_short_take_within_tolerance_passes(self) -> None:
        ok_s = SUSTAINED_A_TASK.target_s * config.QC_SHORT_FRACTION + 0.5
        path = self.write_wav("03_sustained_a_take1.wav", self.sine(ok_s, 0.1))
        result = check_take(path, SUSTAINED_A_TASK, [])
        self.assertTrue(result.duration_ok)
        self.assertEqual(result.warnings, ())

    def test_no_target_task_uses_its_qc_floor(self) -> None:
        # Task 9 has no ceiling and no target; a 1.5 s "maximum phonation
        # time" is an error, not a result (ARCHITECTURE.md section 7).
        self.assertIsNone(MPT_TASK.target_s)
        too_short = self.write_wav("09_mpt_take1.wav", self.sine(MPT_TASK.qc_floor_s - 1.0, 0.1))
        self.assertFalse(check_take(too_short, MPT_TASK, []).duration_ok)

        long_enough = self.write_wav(
            "09_mpt_take2.wav", self.sine(MPT_TASK.qc_floor_s + 1.0, 0.1)
        )
        result = check_take(long_enough, MPT_TASK, [])
        self.assertTrue(result.duration_ok)
        self.assertEqual(result.warnings, ())


class RmsBaselineTests(QCTestCase):
    def measured_rms(self, path: Path, task: TaskSpec = SUSTAINED_A_TASK) -> float:
        """The take's own RMS, so the fixtures below track the config band
        instead of hardcoding dBFS numbers."""
        return check_take(path, task, []).rms_dbfs

    def test_empty_history_reports_no_baseline_and_no_warning(self) -> None:
        # A participant's first session must never look like a failure.
        path = self.write_wav("03_sustained_a_take1.wav", self.sine(5.0, 0.1))
        result = check_take(path, SUSTAINED_A_TASK, [])
        self.assertIsNone(result.rms_in_range)
        self.assertEqual(result.warnings, ())
        self.assertEqual(result.status, "pass")

    def test_rms_inside_the_band_passes(self) -> None:
        path = self.write_wav("03_sustained_a_take1.wav", self.sine(5.0, 0.1))
        baseline = self.measured_rms(path) - (config.QC_RMS_BAND_DB - 1.0)
        result = check_take(path, SUSTAINED_A_TASK, [baseline, baseline])
        self.assertTrue(result.rms_in_range)
        self.assertEqual(result.warnings, ())

    def test_rms_outside_the_band_is_flagged(self) -> None:
        path = self.write_wav("03_sustained_a_take1.wav", self.sine(5.0, 0.1))
        baseline = self.measured_rms(path) - (config.QC_RMS_BAND_DB + 1.0)
        result = check_take(path, SUSTAINED_A_TASK, [baseline, baseline])
        self.assertFalse(result.rms_in_range)
        self.assert_warns_about(result, "LEVEL UNUSUAL")

    def test_silent_take_gives_one_warning_not_two(self) -> None:
        # A silent take is out of range as well, but the operator has exactly
        # one thing to do about it. Only the "no voice" warning is emitted.
        path = self.write_wav("03_sustained_a_take1.wav", self.quiet(5.0))
        result = check_take(path, SUSTAINED_A_TASK, [-25.0, -24.0])
        self.assertFalse(result.rms_in_range)
        self.assertEqual(len(result.warnings), 1)
        self.assert_warns_about(result, "NO VOICE RECORDED")

    def test_dead_microphone_is_caught_even_on_the_silence_task(self) -> None:
        # Task 1 never warns about quiet - so the range check is the only
        # thing that can notice the mic died. It must still fire here.
        path = self.write_wav("01_silence.wav", self.quiet(3.0))
        result = check_take(path, SILENCE_TASK, [-68.0, -70.0])
        self.assertFalse(result.rms_in_range)
        self.assert_warns_about(result, "LEVEL UNUSUAL")

    def test_normal_room_silence_against_its_own_history_passes(self) -> None:
        path = self.write_wav("01_silence.wav", self.sine(3.0, 0.0006))
        baseline = self.measured_rms(path, SILENCE_TASK)
        result = check_take(path, SILENCE_TASK, [baseline + 1.0, baseline - 1.0])
        self.assertTrue(result.rms_in_range)
        self.assertEqual(result.warnings, ())


class FormatTests(QCTestCase):
    def test_wrong_sample_rate_or_subtype_is_flagged(self) -> None:
        # Beyond the four required checks, but a take written at the wrong
        # rate or bit depth invalidates the measurement it exists for, and it
        # costs one header read to catch on take 1 instead of after the
        # mission (CLAUDE.md acceptance criterion 3).
        path = self.write_wav(
            "03_sustained_a_take1.wav",
            self.sine(5.0, 0.1),
            sample_rate=44_100,
            subtype="PCM_16",
        )
        result = check_take(path, SUSTAINED_A_TASK, [])
        self.assert_warns_about(result, "WRONG FORMAT")

    def test_capture_format_is_not_flagged(self) -> None:
        path = self.write_wav("03_sustained_a_take1.wav", self.sine(5.0, 0.1))
        self.assertEqual(check_take(path, SUSTAINED_A_TASK, []).warnings, ())


class LevelHelperTests(unittest.TestCase):
    def test_full_scale_sine_is_about_minus_three_dbfs(self) -> None:
        t = np.arange(config.SAMPLE_RATE_HZ, dtype=np.float64) / config.SAMPLE_RATE_HZ
        signal = np.sin(2.0 * np.pi * 100.0 * t)
        self.assertAlmostEqual(rms_dbfs(signal), -3.01, places=2)
        self.assertAlmostEqual(peak_dbfs(signal), 0.0, places=2)

    def test_all_zero_buffer_reports_a_floor_not_negative_infinity(self) -> None:
        silence = np.zeros(1000, dtype=np.float64)
        self.assertEqual(rms_dbfs(silence), -120.0)
        self.assertEqual(peak_dbfs(silence), -120.0)


if __name__ == "__main__":
    unittest.main()
