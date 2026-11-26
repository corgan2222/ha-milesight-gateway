"""Radiator thermostats behind the gateway."""

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
    """Add a thermostat for each device with a settable target."""
    await lorawan_platforms.async_setup_climates(hass, config_entry, async_add_entities)
