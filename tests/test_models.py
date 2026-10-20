import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.decoder import LogDecoder
from app.main import format_relative_time
from app.models import SignalId, SignalSeries


class SignalSeriesTest(unittest.TestCase):
    def test_formats_enumeration(self) -> None:
        series = SignalSeries(
            SignalId("DoorMsg", "DoorReq", 1),
            "",
            choices={1: "Open"},
        )
        series.append(0.1, 1)
        self.assertEqual(series.format_value(1.0), "Open (1)")


class RelativeTimeFormattingTest(unittest.TestCase):
    def test_millisecond_precision_for_short_times(self) -> None:
        self.assertEqual(format_relative_time(0.01), "10.000 ms")
        self.assertEqual(format_relative_time(0.01, include_sign=True), "+10.000 ms")


class ChannelBindingDecodeTest(unittest.TestCase):
    def test_unbound_channel_is_skipped_and_does_not_extend_time_axis(self) -> None:
        decoder = LogDecoder()
        dbc_path = "selected.dbc"
        message = SimpleNamespace(
            name="DoorMsg",
            frame_id=0x100,
            is_extended_frame=False,
            decode=lambda *_args, **_kwargs: {"DoorReq": 1},
        )
        decoder._messages_by_dbc[dbc_path] = {(0x100, False): message}
        decoder._metadata_by_dbc[dbc_path] = {("DoorMsg", "DoorReq"): ("", {})}
        frames = [
            SimpleNamespace(arbitration_id=0x200, is_error_frame=False, channel=2, timestamp=1.0, is_extended_id=False, data=b"\x00"),
            SimpleNamespace(arbitration_id=0x100, is_error_frame=False, channel=1, timestamp=5.0, is_extended_id=False, data=b"\x00"),
            SimpleNamespace(arbitration_id=0x100, is_error_frame=False, channel=1, timestamp=5.1, is_extended_id=False, data=b"\x00"),
            SimpleNamespace(arbitration_id=0x200, is_error_frame=False, channel=2, timestamp=20.0, is_extended_id=False, data=b"\x00"),
        ]

        reader = MagicMock()
        reader.__enter__.return_value = iter(frames)
        with patch("app.decoder.can.BLFReader", return_value=reader):
            series, end_time, total_frames, decoded_frames = decoder.decode_blf(
                "sample.blf", {1: dbc_path, 2: None}
            )

        self.assertEqual(total_frames, 4)
        self.assertEqual(decoded_frames, 2)
        self.assertAlmostEqual(end_time, 0.1)
        self.assertEqual({signal_id.channel for signal_id in series}, {1})
