"""
Event entities for stateless device actions, like a wireless button press.

Presses arrive via MQTT. A retained message is a press from the past, so it
never fires an event; only live messages do.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback

from .const import CONF_GATEWAY_URL
from .entity import MilesightDeviceEntity

if TYPE_CHECKING:
    from homeassistant.components.mqtt.models import ReceiveMessage
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import MilesightGatewayConfigEntry
    from .api import EntityDefinition, MilesightDevice
    from .coordinator import MilesightGatewayCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# How the Milesight codecs number a press.
_PRESS_TYPES = {1: "single_press", 2: "long_press", 3: "double_press"}


async def async_setup_entry(
    _hass: HomeAssistant,
    config_entry: MilesightGatewayConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create event entities for all event-platform entities."""
    coordinator = config_entry.runtime_data.coordinator
    gateway_url = config_entry.data[CONF_GATEWAY_URL]
    gateway_id = config_entry.runtime_data.gateway_device_id

    async_add_entities(
        MilesightButtonEvent(coordinator, device, entity_def, gateway_url, gateway_id)
        for device in coordinator.data.devices
        for entity_def in device.entities
        if entity_def.platform == "event"
    )


class MilesightButtonEvent(MilesightDeviceEntity, EventEntity):
    """A wireless button; every press fires an event."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types: ClassVar[list[str]] = list(_PRESS_TYPES.values())

    def __init__(
        self,
        coordinator: MilesightGatewayCoordinator,
        device: MilesightDevice,
        entity_def: EntityDefinition,
        configuration_url: str,
        gateway_id: str,
    ) -> None:
        """Initialize the event entity from the entity definition."""
        super().__init__(coordinator, device, configuration_url, gateway_id)
        self._entity_key = entity_def.key
        self._attr_unique_id = f"{device.dev_eui}_{entity_def.key}"
        self._attr_name = entity_def.name
        self._attr_entity_registry_enabled_default = entity_def.enabled_by_default

    @callback
    def _handle_mqtt_message(self, msg: ReceiveMessage) -> None:
        """Fire an event for a live press; ignore retained history."""
        if msg.retain:
            return
        value = self._read_value(msg, self._entity_key)
        if value is None:
            return
        # The gateway's codec sends {"msgid": ..., "status": 1}; others send
        # the press number directly.
        if isinstance(value, dict):
            value = value.get("status")
        try:
            event_type = _PRESS_TYPES[int(value)]
        except KeyError, TypeError, ValueError:
            _LOGGER.debug("Unknown press value %r on topic %s", value, self._data_topic)
            return
        self._trigger_event(event_type)
        self.async_write_ha_state()
