"""Every configurable constant in one place, per CLAUDE.md "Conventions".

Sample rate, bit depth, task durations, task order, QC thresholds, paths,
server binding — if it is tunable, it lives here and nowhere else. The task
battery is defined here as plain data; `capture.domain.tasks` parses and
validates it into typed specs at import time.

DO NOT reorder TASK_BATTERY without being asked: maximal-effort tasks go
last so they do not fatigue the voice before the sensitive measures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

# --- Audio format — CLAUDE.md "Hard audio requirements", non-negotiable ---

SAMPLE_RATE_HZ: Final = 48_000
CHANNELS: Final = 1  # mono
SOUNDFILE_SUBTYPE: Final = "PCM_24"  # 24-bit uncompressed PCM WAV, always
# 24-bit has no native numpy dtype; the plan is to capture int32 and let
# libsndfile keep the significant 3 bytes when writing PCM_24. This MUST be
# confirmed lossless on the actual habitat audio interface before any other
# audio work — the de-risk spike in ARCHITECTURE.md §8.
STREAM_DTYPE: Final = "int32"
BLOCKSIZE_FRAMES: Final = 1024

# --- Recording pipeline (ARCHITECTURE.md §2) ---

# Bounded queue between the PortAudio callback and the writer thread. A full
# queue aborts the take through the session-fatal error path — frames are
# never silently dropped.
QUEUE_MAX_BLOCKS: Final = 256
METER_UPDATE_HZ: Final = 15

# --- Server — loopback only, never 0.0.0.0 (GDPR: no audio leaves laptop) ---

HOST: Final = "127.0.0.1"
PORT: Final = 8765  # arbitrary, no significance (ARCHITECTURE.md §16)

# --- Paths ---

PACKAGE_DIR: Final = Path(__file__).resolve().parent
STATIC_DIR: Final = PACKAGE_DIR / "static"
DATA_DIR: Final = PACKAGE_DIR.parent / "data"
CONSENT_DIR: Final = DATA_DIR / "consent"
MASTER_LOG_PATH: Final = DATA_DIR / "master_log.csv"
PARTICIPANTS_PATH: Final = DATA_DIR / "participants.json"  # pseudonyms + passages
# Which microphone the operator chose. Stored by name + host API, never by
# index: PortAudio renumbers devices whenever anything is plugged in.
SELECTED_DEVICE_PATH: Final = DATA_DIR / "selected_input_device.json"
WITHDRAWAL_LOG_PATH: Final = DATA_DIR / "withdrawals.csv"  # tombstones, §11
PARTIAL_SUFFIX: Final = ".partial"  # in-progress takes; renamed on clean completion
REFERENCE_TONE_WAV: Final = STATIC_DIR / "audio" / "reference_tone.wav"

# The name<->pseudonym key file lives OUTSIDE data/ and outside this repo —
# operator-set, never part of the USB export (ARCHITECTURE.md §11). This is
# only a default; the operator overrides it.
PSEUDONYM_KEY_DEFAULT: Final = Path.home() / "space_ready_pseudonym_key.json"

# --- Reference tone (task 2), vendored asset ---
# 1 kHz is the conventional calibration reference. Amplitude is well below
# full scale so the RECORDED tone has headroom regardless of speaker volume.
REFERENCE_TONE_HZ: Final = 1000.0
REFERENCE_TONE_DURATION_S: Final = 5.0
REFERENCE_TONE_AMPLITUDE_DBFS: Final = -20.0
REFERENCE_TONE_FADE_S: Final = 0.05  # click-free start/end

# --- UI ---

METER_FLOOR_DBFS: Final = -60.0  # bottom of the level meter's range

# --- Consent (GDPR) ---

CONSENT_VERSION: Final = "1.0"
CONSENT_POINTS: Final = (
    "What is recorded: your voice, doing short speech tasks, three times a day.",
    "Why: to test whether voice measures track hydration during the mission.",
    "How long it is kept: until the study analysis is finished, then deleted.",
    "Who sees it: the research team only. Recordings stay on this laptop.",
    "Your right to withdraw: you can stop at any time and have all of your "
    "recordings deleted, without giving a reason.",
)

# --- Mission shape ---

SESSIONS_PER_DAY: Final = 3
MISSION_DAYS: Final = 7
EXPECTED_SESSIONS_PER_PARTICIPANT: Final = SESSIONS_PER_DAY * MISSION_DAYS

# Safety backstop on manual-stop tasks against a forgotten stop click.
# NOT in CLAUDE.md — flagged addition, see ARCHITECTURE.md §7. Should never
# bind in practice.
SAFETY_CAP_S: Final = 120.0

# --- Immediate QC thresholds (ARCHITECTURE.md §9) ---

QC_CLIP_LEVEL: Final = 0.999  # fraction of full scale counted as clipped
QC_CLIP_CONSECUTIVE: Final = 4  # this many consecutive clipped samples => warn
QC_RMS_BAND_DB: Final = 9.0  # warn if take RMS strays this far from the participant's running mean
# Absolute fallback only, used until a session has recorded its room-silence
# take. Measured against a Blue Yeti this sits barely 5 dB above the room
# floor (~-55 dBFS RMS), which is too tight to separate genuine soft
# phonation from noise — hence the relative rule below.
QC_VOICING_FLOOR_DBFS: Final = -50.0  # whole-take RMS below this => "no voicing detected"

# Preferred rule: voicing means "meaningfully above THIS session's measured
# room floor". Task 1 exists precisely to characterise that floor, so every
# later take is judged against the room and microphone actually in use,
# instead of a constant that only suits one gain setting.
QC_VOICING_MARGIN_DB: Final = 6.0

# Gain staging targets for tools/check_levels.py. The gain is set ONCE on the
# microphone, taped and photographed; this only tells the operator when it is
# right. Peaks below the floor waste the ADC's range, above the ceiling risk
# clipping the loudest task (maximum phonation time).
TARGET_PEAK_DBFS_MIN: Final = -18.0
TARGET_PEAK_DBFS_MAX: Final = -6.0
QC_SHORT_FRACTION: Final = 0.6  # take shorter than this fraction of target => warn

# --- master_log.csv schema — one row per completed task, appended immediately ---

MASTER_LOG_COLUMNS: Final = (
    "participant",
    "session_id",
    "session_number",
    "task_number",
    "task_key",
    "stem",
    "take",
    "redo",
    "filename",
    "utc_operator_entered",
    "device_clock",
    "monotonic_offset_s",
    "duration_s",
    "qc_status",
    "note",
)

# --- Task battery — order matters, do not reorder without being asked ---
#
# Fields: see capture.domain.tasks.TaskSpec. "is_vowel" tasks must never have
# a spoken demo (it would anchor the participant's pitch, and fundamental
# frequency is one of the measures) — validated at import in domain.tasks.
# target_s = None means max-effort / guidance-only: no ceiling, QC floor only.
# take_suffix=False mirrors CLAUDE.md's file layout, where tasks 1-2 are
# named without "_takeN".

TASK_BATTERY: Final = (
    {
        "number": 1,
        "key": "silence",
        "title": "Room silence",
        "instruction": "Please stay quiet. We record the room for 3 seconds.",
        "stems": ("silence",),
        "takes_per_stem": 1,
        "stop": "auto",
        "target_s": 3.0,
        "qc_floor_s": 2.5,
        "take_suffix": False,
        "spoken_demo": False,
        "is_vowel": False,
        "borg": False,
    },
    {
        "number": 2,
        "key": "reference_tone",
        "title": "Reference tone",
        "instruction": "A tone will play from the speaker. Stay quiet.",
        "stems": ("reference_tone",),
        "takes_per_stem": 1,
        "stop": "auto",  # auto-stop = tone length, read from the tone file
        "target_s": None,
        "qc_floor_s": 1.0,
        "take_suffix": False,
        "spoken_demo": False,
        "is_vowel": False,
        "borg": False,
    },
    {
        "number": 3,
        "key": "sustained_a",
        "title": "Sustained /a/",
        "instruction": "Say 'aaaa' for about 5 seconds. Comfortable pitch and loudness.",
        "stems": ("sustained_a",),
        "takes_per_stem": 3,
        "stop": "manual",
        "target_s": 5.0,
        "qc_floor_s": 3.0,
        "take_suffix": True,
        "spoken_demo": False,  # vowel — no demo, ever
        "is_vowel": True,
        "borg": True,
    },
    {
        "number": 4,
        "key": "sustained_i",
        "title": "Sustained /i/",
        "instruction": "Say 'iiii' for about 5 seconds. Comfortable pitch and loudness.",
        "stems": ("sustained_i",),
        "takes_per_stem": 1,
        "stop": "manual",
        "target_s": 5.0,
        "qc_floor_s": 3.0,
        "take_suffix": True,
        "spoken_demo": False,  # vowel — no demo, ever
        "is_vowel": True,
        "borg": True,
    },
    {
        "number": 5,
        "key": "soft_pa",
        "title": "Soft /pa/ repetitions",
        "instruction": "Repeat 'pa pa pa ...' as SOFTLY as you can. Softest possible.",
        "stems": ("soft_pa",),
        "takes_per_stem": 1,
        "stop": "manual",
        "target_s": None,  # guidance only
        "qc_floor_s": 2.0,
        "take_suffix": True,
        "spoken_demo": True,
        "is_vowel": False,
        "borg": True,
    },
    {
        "number": 6,
        "key": "connected_speech",
        "title": "Connected speech",
        "instruction": "Read your passage in your own language, as usual. About 30 seconds.",
        "stems": ("connected_speech",),
        "takes_per_stem": 1,
        "stop": "manual",
        "target_s": 30.0,
        "qc_floor_s": 15.0,
        "take_suffix": True,
        "spoken_demo": False,  # participant reads their fixed passage
        "is_vowel": False,
        "borg": True,
    },
    {
        "number": 7,
        "key": "pataka",
        "title": "/pa-ta-ka/",
        "instruction": "Repeat 'pa-ta-ka' as fast and evenly as you can, about 8 seconds.",
        "stems": ("pataka",),
        "takes_per_stem": 1,
        "stop": "manual",
        "target_s": 8.0,
        "qc_floor_s": 4.0,
        "take_suffix": True,
        "spoken_demo": True,
        "is_vowel": False,
        "borg": True,
    },
    {
        "number": 8,
        "key": "s_z",
        "title": "Sustained /s/ then /z/",
        "instruction": "Say 'ssss' as long as you can. Then 'zzzz' as long as you can.",
        "stems": ("sustained_s", "sustained_z"),
        "takes_per_stem": 1,
        "stop": "manual",
        "target_s": None,  # maximum duration — no ceiling
        "qc_floor_s": 3.0,
        "take_suffix": True,
        "spoken_demo": True,
        "is_vowel": False,
        "borg": True,
    },
    {
        "number": 9,
        "key": "mpt",
        "title": "Maximum phonation time",
        "instruction": "Take a deep breath and say 'aaaa' for as LONG as you can.",
        "stems": ("mpt",),
        "takes_per_stem": 2,
        "stop": "manual",
        "target_s": None,  # maximum duration — no ceiling
        "qc_floor_s": 3.0,
        "take_suffix": True,
        "spoken_demo": False,  # vowel — no demo, ever
        "is_vowel": True,
        "borg": True,
    },
)
