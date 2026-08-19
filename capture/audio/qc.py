"""Immediate QC - integrity sanity checks, NOT acoustic analysis.

Runs on a finished take, numpy only (ARCHITECTURE.md section 9): clipping,
RMS far outside the participant's running range, no voicing detected,
implausibly short. Results feed the pass/warn list on the QC review screen so
the operator can redo takes on the spot. Reads the written file; never
touches or rewrites it.

Warning wording is deliberate: the operator reading it is tired, mid-session,
and needs the action, not the diagnosis. One warning per failed check, and
only when there is something to do about it - a list that cries wolf is a
list that gets ignored.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Sequence

import logging

import numpy as np
import soundfile as sf

from capture import config
from capture.domain.models import QCResult
from capture.domain.tasks import TaskSpec

log = logging.getLogger("capture.qc")

_SILENCE_DBFS = -120.0  # reported for an all-zero buffer instead of -inf

# Task keys handled specially below. They mirror config.TASK_BATTERY keys but
# live here because they encode QC's own policy, not a tunable value.
_SILENCE_TASK_KEY = "silence"
_REFERENCE_TONE_TASK_KEY = "reference_tone"


def rms_dbfs(x: np.ndarray) -> float:
    """RMS of a float view scaled to [-1, 1], in dBFS. Pure read."""
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return 20.0 * math.log10(rms) if rms > 0.0 else _SILENCE_DBFS


def peak_dbfs(x: np.ndarray) -> float:
    peak = float(np.max(np.abs(x)))
    return 20.0 * math.log10(peak) if peak > 0.0 else _SILENCE_DBFS


def _longest_true_run(flags: np.ndarray) -> int:
    """Length of the longest run of True in a 1-D boolean array."""
    if not flags.any():
        return 0
    # Pad with False so every run has both a start edge and an end edge, then
    # read the edges off in pairs.
    padded = np.concatenate(([False], flags, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    starts = edges[0::2]
    ends = edges[1::2]
    return int(np.max(ends - starts))


def effective_bit_depth(path: Path) -> int | None:
    """How many bits of the samples actually carry signal.

    A 16-bit ADC written into a PCM_24 file leaves the low 8 bits zero, so
    the container says 24 while the real resolution is 16. Reading the file
    as int32 and finding the fewest trailing zero bits across non-silent
    samples recovers the truth. Returns None for an empty or silent file,
    where there is nothing to measure.
    """
    raw, _ = sf.read(path, dtype="int32", always_2d=False)
    # int64 so that negating the most negative int32 cannot overflow.
    raw = np.asarray(raw, dtype=np.int64).reshape(-1)
    nonzero = raw[raw != 0]
    if nonzero.size == 0:
        return None
    # v & -v isolates the lowest set bit; the smallest such bit across the
    # take is the quantisation step the hardware actually delivers.
    lowest_set = np.bitwise_and(nonzero, -nonzero)
    trailing = int(np.min(lowest_set)).bit_length() - 1
    return 32 - trailing


def robust_noise_floor_dbfs(path: Path, percentile: float = 10.0) -> float:
    """The room's floor, measured so one transient cannot inflate it.

    The whole-take RMS of the silence recording is the obvious measure and
    the wrong one: a door, a cough or a habitat pump cycling during those
    three seconds lifts it by tens of dB, and every later take in the session
    would then be judged against a threshold that is far too high — quiet
    but genuine phonation would be reported as "no voice recorded".

    Measured on a real Yeti the whole-take RMS swung between -55 and -40 dBFS
    across consecutive quiet recordings for exactly this reason. Taking a low
    percentile of short-term RMS instead describes the floor between events
    rather than the events, which is what "noise floor" is meant to mean.
    """
    samples, sample_rate = sf.read(path, dtype="float64", always_2d=False)
    samples = np.asarray(samples).reshape(-1)
    if samples.size == 0:
        return _SILENCE_DBFS

    window = max(1, int(0.05 * sample_rate))  # 50 ms
    usable = samples[: (samples.size // window) * window]
    if usable.size == 0:
        return rms_dbfs(samples)

    frames = usable.reshape(-1, window)
    frame_rms = np.sqrt(np.mean(np.square(frames), axis=1))
    quiet = float(np.percentile(frame_rms, percentile))
    return 20.0 * math.log10(quiet) if quiet > 0.0 else _SILENCE_DBFS


def check_take(
    path: Path,
    task: TaskSpec,
    participant_rms_history_dbfs: Sequence[float],
    noise_floor_dbfs: float | None = None,
) -> QCResult:
    """All four checks on one finished take.

    ``participant_rms_history_dbfs`` is that participant's running record
    from prior sessions' meta.json files; empty on session 1, in which case
    the range check reports "no baseline" (rms_in_range=None), never a false
    warning.

    The file is opened read-only and is never modified, rewritten, or moved.
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"QC: no file at {path} - the take was never written to disk."
        )

    info = sf.info(path)
    samples, sample_rate = sf.read(path, dtype="float64", always_2d=False)

    frame_count = int(samples.shape[0]) if samples.ndim > 0 else 0
    duration_s = frame_count / float(sample_rate)
    if samples.ndim > 1:
        # config.CHANNELS is 1, so this is a defensive path only. Flattening
        # interleaves the channels, which is fine for these coarse checks.
        samples = samples.reshape(-1)

    # soundfile scales any PCM subtype so full scale is 1.0, so every
    # threshold below is a plain fraction of full scale.
    if samples.size == 0:
        # An empty file: no samples to measure. Reported as silence rather
        # than as NaN, and caught by the voicing and duration checks below.
        take_rms = _SILENCE_DBFS
        take_peak = _SILENCE_DBFS
        clipped = False
    else:
        take_rms = rms_dbfs(samples)
        take_peak = peak_dbfs(samples)
        at_full_scale = np.abs(samples) >= config.QC_CLIP_LEVEL
        clipped = _longest_true_run(at_full_scale) >= config.QC_CLIP_CONSECUTIVE

    # --- RMS against this participant's running range ---------------------
    history = [float(value) for value in participant_rms_history_dbfs]
    if history:
        baseline_dbfs: float | None = sum(history) / len(history)
        rms_in_range: bool | None = (
            abs(take_rms - baseline_dbfs) <= config.QC_RMS_BAND_DB
        )
    else:
        # First session for this participant: nothing to compare against.
        # "No baseline" is not a failure - a first session must never look
        # like one, so no warning is emitted here at all.
        baseline_dbfs = None
        rms_in_range = None

    # Voicing is judged against THIS session's measured room floor when it
    # is known (task 1 exists to characterise it), because the right
    # threshold depends on the microphone, its gain and the room. The
    # absolute constant is only a fallback for the silence take itself and
    # for sessions whose floor was never captured.
    if noise_floor_dbfs is not None:
        voicing_threshold = noise_floor_dbfs + config.QC_VOICING_MARGIN_DB
    else:
        voicing_threshold = config.QC_VOICING_FLOOR_DBFS
    voicing_detected = take_rms > voicing_threshold

    if task.target_s is not None:
        minimum_s = task.target_s * config.QC_SHORT_FRACTION
    else:
        # Max-effort / guidance-only tasks have no target; they still have a
        # floor below which the take is implausible (ARCHITECTURE.md 7).
        minimum_s = task.qc_floor_s
    duration_ok = duration_s >= minimum_s

    # --- Warnings: one per failed check, worst first ----------------------
    warnings: list[str] = []

    if (
        info.samplerate != config.SAMPLE_RATE_HZ
        or info.subtype != config.SOUNDFILE_SUBTYPE
        or info.channels != config.CHANNELS
    ):
        # Not one of the four required checks, but a take in the wrong format
        # invalidates the measurement it exists for, and it costs one header
        # read to catch on take 1 instead of after the mission.
        warnings.append(
            f"WRONG FORMAT - {info.samplerate} Hz / {info.subtype} / "
            f"{info.channels} ch, expected {config.SAMPLE_RATE_HZ} Hz / "
            f"{config.SOUNDFILE_SUBTYPE} / {config.CHANNELS} ch. "
            "STOP and fix this before recording anything else."
        )

    warned_no_signal = False
    if not voicing_detected and task.key != _SILENCE_TASK_KEY:
        # Task 1 records the room's noise floor: silence is the CORRECT
        # result there and must never be flagged. Every other task is
        # supposed to contain signal, so a silent take means something was
        # not captured.
        warned_no_signal = True
        if task.key == _REFERENCE_TONE_TASK_KEY:
            warnings.append(
                f"NO TONE RECORDED - take is silent ({take_rms:.0f} dBFS). "
                "Check the speaker is on and in its fixed position, then redo."
            )
        else:
            warnings.append(
                f"NO VOICE RECORDED - take is silent ({take_rms:.0f} dBFS). "
                "Check the microphone is connected, then redo."
            )

    if clipped:
        warnings.append(
            "CLIPPED - the signal hit maximum level. Move the participant "
            "back from the microphone and redo. Do NOT change the gain."
        )

    if not duration_ok:
        warnings.append(
            f"TOO SHORT - {duration_s:.1f} s, expected at least "
            f"{minimum_s:.1f} s. Redo this take."
        )

    if rms_in_range is False and baseline_dbfs is not None and not warned_no_signal:
        # Skipped when the take was already flagged as silent: same problem,
        # same fix, and one warning is easier to act on than two.
        direction = "louder" if take_rms > baseline_dbfs else "quieter"
        warnings.append(
            f"LEVEL UNUSUAL - {take_rms:.0f} dBFS, {direction} than this "
            f"person's usual {baseline_dbfs:.0f} dBFS for this task. Check "
            "the microphone and the mouth-to-mic distance, then redo."
        )

    bits = effective_bit_depth(path)
    if bits is not None and bits < 24:
        # Not a warning the operator can act on mid-session — a 16-bit ADC
        # cannot be made 24-bit — but it must reach the metadata so the
        # analysis knows the real resolution and a mid-mission hardware
        # change is visible afterwards.
        log.info(
            "%s: samples carry %d bits of real resolution inside a 24-bit "
            "container (the microphone's ADC depth).",
            path.name,
            bits,
        )

    return QCResult(
        clipped=clipped,
        rms_dbfs=take_rms,
        peak_dbfs=take_peak,
        rms_in_range=rms_in_range,
        voicing_detected=voicing_detected,
        duration_ok=duration_ok,
        warnings=tuple(warnings),
        effective_bits=bits,
        noise_floor_dbfs=noise_floor_dbfs,
    )
