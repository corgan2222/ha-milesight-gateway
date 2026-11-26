"""
Battery forecast: days left from the trend of the reported battery level.

A least-squares line through the samples since the last battery change gives
the loss per day; days left are the level divided by it. A rise of 25 points
or more is a new battery and starts the trend afresh. Samples are stored, at
most one per device and six hours, so the trend survives a restart.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

STORAGE_VERSION = 1
SAMPLE_EVERY = timedelta(hours=6)
KEEP = timedelta(days=365)
REPLACED = 25
# A trend needs this much time and this many samples before it says anything.
MIN_SPAN = timedelta(days=3)
MIN_SAMPLES = 3
SAVE_DELAY = 60


def days_left(samples: list[tuple[float, float]]) -> float | None:
    """
    Return the days until 0 % from (unix time, percent) samples, or None.

    None while the trend is too short or does not fall.
    """
    start = 0
    for i in range(1, len(samples)):
        if samples[i][1] - samples[i - 1][1] >= REPLACED:
            start = i
    points = samples[start:]
    if len(points) < MIN_SAMPLES:
        return None
    span = points[-1][0] - points[0][0]
    if span < MIN_SPAN.total_seconds():
        return None
    days = [(t - points[0][0]) / 86400 for t, _ in points]
    levels = [level for _, level in points]
    mean_d, mean_l = sum(days) / len(days), sum(levels) / len(levels)
    var = sum((d - mean_d) ** 2 for d in days)
    slope = (
        sum((d - mean_d) * (lv - mean_l) for d, lv in zip(days, levels, strict=True))
        / var
    )
    if slope >= 0:
        return None
    return round(levels[-1] / -slope, 1)


class BatteryLog:
    """The stored battery samples of one entry's devices."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Prepare; async_load() reads the stored samples."""
        self._store: Store[dict[str, list[list[float]]]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.battery.{entry_id}"
        )
        self._samples: dict[str, list[list[float]]] = {}

    async def async_flush(self) -> None:
        """Write now; a delayed write would be lost when the entry reloads."""
        await self._store.async_save(self._samples)

    async def async_load(self) -> None:
        """Read the samples stored before."""
        self._samples = await self._store.async_load() or {}

    def add(self, dev_eui: str, level: Any, at: datetime | None = None) -> None:
        """Keep a reported level, unless one was kept less than six hours ago."""
        try:
            level = float(level)
        except TypeError, ValueError:
            return
        now = (at or dt_util.utcnow()).timestamp()
        samples = self._samples.setdefault(dev_eui, [])
        changed = samples and level - samples[-1][1] >= REPLACED
        if (
            samples
            and now - samples[-1][0] < SAMPLE_EVERY.total_seconds()
            and not changed
        ):
            return
        samples.append([now, level])
        cutoff = now - KEEP.total_seconds()
        self._samples[dev_eui] = [s for s in samples if s[0] >= cutoff]
        self._store.async_delay_save(lambda: self._samples, SAVE_DELAY)

    def days_left(self, dev_eui: str) -> float | None:
        """Return the forecast for one device."""
        return days_left([(t, lv) for t, lv in self._samples.get(dev_eui, [])])
