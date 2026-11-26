"""Share one decoded device across its entities and keep its registry entry true."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from milesight_gateway_api import decoder
from milesight_gateway_api.lorawan_devices import MilesightDevice

from .commands import CommandTracker
from .const import CONF_GATEWAY_URL, DOMAIN
from .link import LinkStats
from .settings import DEVICE, SettingsLog

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
        runtime = getattr(entry, "runtime_data", None)
        self._settings: SettingsLog | None = getattr(runtime, "settings", None)
        self.commands = CommandTracker(
            hass, device, self._remember, getattr(runtime, "journal", None)
        )
        self._seed_settings(device)
        self._settings_uplinks = device.uplinks
        self.link = LinkStats()
        # Field IDs with an entity before the restart; lorawan_entity.py.
        self.registered: set[str] | None = None
        # Settings of the pulse counters' meters, by counter field; meters.py.
        self.meters: dict[str, dict[str, Any]] = {}
        self._link_at: object = None
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
        self._record_uplink()
        self._keep_reported()
        self.commands.check()
        battery = getattr(getattr(self.entry, "runtime_data", None), "battery", None)
        if battery is not None and "battery" in self.data.values:
            battery.add(device_eui(self.data), self.data.values["battery"])
        self._async_update_registry()
        self.async_set_updated_data(self.data)

    def _remember(self, values: dict[str, Any], source: str) -> None:
        if self._settings is not None:
            self._settings.record(device_eui(self.data), values, source)

    def _seed_settings(self, device: MilesightDevice) -> None:
        """Bring back the settings kept before the restart."""
        if self._settings is None:
            return
        for key, kept in self._settings.get(device_eui(device)).items():
            if device.fields.get(key, {}).get("access") == "RW":
                device.values.setdefault(key, kept["v"])

    def _keep_reported(self) -> None:
        """Keep the settings a new uplink reports."""
        device = self.data
        if device.uplinks == self._settings_uplinks:
            return
        self._settings_uplinks = device.uplinks
        decoded = decoder.flatten(device.last_decoded)
        self._remember(
            {
                key: value
                for key, value in decoded.items()
                if device.fields.get(key, {}).get("access") == "RW"
            },
            DEVICE,
        )

    def _record_uplink(self) -> None:
        """Feed the link statistics with a new uplink, once."""
        uplink = self.connection.last_uplinks.get(self.data.descriptor.dev_eui)
        if uplink is None or uplink.received_at == self._link_at:
            return
        self._link_at = uplink.received_at
        self.link.record(
            uplink.received_at,
            [(rx.gateway_id, rx.rssi, rx.snr) for rx in uplink.receptions],
            # Only MQTT payloads carry these.
            f_cnt=getattr(uplink, "f_cnt", None),
            spreading_factor=getattr(uplink, "spreading_factor", None),
        )

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
    def rename(self, name: str) -> None:
        """Take a name the gateway gave the device; one set in Home Assistant stays."""
        if not name or name == self.data.descriptor.name:
            return
        self.data.descriptor = replace(self.data.descriptor, name=name)
        registry = dr.async_get(self.hass)
        if device := registry.async_get_device_by_identifier(
            (DOMAIN, device_eui(self.data)), self.entry.entry_id
        ):
            registry.async_update_device(device.id, name=name)

    @callback
    def async_shutdown_listener(self) -> None:
        """Stop following the model when its collection retires it."""
        self._unsubscribe()
