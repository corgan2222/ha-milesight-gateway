"""
Binary sensor entities for the Milesight Gateway integration.

Entities are created from the device list provided by the coordinator.
State updates are received via MQTT — no polling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import lorawan_platforms
from .const import CONF_GATEWAY_URL, DOMAIN, SOURCE_LORAWAN, data_source
from .coordinator import MilesightDiagnosticsCoordinator
from .entity import MilesightDeviceEntity, fleet_device_info

if TYPE_CHECKING:
    from homeassistant.components.mqtt.models import ReceiveMessage
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import MilesightGatewayConfigEntry
    from .api import ApplicationLink, EntityDefinition, FleetGateway, MilesightDevice
    from .coordinator import MilesightGatewayCoordinator

PARALLEL_UPDATES = 0

# Values that map to True (active/on) for binary sensors
_TRUTHY_VALUES = {"true", "on", "open", "motion", "occupied"}


def is_on(value: Any) -> bool:
    """
    Return whether a payload value means active.

    Numbers count as on above zero, so a people count of 2 still reads as
    occupied. Anything else is matched against the known words.
    """
    if isinstance(value, bool):
        return value
    try:
        return float(value) > 0
    except TypeError, ValueError:
        return str(value).lower() in _TRUTHY_VALUES


async def async_setup_entry(
    _hass: HomeAssistant,
    config_entry: MilesightGatewayConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create binary sensor entities for all binary_sensor-platform entities."""
    coordinator = config_entry.runtime_data.coordinator
    gateway_url = config_entry.data[CONF_GATEWAY_URL]
    # Keyed on the entry id, not the address, so the device survives an IP change.
    gateway_id = config_entry.runtime_data.gateway_device_id

    lorawan = data_source(config_entry.options) == SOURCE_LORAWAN
    # The LoRaWAN source adds its device binary sensors below, as devices report.
    entities: list[BinarySensorEntity] = [
        MilesightBinarySensor(coordinator, device, entity_def, gateway_url, gateway_id)
        for device in (() if lorawan else coordinator.data.devices)
        for entity_def in device.entities
        if entity_def.platform == "binary_sensor"
    ]

    diagnostics = config_entry.runtime_data.diagnostics
    entry_id = config_entry.entry_id
    entities.append(MilesightForwarderBinarySensor(diagnostics, entry_id))
    # Links and fleet gateways are only known once the status was read.
    if diagnostics.data is not None:
        network = diagnostics.data.network
        entities.extend(
            MilesightMqttLinkBinarySensor(diagnostics, link, entry_id)
            for link in network.links
        )
        entities.extend(
            MilesightFleetBinarySensor(
                diagnostics,
                gateway,
                entry_id,
                gateway_id,
                local=gateway.eui == network.gateway_eui,
            )
            for gateway in network.gateways
        )

    async_add_entities(entities)
    if lorawan:
        await lorawan_platforms.async_setup_binary_sensors(
            _hass, config_entry, async_add_entities
        )


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


class MilesightBinarySensor(MilesightDeviceEntity, BinarySensorEntity):
    """A binary sensor whose state is updated via MQTT."""

    def __init__(
        self,
        coordinator: MilesightGatewayCoordinator,
        device: MilesightDevice,
        entity_def: EntityDefinition,
        configuration_url: str,
        gateway_id: str,
    ) -> None:
        """Initialize the binary sensor from the entity definition."""
        super().__init__(coordinator, device, configuration_url, gateway_id)
        self._entity_key = entity_def.key

        self._attr_unique_id = f"{device.dev_eui}_{entity_def.key}"
        self._attr_name = entity_def.name
        self._attr_device_class = entity_def.device_class
        self._attr_entity_registry_enabled_default = entity_def.enabled_by_default

        if entity_def.entity_category:
            self._attr_entity_category = EntityCategory(entity_def.entity_category)

    @callback
    def _handle_mqtt_message(self, msg: ReceiveMessage) -> None:
        """Parse incoming MQTT payload and update state."""
        value = self._read_value(msg, self._entity_key)
        if value is None:
            return

        self._attr_is_on = is_on(value)
        self.async_write_ha_state()
