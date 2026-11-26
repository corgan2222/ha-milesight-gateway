"""Firmware update entity for the Milesight gateway."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from milesight_gateway_api import firmware_key

from .const import DOMAIN
from .coordinator import MilesightFirmwareCoordinator, firmware_check_enabled
from .entity import remove_switched_off_entity

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
    """Create the gateway firmware entity."""
    coordinator = MilesightFirmwareCoordinator(
        hass,
        config_entry,
        config_entry.runtime_data.coordinator.api,
        config_entry.runtime_data.gateway_device_id,
    )
    # Not a first refresh: an unreachable download page must not stop setup.
    # It also fills the gateway device's model and firmware. In the background:
    # the web login, the summary and the download page can take over the 60 s
    # Home Assistant gives a platform while a fleet hub loads at the same time.
    config_entry.async_create_background_task(
        hass, coordinator.async_refresh(), "Read the Milesight gateway firmware"
    )
    # ponytail: without the entity the coordinator has no listener and reads
    # only at setup, so the device's firmware updates on the next start; give
    # it its own listener if that ever matters.
    if firmware_check_enabled(config_entry.options):
        async_add_entities(
            [MilesightFirmwareUpdate(coordinator, config_entry.entry_id)]
        )
    else:
        remove_switched_off_entity(hass, "update", f"{config_entry.entry_id}_firmware")


class MilesightFirmwareUpdate(
    CoordinatorEntity[MilesightFirmwareCoordinator], UpdateEntity
):
    """
    Shows whether Milesight offers newer gateway firmware.

    Installing is left to the gateway's own web interface, which is where
    Milesight wants the firmware file uploaded.
    """

    _attr_has_entity_name = True
    _attr_device_class = UpdateDeviceClass.FIRMWARE

    def __init__(
        self, coordinator: MilesightFirmwareCoordinator, entry_id: str
    ) -> None:
        """Attach the entity to the gateway device."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_firmware"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry_id)})

    @property
    def installed_version(self) -> str | None:
        """Return the firmware the gateway runs."""
        return self.coordinator.data.installed if self.coordinator.data else None

    @property
    def latest_version(self) -> str | None:
        """Return the newest firmware Milesight offers for this gateway."""
        data = self.coordinator.data
        return data.latest.version if data and data.latest else None

    @property
    def release_url(self) -> str | None:
        """Return the release notes of the newest firmware."""
        data = self.coordinator.data
        return data.latest.release_url if data and data.latest else None

    @property
    def release_summary(self) -> str | None:
        """Return Milesight's upgrade notes, such as a required minimum version."""
        data = self.coordinator.data
        notes = data.latest.notes if data and data.latest else None
        # The frontend caps the summary at 255 characters.
        return notes[:255] if notes else None

    @property
    def title(self) -> str | None:
        """Return the product line the firmware is published for."""
        data = self.coordinator.data
        return data.latest.title if data and data.latest else None

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """Compare Milesight versions, which carry revisions such as -r3."""
        return firmware_key(latest_version) > firmware_key(installed_version)
