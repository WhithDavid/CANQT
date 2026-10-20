from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import can
import cantools

from .models import SignalId, SignalSeries

ProgressCallback = Callable[[int, str], None]


@dataclass(frozen=True)
class DbcInfo:
    path: str
    name: str
    message_count: int


class LogDecoder:
    """DBC catalog plus BLF channel scanning and channel-bound decoding."""

    def __init__(self) -> None:
        self._dbc_info: dict[str, DbcInfo] = {}
        self._messages_by_dbc: dict[str, dict[tuple[int, bool], Any]] = {}
        self._metadata_by_dbc: dict[str, dict[tuple[str, str], tuple[str, dict[int, str]]]] = {}
        self.last_scan_duration = 0.0

    @property
    def dbc_files(self) -> list[DbcInfo]:
        return list(self._dbc_info.values())

    def add_dbc_files(self, paths: list[str]) -> list[DbcInfo]:
        added: list[DbcInfo] = []
        for path_string in paths:
            canonical_path = str(Path(path_string).resolve())
            if canonical_path in self._dbc_info:
                continue

            database = cantools.database.load_file(canonical_path)
            messages: dict[tuple[int, bool], Any] = {}
            metadata: dict[tuple[str, str], tuple[str, dict[int, str]]] = {}
            for message in database.messages:
                messages[(message.frame_id, bool(message.is_extended_frame))] = message
                for signal in message.signals:
                    choices = {
                        int(raw): str(label)
                        for raw, label in (signal.choices or {}).items()
                    }
                    metadata[(message.name, signal.name)] = (signal.unit or "", choices)

            info = DbcInfo(canonical_path, Path(canonical_path).name, len(database.messages))
            self._dbc_info[canonical_path] = info
            self._messages_by_dbc[canonical_path] = messages
            self._metadata_by_dbc[canonical_path] = metadata
            added.append(info)
        return added

    def remove_dbc_file(self, path: str) -> None:
        canonical_path = str(Path(path).resolve())
        self._dbc_info.pop(canonical_path, None)
        self._messages_by_dbc.pop(canonical_path, None)
        self._metadata_by_dbc.pop(canonical_path, None)

    def scan_blf_channels(
        self,
        blf_path: str,
        progress: ProgressCallback | None = None,
    ) -> dict[int, int]:
        counts: dict[int, int] = {}
        frame_count = 0
        first_timestamp: float | None = None
        last_timestamp: float | None = None
        with can.BLFReader(blf_path) as reader:
            for frame in reader:
                if not hasattr(frame, "arbitration_id") or frame.is_error_frame:
                    continue
                channel = self._channel_number(frame)
                counts[channel] = counts.get(channel, 0) + 1
                timestamp = float(frame.timestamp)
                if first_timestamp is None:
                    first_timestamp = timestamp
                last_timestamp = timestamp
                frame_count += 1
                if progress is not None and frame_count % 5000 == 0:
                    progress(0, f"Scanned {frame_count:,} CAN frames")
        if progress is not None:
            progress(100, f"Found {len(counts)} BLF channels")
        self.last_scan_duration = (
            max(0.0, last_timestamp - first_timestamp)
            if first_timestamp is not None and last_timestamp is not None
            else 0.0
        )
        return dict(sorted(counts.items()))

    def decode_blf(
        self,
        blf_path: str,
        channel_bindings: dict[int, str | None],
        progress: ProgressCallback | None = None,
    ) -> tuple[dict[SignalId, SignalSeries], float, int, int]:
        selected_bindings = {
            channel: dbc_path
            for channel, dbc_path in channel_bindings.items()
            if dbc_path in self._messages_by_dbc
        }
        if not selected_bindings:
            raise RuntimeError("Select one DBC for at least one BLF channel before decoding.")

        series: dict[SignalId, SignalSeries] = {}
        decoded_frames = 0
        total_frames = 0
        first_timestamp: float | None = None
        last_timestamp = 0.0

        with can.BLFReader(blf_path) as reader:
            for frame in reader:
                if not hasattr(frame, "arbitration_id") or frame.is_error_frame:
                    continue

                total_frames += 1
                channel = self._channel_number(frame)
                dbc_path = selected_bindings.get(channel)
                if dbc_path is None:
                    continue

                timestamp = float(frame.timestamp)
                if first_timestamp is None:
                    first_timestamp = timestamp
                relative_time = timestamp - first_timestamp
                last_timestamp = relative_time

                messages = self._messages_by_dbc[dbc_path]
                message = messages.get((frame.arbitration_id, bool(frame.is_extended_id)))
                if message is None:
                    continue

                try:
                    decoded = message.decode(
                        bytes(frame.data),
                        decode_choices=False,
                        scaling=True,
                        allow_truncated=True,
                    )
                except (ValueError, DecodeError):
                    continue

                metadata = self._metadata_by_dbc[dbc_path]
                for signal_name, value in decoded.items():
                    key = SignalId(
                        message.name,
                        signal_name,
                        channel,
                        message.frame_id,
                        bool(message.is_extended_frame),
                    )
                    item = series.get(key)
                    if item is None:
                        unit, choices = metadata[(message.name, signal_name)]
                        item = SignalSeries(key, unit, choices=choices)
                        series[key] = item
                    item.append(relative_time, value)
                decoded_frames += 1

                if progress is not None and total_frames % 3000 == 0:
                    progress(0, f"Scanned {total_frames:,} frames; decoded {decoded_frames:,}")

        if progress is not None:
            progress(100, f"Decoded {decoded_frames:,} of {total_frames:,} CAN frames")
        return series, last_timestamp, total_frames, decoded_frames

    @staticmethod
    def _channel_number(frame: Any) -> int:
        return int(frame.channel) if frame.channel is not None else 0


try:
    from cantools.database.errors import DecodeError
except ImportError:  # pragma: no cover - compatibility guard
    DecodeError = ValueError
