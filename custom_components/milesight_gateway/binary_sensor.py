"""
Binary sensor entities for the Milesight Gateway integration.

Gateway states read the coordinators; the device binary sensors come from the
codec catalog, as devices report (see lorawan_platforms).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import lorawan_platforms
from .const import DOMAIN, gateway_only
from .coordinator import MilesightDiagnosticsCoordinator, MilesightGatewayCoordinator
from .entity import fleet_device_info, on_first_status, remove_switched_off_entity

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import MilesightGatewayConfigEntry
    from .api import ApplicationLink, FleetGateway

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MilesightGatewayConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create binary sensor entities for all binary_sensor-platform entities."""
    coordinator = config_entry.runtime_data.coordinator
    # Keyed on the entry id, not the address, so the device survives an IP change.
    gateway_id = config_entry.runtime_data.gateway_device_id
    diagnostics = config_entry.runtime_data.diagnostics
    entry_id = config_entry.entry_id
    entities: list[BinarySensorEntity] = [
        MilesightOnlineBinarySensor(coordinator, entry_id)
    ]
    # The forwarder state comes from the network server, behind its login.
    if gateway_only(config_entry.data):
        remove_switched_off_entity(
            hass, "binary_sensor", f"{entry_id}_packet_forwarder"
        )
    else:
        entities.append(MilesightForwarderBinarySensor(diagnostics, entry_id))
    async_add_entities(entities)
    # Links and fleet gateways are only known once the status was read.
    on_first_status(
        config_entry,
        diagnostics,
        lambda data: [
            *(
                MilesightMqttLinkBinarySensor(diagnostics, link, entry_id)
                for link in data.network.links
            ),
            *(
                MilesightFleetBinarySensor(
                    diagnostics,
                    gateway,
                    entry_id,
                    gateway_id,
                    local=gateway.eui == data.network.gateway_eui,
                )
                for gateway in data.network.gateways
            ),
            *(
                MilesightFleetStateBinarySensor(
                    diagnostics, gateway, entry_id, gateway_id, description
                )
                for gateway in data.network.gateways
                if gateway.eui != data.network.gateway_eui
                for description in FLEET_STATES
            ),
        ],
        async_add_entities,
    )
    # Device binary sensors come as devices report.
    if config_entry.runtime_data.devices is not None:
        await lorawan_platforms.async_setup_binary_sensors(
            hass, config_entry, async_add_entities
        )


class MilesightOnlineBinarySensor(
    CoordinatorEntity[MilesightGatewayCoordinator], BinarySensorEntity
):
    """
    Whether the gateway answered the last poll.

    Off instead of unavailable when it does not, since that is the state this
    entity reports. A gateway-only entry polls the login check, every other
    entry the device list.
    """

    _attr_has_entity_name = True
    _attr_name = "Online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: MilesightGatewayCoordinator, entry_id: str) -> None:
        """Attach the state to the gateway device."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_online"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry_id)})

    @property
    def available(self) -> bool:
        """Always available; an unreachable gateway shows as off."""
        return True

    @property
    def is_on(self) -> bool:
        """Return whether the last poll reached the gateway."""
        return self.coordinator.last_update_success


class _StatusBinarySensor(
    CoordinatorEntity[MilesightDiagnosticsCoordinator], BinarySensorEntity
):
    """A connection state from the gateway status."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC


