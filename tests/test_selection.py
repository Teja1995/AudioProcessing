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


class GroupingTests(unittest.TestCase):
    """PortAudio lists one entry per host API, so a single USB microphone
    appears three or four times. The operator should see microphones."""

    def test_same_microphone_across_host_apis_becomes_one_entry(self) -> None:
        # Exactly what a Blue Yeti looks like on Windows, including MME's
        # 31-character truncation of the name.
        yeti = [
            device(index=1, name="Microphone (Yeti Stereo Microph", host_api="MME", rate=44100.0),
            device(index=7, name="Microphone (Yeti Stereo Microphone)", host_api="Windows DirectSound", rate=44100.0),
            device(index=15, name="Microphone (Yeti Stereo Microphone)", host_api="Windows WASAPI"),
            device(index=30, name="Microphone (Yeti Stereo Microphone)", host_api="Windows WDM-KS", rate=44100.0),
        ]
        groups = selection.group_microphones(yeti)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].paths), 4)

    def test_truncated_mme_name_is_never_the_label(self) -> None:
        groups = selection.group_microphones([
            device(index=1, name="Microphone (Yeti Stereo Microph", host_api="MME", rate=44100.0),
            device(index=15, name="Microphone (Yeti Stereo Microphone)", host_api="Windows WASAPI"),
        ])
        self.assertEqual(groups[0].name, "Microphone (Yeti Stereo Microphone)")

    def test_best_path_is_chosen_over_the_resampling_one(self) -> None:
        groups = selection.group_microphones([
            device(index=1, name="Mic", host_api="MME", rate=44100.0),
            device(index=30, name="Mic", host_api="Windows WDM-KS", rate=44100.0),
        ])
        # WDM-KS reaches the hardware pin; MME goes through the mixer.
        self.assertEqual(groups[0].best.host_api, "Windows WDM-KS")
        self.assertEqual(groups[0].best.index, 30)

    def test_speaker_pins_are_not_offered_as_microphones(self) -> None:
        # WDM-KS exposes render pins as inputs; selecting one records the
        # system's own output, or silence.
        groups = selection.group_microphones([
            device(index=25, name="Input (AudioMiniport Wave Speaker)", host_api="Windows WDM-KS")
        ])
        self.assertTrue(groups[0].is_output_pin)
        self.assertFalse(groups[0].offer_by_default)

    def test_virtual_routers_are_not_offered(self) -> None:
        for name in ("Microsoft Sound Mapper - Input", "Primary Sound Capture Driver", "Input ()"):
            groups = selection.group_microphones([device(index=0, name=name, host_api="MME", rate=44100.0)])
            self.assertTrue(groups[0].is_virtual, name)
            self.assertFalse(groups[0].offer_by_default, name)

    def test_a_real_microphone_is_offered(self) -> None:
        groups = selection.group_microphones([
            device(index=15, name="Microphone (Yeti Stereo Microphone)", host_api="Windows WASAPI")
        ])
        self.assertTrue(groups[0].offer_by_default)
        self.assertFalse(groups[0].is_virtual)
        self.assertFalse(groups[0].is_output_pin)

    def test_unusable_device_is_not_offered_but_is_not_called_virtual(self) -> None:
        bluetooth = device(index=18, name="Headset (Hands-Free)", host_api="Windows WDM-KS", rate=8000.0, supports=False)
        groups = selection.group_microphones([bluetooth])
        self.assertFalse(groups[0].offer_by_default)
        self.assertFalse(groups[0].is_virtual)


class MicrophoneMonitorDetectionTests(unittest.TestCase):
    """Windows adopts a USB microphone's own headphone jack as the default
    output. Task 2's tone would play where nobody hears it, and the take
    would record silence while looking like a completed calibration."""

    def test_the_microphones_own_output_is_recognised(self) -> None:
        self.assertTrue(
            selection._same_physical_device(
                "Speakers (Yeti Stereo Microphone)",
                "Microphone (Yeti Stereo Microphone)",
            )
        )

    def test_mme_truncation_without_a_closing_bracket_still_matches(self) -> None:
        # MME cuts names at 31 characters, so the bracket is often missing.
        # Requiring ")" is what let the Yeti's headphone jack pass as a
        # room speaker.
        self.assertTrue(
            selection._same_physical_device(
                "Speakers (Yeti Stereo Microphon",
                "Microphone (Yeti Stereo Microphone)",
            )
        )

    def test_a_different_device_is_not_matched(self) -> None:
        self.assertFalse(
            selection._same_physical_device(
                "Speakers (Cirrus Logic High Definition Audio)",
                "Microphone (Yeti Stereo Microphone)",
            )
        )

    def test_names_without_brackets_do_not_match_everything(self) -> None:
        self.assertFalse(
            selection._same_physical_device("Speakers", "Microphone (Yeti)")
        )


