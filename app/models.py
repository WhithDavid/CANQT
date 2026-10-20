from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SignalId:
    """DBC signal identity; message name keeps duplicate signal names unambiguous."""

    message_name: str
    signal_name: str
    channel: int | None
    frame_id: int | None = None
    is_extended_frame: bool = False

    @property
    def display_name(self) -> str:
        channel = "所有通道" if self.channel is None else f"CH{self.channel}"
        return f"{self.message_name}.{self.signal_name}  [{channel}]"


@dataclass
class SignalSeries:
    signal_id: SignalId
    unit: str
    values: list[float] = field(default_factory=list)
    timestamps: list[float] = field(default_factory=list)
    choices: dict[int, str] = field(default_factory=dict)

    def append(self, timestamp: float, value: Any) -> None:
        if isinstance(value, bool):
            numeric_value = float(value)
        elif isinstance(value, (int, float)):
            numeric_value = float(value)
        else:
            return
        self.timestamps.append(timestamp)
        self.values.append(numeric_value)

    def format_value(self, value: float) -> str:
        integer_value = int(value)
        if value == integer_value and integer_value in self.choices:
            return f"{self.choices[integer_value]} ({integer_value})"
        if self.unit:
            return f"{value:g} {self.unit}"
        return f"{value:g}"
