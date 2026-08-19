"""Generate the vendored reference-tone calibration asset for task 2.

CLAUDE.md task 2 plays a fixed calibration tone through a speaker at a fixed
position and records it, so that gain or placement drift is detectable across
the mission week. That comparison is only meaningful if the *played* signal is
identical every session, so the tone is a vendored WAV generated ONCE and
committed (ARCHITECTURE.md §8) — it is not synthesised at session time.

    python tools/make_reference_tone.py            # write it if it is absent
    python tools/make_reference_tone.py --check     # verify, write nothing
    python tools/make_reference_tone.py --force     # deliberate regeneration

Re-running is safe: if the committed file already matches what would be
generated now, nothing is written and the file is left untouched, mtime and
all. If it does NOT match — because a constant in capture/config.py changed —
the script refuses and says so loudly rather than replacing an asset that
already-recorded sessions were calibrated against. Overriding that refusal
takes --force, and mid-mission it invalidates every week-scale drift
comparison. See tools/README.md.

Every number that shapes the tone comes from capture.config. Nothing about
the output depends on the wall clock, the machine, or any random source: two
renders are byte-identical, and the script proves that on every run before it
writes anything.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final, NamedTuple

import numpy as np
import numpy.typing as npt
import soundfile as sf

# tools/ is not on the import path when this file is run directly, so make the
# project root importable before `capture` is imported.
_PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from capture import config  # noqa: E402  — must follow the sys.path shim above

# --- Constants local to this tool ---------------------------------------
#
# These are NOT in capture/config.py on purpose: config fixes the sample
# format (SOUNDFILE_SUBTYPE) but not the container, which is already implied
# by REFERENCE_TONE_WAV's .wav extension, and the bit depths below are facts
# about libsndfile's subtypes rather than tunable settings.

SOUNDFILE_FORMAT: Final = "WAV"  # uncompressed RIFF/WAVE, never a lossy container

# Sample width of each integer PCM subtype libsndfile can write. This script
# builds integer samples itself, so a float subtype is rejected rather than
# silently mishandled.
_BITS_BY_SUBTYPE: Final[dict[str, int]] = {
    "PCM_16": 16,
    "PCM_24": 24,
    "PCM_32": 32,
}


class RenderedTone(NamedTuple):
    """One rendered tone: the exact integer samples, and the WAV file bytes
    that carry them."""

    frames: npt.NDArray[np.int32]
    wav_bytes: bytes


# --- Tone synthesis -----------------------------------------------------


def bits_per_sample(subtype: str) -> int:
    """Sample width for a libsndfile integer PCM subtype. Fails loudly on
    anything this script cannot write exactly."""
    bits = _BITS_BY_SUBTYPE.get(subtype)
    if bits is None:
        raise ValueError(
            f"config.SOUNDFILE_SUBTYPE is {subtype!r}; this generator writes "
            f"integer PCM only (one of {sorted(_BITS_BY_SUBTYPE)}). "
            "CLAUDE.md requires 24-bit uncompressed PCM."
        )
    return bits


def raised_cosine_ramp(frames: int) -> npt.NDArray[np.float64]:
    """Rising raised-cosine ramp: exactly 0.0 at the first sample, ~1.0 at the
    last. Reversed, it is the fade-out.

    A hard start would put a step discontinuity at the front of the tone, and
    a click is broadband — it would land inside the recorded calibration and
    contaminate exactly the measurement this task exists to make. A linear
    ramp removes the step but leaves a slope discontinuity at both joins; the
    raised cosine removes both.
    """
    if frames <= 0:
        raise ValueError(f"Fade needs at least one sample, got {frames}")
    n = np.arange(frames, dtype=np.float64)
    return 0.5 * (1.0 - np.cos(np.pi * n / float(frames)))


def tone_float() -> npt.NDArray[np.float64]:
    """The mono tone as float64 in [-1.0, 1.0], fades applied.

    Amplitude is read as PEAK dBFS: -20 dBFS means the steady part of the tone
    peaks at 0.1 of full scale, leaving the recorded copy plenty of headroom
    whatever the speaker is set to.
    """
    sample_rate = float(config.SAMPLE_RATE_HZ)
    total_frames = int(round(config.REFERENCE_TONE_DURATION_S * sample_rate))
    fade_frames = int(round(config.REFERENCE_TONE_FADE_S * sample_rate))

    if total_frames <= 0:
        raise ValueError(
            f"REFERENCE_TONE_DURATION_S={config.REFERENCE_TONE_DURATION_S} "
            "yields no samples"
        )
    if fade_frames < 0 or 2 * fade_frames > total_frames:
        raise ValueError(
            f"Fades ({fade_frames} samples each) do not fit in a "
            f"{total_frames}-sample tone"
        )
    if config.REFERENCE_TONE_HZ <= 0.0 or config.REFERENCE_TONE_HZ >= sample_rate / 2:
        raise ValueError(
            f"REFERENCE_TONE_HZ={config.REFERENCE_TONE_HZ} is not below the "
            f"Nyquist frequency for {config.SAMPLE_RATE_HZ} Hz"
        )

    amplitude = 10.0 ** (config.REFERENCE_TONE_AMPLITUDE_DBFS / 20.0)
    if not 0.0 < amplitude <= 1.0:
        raise ValueError(
            f"REFERENCE_TONE_AMPLITUDE_DBFS="
            f"{config.REFERENCE_TONE_AMPLITUDE_DBFS} gives a peak amplitude of "
            f"{amplitude}, which is not inside (0, 1]"
        )

    # Pure sine, no phase offset, no dither, no randomness of any kind.
    # At 1000 Hz / 48 kHz this is exactly 48 samples per cycle, so a 5 s tone
    # is a whole 5000 cycles and starts and ends on a zero crossing.
    t = np.arange(total_frames, dtype=np.float64) / sample_rate
    wave = amplitude * np.sin(2.0 * np.pi * config.REFERENCE_TONE_HZ * t)

    if fade_frames > 0:
        ramp = raised_cosine_ramp(fade_frames)
        wave[:fade_frames] *= ramp
        wave[total_frames - fade_frames :] *= ramp[::-1]

    return wave


def tone_frames() -> npt.NDArray[np.int32]:
    """The tone as the exact integer samples that land in the file.

    soundfile is handed int32 and writes config.SOUNDFILE_SUBTYPE, so the
    values are built in the file's own sample domain and then left-shifted
    into int32: for PCM_24 libsndfile stores the top three bytes, which are
    precisely the 24-bit numbers computed here. Nothing is scaled, dithered or
    rounded again on the way out — verified sample-for-sample after writing.
    """
    bits = bits_per_sample(config.SOUNDFILE_SUBTYPE)
    full_scale = 2 ** (bits - 1)
    shift = 32 - bits

    wave = tone_float()
    quantised = np.rint(wave * full_scale)
    quantised = np.clip(quantised, -full_scale, full_scale - 1)
    mono = (quantised.astype(np.int64) << shift).astype(np.int32)

    if config.CHANNELS == 1:
        return mono
    # A calibration tone is identical in every channel.
    return np.repeat(mono[:, np.newaxis], config.CHANNELS, axis=1)


def render() -> RenderedTone:
    """Render the tone to WAV bytes in memory, and prove the render is
    deterministic before anything is written to disk."""
    frames = tone_frames()
    first = _write_bytes(frames)
    second = _write_bytes(frames)
    if first != second:
        raise RuntimeError(
            "Two renders of the same samples produced different bytes — the "
            "WAV encoder is not deterministic here, so the tone cannot be "
            "guaranteed bit-identical across sessions. Do not ship this file."
        )
    return RenderedTone(frames=frames, wav_bytes=first)


def _write_bytes(frames: npt.NDArray[np.int32]) -> bytes:
    buffer = io.BytesIO()
    sf.write(
        buffer,
        frames,
        config.SAMPLE_RATE_HZ,
        subtype=config.SOUNDFILE_SUBTYPE,
        format=SOUNDFILE_FORMAT,
    )
    return buffer.getvalue()


# --- Verification and reporting -----------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_on_disk(path: Path, rendered: RenderedTone) -> None:
    """Confirm the file on disk is byte-for-byte the render, and that the PCM
    it holds decodes back to exactly the samples that were computed."""
    on_disk = path.read_bytes()
    if on_disk != rendered.wav_bytes:
        raise RuntimeError(
            f"{path} does not match what was just written to it "
            f"(disk {sha256_hex(on_disk)} vs render "
            f"{sha256_hex(rendered.wav_bytes)})"
        )

    decoded, sample_rate = sf.read(path, dtype="int32")
    if sample_rate != config.SAMPLE_RATE_HZ:
        raise RuntimeError(
            f"{path} reads back at {sample_rate} Hz, expected "
            f"{config.SAMPLE_RATE_HZ} Hz"
        )
    if not np.array_equal(decoded, rendered.frames):
        raise RuntimeError(
            f"{path} does not decode to the samples that were generated — the "
            "PCM write was not lossless"
        )


def report(path: Path, rendered: RenderedTone) -> None:
    """Print what was produced. The sha256 is the number the operator writes
    into the mission log; everything else is a sanity check."""
    info = sf.info(path)
    bits = bits_per_sample(config.SOUNDFILE_SUBTYPE)
    full_scale = float(2 ** (bits - 1))

    # Measured from the samples actually in the file, not from the constants.
    # frames is int32-scaled (the subtype's bits sit in the high bytes), so
    # int32 full scale is the correct normaliser here.
    samples = rendered.frames.astype(np.float64) / float(2**31)
    peak = float(np.max(np.abs(samples)))
    rms = float(np.sqrt(np.mean(np.square(samples))))
    peak_dbfs = 20.0 * np.log10(peak) if peak > 0.0 else float("-inf")
    rms_dbfs = 20.0 * np.log10(rms) if rms > 0.0 else float("-inf")

    print(f"  path         : {path}")
    print(f"  frames       : {info.frames}")
    print(f"  duration     : {info.duration:.3f} s")
    print(f"  sample rate  : {info.samplerate} Hz")
    print(f"  channels     : {info.channels}")
    print(f"  format       : {info.format} / {info.subtype} ({bits}-bit)")
    print(f"  size         : {path.stat().st_size} bytes")
    print(f"  sha256       : {sha256_hex(rendered.wav_bytes)}")
    print(
        f"  tone         : {config.REFERENCE_TONE_HZ:g} Hz sine, "
        f"{config.REFERENCE_TONE_FADE_S:g} s raised-cosine fade in/out"
    )
    print(
        f"  amplitude    : peak {peak_dbfs:.2f} dBFS "
        f"(nominal {config.REFERENCE_TONE_AMPLITUDE_DBFS:g}), "
        f"rms {rms_dbfs:.2f} dBFS over the whole file"
    )
    peak_sample = int(np.max(np.abs(rendered.frames.astype(np.int64)))) >> (32 - bits)
    print(
        f"  quantisation : full scale = {full_scale:.0f}, "
        f"peak sample = {peak_sample}"
    )
    print("  determinism  : verified - two independent renders were identical")


# --- Entry point ---------------------------------------------------------


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="make_reference_tone",
        description=(
            "Generate the vendored task-2 reference tone from capture/config.py. "
            "Generated once and committed; see tools/README.md before rerunning."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Verify the committed file matches what would be generated now. "
            "Writes nothing. Exit 1 on mismatch or if the file is missing."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite an existing file that differs. Doing this mid-mission "
            "invalidates week-scale drift comparison."
        ),
    )
    args = parser.parse_args(argv)
    if args.check and args.force:
        parser.error("--check and --force are contradictory")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    path = config.REFERENCE_TONE_WAV
    rendered = render()

    if path.exists():
        existing = path.read_bytes()
        if existing == rendered.wav_bytes:
            print("Reference tone is already correct - nothing written.")
            print()
            report(path, rendered)
            return 0

        print("MISMATCH: the committed reference tone is NOT what the current")
        print("capture/config.py would generate.")
        print(f"  on disk      : {sha256_hex(existing)}")
        print(f"  would write  : {sha256_hex(rendered.wav_bytes)}")
        if args.check:
            print("Check failed. Nothing was written.")
            return 1
        if not args.force:
            print()
            print("Refusing to overwrite. Sessions already recorded were")
            print("calibrated against the file on disk; replacing it breaks")
            print("week-scale drift comparison. Pass --force only if you are")
            print("certain no mission data depends on the existing tone.")
            return 1
        print("--force given: overwriting.")
    elif args.check:
        print(f"MISSING: {path} does not exist. Check failed.")
        return 1

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(rendered.wav_bytes)
    verify_on_disk(path, rendered)
    print("Reference tone written and verified.\n")
    report(path, rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
