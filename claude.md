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
  - **Measured caveat (2026-08-20).** The mission microphone is a Blue Yeti,
    whose ADC is **16-bit**. Recording through WDM-KS, 100% of samples are
    exact multiples of 2^16, which cannot happen by chance across 96,000
    samples. Files are still written as PCM_24 with the low byte zero, so
    nothing the hardware captured is lost, but the dataset's real resolution
    is 16-bit and no software setting changes that. Every take records its
    measured `effective_bits` in `meta.json`, so the analysis works from the
    truth rather than from the container. **Open question for the study
    team:** accept 16-bit (most published voice-perturbation work uses it),
    or source a 24-bit interface.
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
- **Choose the host API, not just the device.** Measured on this hardware:
  MME and DirectSound accept EVERY sample rate offered (8 kHz to 192 kHz)
  because the Windows mixer resamples to reach them, so their acceptance
  proves nothing and their default is a trap. WASAPI and WDM-KS open the
  hardware pin directly and reject a rate it cannot do, so acceptance there
  is proof the rate is native. The Yeti reports a 44.1 kHz default under
  WDM-KS yet captures at 48 kHz with the ADC's 16-bit pattern perfectly
  intact — which resampling would have destroyed. The app therefore groups
  devices by physical microphone and picks the direct path.
- **Choose the speaker too.** Plugging in a USB microphone makes Windows
  adopt ITS headphone jack as the default output. Task 2's calibration tone
  then plays into headphones nobody is wearing while the take records room
  silence, and still looks like a completed calibration.

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
- Allow the operator to redo any individual take without restarting the
  session — including **on the task screen itself**, straight after the take,
  with playback so the operator or participant can hear it first. Waiting for
  the end-of-session list is too late in practice: the participant may have
  moved or left, and a retake made then is not comparable with the original.
- **Every screen must have an exit.** A screen with no way out is itself a
  way to lose a session. Aborting keeps every completed take and releases the
  microphone.
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
habitat temperature, habitat humidity.

> **Removed 2026-08-20: menstrual cycle phase.** Dropped at the researcher's
> request. It is health data under GDPR and, in a crew of 8–10, potentially
> identifying. Note the cost: fluid retention genuinely varies across the
> cycle, so this removes a covariate a hydration study might otherwise want
> to control for. Reinstating it means re-adding the field to
> `domain/models.py`, `routes/session.py` and the covariates form.

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
- Any take where no voicing was detected. Judged against **this session's own
  measured room floor** (task 1 exists to characterise it), not a fixed
  threshold: the right level depends on the microphone, its gain and the
  room. The floor is a low percentile of short-term RMS, not the whole-take
  mean — a single cough during the silence take inflated the mean by 23 dB in
  testing, which would have reported every later take as silent.
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
- **The data directory must not sit inside a cloud-synced folder.** The
  default lives beside the code, and on the development machine that was
  under `OneDrive\Desktop`, so every take was uploaded automatically. The app
  now detects OneDrive, Dropbox, Google Drive, iCloud and similar and warns
  loudly at startup and on screen; set `SPACE_READY_DATA_DIR` to a path
  outside any sync root.

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
