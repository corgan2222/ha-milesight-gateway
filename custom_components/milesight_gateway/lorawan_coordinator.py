"""Share one decoded device across its entities and keep its registry entry true."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from milesight_gateway_api import decoder
from milesight_gateway_api.lorawan_devices import MilesightDevice

from .const import CONF_GATEWAY_URL, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from milesight_gateway_api.lorawan import MilesightConnection

_LOGGER = logging.getLogger(__name__)


def device_eui(device: MilesightDevice) -> str:
    """Return the devEUI as the MQTT source writes it, so both share one device."""
    return device.descriptor.dev_eui.upper()


class MilesightLoRaWANCoordinator(DataUpdateCoordinator[MilesightDevice]):
    """Push updates of one device to its entities; no polling."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: MilesightDevice,
        connection: MilesightConnection,
        gateway_device_id: str,
    ) -> None:
        """
        Follow the device model's updates.

        The connection and the gateway device come in directly: the runtime
        creates coordinators before it is stored on the entry.
        """
        super().__init__(hass, _LOGGER, config_entry=entry, name=device.descriptor.name)
        self.entry = entry
        self.connection = connection
        self._versions: dict[str, str] = {}
        # The device hearing it best; the entry's gateway until an uplink tells.
        self._via = gateway_device_id
        self.async_set_updated_data(device)
        self._unsubscribe = device.add_update_listener(self._async_device_updated)

    @property
    def device_info(self) -> DeviceInfo:
        """Type, model and devEUI, as the MQTT source describes the device."""
        model = decoder.models()[self.data.model_id]
        return DeviceInfo(
            identifiers={(DOMAIN, device_eui(self.data))},
            name=self.data.descriptor.name,
            manufacturer="Milesight",
            model=model.get("description") or model["name"],
            model_id=model["name"],
            serial_number=device_eui(self.data),
            configuration_url=self.entry.data[CONF_GATEWAY_URL],
            via_device_id=self._via,
        )

    @callback
    def _async_device_updated(self) -> None:
        self._async_update_registry()
        self.async_set_updated_data(self.data)

    @callback
    def _async_update_registry(self) -> None:
        """Carry reported versions and the strongest gateway into the registry."""
        registry = dr.async_get(self.hass)
        device = registry.async_get_device_by_identifier(
            (DOMAIN, device_eui(self.data)), self.entry.entry_id
        )
        if device is None:
            return
        changes: dict[str, str] = {}
        values = self.data.values
        for field, attr in (
            ("firmware_version", "sw_version"),
            ("hardware_version", "hw_version"),
        ):
            if (value := values.get(field)) is not None and self._versions.get(
                attr
            ) != str(value):
                changes[attr] = self._versions[attr] = str(value)
        if (via := self._strongest_gateway(registry)) and via != self._via:
            changes["via_device_id"] = self._via = via
        if changes:
            registry.async_update_device(device.id, **changes)

    def _strongest_gateway(self, registry: dr.DeviceRegistry) -> str | None:
        """Return the registry ID of the gateway that heard the last uplink best."""
        uplink = self.connection.last_uplinks.get(self.data.descriptor.dev_eui)
        if uplink is None or not uplink.receptions:
            return None
        best = max(
            uplink.receptions, key=lambda rx: rx.rssi if rx.rssi is not None else -999
        )
        # A fleet gateway is a device of its own; the local one is the entry's.
        for identifier in (
            (DOMAIN, best.gateway_id.upper()),
            (DOMAIN, self.entry.entry_id),
        ):
            if gateway := registry.async_get_device_by_identifier(
                identifier, self.entry.entry_id
            ):
                return gateway.id
        return None

    @callback
    def async_shutdown_listener(self) -> None:
        """Stop following the model when its collection retires it."""
        self._unsubscribe()
