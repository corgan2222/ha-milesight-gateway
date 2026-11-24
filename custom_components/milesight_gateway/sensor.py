"""
Sensor entities for the Milesight Gateway integration.

Entities are created from the device list provided by the coordinator.
LoRaWAN sensor state updates are received via MQTT — no polling.
Gateway diagnostic sensors (online/offline count, ping) use the coordinator
or poll independently.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    CONF_PORT,
    PERCENTAGE,
    UnitOfFrequency,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_GATEWAY_URL, DOMAIN
from .coordinator import (
    DiagnosticsData,
    MilesightDiagnosticsCoordinator,
    MilesightGatewayCoordinator,
)
from .entity import (
    MilesightDeviceEntity,
    fleet_device_info,
    remove_switched_off_entity,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.components.mqtt.models import ReceiveMessage
    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from homeassistant.helpers.typing import StateType

    from . import MilesightGatewayConfigEntry
    from .api import EntityDefinition, FleetGateway, MilesightDevice, Packet

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

_TCP_PING_TIMEOUT = 3.0  # seconds


async def _tcp_ping(host: str, port: int) -> float | None:
    """
    Open a TCP connection to host:port and return the round-trip time in ms.

    Returns None when the host is unreachable or the connection times out.
    """
    try:
        start = time.monotonic()
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=_TCP_PING_TIMEOUT,
        )
        elapsed = round((time.monotonic() - start) * 1000, 1)
        writer.close()
        await writer.wait_closed()
    except OSError, TimeoutError:
        return None
    else:
        return elapsed


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MilesightGatewayConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create all sensor entities for this config entry."""
    coordinator: MilesightGatewayCoordinator = config_entry.runtime_data.coordinator
    gateway_url = config_entry.data[CONF_GATEWAY_URL]
    port = config_entry.data[CONF_PORT]
    # Keyed on the entry id, not the address, so the device survives an IP change.
    gateway_id = config_entry.runtime_data.gateway_device_id
    gateway_host = urlparse(gateway_url).hostname or gateway_url

    gateway_device_info = DeviceInfo(identifiers={(DOMAIN, config_entry.entry_id)})

    entities: list[SensorEntity] = [
        MilesightOnlineDevicesSensor(coordinator, gateway_device_info),
        MilesightOfflineDevicesSensor(coordinator, gateway_device_info),
        MilesightGatewayPingSensor(
            coordinator, gateway_device_info, gateway_host, port
        ),
    ]

    for device in coordinator.data.devices:
        entities.extend(
            MilesightSensor(coordinator, device, entity_def, gateway_url, gateway_id)
            for entity_def in device.entities
            if entity_def.platform == "sensor"
        )
        entities.append(
            MilesightLastSeenSensor(coordinator, device, gateway_url, gateway_id)
        )

    diagnostics = config_entry.runtime_data.diagnostics
    entry_id = config_entry.entry_id
    for description in GATEWAY_STATUS_SENSORS:
        if diagnostics.web_data or not description.web:
            entities.append(
                MilesightGatewayStatusSensor(diagnostics, description, entry_id)
            )
        else:
            remove_switched_off_entity(hass, "sensor", f"{entry_id}_{description.key}")
    for device in coordinator.data.devices:
        entities.extend(
            MilesightRadioSensor(diagnostics, description, device.dev_eui)
            for description in RADIO_SENSORS
        )
    # Gateways in the fleet are only known once the status was read.
    if diagnostics.data is not None:
        network = diagnostics.data.network
        entities.extend(
            MilesightFleetLastSeenSensor(
                diagnostics,
                gateway,
                entry_id,
                gateway_id,
                local=gateway.eui == network.gateway_eui,
            )
            for gateway in network.gateways
        )

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Gateway diagnostic sensors
# ---------------------------------------------------------------------------


class MilesightOnlineDevicesSensor(
    CoordinatorEntity[MilesightGatewayCoordinator], SensorEntity
):
    """Number of online (active) LoRaWAN devices on this gateway."""

    _attr_has_entity_name = True
    _attr_name = "Online devices"
    _attr_icon = "mdi:lan-connect"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: MilesightGatewayCoordinator,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_online_devices"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> int:
        """Return the number of online devices."""
        return self.coordinator.data.online_count


class MilesightOfflineDevicesSensor(
    CoordinatorEntity[MilesightGatewayCoordinator], SensorEntity
):
    """Number of offline (inactive) LoRaWAN devices on this gateway."""

    _attr_has_entity_name = True
    _attr_name = "Offline devices"
    _attr_icon = "mdi:lan-disconnect"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: MilesightGatewayCoordinator,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_offline_devices"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> int:
        """Return the number of offline devices."""
        return self.coordinator.data.offline_count


