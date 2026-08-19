"""TakeWriter — the only code that writes audio to disk.

Contract (ARCHITECTURE.md §2, the single most important pattern in the app):

- open(): create ``<final>.wav.partial`` via soundfile, mode write,
  SAMPLE_RATE_HZ / CHANNELS / SOUNDFILE_SUBTYPE from config. The final path
  must have been vetted by storage.paths (exists() is a caller bug).
- write(block): append exactly the bytes captured — no gain, no filtering,
  no dtype tricks beyond the spike-verified int32 -> PCM_24 path.
- finalize(): flush, fsync, close, then os.replace the .partial onto the
  final name. Only after this rename does a take "exist". "Completed" and
  "has its final filename" are the same event — that is what makes a
  mid-session kill leave every completed take intact.
- abort(): close and LEAVE the .partial (or delete it) — never, under any
  error, rename it to a final name.

Runs on the dedicated writer thread; owns all blocking file I/O for audio.

Two findings from the de-risk spike (ARCHITECTURE.md §8) are baked in here,
because both are silent traps:

1. ``format="WAV"`` must be passed explicitly. soundfile infers the format
   from the file extension, and our in-progress name ends ``.wav.partial``,
   which it cannot parse. Without it, open() raises TypeError.
2. We own the OS file handle ourselves and hand soundfile a file object,
   rather than letting soundfile open the path. That is the only way to
   fsync the real bytes: libsndfile writes the final RIFF/data sizes when
   *its* handle closes, so an fsync issued before that close would flush a
   header still claiming zero frames. Reopening the path afterwards is not
   an option either — on Windows, ``os.fsync`` on a read-only descriptor
   fails with EBADF (verified). Owning a ``wb+`` handle sidesteps both.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import BinaryIO

import numpy as np
import soundfile as sf

from capture import config
from capture.errors import StorageWriteError
from capture.storage import paths

log = logging.getLogger("capture.storage.writer")

# libsndfile cannot infer a container from the ".wav.partial" name we write
# under, so the format is stated outright. The audio format itself always
# comes from config (48 kHz / mono / PCM_24) — never from a literal here.
_WRITE_FORMAT = "WAV"


class TakeWriter:
    def __init__(self, final_path: Path) -> None:
        self._final_path = final_path
        self._partial_path = paths.partial_path(final_path)
        self._frames_written = 0
        self._raw: BinaryIO | None = None
        self._sound: sf.SoundFile | None = None

    @property
    def frames_written(self) -> int:
        return self._frames_written

    @property
    def final_path(self) -> Path:
        return self._final_path

    @property
    def partial_path(self) -> Path:
        """Where audio is actually landing right now. Usually the plain
        ``<final>.wav.partial``; see open() for when it is not."""
        return self._partial_path

    @property
    def is_open(self) -> bool:
        return self._sound is not None

    def open(self) -> None:
        if self._sound is not None:
            raise StorageWriteError(
                "writer_already_open",
                f"open() called twice for {self._final_path.name}",
            )
        if self._final_path.exists():
            # Never overwrite. The caller (storage.paths.next_free_path) is
            # supposed to have picked a free name; if we got here anyway,
            # stop rather than record over a take that already exists.
            raise StorageWriteError(
                "final_path_exists",
                f"{self._final_path} already exists — refusing to record over it",
            )

        # A leftover .partial is a previous crash's evidence. Do not truncate
        # it: step aside onto a free name and say so loudly. Refusing to
        # record would be the larger loss (CLAUDE.md, Data safety).
        partial = paths.next_free_path(self._partial_path)
        if partial != self._partial_path:
            log.warning(
                "Leftover in-progress file %s kept; recording to %s instead",
                self._partial_path.name,
                partial.name,
            )
        self._partial_path = partial

        try:
            self._final_path.parent.mkdir(parents=True, exist_ok=True)
            raw = self._partial_path.open("wb+")
        except OSError as exc:
            raise StorageWriteError(
                "partial_open_failed",
                f"Could not create {self._partial_path}: {exc}",
            ) from exc

        try:
            sound = sf.SoundFile(
                raw,
                mode="w",
                samplerate=config.SAMPLE_RATE_HZ,
                channels=config.CHANNELS,
                subtype=config.SOUNDFILE_SUBTYPE,
                format=_WRITE_FORMAT,
            )
        except (sf.SoundFileError, OSError, TypeError, ValueError) as exc:
            _close_quietly(raw, self._partial_path)
            raise StorageWriteError(
                "partial_open_failed",
                f"soundfile could not open {self._partial_path.name} as "
                f"{config.SAMPLE_RATE_HZ} Hz / {config.CHANNELS} ch / "
                f"{config.SOUNDFILE_SUBTYPE}: {exc}",
            ) from exc

        self._raw = raw
        self._sound = sound
        log.info(
            "Recording to %s (%d Hz, %d ch, %s)",
            self._partial_path.name,
            config.SAMPLE_RATE_HZ,
            config.CHANNELS,
            config.SOUNDFILE_SUBTYPE,
        )

    def write(self, block: np.ndarray) -> None:
        """Append the block exactly as captured.

        Nothing here scales, normalises, filters, trims or re-types the
        samples. The array handed in is the array PortAudio produced, and
        soundfile writes its significant bits straight out as PCM_24.
        """
        sound = self._sound
        if sound is None:
            raise StorageWriteError(
                "writer_not_open",
                f"write() called before open() for {self._final_path.name}",
            )
        try:
            sound.write(block)
        except (sf.SoundFileError, OSError, ValueError) as exc:
            raise StorageWriteError(
                "take_write_failed",
                f"Could not write audio to {self._partial_path.name} "
                f"(disk full or device lost?): {exc}",
            ) from exc
        self._frames_written += int(block.shape[0])

    def finalize(self) -> Path:
        """Returns the final path the take now lives at."""
        sound = self._sound
        raw = self._raw
        if sound is None or raw is None:
            raise StorageWriteError(
                "writer_not_open",
                f"finalize() called before open() for {self._final_path.name}",
            )
        self._sound = None
        self._raw = None

        try:
            # Order matters, and is not the obvious one. libsndfile writes the
            # real RIFF/data sizes when ITS handle closes, so the header is
            # only correct after sound.close(). We keep our own descriptor open
            # across that close so the fsync covers the finished header as well
            # as the audio. Only then is the file safe to rename.
            sound.flush()
            sound.close()
            raw.flush()
            os.fsync(raw.fileno())
            raw.close()
        except (sf.SoundFileError, OSError, ValueError) as exc:
            _close_quietly(raw, self._partial_path)
            raise StorageWriteError(
                "take_finalize_failed",
                f"Could not flush and close {self._partial_path.name}; the "
                f"recording is left there, unrenamed: {exc}",
            ) from exc

        if self._final_path.exists():
            # os.replace clobbers silently (verified in the spike), so this
            # check is what stands between a redo and a destroyed take.
            raise StorageWriteError(
                "final_path_exists",
                f"{self._final_path} appeared while recording — refusing to "
                f"overwrite it. This take is intact at {self._partial_path}",
            )

        try:
            os.replace(self._partial_path, self._final_path)
        except OSError as exc:
            raise StorageWriteError(
                "take_rename_failed",
                f"Recorded {self._frames_written} frames but could not rename "
                f"{self._partial_path.name} to {self._final_path.name}; the "
                f"audio is intact at {self._partial_path}: {exc}",
            ) from exc

        log.info(
            "Take saved: %s (%d frames)", self._final_path.name, self._frames_written
        )
        return self._final_path

    def abort(self) -> None:
        """Close the handles and LEAVE the .partial where it is.

        A partial recording is evidence — of a mic that came unplugged, of a
        full disk — so it is never renamed to a final name and never deleted
        here. Must never raise past itself: abort() is what runs when things
        have already gone wrong, and a second exception on top of the first
        would hide the reason.
        """
        sound = self._sound
        raw = self._raw
        self._sound = None
        self._raw = None
        if sound is None and raw is None:
            return

        if sound is not None:
            try:
                sound.close()
            except (sf.SoundFileError, OSError, ValueError):
                log.exception("Error closing soundfile handle for %s", self._partial_path)
        if raw is not None:
            _close_quietly(raw, self._partial_path)

        log.error(
            "Take aborted after %d frames. Nothing renamed; the partial "
            "recording is kept at %s",
            self._frames_written,
            self._partial_path,
        )


def _close_quietly(handle: BinaryIO, path: Path) -> None:
    """Release an OS handle on a cleanup path.

    Not a swallowed exception: the failure is logged with its traceback. It
    is merely not re-raised, because every caller is already raising or
    reporting the original, more informative error.
    """
    try:
        handle.close()
    except OSError:
        log.exception("Could not close the file handle for %s", path)
