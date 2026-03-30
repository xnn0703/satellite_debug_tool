"""
Protocol data frame structures.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class ChannelData:
    """Single channel data."""

    name: str
    value: float


@dataclass
class DataFrame:
    """
    Debug protocol data frame.

    Attributes:
        cmd_type: Command type (0x01=data report, 0x02=command response, 0x03=debug control)
        timestamp: Milliseconds since device boot
        channels: List of channel data
    """

    cmd_type: int
    timestamp: int
    channels: List[ChannelData] = field(default_factory=list)

    @property
    def channel_count(self) -> int:
        """Get number of channels."""
        return len(self.channels)

    def get_channel(self, name: str) -> float | None:
        """
        Get channel value by name.

        Args:
            name: Channel name

        Returns:
            Channel value or None if not found
        """
        for ch in self.channels:
            if ch.name == name:
                return ch.value
        return None

    def get_all_values(self) -> dict:
        """
        Get all channel values as dictionary.

        Returns:
            Dict mapping channel name to value
        """
        return {ch.name: ch.value for ch in self.channels}
