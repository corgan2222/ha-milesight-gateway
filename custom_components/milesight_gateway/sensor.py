"""
Sensor entities for the Milesight Gateway integration.

Gateway sensors read the coordinators; the device sensors come from the
codec catalog, as devices report (see lorawan_platforms).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfFrequency,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import lorawan_platforms, meters
from .api import FLEET_MASTER, FLEET_NODE, FLEET_STANDALONE
from .const import CONF_GATEWAY_URL, DOMAIN, gateway_only
from .coordinator import (
    DiagnosticsData,
    MilesightDiagnosticsCoordinator,
    MilesightGatewayCoordinator,
)
from .entity import fleet_device_info, on_first_status, remove_switched_off_entity

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from homeassistant.helpers.typing import StateType

    from . import MilesightGatewayConfigEntry
    from .api import FleetGateway, Packet
    from .fleet import MemberStatus

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
    entry_id = config_entry.entry_id
    # Without the network server login there is no device list to count.
    only_gateway = gateway_only(config_entry.data)

    entities: list[SensorEntity] = [
        MilesightGatewayPingSensor(
            coordinator, gateway_device_info, gateway_host, port
        ),
    ]
    if only_gateway:
        for key in ("online_devices", "offline_devices"):
            remove_switched_off_entity(hass, "sensor", f"{entry_id}_{key}")
    else:
        entities += [
            MilesightOnlineDevicesSensor(coordinator, gateway_device_info),
            MilesightOfflineDevicesSensor(coordinator, gateway_device_info),
        ]

    diagnostics = config_entry.runtime_data.diagnostics
    for description in GATEWAY_STATUS_SENSORS:
        # Web values need the web option; the others the network server login.
        readable = description.web or not only_gateway
        if readable and (diagnostics.web_data or not description.web):
            entities.append(
                MilesightGatewayStatusSensor(diagnostics, description, entry_id)
            )
        else:
            remove_switched_off_entity(hass, "sensor", f"{entry_id}_{description.key}")
    # The full description, so the device exists whichever entity comes first.
    runtime = config_entry.runtime_data.devices
    described = {
        eui.upper(): device.device_info
        for eui, device in (runtime.coordinators.items() if runtime else ())
    }
    for device in coordinator.data.devices:
        if runtime is not None and device.dev_eui.upper() not in described:
            continue  # another entry has the device, or it has no model
        entities.extend(
            MilesightRadioSensor(
                diagnostics,
                description,
                device.dev_eui,
                described.get(device.dev_eui.upper()),
            )
            for description in RADIO_SENSORS
        )
    async_add_entities(entities)
    # Gateways in the fleet are only known once the status was read.
    on_first_status(
        config_entry,
        diagnostics,
        lambda data: (
            [
                MilesightFleetLastSeenSensor(
                    diagnostics,
                    gateway,
                    entry_id,
                    gateway_id,
                    local=gateway.eui == data.network.gateway_eui,
                )
                for gateway in data.network.gateways
            ]
            + [
                MilesightFleetValueSensor(
                    diagnostics, gateway, entry_id, gateway_id, description
                )
                for gateway in data.network.gateways
                if gateway.eui != data.network.gateway_eui
                for description in FLEET_VALUES
            ]
            + [
                MilesightFleetMemberSensor(
                    diagnostics, gateway, entry_id, gateway_id, description
                )
                for gateway in data.network.gateways
                # The gateway itself has these from its own status already.
                if gateway.eui in data.members
                and gateway.eui != data.network.gateway_eui
                for description in MEMBER_VALUES
            ]
        ),
        async_add_entities,
    )
    # Device sensors come as devices report.
    if config_entry.runtime_data.devices is not None:
        await lorawan_platforms.async_setup_sensors(
            hass, config_entry, async_add_entities
        )
        meters.setup_sensors(config_entry, async_add_entities)


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
        key="fleet_role",
        name="Fleet role",
        translation_key="fleet_role",
        icon="mdi:lan",
        device_class=SensorDeviceClass.ENUM,
        options=[FLEET_MASTER, FLEET_NODE, FLEET_STANDALONE],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_system("fleet_role"),
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
        device_info: DeviceInfo | None = None,
    ) -> None:
        """Attach the value to the device."""
        super().__init__(coordinator)
        self.entity_description = description
        self._dev_eui = dev_eui.lower()
        self._attr_unique_id = f"{dev_eui}_{description.key}"
        self._attr_device_info = device_info or DeviceInfo(
            identifiers={(DOMAIN, dev_eui)}
        )

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


def _fleet_boot(gateway: FleetGateway) -> datetime | None:
    """Return when a fleet member started, to the minute so it does not jitter."""
    if gateway.uptime is None:
        return None
    booted = dt_util.utcnow() - timedelta(seconds=gateway.uptime)
    return booted.replace(second=0, microsecond=0)


@dataclass(frozen=True, kw_only=True)
class FleetValueDescription(SensorEntityDescription):
    """A value the hub knows of a fleet member."""

    value_fn: Callable[[FleetGateway], datetime | str | int | None]


# The hub's network server reports these of every other gateway of its fleet.
FLEET_VALUES = (
    FleetValueDescription(
        key="ip_address",
        name="IP address",
        value_fn=lambda gateway: gateway.ip,
    ),
    FleetValueDescription(
        key="last_boot",
        name="Last boot",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_fleet_boot,
    ),
    FleetValueDescription(
        key="cellular_signal",
        name="Cellular signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        # Most gateways carry no SIM: off until someone wants it.
        entity_registry_enabled_default=False,
        value_fn=lambda gateway: gateway.cellular_signal,
    ),
)


class MilesightFleetValueSensor(
    CoordinatorEntity[MilesightDiagnosticsCoordinator], SensorEntity
):
    """One value of a fleet member, as the hub reports it."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description: FleetValueDescription

    def __init__(
        self,
        coordinator: MilesightDiagnosticsCoordinator,
        gateway: FleetGateway,
        entry_id: str,
        gateway_id: str,
        description: FleetValueDescription,
    ) -> None:
        """Attach the value to the member's device."""
        super().__init__(coordinator)
        self.entity_description = description
        self._eui = gateway.eui
        self._attr_unique_id = f"{entry_id}_{gateway.eui}_{description.key}"
        self._attr_device_info = fleet_device_info(
            gateway, entry_id, gateway_id, local=False
        )

    @property
    def native_value(self) -> datetime | str | int | None:
        """Return the value, None while the hub does not tell."""
        data = self.coordinator.data
        gateway = (
            next((gw for gw in data.network.gateways if gw.eui == self._eui), None)
            if data
            else None
        )
        return self.entity_description.value_fn(gateway) if gateway else None


