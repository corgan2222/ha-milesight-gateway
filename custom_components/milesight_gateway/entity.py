"""Base entity for the LoRaWAN devices behind the gateway."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.mqtt import async_subscribe
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MilesightGatewayCoordinator

if TYPE_CHECKING:
    from homeassistant.components.mqtt.models import ReceiveMessage

    from .api import MilesightDevice

_LOGGER = logging.getLogger(__name__)


class MilesightDeviceEntity(CoordinatorEntity[MilesightGatewayCoordinator]):
    """
    An entity of one LoRaWAN device, fed by the device's MQTT topic.

    Values arrive by MQTT, but whether the device is reachable comes from the
    gateway's device list, so the entity follows the coordinator for that.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MilesightGatewayCoordinator,
        device: MilesightDevice,
        configuration_url: str,
        gateway_id: str,
    ) -> None:
        """Link the entity to its device and the gateway."""
        super().__init__(coordinator)
        self._dev_eui = device.dev_eui
        self._data_topic = device.data_topic
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

    @property
    def available(self) -> bool:
        """Return whether the gateway answers and lists the device as active."""
        return super().available and any(
            device.dev_eui == self._dev_eui for device in self.coordinator.data.devices
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to the device's MQTT topic."""
        await super().async_added_to_hass()
        self.async_on_remove(
            await async_subscribe(
                self.hass, self._data_topic, self._handle_mqtt_message
            )
        )

    @callback
    def _handle_mqtt_message(self, msg: ReceiveMessage) -> None:
        """Handle a message on the device's topic."""
        raise NotImplementedError

    def _read_value(self, msg: ReceiveMessage, key: str) -> Any:
        """Return ``key`` from the message's JSON payload, or None."""
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            _LOGGER.warning(
                "Invalid JSON on topic %s: %s", self._data_topic, msg.payload
            )
            return None
        if not isinstance(payload, dict):
            _LOGGER.warning(
                "Unexpected payload on topic %s: %s", self._data_topic, msg.payload
            )
            return None
        return payload.get(key)
