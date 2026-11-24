"""Base entity for the LoRaWAN devices behind the gateway."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.mqtt import async_subscribe
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import APIAuthError, APIConnectionError
from .const import DOMAIN
from .coordinator import MilesightGatewayCoordinator

if TYPE_CHECKING:
    from homeassistant.components.mqtt.models import ReceiveMessage

    from .api import FleetGateway, MilesightDevice

_LOGGER = logging.getLogger(__name__)


@callback
def remove_switched_off_entity(
    hass: HomeAssistant, platform: str, unique_id: str
) -> None:
    """Drop an entity an option switched off, instead of leaving it orphaned."""
    registry = er.async_get(hass)
    if entity_id := registry.async_get_entity_id(platform, DOMAIN, unique_id):
        registry.async_remove(entity_id)


def fleet_device_info(
    gateway: FleetGateway, entry_id: str, gateway_id: str, *, local: bool
) -> DeviceInfo:
    """Return the device of a fleet gateway; the local one is the gateway device."""
    if local:
        return DeviceInfo(identifiers={(DOMAIN, entry_id)})
    return DeviceInfo(
        identifiers={(DOMAIN, gateway.eui)},
        name=gateway.name,
        model="LoRaWAN Gateway",
        serial_number=gateway.eui,
        via_device_id=gateway_id,
    )


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
        return self._read_path(msg, (key,))

    def _read_path(self, msg: ReceiveMessage, path: tuple[str, ...]) -> Any:
        """Walk ``path`` through the payload's nested objects, or None."""
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
        value: Any = payload
        for key in path:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value

    async def _async_send_downlink(self, command: dict[str, Any]) -> None:
        """Queue a downlink; a failure reaches the user as a readable error."""
        try:
            await self.coordinator.api.async_enqueue_downlink(
                async_get_clientsession(self.hass), self._dev_eui, command
            )
        except (APIAuthError, APIConnectionError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="downlink_failed",
                translation_placeholders={"error": str(err)},
            ) from err


def nested_command(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    """Build the downlink command the codec expects for a payload path."""
    command: Any = value
    for key in reversed(path):
        command = {key: command}
    return command
