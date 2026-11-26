"""Firmware update entity for the Milesight gateway."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from milesight_gateway_api import firmware_key, next_firmware

from .const import DOMAIN, gateway_only
from .coordinator import (
    MilesightDiagnosticsCoordinator,
    MilesightFirmwareCoordinator,
    async_install_firmware,
    firmware_check_enabled,
    firmware_family,
)
from .entity import fleet_device_info, on_first_status, remove_switched_off_entity

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import MilesightGatewayConfigEntry
    from .api import FirmwareRelease, FleetGateway

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
        return
    if gateway_only(config_entry.data):
        return
    diagnostics = config_entry.runtime_data.diagnostics
    # The fleet's gateways are only known once the status was read.
    on_first_status(
        config_entry,
        diagnostics,
        lambda data: [
            MilesightFleetFirmwareUpdate(
                diagnostics,
                coordinator,
                gateway,
                config_entry.entry_id,
                config_entry.runtime_data.gateway_device_id,
            )
            for gateway in data.network.gateways
            # The entry's own gateway has the entity above.
            if gateway.eui != data.network.gateway_eui
            and (member := data.members.get(gateway.eui)) is not None
            and member.firmware
        ],
        async_add_entities,
    )


class _FirmwareUpdate(UpdateEntity):
    """
    Shows whether Milesight offers newer gateway firmware, and installs it.

    Installing uploads the image the way the gateway's own Upgrade page does,
    after a full backup. Where the newest firmware only installs on a minimum
    version the gateway has not reached, the entity offers that version first
    and the newest one after it.
    """

    _attr_has_entity_name = True
    _attr_device_class = UpdateDeviceClass.FIRMWARE

    @property
    def _installed(self) -> str | None:
        raise NotImplementedError

    @property
    def _step(self) -> FirmwareRelease | None:
        """The release to install next, if any."""
        raise NotImplementedError

    @property
    def _newest(self) -> FirmwareRelease | None:
        raise NotImplementedError

    @property
    def _can_install(self) -> bool:
        raise NotImplementedError

    async def _async_install(self, progress: Callable[[int], None]) -> None:
        raise NotImplementedError

    @property
    def supported_features(self) -> UpdateEntityFeature:
        """Offer the installation where the web interface may be used."""
        if not self._can_install:
            return UpdateEntityFeature.RELEASE_NOTES
        return (
            UpdateEntityFeature.RELEASE_NOTES
            | UpdateEntityFeature.INSTALL
            | UpdateEntityFeature.PROGRESS
        )

    @property
    def _offered(self) -> FirmwareRelease | None:
        """The release this entity offers: the next step, else the newest."""
        return self._step or self._newest

    async def async_install(
        self,
        version: str | None,  # noqa: ARG002 - always the offered step
        backup: bool,  # noqa: ARG002, FBT001 - a backup is always taken
        **kwargs: Any,  # noqa: ARG002
    ) -> None:
        """Back the gateway up, upload the offered firmware and wait for it."""
        self._attr_in_progress = True
        self._attr_update_percentage = 0
        self.async_write_ha_state()

        def progress(percent: int) -> None:
            self._attr_update_percentage = percent
            self.async_write_ha_state()

        try:
            await self._async_install(progress)
        finally:
            self._attr_in_progress = False
            self._attr_update_percentage = None
            self.async_write_ha_state()

    @property
    def installed_version(self) -> str | None:
        """Return the firmware the gateway runs."""
        return self._installed

    @property
    def latest_version(self) -> str | None:
        """Return the firmware to install next; the newest, unless it needs a step."""
        offered = self._offered
        return offered.version if offered else None

    @property
    def release_url(self) -> str | None:
        """Return the release notes of the offered firmware."""
        offered = self._offered
        return offered.release_url if offered else None

    @property
    def release_summary(self) -> str | None:
        """Return the start of the notes; the state holds no more."""
        notes = self._notes
        # Home Assistant caps the summary at 255 characters.
        return notes[:255] if notes else None

    async def async_release_notes(self) -> str | None:
        """Return Milesight's upgrade notes in full, for the update dialog."""
        return self._notes

    @property
    def _notes(self) -> str | None:
        """Milesight's upgrade notes, such as a required minimum version."""
        offered = self._offered
        notes = offered.notes if offered else None
        newest = self._newest
        if offered and newest and offered.version != newest.version:
            # Said first: the notes below are the step's, not the newest one's.
            notes = (
                f"Step towards {newest.version}: Milesight requires "
                f"{offered.version} first.\n{notes or ''}"
            ).strip()
        return notes or None

    @property
    def title(self) -> str | None:
        """Return the product line the firmware is published for."""
        offered = self._offered
        return offered.title if offered else None

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """Compare Milesight versions, which carry revisions such as -r3."""
        return firmware_key(latest_version) > firmware_key(installed_version)


