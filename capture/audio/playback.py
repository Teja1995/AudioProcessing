"""Playback: reference tone (task 2) and spoken demos for consonant tasks.

The reference tone plays through an OutputStream while the session's
already-open InputStream records - two independent streams, no loopback
routing. The tone is a vendored WAV, bit-identical every session; that is
what makes week-scale drift comparison meaningful (ARCHITECTURE.md section 8).

Demo clips exist ONLY for consonant tasks. No vowel demo asset is shipped
at all, so pitch anchoring is structurally impossible - domain.tasks
validates the same rule on the config side.

Nothing here touches the captured signal: this module only reads vendored
files and sends them to the output device.
"""

from __future__ import annotations

from pathlib import Path

import sounddevice as sd
import soundfile as sf

from capture import config
from capture.domain.tasks import TaskSpec


def demo_clip_path(task: TaskSpec) -> Path:
    """Where the vendored demo WAV for this task lives. Raises for tasks
    that must not have one - callers cannot even build the path."""
    if not task.spoken_demo:
        raise ValueError(f"Task {task.key} has no spoken demo by design")
    return config.STATIC_DIR / "audio" / f"demo_{task.key}.wav"


def _require_file(path: Path, what: str) -> None:
    """A missing vendored asset must be loud, never a silent no-op.

    Task 2 exists to detect gain and placement drift across the week; a
    reference tone that quietly failed to play would leave a file that looks
    like a valid take and is not one.
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"{what} is missing: {path}. Nothing was played. Restore the "
            "vendored audio asset before recording this task."
        )


def play_wav(path: Path) -> None:
    """Play a vendored WAV through the operator's chosen speaker, blocking.

    Plays at the file's own sample rate - no resampling, no gain, no
    processing of any kind. Called from asyncio.to_thread, so blocking here
    is intended.

    Uses an explicit, scoped OutputStream rather than ``sd.play()``. That
    convenience keeps a MODULE-LEVEL stream alive between calls and tears the
    previous one down on the next call, so a tone played from the start
    screen was still holding global state when task 2 tried to play during a
    live session. The result was a PortAudio failure that named the CAPTURE
    format ("IsFormatSupported(capture) failed") while opening an output --
    baffling to read, and it killed a session in the field. A stream created
    and closed inside this function shares nothing with anything else.

    PortAudio errors propagate unchanged: sounddevice's own message names the
    real cause, and SessionService converts them into a recoverable
    PlaybackError so a speaker problem never ends a session.
    """
    _require_file(path, "Playback asset")
    # The operator's chosen speaker. Windows adopts a USB microphone's own
    # headphone jack as the default output, which would play task 2's
    # calibration tone into headphones nobody is wearing while the take
    # recorded silence. None means "no choice made, use the OS default".
    from capture.audio.selection import resolve_playback_device

    device = resolve_playback_device()
    # float32 holds a 24-bit sample exactly, so this is a faithful copy of
    # the vendored file, not a re-render of it.
    data, samplerate = sf.read(path, dtype="float32", always_2d=True)
    channels = int(data.shape[1])

    stream = sd.OutputStream(
        device=device,
        samplerate=samplerate,
        channels=channels,
        dtype="float32",
    )
    stream.start()
    try:
        stream.write(data)
    finally:
        # Closed on every path, so a failed or interrupted playback cannot
        # leave a device claimed for the rest of the session.
        try:
            stream.stop()
        finally:
            stream.close()


def reference_tone_duration_s() -> float:
    """Length of the vendored tone - task 2's auto-stop duration.

    Read from the WAV header, never assumed: if the vendored asset is ever
    re-rendered at a different length, the auto-stop follows it.
    """
    path = config.REFERENCE_TONE_WAV
    _require_file(path, "Reference tone")
    info = sf.info(path)
    return float(info.frames) / float(info.samplerate)
