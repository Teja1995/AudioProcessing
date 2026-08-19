# tools/

One-off, build-time utilities. **Nothing here is imported by the running
application** — `python -m capture` never touches this directory, and the
mission laptop never needs to run any of it. These scripts produce vendored
assets that are generated once, committed to the repository, and then left
alone.

Contents:

| Script | Produces |
|---|---|
| `make_reference_tone.py` | `capture/static/audio/reference_tone.wav` — the task 2 calibration tone |

---

## make_reference_tone.py

Generates the fixed calibration tone that task 2 plays through the speaker
while the microphone records it (CLAUDE.md, "Task battery" item 2;
ARCHITECTURE.md §8).

Every number that shapes the tone is read from `capture/config.py`; the
script duplicates none of them.

| Constant | Value | Effect on the tone |
|---|---|---|
| `REFERENCE_TONE_HZ` | `1000.0` | pure sine at 1 kHz |
| `REFERENCE_TONE_DURATION_S` | `5.0` | 240 000 frames |
| `REFERENCE_TONE_AMPLITUDE_DBFS` | `-20.0` | peak amplitude 0.1 of full scale |
| `REFERENCE_TONE_FADE_S` | `0.05` | raised-cosine fade in and out |
| `SAMPLE_RATE_HZ` | `48000` | |
| `CHANNELS` | `1` | mono |
| `SOUNDFILE_SUBTYPE` | `PCM_24` | 24-bit uncompressed PCM |

The fades are raised-cosine rather than linear or absent because a step (or
even a slope discontinuity) at the start or end of playback produces a
broadband click. That click would be picked up by the microphone and land
inside the recorded calibration, contaminating the exact measurement the task
exists to make.

### The committed asset

```
capture/static/audio/reference_tone.wav
  WAV / PCM_24, 48000 Hz, mono, 240000 frames, 5.000 s
  720044 bytes (44-byte canonical RIFF header + 240000 x 3 bytes of PCM)
  peak -20.00 dBFS, whole-file RMS -23.06 dBFS
  sha256  0dda9d84be245f4cfc3ea1ce6cf850d6e9e699f8cf15a71c7567c0169c3d349f
```

Record that sha256 in the mission log. It is the fingerprint that proves,
after the fact, that every session was calibrated against the same signal.
The script prints it on every run.

The output is deterministic: no timestamp, no randomness, no dither, and no
machine-dependent step. Two runs — in the same process or in different
processes, before or after deleting the file — produce byte-identical output.
The script re-renders the tone twice and compares the bytes on every run
before it writes anything, and refuses to proceed if they ever differ.

### Do not regenerate it mid-mission

Task 2 detects gain drift and speaker/microphone placement drift across the
seven mission days. It works by holding the **played** signal constant and
letting the **recorded** signal vary, so that any difference between day 1
and day 7 is attributable to the equipment or the room rather than to the
stimulus.

Replacing the tone partway through changes the thing being held constant.
Every cross-session comparison spanning the change becomes meaningless, and
it cannot be repaired afterwards — the sessions recorded before the change
cannot be re-run. There is one chance to collect this dataset.

**So: generate once, commit, and do not touch it again for the duration of
the mission.** If a constant in `config.py` genuinely has to change, that is
a decision to restart the calibration series, not a routine edit.

The script defends this itself. If the file on disk differs from what the
current config would produce, it refuses to write, prints both hashes, and
exits non-zero. Overriding that takes an explicit `--force`.

### Usage

Run from the project root:

```
python tools/make_reference_tone.py            # write it if it is absent
python tools/make_reference_tone.py --check    # verify only; writes nothing
python tools/make_reference_tone.py --force    # deliberate regeneration
```

`python -m tools.make_reference_tone` works too, with the same flags.

Re-running the plain command is safe and idempotent: if the committed file
already matches, nothing is written and the file is left untouched, mtime and
all.

`--check` is the pre-flight integrity check — run it before travel. It exits
`1` if the asset is missing, corrupted (a bad copy, bit rot), or out of step
with `config.py`, and `0` with the full report if all is well. It never
writes.