class MilesightForwarderBinarySensor(_StatusBinarySensor):
    """Whether the packet forwarder reaches its network server."""

    _attr_name = "Packet forwarder"

    def __init__(
        self, coordinator: MilesightDiagnosticsCoordinator, entry_id: str
    ) -> None:
        """Attach the state to the gateway device."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_packet_forwarder"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry_id)})

    @property
    def is_on(self) -> bool | None:
        """Return whether the forwarder is connected."""
        data = self.coordinator.data
        return data.network.forwarder_connected if data else None


class MilesightMqttLinkBinarySensor(_StatusBinarySensor):
    """
    Whether an application's MQTT link reaches its broker.

    Each application forwards the data of its own devices, so a gateway can have
    several links at once; one sensor per application that has devices.
    """

    def __init__(
        self,
        coordinator: MilesightDiagnosticsCoordinator,
        link: ApplicationLink,
        entry_id: str,
    ) -> None:
        """Attach the state to the gateway device."""
        super().__init__(coordinator)
        self._app_id = link.app_id
        self._attr_name = f"MQTT {link.name}"
        self._attr_unique_id = f"{entry_id}_mqtt_{link.app_id}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry_id)})

    def _link(self) -> ApplicationLink | None:
        data = self.coordinator.data
        if data is None:
            return None
        return next(
            (ln for ln in data.network.links if ln.app_id == self._app_id), None
        )

    @property
    def is_on(self) -> bool | None:
        """Return whether the link is connected."""
        link = self._link()
        return link.connected if link else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Name the broker the link points at."""
        link = self._link()
        return {"broker": link.host} if link and link.host else None


@dataclass(frozen=True, kw_only=True)
class FleetStateDescription(BinarySensorEntityDescription):
    """A state the hub knows of a fleet member."""

    value_fn: Callable[[FleetGateway], bool | None]


# The hub's network server reports these of every other gateway of its fleet.
FLEET_STATES = (
    FleetStateDescription(
        key="wan",
        name="WAN",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda gateway: gateway.wan_connected,
    ),
    FleetStateDescription(
        key="system_problem",
        name="System",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda gateway: (
            None if gateway.system_ok is None else not gateway.system_ok
        ),
    ),
    FleetStateDescription(
        key="cellular",
        name="Cellular",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        # Most gateways carry no SIM: off until someone wants it.
        entity_registry_enabled_default=False,
        value_fn=lambda gateway: gateway.cellular_connected,
    ),
)


class MilesightFleetStateBinarySensor(_StatusBinarySensor):
    """One state of a fleet member, as the hub reports it."""

    entity_description: FleetStateDescription

    def __init__(
        self,
        coordinator: MilesightDiagnosticsCoordinator,
        gateway: FleetGateway,
        entry_id: str,
        gateway_id: str,
        description: FleetStateDescription,
    ) -> None:
        """Attach the state to the member's device."""
        super().__init__(coordinator)
        self.entity_description = description
        self._eui = gateway.eui
        self._attr_unique_id = f"{entry_id}_{gateway.eui}_{description.key}"
        self._attr_device_info = fleet_device_info(
            gateway, entry_id, gateway_id, local=False
        )

    @property
    def is_on(self) -> bool | None:
        """Return the state, None while the hub does not tell."""
        data = self.coordinator.data
        gateway = (
            next((gw for gw in data.network.gateways if gw.eui == self._eui), None)
            if data
            else None
        )
        return self.entity_description.value_fn(gateway) if gateway else None


class MilesightFleetBinarySensor(_StatusBinarySensor):
    """Whether the network server currently hears a gateway's radio."""

    _attr_name = "Radio"

    def __init__(
        self,
        coordinator: MilesightDiagnosticsCoordinator,
        gateway: FleetGateway,
        entry_id: str,
        gateway_id: str,
        *,
        local: bool,
    ) -> None:
        """Attach the state to the gateway's device."""
        super().__init__(coordinator)
        self._eui = gateway.eui
        self._attr_unique_id = f"{entry_id}_{gateway.eui}_radio"
        self._attr_device_info = fleet_device_info(
            gateway, entry_id, gateway_id, local=local
        )

    @property
    def is_on(self) -> bool | None:
        """Return whether the gateway is connected."""
        data = self.coordinator.data
        if data is None:
            return None
        gateway = next(
            (gw for gw in data.network.gateways if gw.eui == self._eui), None
        )
        return gateway.connected if gateway else None
