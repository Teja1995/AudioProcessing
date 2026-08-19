"""Record the spoken examples participants hear before the consonant tasks.

Run once, before the mission:

    python -m tools.record_demos              # record the ones that are missing
    python -m tools.record_demos --force      # re-record everything
    python -m tools.record_demos --task pataka

WHY THESE ARE RECORDED RATHER THAN DOWNLOADED
---------------------------------------------
Stock phonetic audio (Wikimedia's IPA library and similar) contains isolated
phonemes: a single /p/, a single /s/. The battery does not ask for phonemes.
It asks for *repeated soft /pa/ at minimal effort*, for */pa-ta-ka/ as fast
and evenly as possible*, and for */s/ and /z/ sustained to maximum duration.
No stock recording demonstrates rate, effort or duration, and a demo that
models the wrong thing is worse than no demo at all — the participant will
copy whatever they hear. One voice, recorded once and reused all week, is
also the consistent stimulus a study wants.

WHY THERE ARE ONLY THREE
------------------------
Tasks 3, 4 and 9 (/a/, /i/, maximum phonation time) are vowel tasks and get
NO example, ever. Hearing a model would anchor the participant's pitch, and
fundamental frequency is one of the measures this study exists to collect
(CLAUDE.md, task battery). This tool refuses to record them, and the app
ships no asset that would let them be played.

The recorded files are demonstration stimuli, not data: they live in
capture/static/audio/ and are committed with the code, never under data/.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

from capture import config
from capture.audio import selection
from capture.domain.tasks import TASKS

# What the person recording the demo should actually do. These describe the
# TASK, not the phoneme, which is the whole reason the files are recorded.
SCRIPTS: dict[str, tuple[str, float]] = {
    "soft_pa": (
        "Say 'pa pa pa pa pa' as SOFTLY as you possibly can — barely voiced, "
        "the quietest sound that is still a /pa/. About 5 repetitions.\n"
        "     This one matters most: participants copy the EFFORT they hear, "
        "and the task is a surrogate for phonation threshold pressure.",
        6.0,
    ),
    "pataka": (
        "Say 'pa-ta-ka pa-ta-ka pa-ta-ka…' as fast and as EVENLY as you can, "
        "for the full 8 seconds.\n"
        "     Demonstrate the rate you want, not a comfortable one.",
        8.0,
    ),
    "s_z": (
        "Sustain 'sssss' for as long as you comfortably can, take a breath, "
        "then sustain 'zzzzz'.\n"
        "     Demonstrate that this is ONE long breath each, not a short "
        "sample — participants copy the duration they hear.",
        12.0,
    ),
}


def demo_path(task_key: str) -> Path:
    return config.STATIC_DIR / "audio" / f"demo_{task_key}.wav"


def consonant_tasks() -> list[str]:
    """Task keys that may have an example. Vowel tasks are never included."""
    return [task.key for task in TASKS if task.spoken_demo]


def countdown(seconds: int = 3) -> None:
    for remaining in range(seconds, 0, -1):
        print(f"     {remaining}…", end="\r", flush=True)
        time.sleep(1.0)
    print("     RECORDING     ", flush=True)


def record(device_index: int, seconds: float) -> np.ndarray:
    return sd.rec(
        int(seconds * config.SAMPLE_RATE_HZ),
        samplerate=config.SAMPLE_RATE_HZ,
        channels=config.CHANNELS,
        dtype=config.STREAM_DTYPE,
        device=device_index,
        blocking=True,
    ).reshape(-1)


def review(samples: np.ndarray) -> tuple[float, bool]:
    """Peak dBFS and whether anything was actually captured."""
    as_float = samples.astype(np.float64)
    peak_raw = float(np.abs(as_float).max())
    peak = 20.0 * np.log10(max(peak_raw, 1.0) / float(2**31))
    return peak, peak > -45.0


def record_one(task_key: str, device_index: int, device_name: str) -> bool:
    script, seconds = SCRIPTS[task_key]
    path = demo_path(task_key)

    print()
    print("=" * 70)
    print(f"  demo_{task_key}.wav   ({seconds:.0f} seconds)")
    print("=" * 70)
    print(f"     {script}")
    print()
    input("     Press Enter when ready (then wait for the countdown): ")
    countdown()
    samples = record(device_index, seconds)

    peak, has_signal = review(samples)
    print(f"     captured: peak {peak:.1f} dBFS")
    if not has_signal:
        print("     That looks silent. Was the microphone muted?")
    elif peak > -1.0:
        print("     That clipped. Move back from the microphone and redo.")

    answer = input("     Keep it? [y]es / [r]etry / [s]kip: ").strip().lower()
    if answer.startswith("r"):
        return record_one(task_key, device_index, device_name)
    if answer.startswith("s"):
        print("     skipped")
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    # Same format as the study's own recordings, so the browser plays it and
    # nothing needs converting in the habitat.
    sf.write(path, samples, config.SAMPLE_RATE_HZ, subtype=config.SOUNDFILE_SUBTYPE)
    print(f"     saved {path}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Record the spoken task examples.")
    parser.add_argument("--task", action="append", help="only this task key")
    parser.add_argument("--force", action="store_true", help="re-record existing files")
    parser.add_argument("--device", type=int, default=None)
    args = parser.parse_args()

    allowed = consonant_tasks()
    wanted = args.task or allowed
    unknown = [key for key in wanted if key not in allowed]
    if unknown:
        print(f"Not a task that may have an example: {', '.join(unknown)}")
        print(f"Allowed: {', '.join(allowed)}")
        print(
            "Vowel tasks (/a/, /i/, maximum phonation time) get no example: a "
            "model would anchor the participant's pitch, and fundamental "
            "frequency is one of the measures."
        )
        return 2

    if args.device is not None:
        index, name = args.device, f"device {args.device}"
    else:
        device = selection.resolve_capture_device()
        index, name = device.index, device.name

    print(f"Microphone: {name}  (index {index})")
    print(f"Format: {config.SAMPLE_RATE_HZ} Hz, {config.SOUNDFILE_SUBTYPE}, mono")
    print()
    print("Record these in the voice and at the distance the crew will hear.")
    print("Demonstrate the TASK — its effort, rate and duration — not just the sound.")

    written = 0
    for task_key in wanted:
        if demo_path(task_key).exists() and not args.force:
            print(f"\n  demo_{task_key}.wav already exists — skipping (use --force)")
            continue
        if record_one(task_key, index, name):
            written += 1

    print()
    print("=" * 70)
    missing = [key for key in allowed if not demo_path(key).exists()]
    print(f"  {written} example(s) recorded this run.")
    if missing:
        print(f"  Still missing: {', '.join(missing)}")
        print("  Tasks without an example simply show no play control; the")
        print("  operator demonstrates out loud instead. Nothing breaks.")
    else:
        print("  All three consonant examples are in place.")
    print("  Commit them: they are stimuli, not data, and belong with the code.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
