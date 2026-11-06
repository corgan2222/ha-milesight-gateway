"""
Binary sensor entities for the Milesight Gateway integration.

Entities are created from the device list provided by the coordinator.
State updates are received via MQTT — no polling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory

from .const import CONF_GATEWAY_URL
from .entity import MilesightDeviceEntity

if TYPE_CHECKING:
    from homeassistant.components.mqtt.models import ReceiveMessage
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import MilesightGatewayConfigEntry
    from .api import EntityDefinition, MilesightDevice
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

    async_add_entities(
        MilesightBinarySensor(coordinator, device, entity_def, gateway_url, gateway_id)
        for device in coordinator.data.devices
        for entity_def in device.entities
        if entity_def.platform == "binary_sensor"
    )


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
