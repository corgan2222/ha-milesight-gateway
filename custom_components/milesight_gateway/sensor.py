"""
Sensor entities for the Milesight Gateway integration.

Entities are created from the device list provided by the coordinator.
LoRaWAN sensor state updates are received via MQTT — no polling.
Gateway diagnostic sensors (online/offline count, ping) use the coordinator
or poll independently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from homeassistant.components.mqtt import async_subscribe
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CONF_PORT, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_GATEWAY_URL, DOMAIN
from .coordinator import MilesightGatewayCoordinator

if TYPE_CHECKING:
    from homeassistant.components.mqtt.models import ReceiveMessage
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import MilesightGatewayConfigEntry
    from .api import EntityDefinition, MilesightDevice

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
    _hass: HomeAssistant,
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

    async_add_entities(entities)


def _device_info(
    device: MilesightDevice, configuration_url: str, gateway_device_id: str
) -> DeviceInfo:
    """Build DeviceInfo for a LoRaWAN device."""
    return DeviceInfo(
        identifiers={(DOMAIN, device.dev_eui)},
        name=device.name,
        manufacturer="Milesight",
        model=device.model or None,
        model_id=device.model_id or None,
        serial_number=device.dev_eui,
        configuration_url=configuration_url,
        via_device_id=gateway_device_id,
    )


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


class MilesightSensor(SensorEntity):
    """A sensor whose state is updated via MQTT."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        _coordinator: MilesightGatewayCoordinator,
        device: MilesightDevice,
        entity_def: EntityDefinition,
        configuration_url: str,
        gateway_id: str,
    ) -> None:
        """Initialize the sensor from the entity definition provided by the API."""
        self._data_topic = device.data_topic
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

        self._attr_device_info = _device_info(device, configuration_url, gateway_id)

    async def async_added_to_hass(self) -> None:
        """Subscribe to the device MQTT topic when added to HA."""
        _LOGGER.debug(
            "Subscribing to MQTT topic: %s (key: %s)",
            self._data_topic,
            self._entity_key,
        )
        try:
            self.async_on_remove(
                await async_subscribe(
                    self.hass,
                    self._data_topic,
                    self._handle_mqtt_message,
                )
            )
        except Exception:
            _LOGGER.exception("Failed to subscribe to MQTT topic %s", self._data_topic)
            raise

    @callback
    def _handle_mqtt_message(self, msg: ReceiveMessage) -> None:
        """Parse incoming MQTT payload and update state."""
        _LOGGER.debug("MQTT message on %s: %s", self._data_topic, msg.payload)
        try:
            payload = json.loads(msg.payload)
        except json.JSONDecodeError, ValueError:
            _LOGGER.warning(
                "Invalid JSON on topic %s: %s", self._data_topic, msg.payload
            )
            return

        value = payload.get(self._entity_key)
        if value is None:
            _LOGGER.debug(
                "Key %s not found in payload for topic %s",
                self._entity_key,
                self._data_topic,
            )
            return

        # Timestamp sensors require a timezone-aware datetime, not a raw string.
        if self._attr_device_class == SensorDeviceClass.TIMESTAMP and isinstance(
            value, str
        ):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                _LOGGER.warning(
                    "Cannot parse timestamp value %r for key %s on topic %s",
                    value,
                    self._entity_key,
                    self._data_topic,
                )
                return

        _LOGGER.debug("Updating %s = %s", self._entity_key, value)
        self._attr_native_value = value
        self.async_write_ha_state()


class MilesightLastSeenSensor(SensorEntity):
    """Timestamp sensor that updates whenever any MQTT message arrives for a device."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True
    _attr_name = "Last seen"

    def __init__(
        self,
        _coordinator: MilesightGatewayCoordinator,
        device: MilesightDevice,
        configuration_url: str,
        gateway_id: str,
    ) -> None:
        """Initialize the last-seen sensor."""
        self._data_topic = device.data_topic
        self._attr_unique_id = f"{device.dev_eui}_last_seen"
        self._attr_device_info = _device_info(device, configuration_url, gateway_id)

    async def async_added_to_hass(self) -> None:
        """Subscribe to the device MQTT topic when added to HA."""
        self.async_on_remove(
            await async_subscribe(
                self.hass,
                self._data_topic,
                self._handle_mqtt_message,
            )
        )

    @callback
    def _handle_mqtt_message(self, _msg: ReceiveMessage) -> None:
        """Update the last-seen timestamp on every incoming message."""
        self._attr_native_value = datetime.now(UTC)
        self.async_write_ha_state()
