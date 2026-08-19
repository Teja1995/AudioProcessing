"""Input-device discovery and the startup device report.

CLAUDE.md requires, at startup:
- log the OS-reported input device name and volume/gain level (drift anchor),
- verify/warn about OS audio enhancements where readable; otherwise show a
  manual checklist the operator confirms.

v1 decision (ARCHITECTURE.md section 8): no bespoke platform enhancement
reader - ``enhancement_status()`` returns UNKNOWN and the UI shows the manual
checklist, screenshotted from the actual habitat laptop.

READ-ONLY BY CONSTRUCTION. Nothing in this module (or anywhere in this app)
writes a device parameter. The physical gain is set once, taped, and
photographed; there is no setter to misuse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

import sounddevice as sd

from capture import config
from capture.errors import DeviceError

log = logging.getLogger("capture.devices")


class EnhancementStatus(StrEnum):
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"  # -> operator confirms the manual checklist


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    index: int
    name: str
    max_input_channels: int
    default_samplerate: float


# --- Module-level constants (deliberately NOT in config.py) ----------------

# PortAudio's device dictionary carries no gain/volume field on any host API
# we ship against. These hints exist so that if a future PortAudio or host
# API ever does expose one, it gets reported rather than silently ignored.
_GAIN_KEY_HINTS: Final[tuple[str, ...]] = ("gain", "volume", "level")

# Why the OS gain is unreadable here, in the words the log reader will need.
GAIN_UNREADABLE_NOTE: Final = (
    "PortAudio exposes no input volume/gain value, and reading the Windows "
    "endpoint volume would need an extra dependency we may not add. The gain "
    "is anchored physically instead: taped, photographed, and confirmed on "
    "the manual checklist below every session."
)

# Windows 11 moved per-device audio enhancements out of the Windows 10 dialog
# (ARCHITECTURE.md section 8, item 3). These are the steps as Windows 11
# presents them; screenshot them from the ACTUAL habitat laptop before travel
# and correct this list if a screen does not match.
MANUAL_ENHANCEMENT_CHECKLIST: Final[tuple[str, ...]] = (
    "Settings > System > Sound > Input: confirm the device named in this "
    "report is the selected input device.",
    "Click that device to open its properties page.",
    "Set 'Audio enhancements' to OFF on that page.",
    "Settings > System > Sound > More sound settings > Recording tab > the "
    "device > Properties > Advanced: untick 'Enable audio enhancements'.",
    "In that same Properties dialog check every extra tab (Levels, Custom, "
    "Enhancements, Effects): turn OFF noise suppression, echo cancellation, "
    "AGC / automatic level, 'Voice Focus', and beamforming.",
    "Levels tab: photograph the input level. It must read the SAME value "
    "every session. Do not change it - this app never touches it.",
    "Confirm the microphone gain knob is still taped at its marked position, "
    "and photograph it.",
    "If any screen does not match these steps, screenshot what you see and "
    "record it - do not guess.",
)


# --- Device query ----------------------------------------------------------


def _query_default_input_raw() -> dict[str, Any]:
    """PortAudio's raw dict for the default input device.

    Raises DeviceError when there is no usable input device at all - the one
    failure that must never be quiet, because everything after it assumes a
    microphone exists.
    """
    try:
        raw = sd.query_devices(kind="input")
    except (sd.PortAudioError, ValueError) as exc:
        # PortAudioError: no default input device, or the query itself failed.
        # ValueError: the default device is not an input device.
        raise DeviceError(
            "no_input_device",
            "No usable input device. Is the microphone plugged in? "
            f"PortAudio reports: {exc}",
        ) from exc
    if int(raw["max_input_channels"]) < 1:
        raise DeviceError(
            "no_input_device",
            f"Default device {raw['name']!r} has no input channels - the "
            "microphone is not selected as the input device.",
        )
    return dict(raw)


def describe_default_input() -> DeviceInfo:
    """The OS default input device, as PortAudio reports it."""
    raw = _query_default_input_raw()
    return DeviceInfo(
        index=int(raw["index"]),
        name=str(raw["name"]),
        max_input_channels=int(raw["max_input_channels"]),
        default_samplerate=float(raw["default_samplerate"]),
    )


def _host_api_name(raw: dict[str, Any]) -> str:
    """Host API name for a raw device dict (MME / WASAPI / ...)."""
    try:
        return str(sd.query_hostapis(int(raw["hostapi"]))["name"])
    except (sd.PortAudioError, ValueError, KeyError) as exc:
        raise DeviceError(
            "host_api_unreadable",
            f"Could not read the host API for device {raw['name']!r}: {exc}",
        ) from exc


def read_os_input_gain() -> str | None:
    """OS-reported input volume/gain, as an opaque string for the log.

    Read-only by design - there is no setter anywhere in this codebase; the
    physical gain is fixed, taped, and photographed. Returns None when the
    platform offers no readable value (still logged, as "unreadable").

    On Windows, PortAudio does not expose the endpoint volume, and reading it
    would need an extra dependency that CLAUDE.md's dependency rule does not
    permit for this. So in practice this returns None today. It still checks
    PortAudio's device dict for anything gain-shaped, so a host API that does
    report one is picked up instead of ignored.
    """
    raw = _query_default_input_raw()
    gain_fields = {
        key: value
        for key, value in raw.items()
        if any(hint in key.lower() for hint in _GAIN_KEY_HINTS)
    }
    if not gain_fields:
        return None  # unreadable on this platform - see GAIN_UNREADABLE_NOTE
    return ", ".join(f"{key}={value}" for key, value in sorted(gain_fields.items()))


def enhancement_status() -> EnhancementStatus:
    """v1: always UNKNOWN - the UI must show the manual checklist."""
    return EnhancementStatus.UNKNOWN


def startup_report() -> dict[str, object]:
    """Device name + gain + enhancement status, logged before first session.

    This is the drift-detection anchor CLAUDE.md asks for: it lands in the run
    log at every launch, so a device or level change mid-mission is visible
    afterwards. Raises DeviceError when no input device exists - a recording
    tool with no microphone must fail at startup, loudly.
    """
    raw = _query_default_input_raw()
    device = describe_default_input()
    gain = read_os_input_gain()
    status = enhancement_status()

    report: dict[str, object] = {
        "input_device_name": device.name,
        "input_device_index": device.index,
        "host_api": _host_api_name(raw),
        "max_input_channels": device.max_input_channels,
        "device_default_samplerate_hz": device.default_samplerate,
        "capture_sample_rate_hz": config.SAMPLE_RATE_HZ,
        "capture_channels": config.CHANNELS,
        "capture_subtype": config.SOUNDFILE_SUBTYPE,
        "os_gain_reading": gain,
        "os_gain_readable": gain is not None,
        "os_gain_note": GAIN_UNREADABLE_NOTE if gain is None else "",
        "enhancement_status": str(status),
        "manual_checklist": list(MANUAL_ENHANCEMENT_CHECKLIST),
    }

    log.info("Startup device report (gain / enhancement drift anchor):")
    for key, value in report.items():
        if key == "manual_checklist":
            continue
        log.info("    %s: %s", key, value)

    if status is EnhancementStatus.UNKNOWN:
        # CLAUDE.md: "verify and warn ... If they cannot be read, display a
        # checklist the operator confirms manually."
        log.warning(
            "Audio enhancement flags could NOT be read automatically - the "
            "operator must confirm the manual checklist before recording."
        )
    for step in MANUAL_ENHANCEMENT_CHECKLIST:
        log.info("    [ ] %s", step)

    if device.default_samplerate != float(config.SAMPLE_RATE_HZ):
        # Not an error: the stream is opened at config.SAMPLE_RATE_HZ and
        # PortAudio resamples nothing here. Worth seeing in the log anyway.
        log.info(
            "    note: device default rate is %s Hz; capture opens the "
            "stream at %s Hz.",
            device.default_samplerate,
            config.SAMPLE_RATE_HZ,
        )

    return report
