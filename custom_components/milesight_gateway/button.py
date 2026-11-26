"""Device commands such as reboot, sent as downlinks, and the gateway's sync."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory

from . import lorawan_platforms
from .const import DOMAIN
from .sync import async_sync_devices, can_sync

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import MilesightGatewayConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MilesightGatewayConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the commands of each device, and the sync on the gateway."""
    if can_sync(config_entry):
        async_add_entities([MilesightSyncButton(config_entry)])
    await lorawan_platforms.async_setup_buttons(hass, config_entry, async_add_entities)


class MilesightSyncButton(ButtonEntity):
    """Remove the devices the gateway no longer lists."""

    _attr_has_entity_name = True
    _attr_translation_key = "sync_devices"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: MilesightGatewayConfigEntry) -> None:
        """Attach the button to the gateway device."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sync_devices"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_press(self) -> None:
        """Compare with the gateway's device list and remove what is gone."""
        await async_sync_devices(self.hass, self._entry)