class StreamInterlockTests(unittest.TestCase):
    """Re-enumerating terminates PortAudio, which destroys any open stream.

    A live session died mid-take because the speaker picker called
    resolve_capture_device(), which refreshed unconditionally. Guarding each
    caller was not enough, so the unsafe operation now refuses instead.
    """

    def setUp(self) -> None:
        # Whatever this test does, leave the counter as it was found.
        self.addCleanup(self._drain)

    def _drain(self) -> None:
        while selection.streams_are_open():
            selection.register_stream_closed()

    def test_refresh_is_skipped_while_a_stream_is_open(self) -> None:
        selection.register_stream_open()
        with mock.patch.object(selection.sd, "_terminate") as terminate:
            selection.refresh_device_list()
        terminate.assert_not_called()

    def test_refresh_runs_once_the_stream_is_closed(self) -> None:
        selection.register_stream_open()
        selection.register_stream_closed()
        with mock.patch.object(selection.sd, "_terminate") as terminate, mock.patch.object(
            selection.sd, "_initialize"
        ):
            selection.refresh_device_list()
        terminate.assert_called_once()

    def test_nested_streams_keep_the_interlock_set(self) -> None:
        selection.register_stream_open()
        selection.register_stream_open()
        selection.register_stream_closed()
        self.assertTrue(selection.streams_are_open())
        selection.register_stream_closed()
        self.assertFalse(selection.streams_are_open())

    def test_the_counter_never_goes_negative(self) -> None:
        # An engine that fails to open still calls close(); an unbalanced
        # release must not leave the interlock permanently disabled.
        selection.register_stream_closed()
        selection.register_stream_closed()
        selection.register_stream_open()
        self.assertTrue(selection.streams_are_open())

    def test_listing_outputs_does_not_re_enumerate(self) -> None:
        # This is the exact path that killed the session.
        selection.register_stream_open()
        with mock.patch.object(selection.sd, "_terminate") as terminate:
            selection.list_output_devices(refresh=False)
        terminate.assert_not_called()


def output(index=12, name="Speakers (Cirrus Logic High Definition Audio)",
           host_api="Windows WASAPI", monitor=False):
    return selection.OutputDevice(
        index=index, name=name, host_api=host_api, max_output_channels=2,
        default_samplerate=48000.0, is_os_default=False, is_selected=False,
        is_microphone_monitor=monitor,
        warnings=("This is the microphone's own headphone output, not a "
                  "speaker in the room.",) if monitor else (),
    )


class OutputRankingTests(unittest.TestCase):
    """Playback ranking is the OPPOSITE of capture ranking. For capture the
    mixer's resampling corrupts the data, so the direct pin wins; for
    playback the recording of the acoustic result IS the data, and the
    exclusive WDM-KS pin collides with the open capture stream — one such
    collision killed a session mid-take."""

    def test_wasapi_is_preferred_over_the_exclusive_kernel_pin(self) -> None:
        groups = selection.group_outputs([
            output(index=31, host_api="Windows WDM-KS"),
            output(index=12, host_api="Windows WASAPI"),
        ])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].best.host_api, "Windows WASAPI")

    def test_a_wdm_ks_only_speaker_is_not_offered(self) -> None:
        groups = selection.group_outputs([
            output(index=23, name="Output 1 (AudioMiniport Wave Speaker)",
                   host_api="Windows WDM-KS"),
        ])
        self.assertFalse(groups[0].offer_by_default)

    def test_the_microphones_monitor_is_still_not_offered(self) -> None:
        groups = selection.group_outputs([
            output(index=12, name="Speakers (Yeti Stereo Microphone)", monitor=True),
        ])
        self.assertFalse(groups[0].offer_by_default)

    def test_a_real_speaker_on_a_shared_api_is_offered(self) -> None:
        groups = selection.group_outputs([output()])
        self.assertTrue(groups[0].offer_by_default)


class PlaybackErrorMappingTests(unittest.TestCase):
    """A failed tone must abort the take and surface as a recoverable 409 —
    a 500 here once wedged a whole session behind the fatal overlay."""

    def test_playback_error_maps_to_409(self) -> None:
        from fastapi import HTTPException

        from capture.errors import PlaybackError
        from capture.routes.session import http_errors

        with self.assertRaises(HTTPException) as caught:
            with http_errors():
                raise PlaybackError("The reference tone could not be played.")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("reference tone", caught.exception.detail)