@dataclass(frozen=True, kw_only=True)
class MemberValueDescription(SensorEntityDescription):
    """A value a fleet member's own web interface reports."""

    value_fn: Callable[[MemberStatus], StateType]


def _member_system(attr: str) -> Callable[[MemberStatus], StateType]:
    return lambda member: getattr(member.system, attr) if member.system else None


MEMBER_VALUES = (
    MemberValueDescription(
        key="cpu_load",
        name="CPU load",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_member_system("cpu_load"),
    ),
    MemberValueDescription(
        key="memory_available",
        name="Memory available",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_member_system("memory_available"),
    ),
    MemberValueDescription(
        key="storage_available",
        name="Storage available",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_member_system("storage_available"),
    ),
    MemberValueDescription(
        key="firmware",
        name="Firmware",
        value_fn=lambda member: member.firmware,
    ),
    MemberValueDescription(
        key="wifi_ssid",
        name="Wi-Fi SSID",
        value_fn=lambda member: member.wifi_ssid,
    ),
)


class MilesightFleetMemberSensor(
    CoordinatorEntity[MilesightDiagnosticsCoordinator], SensorEntity
):
    """One value of a fleet member, read from its own web interface."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description: MemberValueDescription

    def __init__(
        self,
        coordinator: MilesightDiagnosticsCoordinator,
        gateway: FleetGateway,
        entry_id: str,
        gateway_id: str,
        description: MemberValueDescription,
    ) -> None:
        """Attach the value to the member's device."""
        super().__init__(coordinator)
        self.entity_description = description
        self._eui = gateway.eui
        self._attr_unique_id = f"{entry_id}_{gateway.eui}_{description.key}"
        self._attr_device_info = fleet_device_info(
            gateway, entry_id, gateway_id, local=False
        )

    @property
    def _member(self) -> MemberStatus | None:
        data = self.coordinator.data
        return data.members.get(self._eui) if data else None

    @property
    def available(self) -> bool:
        """Unavailable while the member does not answer."""
        return super().available and self._member is not None

    @property
    def native_value(self) -> StateType:
        """Return the value the member reported last."""
        member = self._member
        return self.entity_description.value_fn(member) if member else None
