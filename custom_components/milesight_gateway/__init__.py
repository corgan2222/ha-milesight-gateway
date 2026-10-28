"""The Milesight Gateway integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PORT, Platform
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import CONF_GATEWAY_URL, DOMAIN
from .coordinator import MilesightGatewayCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceEntry
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

# Bumped when entry data or the registry layout changes; see
# async_migrate_entry and ConfigFlow.MINOR_VERSION.
ENTRY_VERSION = 1
ENTRY_MINOR_VERSION = 2

type MilesightGatewayConfigEntry = ConfigEntry[RuntimeData]


@dataclass
class RuntimeData:
    """Runtime data stored on the config entry."""

    coordinator: DataUpdateCoordinator


async def async_migrate_entry(
    hass: HomeAssistant, entry: MilesightGatewayConfigEntry
) -> bool:
    """
    Migrate the gateway device off its address-derived identifier.

    Runs before setup, while entry.data still holds the address the existing
    device was registered under, so it must ship with the change that makes the
    address editable — not after it.
    """
    if entry.version == ENTRY_VERSION and entry.minor_version < ENTRY_MINOR_VERSION:
        device_registry = dr.async_get(hass)
        legacy_id = f"{entry.data[CONF_GATEWAY_URL]}:{entry.data[CONF_PORT]}"
        if device := device_registry.async_get_device(
            identifiers={(DOMAIN, legacy_id)}
        ):
            device_registry.async_update_device(
                device.id, new_identifiers={(DOMAIN, entry.entry_id)}
            )
        hass.config_entries.async_update_entry(entry, minor_version=ENTRY_MINOR_VERSION)

    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: MilesightGatewayConfigEntry
) -> bool:
    """Set up Milesight Gateway from a config entry."""
    coordinator = MilesightGatewayCoordinator(hass, entry)

    await coordinator.async_config_entry_first_refresh()

    if not coordinator.api.connected:
        raise ConfigEntryNotReady

    # Register the gateway as a device so sensor devices can link to it
    # via via_device. Identified by the entry id rather than the address, so
    # changing the gateway's URL does not create a second device.
    gateway_url = entry.data[CONF_GATEWAY_URL]
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="Milesight Gateway",
        manufacturer="Milesight",
        model="LoRaWAN Gateway",
        configuration_url=gateway_url,
    )

    entry.runtime_data = RuntimeData(coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_remove_config_entry_device(
    _hass: HomeAssistant,
    _config_entry: MilesightGatewayConfigEntry,
    _device_entry: DeviceEntry,
) -> bool:
    """Allow a device to be removed from the UI."""
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: MilesightGatewayConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
