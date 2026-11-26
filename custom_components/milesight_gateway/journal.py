"""
What happened to the downlinks of one entry: commands, ACKs, errors, joins.

Each command is one record, updated as it moves on (pending, acknowledged,
confirmed ...). The newest records are stored, so the device page shows the
history after a restart too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

STORAGE_VERSION = 1
KEEP = 500
SAVE_DELAY = 15


class Journal:
    """The newest records of one entry, newest last."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Prepare; async_load() reads what was stored."""
        self._store: Store[list[dict[str, Any]]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.journal.{entry_id}"
        )
        self.records: list[dict[str, Any]] = []
        self._sequence = 0

    async def async_flush(self) -> None:
        """Write now; a delayed write would be lost when the entry reloads."""
        await self._store.async_save(self.records)

    async def async_load(self) -> None:
        """Read the stored records."""
        self.records = await self._store.async_load() or []
        self._sequence = max((r.get("id", 0) for r in self.records), default=0)

    def open_commands(
        self, dev_eui: str, states: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """Return a device's commands in one of these states, newest last."""
        return [
            record
            for record in self.records
            if record.get("kind") == "command"
            and record.get("dev_eui") == dev_eui.upper()
            and record.get("state") in states
        ]

    def add(self, dev_eui: str, kind: str, **data: Any) -> dict[str, Any]:
        """Start a record; the caller may update it in place, then call saved()."""
        self._sequence += 1
        record = {
            "id": self._sequence,
            "t": dt_util.utcnow().isoformat(),
            "dev_eui": dev_eui.upper(),
            "kind": kind,
            **data,
        }
        self.records.append(record)
        del self.records[:-KEEP]
        self.saved()
        return record

    def saved(self) -> None:
        """Store the records soon."""
        self._store.async_delay_save(lambda: self.records, SAVE_DELAY)