class MilesightFirmwareUpdate(
    CoordinatorEntity[MilesightFirmwareCoordinator], _FirmwareUpdate
):
    """The firmware of the gateway the entry talks to."""

    def __init__(
        self, coordinator: MilesightFirmwareCoordinator, entry_id: str
    ) -> None:
        """Attach the entity to the gateway device."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_firmware"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry_id)})

    @property
    def _installed(self) -> str | None:
        return self.coordinator.data.installed if self.coordinator.data else None

    @property
    def _step(self) -> FirmwareRelease | None:
        return self.coordinator.data.step if self.coordinator.data else None

    @property
    def _newest(self) -> FirmwareRelease | None:
        return self.coordinator.data.latest if self.coordinator.data else None

    @property
    def _can_install(self) -> bool:
        return self.coordinator.can_install

    async def _async_install(self, progress: Callable[[int], None]) -> None:
        await self.coordinator.async_install(progress)


class MilesightFleetFirmwareUpdate(
    CoordinatorEntity[MilesightDiagnosticsCoordinator], _FirmwareUpdate
):
    """
    The firmware of a fleet member, installed through its own web interface.

    The member's status names what it runs; the releases are the ones read for
    the hub, so a member of another product family is shown without an offer.
    """

    def __init__(
        self,
        coordinator: MilesightDiagnosticsCoordinator,
        firmware: MilesightFirmwareCoordinator,
        gateway: FleetGateway,
        entry_id: str,
        gateway_id: str,
    ) -> None:
        """Attach the entity to the member's device."""
        super().__init__(coordinator)
        self._firmware = firmware
        self._gateway = gateway
        self._attr_unique_id = f"{entry_id}_{gateway.eui}_firmware"
        self._attr_device_info = fleet_device_info(
            gateway, entry_id, gateway_id, local=False
        )

    async def async_added_to_hass(self) -> None:
        """Follow the releases as well as the member's status."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._firmware.async_add_listener(self._handle_coordinator_update)
        )

    @property
    def available(self) -> bool:
        """Unavailable while the member does not answer, unless it is installing."""
        return super().available and (
            self._installed is not None or bool(self.in_progress)
        )

    @property
    def _installed(self) -> str | None:
        data = self.coordinator.data
        member = data.members.get(self._gateway.eui) if data else None
        return member.firmware if member else None

    @property
    def _releases(self) -> list[FirmwareRelease]:
        """The hub's releases, if the member is of the hub's product family."""
        data = self._firmware.data
        installed = self._installed
        if (
            data is None
            or installed is None
            or firmware_family(installed) != firmware_family(data.installed)
        ):
            return []
        return data.releases

    @property
    def _step(self) -> FirmwareRelease | None:
        installed = self._installed
        return next_firmware(installed, self._releases) if installed else None

    @property
    def _newest(self) -> FirmwareRelease | None:
        releases = self._releases
        return releases[-1] if releases else None

    @property
    def _can_install(self) -> bool:
        return self.coordinator.web_data

    async def _async_install(self, progress: Callable[[int], None]) -> None:
        installed, step = self._installed, self._step
        if installed is None or step is None:
            msg = "There is no firmware to install on this gateway"
            raise HomeAssistantError(msg)
        await async_install_firmware(
            self.hass,
            self.coordinator.members.api(self._gateway.eui),
            address=self._gateway.ip or self._gateway.eui,
            installed=installed,
            step=step,
            progress=progress,
        )
        await self.coordinator.async_refresh()
