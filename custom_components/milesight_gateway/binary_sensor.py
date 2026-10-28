"""
Binary sensor entities for the Milesight Gateway integration.

Entities are created from the device list provided by the coordinator.
State updates are received via MQTT — no polling.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.mqtt import async_subscribe
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory

from .const import CONF_GATEWAY_URL, DOMAIN

if TYPE_CHECKING:
    from homeassistant.components.mqtt.models import ReceiveMessage
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import MilesightGatewayConfigEntry
    from .api import EntityDefinition, MilesightDevice
    from .coordinator import MilesightGatewayCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# Values that map to True (active/on) for binary sensors
_TRUTHY_VALUES = {True, "true", "1", "on", "open", "motion", "occupied"}


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
    configuration_url = gateway_url

    entities = [
        MilesightBinarySensor(
            coordinator, device, entity_def, configuration_url, gateway_id
        )
        for device in coordinator.data.devices
        for entity_def in device.entities
        if entity_def.platform == "binary_sensor"
    ]

    async_add_entities(entities)


class MilesightBinarySensor(BinarySensorEntity):
    """A binary sensor whose state is updated via MQTT."""

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
        """Initialize the binary sensor from the entity definition."""
        self._data_topic = device.data_topic
        self._entity_key = entity_def.key

        self._attr_unique_id = f"{device.dev_eui}_{entity_def.key}"
        self._attr_name = entity_def.name
        self._attr_device_class = entity_def.device_class
        self._attr_entity_registry_enabled_default = entity_def.enabled_by_default

        if entity_def.icon:
            self._attr_icon = entity_def.icon

        if entity_def.entity_category:
            self._attr_entity_category = EntityCategory(entity_def.entity_category)

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.dev_eui)},
            name=device.name,
            manufacturer="Milesight",
            model=device.model or None,
            model_id=device.model_id or None,
            serial_number=device.dev_eui,
            configuration_url=configuration_url,
            via_device_id=gateway_id,
        )

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
    def _handle_mqtt_message(self, msg: ReceiveMessage) -> None:
        """Parse incoming MQTT payload and update state."""
        try:
            payload = json.loads(msg.payload)
        except json.JSONDecodeError, ValueError:
            _LOGGER.warning(
                "Invalid JSON on topic %s: %s", self._data_topic, msg.payload
            )
            return

        value = payload.get(self._entity_key)
        if value is None:
            return

        self._attr_is_on = str(value).lower() in _TRUTHY_VALUES
        self.async_write_ha_state()
