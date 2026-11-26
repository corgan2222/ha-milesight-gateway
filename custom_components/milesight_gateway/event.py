"""Button presses of the devices behind the gateway."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import lorawan_platforms

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
    """Add an event entity for each button a device reports."""
    if config_entry.runtime_data.devices is not None:
        await lorawan_platforms.async_setup_events(
            hass, config_entry, async_add_entities
        )
