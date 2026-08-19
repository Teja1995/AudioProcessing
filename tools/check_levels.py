"""Set the microphone gain once, before the mission, and prove it is right.

CLAUDE.md fixes the gain physically: it is set once on the microphone, taped
and photographed, and the app never touches it. That makes the moment of
setting it the only chance to get it right, so this tool exists for exactly
that moment. It changes nothing — it only measures and tells you.

Run it from the project root:

    python -m tools.check_levels            # use the selected device
    python -m tools.check_levels --list     # show selectable devices
    python -m tools.check_levels --device 30

Two phases:

1. **Room floor** — say nothing. Characterises the noise the room and the
   microphone contribute, which is the same measurement task 1 makes every
   session.
2. **Loudest task** — sustain /a/ as loudly as the protocol ever asks
   (maximum phonation time is the loudest thing the mission records). If the
   gain survives this without clipping, nothing else will clip either.

The verdict is about PEAK level, not loudness: peaks too low waste the ADC's
range, peaks too high clip, and clipping is unrecoverable. Aim between
config.TARGET_PEAK_DBFS_MIN and _MAX, leaving headroom for the one
participant who is louder than everyone else.
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import sounddevice as sd

from capture import config
from capture.audio import selection

_FULL_SCALE = float(2**31)  # int32 capture


def dbfs(value: float) -> float:
    """Amplitude (int32 units) as dBFS. Silence reports -120, not -inf."""
    return 20.0 * np.log10(max(abs(value), 1.0) / _FULL_SCALE)


def record(device_index: int, seconds: float) -> np.ndarray:
    return sd.rec(
        int(seconds * config.SAMPLE_RATE_HZ),
        samplerate=config.SAMPLE_RATE_HZ,
        channels=config.CHANNELS,
        dtype=config.STREAM_DTYPE,
        device=device_index,
        blocking=True,
    ).reshape(-1)


def measure(samples: np.ndarray) -> tuple[float, float, int | None]:
    """Peak dBFS, RMS dBFS, and the real bit depth of the samples."""
    as_float = samples.astype(np.float64)
    peak = dbfs(float(np.abs(as_float).max()))
    rms = dbfs(float(np.sqrt((as_float**2).mean())))

    wide = samples.astype(np.int64)
    nonzero = wide[wide != 0]
    if nonzero.size == 0:
        return peak, rms, None
    lowest_set = np.bitwise_and(nonzero, -nonzero)
    bits = 32 - (int(np.min(lowest_set)).bit_length() - 1)
    return peak, rms, bits


def show_devices() -> None:
    for device in selection.list_input_devices():
        mark = "*" if device.is_selected else (">" if device.is_os_default else " ")
        label = (
            "RECOMMENDED"
            if device.recommended
            else ("usable" if device.supports_capture else "UNUSABLE")
        )
        print(f"{mark}[{device.index:3d}] {label:11s} {device.name[:38]:38s} {device.host_api}")
        for warning in device.warnings:
            print(f"          ! {warning}")
    print("\n  * = currently selected    > = Windows default")


def verdict(peak: float, floor_rms: float, bits: int | None) -> int:
    """Print the judgement. Returns a process exit code."""
    low, high = config.TARGET_PEAK_DBFS_MIN, config.TARGET_PEAK_DBFS_MAX
    print()
    print("=" * 68)
    problems = 0

    if peak >= -0.1:
        print("  CLIPPED. The loudest task hit full scale and is unrecoverable.")
        print("  Turn the microphone gain DOWN and run this again.")
        problems += 1
    elif peak > high:
        print(f"  TOO HOT: peaks reached {peak:.1f} dBFS (target {low:.0f} to {high:.0f}).")
        print("  Turn the gain DOWN a little and run this again — a louder")
        print("  participant than this one would clip.")
        problems += 1
    elif peak < low:
        print(f"  TOO QUIET: peaks only reached {peak:.1f} dBFS (target {low:.0f} to {high:.0f}).")
        print("  Turn the gain UP a little and run this again.")
        problems += 1
    else:
        print(f"  GAIN IS GOOD: peaks reached {peak:.1f} dBFS, inside the")
        print(f"  {low:.0f} to {high:.0f} dBFS target with headroom to spare.")

    headroom = peak - floor_rms
    print(f"  Signal sits {headroom:.0f} dB above the room floor.")
    if headroom < 40:
        print("  That is a poor margin. Quieten the room, move the microphone")
        print("  closer, or check nothing is humming nearby.")
        problems += 1

    if bits is not None and bits < 24:
        print()
        print(f"  NOTE: this microphone delivers {bits} bits of real resolution.")
        print("  Files are still written as 24-bit, with the unused low bits")
        print("  zero. Nothing is lost that the hardware ever captured, but")
        print(f"  the dataset's true depth is {bits}-bit — record that fact.")

    print("=" * 68)
    if problems == 0:
        print("\n  Now TAPE the gain knob and PHOTOGRAPH it. From here on the")
        print("  gain must never move — the app will never touch it, and a")
        print("  mid-mission change would break comparison across the week.")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list input devices and exit")
    parser.add_argument("--device", type=int, default=None, help="PortAudio device index")
    parser.add_argument("--floor-seconds", type=float, default=3.0)
    parser.add_argument("--loud-seconds", type=float, default=5.0)
    args = parser.parse_args()

    if args.list:
        show_devices()
        return 0

    if args.device is not None:
        index, name = args.device, f"device {args.device}"
    else:
        device = selection.resolve_capture_device()
        index, name = device.index, device.name

    print(f"Microphone: {name}  (index {index})")
    print(f"Format: {config.SAMPLE_RATE_HZ} Hz, {config.CHANNELS} channel\n")

    input(f"1/2  ROOM FLOOR — say nothing for {args.floor_seconds:.0f} s. Enter to start: ")
    floor_peak, floor_rms, _ = measure(record(index, args.floor_seconds))
    print(f"     room floor: {floor_rms:.1f} dBFS rms, peaks {floor_peak:.1f} dBFS")

    print()
    print(f"2/2  LOUDEST TASK — sustain /a/ AS LOUD as the protocol ever asks,")
    print(f"     for {args.loud_seconds:.0f} s, at the real mouth-to-mic distance.")
    input("     Enter to start: ")
    loud_peak, loud_rms, bits = measure(record(index, args.loud_seconds))
    print(f"     loud take: peaks {loud_peak:.1f} dBFS, {loud_rms:.1f} dBFS rms")

    return verdict(loud_peak, floor_rms, bits)


if __name__ == "__main__":
    sys.exit(main())