class MilesightGatewayPingSensor(
    CoordinatorEntity[MilesightGatewayCoordinator], SensorEntity
):
    """
    TCP round-trip time to the gateway's API endpoint in milliseconds.

    Updated every time the coordinator refreshes so the poll interval follows
    the same configured scan interval as the device list.
    """

    _attr_has_entity_name = True
    _attr_name = "Response time"
    _attr_icon = "mdi:lan-pending"
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: MilesightGatewayCoordinator,
        device_info: DeviceInfo,
        host: str,
        port: int,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._host = host
        self._port = port
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_ping"
        self._attr_device_info = device_info

    @callback
    def _handle_coordinator_update(self) -> None:
        """Schedule a ping when the coordinator updates."""
        self.hass.async_create_task(self._async_ping())

    async def _async_ping(self) -> None:
        """Run the TCP ping and update state."""
        self._attr_native_value = await _tcp_ping(self._host, self._port)
        self.async_write_ha_state()


# ---------------------------------------------------------------------------
# Per-device sensors (MQTT-driven)
# ---------------------------------------------------------------------------


class MilesightSensor(MilesightDeviceEntity, SensorEntity):
    """A sensor whose state is updated via MQTT."""

    def __init__(
        self,
        coordinator: MilesightGatewayCoordinator,
        device: MilesightDevice,
        entity_def: EntityDefinition,
        configuration_url: str,
        gateway_id: str,
    ) -> None:
        """Initialize the sensor from the entity definition provided by the API."""
        super().__init__(coordinator, device, configuration_url, gateway_id)
        self._entity_key = entity_def.key

        self._attr_unique_id = f"{device.dev_eui}_{entity_def.key}"
        self._attr_name = entity_def.name
        self._attr_native_unit_of_measurement = entity_def.unit
        self._attr_device_class = entity_def.device_class
        self._attr_state_class = (
            SensorStateClass(entity_def.state_class) if entity_def.state_class else None
        )
        self._attr_entity_registry_enabled_default = entity_def.enabled_by_default

        if entity_def.icon:
            self._attr_icon = entity_def.icon

        if entity_def.entity_category:
            self._attr_entity_category = EntityCategory(entity_def.entity_category)

    @callback
    def _handle_mqtt_message(self, msg: ReceiveMessage) -> None:
        """Parse incoming MQTT payload and update state."""
        value = self._read_value(msg, self._entity_key)
        if value is None:
            _LOGGER.debug(
                "Key %s not found in payload for topic %s",
                self._entity_key,
                self._data_topic,
            )
            return

        self._attr_native_value = value
        self.async_write_ha_state()


class MilesightLastSeenSensor(MilesightDeviceEntity, SensorEntity):
    """Timestamp sensor that updates whenever any MQTT message arrives for a device."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Last seen"

    def __init__(
        self,
        coordinator: MilesightGatewayCoordinator,
        device: MilesightDevice,
        configuration_url: str,
        gateway_id: str,
    ) -> None:
        """Initialize the last-seen sensor."""
        super().__init__(coordinator, device, configuration_url, gateway_id)
        self._attr_unique_id = f"{device.dev_eui}_last_seen"

    @callback
    def _handle_mqtt_message(self, _msg: ReceiveMessage) -> None:
        """Update the last-seen timestamp on every incoming message."""
        self._attr_native_value = datetime.now(UTC)
        self.async_write_ha_state()


# ---------------------------------------------------------------------------
# Gateway status (network server and web interface)
# ---------------------------------------------------------------------------


def _last_boot(data: DiagnosticsData) -> datetime | None:
    """Return when the gateway started, to the minute so it does not jitter."""
    if data.system is None or data.system.uptime is None:
        return None
    booted = dt_util.utcnow() - timedelta(seconds=data.system.uptime)
    return booted.replace(second=0, microsecond=0)


@dataclass(frozen=True, kw_only=True)
class GatewayStatusDescription(SensorEntityDescription):
    """A gateway status value read from the diagnostics data."""

    value_fn: Callable[[DiagnosticsData], StateType | datetime]
    # Read from the web interface, so absent when web data is switched off.
    web: bool = False


def _system(attr: str) -> Callable[[DiagnosticsData], StateType]:
    """Read one system status value, if the web interface could be read."""
    return lambda data: getattr(data.system, attr) if data.system else None


GATEWAY_STATUS_SENSORS: tuple[GatewayStatusDescription, ...] = (
    GatewayStatusDescription(
        key="uplinks_per_minute",
        name="Uplinks per minute",
        icon="mdi:upload-network",
        native_unit_of_measurement="packets/min",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.uplinks_per_minute,
    ),
    GatewayStatusDescription(
        key="downlinks_per_minute",
        name="Downlinks per minute",
        icon="mdi:download-network",
        native_unit_of_measurement="packets/min",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.downlinks_per_minute,
    ),
    GatewayStatusDescription(
        key="pending_packets",
        name="Pending packets",
        icon="mdi:tray-full",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.network.pending_packets,
    ),
    GatewayStatusDescription(
        key="applications",
        name="Applications",
        icon="mdi:application-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.network.application_count,
    ),
    GatewayStatusDescription(
        key="payload_codecs",
        name="Payload codecs",
        icon="mdi:code-json",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.network.codec_count,
    ),
    GatewayStatusDescription(
        key="device_profiles",
        name="Device profiles",
        icon="mdi:card-account-details-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.network.profile_count,
    ),
    GatewayStatusDescription(
        key="codec_library_version",
        name="Codec library version",
        icon="mdi:library-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.network.codec_library_version,
    ),
    GatewayStatusDescription(
        key="cpu_load",
        name="CPU load",
        icon="mdi:cpu-64-bit",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_system("cpu_load"),
        web=True,
    ),
    GatewayStatusDescription(
        key="memory_available",
        name="Memory available",
        icon="mdi:memory",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_system("memory_available"),
        web=True,
    ),
    GatewayStatusDescription(
        key="storage_available",
        name="Storage available",
        icon="mdi:harddisk",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_system("storage_available"),
        web=True,
    ),
    GatewayStatusDescription(
        key="last_boot",
        name="Last boot",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_last_boot,
        web=True,
    ),
)


class MilesightGatewayStatusSensor(
    CoordinatorEntity[MilesightDiagnosticsCoordinator], SensorEntity
):
    """A status value of the gateway itself."""

    _attr_has_entity_name = True
    entity_description: GatewayStatusDescription

    def __init__(
        self,
        coordinator: MilesightDiagnosticsCoordinator,
        description: GatewayStatusDescription,
        entry_id: str,
    ) -> None:
        """Attach the value to the gateway device."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry_id)})

    @property
    def native_value(self) -> StateType | datetime:
        """Return the value from the latest status."""
        data = self.coordinator.data
        return self.entity_description.value_fn(data) if data else None


