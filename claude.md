# CLAUDE.md — Voice Capture Tool (SPACE READY_4 Analog Mission)

## What this project is

A local, offline recording application used to collect standardised voice samples
from a crew of 8–10 people, three times daily, for seven days, inside an analog
astronaut habitat (AATC, near Kraków, 22–30 August 2026).

The recordings will later be analysed to test whether acoustic measures track
hydration status. **This tool does not perform any analysis.** Its only job is to
capture scientifically valid audio and the metadata needed to interpret it, and
to never lose a sample.

Treat data integrity as the top priority. A session that records slightly wrong
is recoverable; a session that silently fails is not. There is exactly one
chance to collect this dataset.

## Operating context (read this before designing anything)

- Runs on a single laptop, offline. **No internet in the habitat. No CDNs, no
  remote fonts, no telemetry, no cloud calls.** All dependencies vendored.
- Operated by the researcher (Ravi), who is also a participant. Other crew
  members sit at the laptop and follow on-screen prompts.
- The crew is international. Prompts must be language-neutral or English-simple.
  Connected-speech tasks are performed in each participant's own native language.
- **Habitat device clocks are deliberately scrambled** as part of the mission's
  time-perception protocol. The laptop clock cannot be trusted. See "Time" below.
- Sessions happen under time pressure between other mission activities. The
  operator will be tired. The UI must be impossible to get wrong.

## Hard audio requirements

These are non-negotiable. Violating any one of them invalidates the dataset.

- **Capture in Python, not the browser.** `sounddevice` (PortAudio) →
  `soundfile`. The browser's `getUserMedia` applies automatic gain control, echo
  cancellation and noise suppression that cannot be reliably disabled across
  platforms, and `MediaRecorder` defaults to lossy Opus. Both destroy the
  perturbation and cepstral measures this study depends on.
- **48 kHz, 24-bit, mono, uncompressed PCM WAV.** Never MP3, never Opus, never
  float32 output files.
- **No processing of any kind** on the captured signal. No normalisation, no
  filtering, no trimming, no gain applied in software. Write exactly what the
  ADC produced.
- **Fixed input gain.** The gain is set once on the microphone, taped, and
  photographed. The app must never change it. On startup, log the OS-reported
  input device name and volume/gain level so drift is detectable afterwards.
- On startup, **verify and warn** if the OS input device has any enhancement
  flags enabled that can be read (Windows "Audio Enhancements", macOS ambient
  noise reduction). If they cannot be read, display a checklist the operator
  confirms manually.

## Architecture

Python backend + local browser front-end.

- FastAPI serving `127.0.0.1` only.
- WebSocket for live level metering and task state.
- Audio capture entirely in the Python process. The browser is a display and
  input surface — it never touches the microphone.
- Static assets served from disk. No build step. Plain HTML/CSS/JS is fine and
  preferred; do not introduce a bundler or framework unless there is a concrete
  need.
- Single command to launch: `python -m capture` or equivalent. It should open
  the browser itself.

## Time

Habitat clocks are scrambled, so `datetime.now()` is untrustworthy.

- At the start of every session the operator enters the current UTC time from a
  trusted external source (a watch kept on UTC, or a GPS/phone reading if
  available). The app records this alongside the laptop's own clock reading.
- Every filename and metadata record carries **both**: `utc_operator_entered`
  and `device_clock`. Never silently substitute one for the other.
- Store a monotonic elapsed-time counter per session so intra-session task
  ordering is always recoverable even if the entered UTC is wrong.

## Task battery

Order matters. Maximal-effort tasks go last so they do not fatigue the voice
before the sensitive measures. Do not reorder without being asked.

1. **Room silence** — 3 s, no speech. Characterises the noise floor. Mandatory.
2. **Reference tone** — play a fixed calibration tone through a speaker at a
   fixed position and record it. Detects gain or placement drift across the week.
