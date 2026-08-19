"""Input-device discovery and selection — which microphone we record from.

CLAUDE.md fixes the capture format (48 kHz, 24-bit, mono, no processing) but
leaves the device open. A USB microphone will be used in the habitat, so the
operator must be able to see every connected input and choose one.

Two facts make this more than a dropdown:

1. **Device indices are not stable.** PortAudio renumbers devices when
   anything is plugged in or removed. A selection stored as an index would
   silently point at a different microphone after a reboot with the USB mic
   unplugged. The selection is therefore stored as (name, host API) and the
   index is re-resolved at every session start.

2. **Some host APIs resample silently.** MME and DirectSound sit behind the
   Windows mixer: ask them for 48 kHz while the endpoint is configured at
   44.1 kHz and they answer "supported" and quietly resample — processing
   applied to the signal, which this study cannot tolerate. WASAPI and WDM-KS
   refuse the mismatch instead, which is the honest answer we want.
   Every device is probed so the UI can warn before a take exists.

READ-ONLY, like the rest of capture.audio: choosing which device to open is
not the same as changing its settings. Nothing here writes a device
parameter, and there is still no gain setter anywhere in the app.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, Final

import sounddevice as sd

from capture import config
from capture.audio.devices import DeviceInfo
from capture.errors import DeviceError

log = logging.getLogger("capture.selection")

# Host APIs that route through the Windows mixer. They accept a sample rate
# the hardware endpoint is not set to and resample to reach it, without
# reporting that they did.
_MIXER_HOST_APIS: Final[frozenset[str]] = frozenset({"MME", "Windows DirectSound"})

# Host APIs that talk to the endpoint directly, so a rate mismatch surfaces
# as an error rather than hiding behind a resampler.
_DIRECT_HOST_APIS: Final[frozenset[str]] = frozenset(
    {"Windows WASAPI", "Windows WDM-KS"}
)


@dataclass(frozen=True, slots=True)
class InputDevice:
    """One selectable input, with everything the operator needs to judge it."""

    index: int
    name: str
    host_api: str
    max_input_channels: int
    default_samplerate: float
    is_os_default: bool
    is_selected: bool
    # Can PortAudio open it at exactly our capture format?
    supports_capture: bool
    capture_error: str | None
    # True when the endpoint is already at our rate, so nothing resamples.
    rate_is_native: bool
    # Operator-readable reasons this device may harm the recording.
    warnings: tuple[str, ...]

    @property
    def recommended(self) -> bool:
        """Safe to record the study on: right format, nothing in the path."""
        return self.supports_capture and not self.warnings


def _host_api_name(hostapi_index: int) -> str:
    try:
        return str(sd.query_hostapis(int(hostapi_index))["name"])
    except (sd.PortAudioError, ValueError, KeyError):
        return f"host api {hostapi_index}"


def _probe_capture(index: int) -> tuple[bool, str | None]:
    """Will PortAudio open this device at exactly our capture format?"""
    try:
        sd.check_input_settings(
            device=index,
            samplerate=config.SAMPLE_RATE_HZ,
            channels=config.CHANNELS,
            dtype=config.STREAM_DTYPE,
        )
    except (sd.PortAudioError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, None


def _warnings_for(
    host_api: str,
    default_samplerate: float,
    supports_capture: bool,
    capture_error: str | None,
    max_input_channels: int,
) -> tuple[str, ...]:
    """Everything wrong with this device, in words the operator can act on.

    Whether a rate mismatch matters depends on the host API, which was
    established empirically against a Yeti on this machine:

    * MME / DirectSound accept EVERY rate offered — 8 kHz through 192 kHz —
      because the Windows mixer resamples to reach them. Their acceptance
      means nothing, so they are always warned about.
    * WDM-KS and WASAPI open the hardware pin directly. They reject a rate
      the pin cannot do, so acceptance is proof the rate is native. The Yeti
      reports a 44.1 kHz default under WDM-KS yet captures at 48 kHz with the
      ADC's 16-bit sample pattern perfectly intact — which resampling would
      have destroyed. A default-rate mismatch is therefore NOT evidence of
      resampling on these APIs.
    """
    found: list[str] = []
    rate_matches = default_samplerate == float(config.SAMPLE_RATE_HZ)

    if not supports_capture:
        found.append(
            f"Cannot record at {config.SAMPLE_RATE_HZ} Hz mono. {capture_error}"
        )
    if host_api in _MIXER_HOST_APIS:
        found.append(
            f"{host_api} runs through the Windows mixer, which resamples and "
            "can apply enhancements without saying so. Use the same "
            "microphone where it is listed under WASAPI or WDM-KS."
        )
        if not rate_matches:
            found.append(
                f"Windows has this device set to {default_samplerate:.0f} Hz "
                f"while the study records at {config.SAMPLE_RATE_HZ} Hz, so "
                "the mixer WILL resample — processing applied to the signal."
            )
    elif host_api not in _DIRECT_HOST_APIS and not rate_matches:
        # Unknown host API: no evidence either way, so stay conservative.
        found.append(
            f"Device default is {default_samplerate:.0f} Hz but the study "
            f"records at {config.SAMPLE_RATE_HZ} Hz, and whether "
            f"{host_api} resamples is unknown. Prefer WASAPI or WDM-KS."
        )
    if max_input_channels < config.CHANNELS:
        found.append(
            f"Reports {max_input_channels} input channel(s); "
            f"{config.CHANNELS} needed."
        )
    return tuple(found)


def list_input_devices() -> list[InputDevice]:
    """Every connected input device, probed at the study's capture format."""
    try:
        raw_devices = sd.query_devices()
    except sd.PortAudioError as exc:
        raise DeviceError(
            "device_query_failed", f"Could not list audio devices: {exc}"
        ) from exc

    try:
        default_input_index = sd.default.device[0]
    except (TypeError, IndexError):
        default_input_index = None

    selected = load_selection()
    devices: list[InputDevice] = []

    for index, raw in enumerate(raw_devices):
        if int(raw["max_input_channels"]) < 1:
            continue
        host_api = _host_api_name(raw["hostapi"])
        name = str(raw["name"])
        default_rate = float(raw["default_samplerate"])
        channels = int(raw["max_input_channels"])
        supports, error = _probe_capture(index)

        devices.append(
            InputDevice(
                index=index,
                name=name,
                host_api=host_api,
                max_input_channels=channels,
                default_samplerate=default_rate,
                is_os_default=(index == default_input_index),
                is_selected=(
                    selected is not None
                    and selected["name"] == name
                    and selected["host_api"] == host_api
                ),
                supports_capture=supports,
                capture_error=error,
                rate_is_native=default_rate == float(config.SAMPLE_RATE_HZ),
                warnings=_warnings_for(
                    host_api, default_rate, supports, error, channels
                ),
            )
        )
    return devices


