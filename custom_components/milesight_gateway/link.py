"""
How well a device's uplinks get through: signal quality, packet loss, gateways.

The link margin is the SNR above what the spreading factor can still
demodulate (Semtech SX1276 datasheet, table 13). Packet loss counts the gaps
in the frame counter since local midnight; a counter that falls (rejoin,
reboot) or jumps by 1000 or more starts afresh instead of counting a loss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from collections.abc import Iterable

# Lowest SNR (dB) each spreading factor demodulates.
DEMOD_FLOOR = {7: -7.5, 8: -10.0, 9: -12.5, 10: -15.0, 11: -17.5, 12: -20.0}
# EU868 data rates; other regions number them differently, so only this one.
EU868_SF = {0: 12, 1: 11, 2: 10, 3: 9, 4: 8, 5: 7}
# Margin (dB) from which a link counts as excellent, good, fair; below: poor.
QUALITY = ((10.0, "excellent"), (5.0, "good"), (0.0, "fair"))
QUALITIES = ["excellent", "good", "fair", "poor"]
COUNTER_JUMP = 1000
# A device answers a downlink within seconds; such a gap is no interval.
SHORTEST_INTERVAL = 30
RANGE_WINDOW = timedelta(hours=24)


def link_margin(snr: float | None, spreading_factor: int | None) -> float | None:
    """Return the SNR above the demodulation floor, or None without both."""
    if snr is None or spreading_factor not in DEMOD_FLOOR:
        return None
    return round(snr - DEMOD_FLOOR[spreading_factor], 1)


def quality(margin: float | None) -> str | None:
    """Name the link by its margin."""
    if margin is None:
        return None
    return next((name for floor, name in QUALITY if margin >= floor), "poor")


@dataclass
class Heard:
    """One gateway's newest reception of the device."""

    at: datetime
    rssi: float | None
    snr: float | None


@dataclass
class LinkStats:
    """The uplinks of one device since Home Assistant started."""

    snr: float | None = None
    rssi: float | None = None
    spreading_factor: int | None = None
    # Seconds between the last two regular uplinks, as measured here.
    interval: float | None = None
    gateways: dict[str, Heard] = field(default_factory=dict)
    received_today: int = 0
    lost_today: int = 0
    _day: object = None
    _f_cnt: int | None = None
    _last_at: datetime | None = None

    def record(
        self,
        at: datetime,
        receptions: Iterable[tuple[str, float | None, float | None]],
        *,
        f_cnt: int | None = None,
        spreading_factor: int | None = None,
    ) -> None:
        """Take one uplink: who heard it how well, and its frame counter."""
        if self._last_at is not None:
            gap = (at - self._last_at).total_seconds()
            if gap >= SHORTEST_INTERVAL:
                self.interval = gap
        if self._last_at is None or at > self._last_at:
            self._last_at = at
        day = dt_util.as_local(at).date()
        if day != self._day:
            self._day, self.received_today, self.lost_today = day, 0, 0
        self.received_today += 1
        if f_cnt is not None:
            gap = f_cnt - self._f_cnt if self._f_cnt is not None else 1
            if 1 < gap < COUNTER_JUMP:
                self.lost_today += gap - 1
            self._f_cnt = f_cnt
        best: Heard | None = None
        for gateway, rssi, snr in receptions:
            heard = self.gateways[gateway or "?"] = Heard(at, rssi, snr)
            if best is None or (rssi or -999) > (best.rssi or -999):
                best = heard
        if best is not None:
            self.rssi, self.snr = best.rssi, best.snr
        if spreading_factor is not None:
            self.spreading_factor = spreading_factor

    def joined(self) -> None:
        """Start the frame counter afresh: a join resets it on the device."""
        self._f_cnt = None

    @property
    def packet_loss(self) -> float | None:
        """Lost uplinks today in percent; None before a frame counter."""
        if self._f_cnt is None or not self.received_today:
            return None
        total = self.received_today + self.lost_today
        return round(100 * self.lost_today / total, 1)

    def in_range(self, now: datetime | None = None) -> dict[str, Heard]:
        """Return the gateways that heard the device within the last 24 hours."""
        since = (now or dt_util.utcnow()) - RANGE_WINDOW
        return {gw: heard for gw, heard in self.gateways.items() if heard.at >= since}
