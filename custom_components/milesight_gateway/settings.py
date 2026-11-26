"""
The settings each device was given or reported, kept over restarts.

A setting reaches Home Assistant three ways: it was sent ("sent"), the
gateway acknowledged the downlink ("ack"), or the device reported it in an
uplink ("device"). A later report replaces an earlier one; a send does not
replace what the device reported afterwards. After a restart the values
come back, so a setting the device reports only on request is not unknown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

STORAGE_VERSION = 1
SAVE_DELAY = 10
# Where a kept value comes from; commands.py names the other two.
DEVICE = "device"


class SettingsLog:
    """One entry's stored settings: {devEUI: {field: {v, t, src}}}."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Prepare; async_load() reads what was stored."""
        self._store: Store[dict[str, dict[str, dict[str, Any]]]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.settings.{entry_id}"
        )
        self._settings: dict[str, dict[str, dict[str, Any]]] = {}

    async def async_flush(self) -> None:
        """Write now; a delayed write would be lost when the entry reloads."""
        await self._store.async_save(self._settings)

    async def async_load(self) -> None:
        """Read the stored settings."""
        self._settings = await self._store.async_load() or {}

    def record(self, dev_eui: str, values: dict[str, Any], source: str) -> None:
        """Keep these values of one device, unless they are unchanged."""
        if not values:
            return
        stored = self._settings.setdefault(dev_eui.upper(), {})
        now = dt_util.utcnow().isoformat()
        changed = False
        for key, value in values.items():
            old = stored.get(key)
            if old is not None and old["v"] == value and old["src"] == source:
                continue
            stored[key] = {"v": value, "t": now, "src": source}
            changed = True
        if changed:
            self._store.async_delay_save(lambda: self._settings, SAVE_DELAY)

    def get(self, dev_eui: str) -> dict[str, dict[str, Any]]:
        """Return one device's settings, by field."""
        return self._settings.get(dev_eui.upper(), {})
