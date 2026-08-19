"""Device selection: warnings, persistence, and refusing the wrong mic.

The hardware-dependent parts are exercised against fake device lists, so
these run identically on the habitat laptop and on a machine with no
microphone at all.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capture import config
from capture.audio import selection
from capture.audio.selection import InputDevice
from capture.errors import DeviceError


def device(
    index: int = 9,
    name: str = "Blue Yeti",
    host_api: str = "Windows WASAPI",
    rate: float = 48000.0,
    supports: bool = True,
    channels: int = 2,
) -> InputDevice:
    """An InputDevice with warnings derived the same way production does."""
    error = None if supports else "PortAudioError: Invalid sample rate"
    return InputDevice(
        index=index,
        name=name,
        host_api=host_api,
        max_input_channels=channels,
        default_samplerate=rate,
        is_os_default=False,
        is_selected=False,
        supports_capture=supports,
        capture_error=error,
        rate_is_native=rate == float(config.SAMPLE_RATE_HZ),
        warnings=selection._warnings_for(host_api, rate, supports, error, channels),
    )


class WarningTests(unittest.TestCase):
    def test_native_rate_wasapi_device_is_recommended(self) -> None:
        self.assertTrue(device().recommended)
        self.assertEqual(device().warnings, ())

    def test_mixer_api_rate_mismatch_warns_about_resampling(self) -> None:
        # The trap: MME accepts 48 kHz on a 44.1 kHz endpoint and quietly
        # resamples, which is processing applied to the signal.
        d = device(rate=44100.0, host_api="MME")
        self.assertFalse(d.recommended)
        self.assertTrue(any("resample" in w for w in d.warnings))

    def test_direct_api_rate_mismatch_is_not_treated_as_resampling(self) -> None:
        # Measured on a Yeti: WDM-KS reports a 44.1 kHz default yet captures
        # at 48 kHz with the ADC's 16-bit sample pattern perfectly intact,
        # which resampling would have destroyed. Direct host APIs reject a
        # rate the pin cannot do, so acceptance proves the rate is native and
        # the reported default is irrelevant.
        d = device(rate=44100.0, host_api="Windows WDM-KS", supports=True)
        self.assertEqual(d.warnings, ())
        self.assertTrue(d.recommended)

    def test_mixer_host_apis_are_always_warned_about(self) -> None:
        # Even at a matching rate, the mixer can apply enhancements silently.
        for host_api in ("MME", "Windows DirectSound"):
            d = device(host_api=host_api, rate=48000.0)
            self.assertFalse(d.recommended, host_api)
            self.assertTrue(any("mixer" in w for w in d.warnings), host_api)

    def test_unknown_host_api_stays_conservative(self) -> None:
        d = device(host_api="Some Future API", rate=44100.0)
        self.assertFalse(d.recommended)

    def test_unsupported_device_is_not_recommended(self) -> None:
        d = device(rate=8000.0, supports=False)
        self.assertFalse(d.recommended)
        self.assertTrue(any("Cannot record" in w for w in d.warnings))

    def test_too_few_channels_is_warned_about(self) -> None:
        d = device(channels=0)
        self.assertTrue(any("channel" in w for w in d.warnings))


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "selected_input_device.json"
        patcher = mock.patch.object(config, "SELECTED_DEVICE_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_no_selection_reads_as_none(self) -> None:
        self.assertIsNone(selection.load_selection())

    def test_selection_stores_name_and_host_api_not_index(self) -> None:
        # PortAudio renumbers devices when anything is plugged in, so the
        # index must not be the stored identity.
        with mock.patch.object(
            selection, "list_input_devices", return_value=[device(index=9)]
        ):
            selection.save_selection(9)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored, {"name": "Blue Yeti", "host_api": "Windows WASAPI"})
        self.assertNotIn("index", stored)

    def test_unsuitable_device_is_refused(self) -> None:
        bad = device(index=3, rate=8000.0, supports=False)
        with mock.patch.object(selection, "list_input_devices", return_value=[bad]):
            with self.assertRaises(DeviceError) as caught:
                selection.save_selection(3)
        self.assertEqual(caught.exception.code, "device_unsuitable")
        self.assertFalse(self.path.exists())

    def test_unknown_index_is_refused(self) -> None:
        with mock.patch.object(selection, "list_input_devices", return_value=[]):
            with self.assertRaises(DeviceError) as caught:
                selection.save_selection(42)
        self.assertEqual(caught.exception.code, "device_not_found")

    def test_malformed_selection_file_is_ignored_not_fatal(self) -> None:
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(selection.load_selection())

    def test_clear_selection_is_idempotent(self) -> None:
        selection.clear_selection()  # nothing stored yet
        with mock.patch.object(
            selection, "list_input_devices", return_value=[device()]
        ):
            selection.save_selection(9)
        selection.clear_selection()
        self.assertIsNone(selection.load_selection())


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "selected_input_device.json"
        patcher = mock.patch.object(config, "SELECTED_DEVICE_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _select(self, dev: InputDevice) -> None:
        self.path.write_text(
            json.dumps({"name": dev.name, "host_api": dev.host_api}),
            encoding="utf-8",
        )

    def test_resolves_by_name_even_when_the_index_moved(self) -> None:
        # The USB mic was index 9 when chosen and is index 4 today.
        chosen = device(index=9)
        moved = device(index=4)
        self._select(chosen)
        with mock.patch.object(
            selection, "list_input_devices", return_value=[moved]
        ):
            resolved = selection.resolve_capture_device()
        self.assertEqual(resolved.index, 4)
        self.assertEqual(resolved.name, "Blue Yeti")

    def test_missing_selected_device_fails_instead_of_falling_back(self) -> None:
        # Silently recording the built-in mic instead of the chosen USB mic
        # would destroy comparability across the mission week.
        self._select(device(name="Blue Yeti"))
        other = device(index=1, name="Internal Mic", host_api="MME", rate=44100.0)
        with mock.patch.object(
            selection, "list_input_devices", return_value=[other]
        ):
            with self.assertRaises(DeviceError) as caught:
                selection.resolve_capture_device()
        self.assertEqual(caught.exception.code, "selected_device_missing")
        self.assertIn("Internal Mic", caught.exception.message)

    def test_selected_device_that_became_unusable_fails(self) -> None:
        self._select(device(name="Blue Yeti"))
        broken = device(name="Blue Yeti", rate=8000.0, supports=False)
        with mock.patch.object(
            selection, "list_input_devices", return_value=[broken]
        ):
            with self.assertRaises(DeviceError) as caught:
                selection.resolve_capture_device()
        self.assertEqual(caught.exception.code, "device_unsuitable")


if __name__ == "__main__":
    unittest.main()