@dataclass(frozen=True, kw_only=True)
class RadioDescription(SensorEntityDescription):
    """A radio value of a device's newest uplink."""

    value_fn: Callable[[Packet], StateType]


RADIO_SENSORS: tuple[RadioDescription, ...] = (
    RadioDescription(
        key="spreading_factor",
        name="Spreading factor",
        icon="mdi:sine-wave",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda packet: packet.spread_factor,
    ),
    RadioDescription(
        key="frequency",
        name="Frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        suggested_unit_of_measurement=UnitOfFrequency.MEGAHERTZ,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda packet: packet.frequency,
    ),
)


class MilesightRadioSensor(
    CoordinatorEntity[MilesightDiagnosticsCoordinator], SensorEntity
):
    """How a device's newest uplink was sent, from the gateway's packet log."""

    _attr_has_entity_name = True
    entity_description: RadioDescription

    def __init__(
        self,
        coordinator: MilesightDiagnosticsCoordinator,
        description: RadioDescription,
        dev_eui: str,
    ) -> None:
        """Attach the value to the device."""
        super().__init__(coordinator)
        self.entity_description = description
        self._dev_eui = dev_eui.lower()
        self._attr_unique_id = f"{dev_eui}_{description.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, dev_eui)})

    @property
    def native_value(self) -> StateType:
        """Return the value of the device's newest uplink, if one was logged."""
        data = self.coordinator.data
        packet = data.last_uplinks.get(self._dev_eui) if data else None
        return self.entity_description.value_fn(packet) if packet else None


class MilesightFleetLastSeenSensor(
    CoordinatorEntity[MilesightDiagnosticsCoordinator], SensorEntity
):
    """When the network server last heard from a gateway."""

    _attr_has_entity_name = True
    _attr_name = "Radio last seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: MilesightDiagnosticsCoordinator,
        gateway: FleetGateway,
        entry_id: str,
        gateway_id: str,
        *,
        local: bool,
    ) -> None:
        """Attach the value to the gateway's device."""
        super().__init__(coordinator)
        self._eui = gateway.eui
        self._attr_unique_id = f"{entry_id}_{gateway.eui}_last_seen"
        self._attr_device_info = fleet_device_info(
            gateway, entry_id, gateway_id, local=local
        )

    @property
    def native_value(self) -> datetime | None:
        """Return the last time, read as Home Assistant's local time."""
        data = self.coordinator.data
        gateway = (
            next((gw for gw in data.network.gateways if gw.eui == self._eui), None)
            if data
            else None
        )
        if gateway is None or gateway.last_seen is None:
            return None
        # ponytail: assumes the gateway's clock zone matches Home Assistant's,
        # since the API gives no zone; add an option if that ever differs.
        return gateway.last_seen.replace(tzinfo=dt_util.get_default_time_zone())
