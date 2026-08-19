"""RecordingEngine — owns the one InputStream; arms and disarms takes.

Invariants (ARCHITECTURE.md §2, §8 — non-negotiable):

- The InputStream opens ONCE at session start and stays open for the whole
  session. Device parameters are never touched after open. There is no code
  path here (or anywhere) that sets input gain.
- The exact bytes PortAudio delivers are the bytes soundfile writes. The
  callback copies the buffer (PortAudio reuses it) and enqueues the copy —
  a memcpy is not processing. Metering computes RMS/peak from a read-only
  view; nothing mutates the signal. No normalisation, filtering, trimming.
- The callback never blocks, never raises past its boundary, and never
  drops frames silently: a full queue or a bad status flag aborts the take
  through the session-fatal error path.
- 48 kHz / 24-bit / mono / PCM WAV, from capture.config. The int32-capture ->
  PCM_24-write path must be spike-verified on the habitat interface first.

Spike result (ARCHITECTURE.md §8, run on this laptop against the real
"Digital Microphone (Cirrus Logic)"): int32 capture -> PCM_24 write is
confirmed lossless. libsndfile keeps the top 24 bits of every int32 sample
and discards only the low byte, which the driver leaves zero in every one of
96 256 captured frames — the device delivers 24-bit data left-justified in
int32, so nothing is lost. config.STREAM_DTYPE = "int32" stands.

Threading, in one paragraph, because it is the whole design:

    PortAudio callback thread   copies each block, puts it on a bounded
                                queue, and does nothing else.
    Take writer thread          drains the queue, writes blocks to the
                                TakeWriter, meters them, and owns every
                                finalize/abort. All blocking file I/O.
    Event loop / route threads  call open_session_stream, arm_take,
                                stop_take, close — and wait on events, never
                                on the audio path.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import sounddevice as sd

from capture import config
from capture.errors import DeviceError, SessionFatalError, StorageWriteError
from capture.storage.writer import TakeWriter

# Host APIs that open the hardware pin directly, so a reported default
# rate that differs from ours is not evidence of resampling. Defined in
# capture.audio.selection; imported here to keep one definition.
from capture.audio.selection import (
    DIRECT_HOST_APIS,
    register_stream_closed,
    register_stream_open,
)

log = logging.getLogger("capture.audio.engine")

# --- Module-level constants (deliberately NOT in config.py: these are
# implementation timing details of this file, not mission settings). ---

# Capture dtype full scale, for the meter's dBFS maths only. Integer capture
# only — np.iinfo raises loudly if config.STREAM_DTYPE is ever set to a float
# type, which CLAUDE.md forbids in this path anyway.
_STREAM_DTYPE = np.dtype(config.STREAM_DTYPE)
_FULL_SCALE = float(-np.iinfo(_STREAM_DTYPE).min)

_SILENCE_DBFS = -120.0  # reported for an all-zero block instead of -inf
_WRITER_POLL_S = 0.05  # how often the writer thread checks for stop/finish
_FINALIZE_TIMEOUT_S = 10.0  # bound on stop_take(); a dead thread must not hang the UI
_THREAD_JOIN_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class LevelUpdate:
    """One meter reading, broadcast to the UI over the WebSocket hub."""

    rms_dbfs: float
    peak_dbfs: float
    clipping: bool


@dataclass(frozen=True, slots=True)
class TakeResult:
    """What stop_take() returns once the file is finalized on disk."""

    final_path: Path
    duration_s: float
    frames_written: int


def _dbfs(amplitude: float) -> float:
    """Amplitude already scaled to [-1, 1] -> dBFS. Pure read, no state."""
    if amplitude <= 0.0:
        return _SILENCE_DBFS
    return 20.0 * math.log10(amplitude)


class RecordingEngine:
    """One instance per session. Not thread-safe from multiple routes —
    FastAPI handlers call it from the single event loop only."""

    def __init__(
        self,
        device_index: int,
        on_level: Callable[[LevelUpdate], None],
        on_fatal: Callable[[str], None],
    ) -> None:
        self._device_index = device_index
        self._on_level = on_level  # marshalled to the event loop by the caller
        self._on_fatal = on_fatal
        self._blocks: queue.Queue[np.ndarray] = queue.Queue(
            maxsize=config.QUEUE_MAX_BLOCKS
        )
        self._stream: sd.InputStream | None = None

        # Writer thread and the handshakes the control thread waits on.
        self._writer_thread: threading.Thread | None = None
        self._writer: TakeWriter | None = None
        self._writer_lock = threading.Lock()  # guards self._writer only
        self._stop_thread = threading.Event()
        self._finish_requested = threading.Event()
        self._finish_done = threading.Event()
        self._take_result: TakeResult | None = None
        self._finish_error: SessionFatalError | None = None

        # Cheap flags the PortAudio callback is allowed to read.
        self._take_armed = threading.Event()
        self._fatal = threading.Event()
        self._closing = threading.Event()

        # Meter accumulators, touched by the writer thread only.
        self._meter_sumsq = 0.0
        self._meter_samples = 0
        self._meter_peak = 0.0
        self._meter_last_emit = 0.0

    # --- Session lifetime (build-order step 3) ---------------------------

    def open_session_stream(self) -> None:
        """Open the InputStream once; meter runs from now on, even unarmed,
        so the operator sees clipping before wasting a take."""
        if self._stream is not None:
            raise SessionFatalError(
                "stream_already_open", "open_session_stream() called twice"
            )

        self._stop_thread.clear()
        self._fatal.clear()
        self._closing.clear()
        self._meter_last_emit = time.monotonic()

        thread = threading.Thread(
            target=self._writer_loop, name="take-writer", daemon=True
        )
        thread.start()
        self._writer_thread = thread

        try:
            stream = sd.InputStream(
                device=self._device_index,
                samplerate=config.SAMPLE_RATE_HZ,
                channels=config.CHANNELS,
                dtype=config.STREAM_DTYPE,
                blocksize=config.BLOCKSIZE_FRAMES,
                callback=self._callback,
                finished_callback=self._on_stream_finished,
            )
            stream.start()
        except (sd.PortAudioError, OSError, ValueError) as exc:
            self._stop_thread.set()
            thread.join(timeout=_THREAD_JOIN_TIMEOUT_S)
            self._writer_thread = None
            raise DeviceError(
                "input_device_unavailable",
                f"Could not open the microphone (device index "
                f"{self._device_index}) at {config.SAMPLE_RATE_HZ} Hz / "
                f"{config.CHANNELS} channel / {config.STREAM_DTYPE}: {exc}",
            ) from exc

        # The gain the microphone is set to is the gain we record at. Nothing
        # in this file, or anywhere else, changes it — the guarantee is
        # enforced by omission (ARCHITECTURE.md §2).
        self._stream = stream
        # Block device re-enumeration for as long as this stream lives.
        # Re-enumerating terminates PortAudio, which destroys the stream out
        # from under a take in progress.
        register_stream_open()

        if float(stream.samplerate) != float(config.SAMPLE_RATE_HZ):
            self.close()
            raise DeviceError(
                "sample_rate_mismatch",
                f"Asked for {config.SAMPLE_RATE_HZ} Hz but the stream opened "
                f"at {stream.samplerate} Hz. Recording would not be at the "
                f"rate the protocol requires.",
            )

        log.info(
            "Input stream open: device %d, %.0f Hz, %d ch, dtype %s, blocksize %d",
            self._device_index,
            float(stream.samplerate),
            int(stream.channels),
            stream.dtype,
            int(stream.blocksize),
        )
        self._warn_if_os_will_resample()

    def _warn_if_os_will_resample(self) -> None:
        """Warn when the OS will resample on the way to us.

        Resampling is processing applied to the signal before we ever see it
        (CLAUDE.md, Hard audio requirements). We cannot fix it from here; we
        can refuse to let it pass unnoticed. The fix is in Windows Sound
        settings: set the device's default format to 48000 Hz.

        Whether a default-rate mismatch actually means resampling depends on
        the host API, established by measurement rather than assumption:

        * MME and DirectSound go through the Windows mixer and accept EVERY
          rate offered (8 kHz to 192 kHz), resampling to reach them. Their
          acceptance proves nothing, so a mismatch there is a real warning.
        * WASAPI and WDM-KS open the hardware pin directly and reject a rate
          the pin cannot do. A Blue Yeti reports a 44.1 kHz default under
          WDM-KS yet captures at 48 kHz with its ADC's 16-bit sample pattern
          perfectly intact — which resampling would have destroyed. So on
          these APIs the reported default is not evidence of anything.

        Warning on every session for a path that is provably clean would only
        teach the operator to ignore warnings.
        """
        try:
            info = sd.query_devices(self._device_index)
            host_api = str(sd.query_hostapis(int(info["hostapi"]))["name"])
        except (sd.PortAudioError, ValueError, KeyError):
            log.exception(
                "Could not re-read device %d for the rate check", self._device_index
            )
            return

        device_default = float(info["default_samplerate"])
        if device_default == float(config.SAMPLE_RATE_HZ):
            return
        if host_api in DIRECT_HOST_APIS:
            log.info(
                "Microphone %r reports a %.0f Hz default under %s, but this "
                "host API opens the hardware pin directly and accepted %d Hz, "
                "so the capture is native and nothing is resampling.",
                info["name"],
                device_default,
                host_api,
                config.SAMPLE_RATE_HZ,
            )
            return

        log.warning(
            "Microphone %r is configured at %.0f Hz in the OS and %s routes "
            "through the Windows mixer, so the signal IS being resampled to "
            "reach %d Hz — processing applied before capture. Either set the "
            "device's default format to %d Hz in the OS sound settings, or "
            "select the same microphone under WASAPI or WDM-KS on the start "
            "screen.",
            info["name"],
            device_default,
            host_api,
            config.SAMPLE_RATE_HZ,
            config.SAMPLE_RATE_HZ,
        )

    def close(self) -> None:
        """Stop the stream and the writer thread. Any take still armed is
        aborted, never renamed.

        Best-effort by design: every completed take is already renamed on
        disk and nothing here can undo that, so a failure to release the
        device is logged loudly rather than raised — turning a finished
        session into an error would help nobody.
        """
        self._closing.set()

        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except (sd.PortAudioError, OSError):
                log.exception("Error closing the input stream")
            finally:
                # Released even if closing failed: leaving the interlock set
                # would block every future rescan for the life of the process.
                register_stream_closed()

        # Only after the stream is stopped: no new blocks can arrive now.
        self._abort_current_take()

        self._stop_thread.set()
        thread = self._writer_thread
        self._writer_thread = None
        if thread is not None:
            thread.join(timeout=_THREAD_JOIN_TIMEOUT_S)
            if thread.is_alive():
                log.error(
                    "Take writer thread did not stop within %.0f s",
                    _THREAD_JOIN_TIMEOUT_S,
                )

    # --- Take lifetime (build-order step 3) ------------------------------

    def arm_take(self, final_path: Path) -> None:
        """Attach a TakeWriter at final_path's .partial sibling and start
        persisting blocks. final_path must not exist (storage.paths guards)."""
        if self._stream is None:
            raise SessionFatalError(
                "stream_not_open", "arm_take() called before open_session_stream()"
            )
        if self._fatal.is_set():
            raise SessionFatalError(
                "session_fatal",
                "Recording already failed this session — cannot arm another take",
            )
        if self._take_armed.is_set():
            raise SessionFatalError(
                "take_already_armed",
                f"A take is already recording; cannot arm {final_path.name}",
            )

        writer = TakeWriter(final_path)
        writer.open()  # raises StorageWriteError loudly if the file cannot be made

        # Discard blocks captured while idle so the take starts at the click,
        # not with several seconds of whatever the room was doing before it.
        self._drain_queue()

        self._take_result = None
        self._finish_error = None
        self._finish_done.clear()
        self._finish_requested.clear()

        with self._writer_lock:
            self._writer = writer
        self._take_armed.set()
        log.info("Take armed: %s", final_path.name)

    def stop_take(self) -> TakeResult:
        """Flush, close, rename .partial -> final. Only after a clean rename
        does a file exist at its final name."""
        if self._fatal.is_set():
            # The take has already been aborted by the writer thread and its
            # .partial left on disk. Say why, rather than "nothing was armed".
            raise SessionFatalError(
                "session_fatal",
                "Recording failed during this take; it was not saved. The "
                "partial recording is on disk and nothing was overwritten.",
            )
        if not self._take_armed.is_set():
            raise SessionFatalError(
                "no_take_armed", "stop_take() called with no take recording"
            )

        # The take is over as of now; blocks already queued still belong to it
        # and are written by the writer thread before it finalizes.
        self._take_armed.clear()
        self._take_result = None
        self._finish_error = None
        self._finish_done.clear()
        self._finish_requested.set()

        if not self._finish_done.wait(timeout=_FINALIZE_TIMEOUT_S):
            self._signal_fatal(
                f"The recording did not finish saving within "
                f"{_FINALIZE_TIMEOUT_S:.0f} s. Do not overwrite anything on "
                f"disk; check the in-progress .partial file."
            )
            raise SessionFatalError(
                "take_finalize_timeout",
                f"Take did not finish saving within {_FINALIZE_TIMEOUT_S:.0f} s",
            )

        if self._finish_error is not None:
            raise self._finish_error
        result = self._take_result
        if result is None:
            raise SessionFatalError(
                "take_finalize_failed", "Take finished with no result and no error"
            )
        return result

    def abort_take(self) -> None:
        """Session-fatal path: stop writing, leave/remove the .partial —
        never rename it to a final name."""
        self._abort_current_take()

    # --- PortAudio callback ----------------------------------------------

    def _callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: Any,
        status: sd.CallbackFlags,
    ) -> None:
        """Runs on the PortAudio native thread.

        In order: (1) a status flag during a take means samples were lost —
        signal fatal and return; (2) copy indata, because PortAudio reuses
        the buffer as soon as we return; (3) put the copy on the queue, and
        if the queue is full signal fatal rather than drop frames silently;
        (4) return. No disk I/O, no allocation beyond the one copy, and no
        exception may escape this frame into PortAudio.
        """
        try:
            if status:
                # A status flag means PortAudio lost or mishandled samples.
                # While a take is armed that is a hole in the data and the
                # session cannot continue. While idle nothing is being kept,
                # so a glitch costs nothing but a moment of meter — say so and
                # carry on rather than ending a session over it.
                if self._take_armed.is_set():
                    self._signal_fatal(
                        f"The microphone dropped audio during a take "
                        f"({status}). This take has a gap in it and cannot be "
                        f"trusted. Check the microphone connection."
                    )
                    return
                log.warning("Audio status flag while idle (no take armed): %s", status)

            block = indata.copy()  # PortAudio reuses indata after we return

            try:
                self._blocks.put_nowait(block)
            except queue.Full:
                self._signal_fatal(
                    "The recording buffer filled up — audio is arriving "
                    "faster than it can be written to disk. Frames would be "
                    "lost, so this take has been stopped."
                )
                return
        # Nothing may escape this frame into PortAudio's native thread.
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected error in the audio callback")
            self._signal_fatal(f"Unexpected error while capturing audio: {exc!r}")

    def _on_stream_finished(self) -> None:
        """PortAudio calls this when the stream stops. If we did not ask for
        it, the device went away mid-session — CLAUDE.md acceptance criterion
        2: a clear error, never a crash and never a silent empty file."""
        if self._closing.is_set():
            return
        self._signal_fatal(
            "The microphone stopped unexpectedly — it may have been "
            "unplugged. Recording has stopped; nothing has been overwritten."
        )

    # --- Writer thread ----------------------------------------------------

    def _writer_loop(self) -> None:
        """Drains the queue: writes blocks, meters them, finalizes takes.
        The only thread that performs file I/O for audio."""
        log.info("Take writer thread started")
        try:
            while not self._stop_thread.is_set():
                block = self._next_block()
                if block is not None:
                    self._consume(block)
                if self._fatal.is_set():
                    self._abort_current_take()
                    self._finish_requested.clear()
                    self._finish_done.set()  # never leave stop_take() waiting
                elif self._finish_requested.is_set():
                    self._finish_current_take()
        # A writer thread that dies quietly is the exact failure mode this
        # application must never have, so every escape routes to on_fatal.
        except Exception as exc:  # noqa: BLE001
            log.exception("Take writer thread failed")
            self._abort_current_take()
            self._signal_fatal(f"The recording writer stopped unexpectedly: {exc!r}")
            self._finish_done.set()
        finally:
            log.info("Take writer thread stopped")

    def _next_block(self) -> np.ndarray | None:
        try:
            return self._blocks.get(timeout=_WRITER_POLL_S)
        except queue.Empty:
            return None

    def _consume(self, block: np.ndarray) -> None:
        """Write first, meter second. The buffer handed to the writer is the
        one PortAudio produced; metering only reads a scaled copy of it."""
        with self._writer_lock:
            writer = self._writer
            if writer is not None:
                writer.write(block)
        self._meter(block)

    def _finish_current_take(self) -> None:
        # Everything captured before the operator pressed stop belongs to this
        # take. Drain it before closing the file, or the tail is lost.
        while True:
            block = self._drain_one()
            if block is None:
                break
            self._consume(block)

        with self._writer_lock:
            writer = self._writer
            self._writer = None
        self._finish_requested.clear()

        if writer is None:
            self._finish_error = SessionFatalError(
                "no_take_armed", "Nothing was recording when the take was stopped"
            )
            self._finish_done.set()
            return

        frames = writer.frames_written
        try:
            final_path = writer.finalize()
        except StorageWriteError as exc:
            self._finish_error = exc
            self._finish_done.set()
            self._signal_fatal(exc.message)
            return

        self._take_result = TakeResult(
            final_path=final_path,
            duration_s=frames / config.SAMPLE_RATE_HZ,
            frames_written=frames,
        )
        self._finish_done.set()

    def _abort_current_take(self) -> None:
        """Detach and abort whatever is being written. Never renames."""
        self._take_armed.clear()
        with self._writer_lock:
            writer = self._writer
            self._writer = None
        if writer is not None:
            writer.abort()  # never raises past itself

    def _drain_one(self) -> np.ndarray | None:
        try:
            return self._blocks.get_nowait()
        except queue.Empty:
            return None

    def _drain_queue(self) -> None:
        while self._drain_one() is not None:
            pass

    # --- Metering (reads only; never transforms the signal) ---------------

    def _meter(self, block: np.ndarray) -> None:
        """Accumulate RMS/peak and emit at config.METER_UPDATE_HZ.

        Accumulating between emissions rather than sampling one block in
        three is what makes the clipping indicator trustworthy: a clipped
        block that lands between updates is still reported.
        """
        if block.size == 0:
            return

        # astype() COPIES. The captured buffer is never touched by any of
        # this — it is on its way to disk unmodified.
        unit = block.astype(np.float64) / _FULL_SCALE
        flat = unit.reshape(-1)
        self._meter_sumsq += float(np.dot(flat, flat))
        self._meter_samples += int(flat.size)
        # max/min separately, as ints: abs() on the most negative int32 would
        # overflow back to itself.
        block_peak = max(abs(int(block.max())), abs(int(block.min()))) / _FULL_SCALE
        self._meter_peak = max(self._meter_peak, block_peak)

        now = time.monotonic()
        if now - self._meter_last_emit < 1.0 / config.METER_UPDATE_HZ:
            return
        self._meter_last_emit = now

        rms = math.sqrt(self._meter_sumsq / self._meter_samples)
        peak = self._meter_peak
        self._meter_sumsq = 0.0
        self._meter_samples = 0
        self._meter_peak = 0.0

        update = LevelUpdate(
            rms_dbfs=_dbfs(rms),
            peak_dbfs=_dbfs(peak),
            clipping=peak >= config.QC_CLIP_LEVEL,
        )
        # A failed meter push must never disturb the recording path. Logged
        # with its traceback, so not swallowed — just not fatal.
        try:
            self._on_level(update)
        except Exception:  # noqa: BLE001
            log.exception("Level callback raised; recording continues")

    # --- Fatal error path -------------------------------------------------

    def _signal_fatal(self, message: str) -> None:
        """Callable from any thread, including the PortAudio callback: sets a
        flag and reports. Does no disk I/O — the writer thread sees the flag
        and aborts the take (leaving its .partial) on its own."""
        if self._fatal.is_set():
            return  # the first error is the informative one; do not pile on
        self._fatal.set()
        log.error("Session-fatal recording error: %s", message)
        # Reporting the fault must not mask the fault.
        try:
            self._on_fatal(message)
        except Exception:  # noqa: BLE001
            log.exception("on_fatal callback raised while reporting: %s", message)