def as_dicts(devices: list[InputDevice]) -> list[dict[str, Any]]:
    """JSON-ready, with the derived 'recommended' flag included."""
    out: list[dict[str, Any]] = []
    for device in devices:
        row = asdict(device)
        row["warnings"] = list(device.warnings)
        row["recommended"] = device.recommended
        out.append(row)
    return out


# --- Persistence: identity is (name, host_api), never the index ------------


def load_selection() -> dict[str, str] | None:
    """The operator's stored choice, or None if they never made one."""
    path = config.SELECTED_DEVICE_PATH
    if not path.exists():
        return None
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Could not read the selected input device (%s): %s", path, exc)
        return None
    if not isinstance(stored, dict) or "name" not in stored:
        log.error("Selected-device file %s is malformed; ignoring it", path)
        return None
    return {
        "name": str(stored["name"]),
        "host_api": str(stored.get("host_api", "")),
    }


def save_selection(index: int) -> InputDevice:
    """Record the operator's choice, storing name + host API, not the index.

    Refuses a device that cannot record the study format: better to fail here,
    in front of the operator, than to discover it in the audio afterwards.
    """
    match = next((d for d in list_input_devices() if d.index == index), None)
    if match is None:
        raise DeviceError(
            "device_not_found",
            f"No input device with index {index}. It may have been unplugged — "
            "refresh the device list and choose again.",
        )
    if not match.supports_capture:
        raise DeviceError(
            "device_unsuitable",
            f"{match.name!r} ({match.host_api}) cannot record at "
            f"{config.SAMPLE_RATE_HZ} Hz mono, which the study requires. "
            f"{match.capture_error}",
        )

    config.SELECTED_DEVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"name": match.name, "host_api": match.host_api}
    tmp = config.SELECTED_DEVICE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(config.SELECTED_DEVICE_PATH)

    log.info(
        "Input device selected: %r on %s (index %d at selection time)",
        match.name,
        match.host_api,
        match.index,
    )
    for warning in match.warnings:
        log.warning("Selected device warning: %s", warning)
    return match


def clear_selection() -> None:
    """Fall back to the OS default input device."""
    config.SELECTED_DEVICE_PATH.unlink(missing_ok=True)
    log.info("Input device selection cleared; using the OS default")


def resolve_capture_device() -> DeviceInfo:
    """The device a session should open, re-resolved by name every time.

    If the operator chose a device and it is not currently connected, this
    FAILS rather than falling back to the built-in microphone. Silently
    recording a different microphone partway through the mission would
    corrupt exactly the week-scale comparison the study depends on.
    """
    selected = load_selection()
    devices = list_input_devices()

    if selected is None:
        # No explicit choice: fall back to whatever Windows calls the default.
        # That default is frequently an MME/DirectSound endpoint at 44.1 kHz,
        # which resamples silently, so say so loudly rather than letting a
        # whole session be recorded through it unnoticed.
        from capture.audio.devices import describe_default_input

        fallback = describe_default_input()
        match = next((d for d in devices if d.index == fallback.index), None)
        if match is None:
            log.warning(
                "No microphone selected; using the OS default %r, which could "
                "not be probed. Choose a device on the start screen.",
                fallback.name,
            )
        elif not match.recommended:
            log.warning(
                "No microphone selected, so the OS default %r (%s) is being "
                "used and it is NOT suitable for this study:",
                match.name,
                match.host_api,
            )
            for warning in match.warnings:
                log.warning("    %s", warning)
            log.warning(
                "    Choose a device on the start screen before recording."
            )
        return fallback

    matches = [
        d
        for d in devices
        if d.name == selected["name"] and d.host_api == selected["host_api"]
    ]
    if not matches:
        available = ", ".join(sorted({d.name for d in devices})) or "none"
        raise DeviceError(
            "selected_device_missing",
            f"The selected microphone {selected['name']!r} "
            f"({selected['host_api']}) is not connected. Plug it back in, or "
            "choose a different device on the start screen. Recording was NOT "
            "started with a different microphone, because that would break "
            f"comparison with earlier sessions. Currently connected: {available}.",
        )

    device = matches[0]
    if not device.supports_capture:
        raise DeviceError(
            "device_unsuitable",
            f"{device.name!r} can no longer record at "
            f"{config.SAMPLE_RATE_HZ} Hz mono. {device.capture_error}",
        )
    for warning in device.warnings:
        log.warning("Capture device warning: %s", warning)

    return DeviceInfo(
        index=device.index,
        name=device.name,
        max_input_channels=device.max_input_channels,
        default_samplerate=device.default_samplerate,
    )