3. **Sustained /a/** — ×3 takes, ~5 s each, comfortable pitch and loudness.
4. **Sustained /i/** — ×1, ~5 s.
5. **Soft /pa/ repetitions** — quiet, minimally effortful repeated /pa/ syllables.
   Surrogate for phonation threshold pressure. Emphasise *softest possible*.
6. **Connected speech** — participant reads or speaks a fixed passage **in their
   own native language**. Same passage every session for that participant.
   ~30 s. Store the passage per participant so it is reused identically.
7. **/pa-ta-ka/** — diadochokinetic, ~8 s, as fast and evenly as possible.
8. **Sustained /s/ then /z/** — for the s/z ratio, one take each, maximum duration.
9. **Maximum phonation time** — sustained /a/ for as long as possible, ×2 takes.

For each task the UI must:
- Show a short written instruction (language-simple, with a diagram where useful).
- Optionally play a spoken demonstration for consonant tasks. **Do not play a
  tonal model for vowel tasks** — it would anchor the participant's pitch, and
  fundamental frequency is one of the measures.
- Show a live input level meter with a clear clipping indicator.
- Allow the operator to redo any individual take without restarting the session.
- Record a Borg CR-10 perceived-effort rating after the phonation tasks.

## Metadata captured per session

Written as a JSON sidecar next to the audio, and appended to a master CSV.

**Session:** participant pseudonym, session number, `utc_operator_entered`,
`device_clock`, input device name, sample rate, bit depth, OS gain reading,
mouth-to-microphone distance in cm, room/location label.

**Reference measures** (entered at the same moment as recording — this
simultaneity matters, they are the criterion the voice data is tested against):
urine specific gravity, urine colour chart value (1–8), body mass, fluid intake
since last session.

**Covariates** (checklist, per session): minutes since last exercise, breathing
route (nasal/oral), caffeine since last session, alcohol, estimated speaking
load since last session, hours slept, upper-respiratory symptoms, medication,
menstrual cycle phase where applicable, habitat temperature, habitat humidity.

Every field must be recordable as "not available" rather than blocking the
session. A missing covariate is a small loss; a refused session is a large one.

## File layout

```
data/
  <participant_pseudonym>/
    <session_id>/
      meta.json
      01_silence.wav
      02_reference_tone.wav
      03_sustained_a_take1.wav
      ...
  master_log.csv
  consent/
    <participant_pseudonym>.json
```

Session IDs must sort chronologically and must not depend on the untrusted
device clock — use a zero-padded incrementing counter plus the entered UTC.

## Data safety

- Write each take to disk **immediately** on completion. Never buffer a whole
  session in memory.
- Append to `master_log.csv` after every task, not at session end.
- Never overwrite an existing file. If a path exists, suffix and warn.
- Provide a one-click "copy everything to USB" that verifies by file count and
  byte size, and reports mismatches loudly.
- On startup, show how many sessions and takes exist so far, so a silent failure
  the previous day is visible.

## Immediate quality control

After each session, without doing real analysis, flag obvious failures:
- Any take that clipped.
- Any take whose RMS is far outside that participant's running range.
- Any take where no voicing was detected (empty or silent recording).
- Any take much shorter than expected for its task.

Show these as a simple pass/warn list so the operator can redo takes on the spot.

## Adherence dashboard

A single screen showing, per participant, which sessions are complete, missing
or flagged, for the whole mission. This is how the operator notices on day 4
that someone has quietly missed six sessions.

## Consent and data protection (GDPR)

Voice recordings of identifiable people are personal data; voiceprints used for
identification are special-category biometric data. This study is conducted in
the EU.

- First-run consent screen per participant, recorded with a timestamp, covering:
  what is recorded, why, how long it is kept, who sees it, and the right to
  withdraw.
- **Pseudonymous participant IDs everywhere.** The name-to-ID mapping is never
  stored in the data directory — it lives in a separate file the operator keeps
  apart.
- A withdrawal function that deletes all of a participant's audio and metadata
  and records that the withdrawal happened.
- No audio leaves the laptop except via the operator's explicit USB copy.

## Non-goals

Do not build any of these unless explicitly asked:
- Acoustic analysis, feature extraction, or anything touching Praat/openSMILE.
- Statistics, plots, or hypothesis testing.
- Cloud sync, accounts, or multi-device support.
- Speech recognition or transcription.
- Anything that modifies the audio signal.

## Conventions

- Python 3.11+. Standard library plus `sounddevice`, `soundfile`, `numpy`,
  `fastapi`, `uvicorn`. Justify any addition.
- Type hints throughout. Keep the audio path free of clever abstraction — it
  should be readable at 3 a.m. by a tired person.
- Fail loudly. No bare `except`. Any swallowed exception in the recording path
  is a bug.
- Every configurable constant (sample rate, task durations, task order) in one
  config module, not scattered.

## Acceptance criteria

The tool is ready when:
1. A full session can be completed by someone who has never seen it, using only
   on-screen instructions.
2. Unplugging the microphone mid-session produces a clear error, not a crash or
   a silent empty file.
3. Recorded WAVs open in Praat at 48 kHz/24-bit with no processing artefacts.
4. Killing the process mid-session leaves all completed takes intact on disk.
5. The whole thing runs with no network connection at all.
