"""The Milesight Gateway integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PORT, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.start import async_at_started

from .const import (
    CONF_GATEWAY_URL,
    CONF_MAC,
    DOMAIN,
    gateway_name,
    gateway_only,
    uses_lorawan,
    uses_mqtt_discovery,
)
from .coordinator import MilesightDiagnosticsCoordinator, MilesightGatewayCoordinator
from .hostname import learn_hostname
from .lorawan import LoRaWANRuntime
from .lorawan_cleanup import remove_lorawan_only_entities, remove_mqtt_only_entities
from .mqtt_discovery import MqttDiscoveryRuntime
from .network import async_setup_network_view

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceEntry

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.EVENT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.UPDATE,
]

# Bumped when entry data or the registry layout changes; see
# async_migrate_entry and ConfigFlow.MINOR_VERSION.
ENTRY_VERSION = 1
ENTRY_MINOR_VERSION = 2

type MilesightGatewayConfigEntry = ConfigEntry[RuntimeData]


@dataclass
class RuntimeData:
    """Runtime data stored on the config entry."""

    coordinator: MilesightGatewayCoordinator
    gateway_device_id: str
    diagnostics: MilesightDiagnosticsCoordinator
    # Set when the data source is LoRaWAN; MQTT entries leave it None.
    lorawan: LoRaWANRuntime | None = None
    # Set for a gateway-only entry that finds its devices on MQTT.
    mqtt: MqttDiscoveryRuntime | None = None

    @property
    def devices(self) -> LoRaWANRuntime | MqttDiscoveryRuntime | None:
        """The source of catalog-built device entities, if the entry has one."""
        return self.lorawan or self.mqtt


async def async_migrate_entry(
    hass: HomeAssistant, entry: MilesightGatewayConfigEntry
) -> bool:
    """
    Migrate the gateway device off its address-derived identifier.

    Runs before setup, while entry.data still holds the address the existing
    device was registered under, so it must ship with the change that makes the
    address editable — not after it.
    """
    if entry.version == ENTRY_VERSION and entry.minor_version < ENTRY_MINOR_VERSION:
        device_registry = dr.async_get(hass)
        legacy_id = f"{entry.data[CONF_GATEWAY_URL]}:{entry.data[CONF_PORT]}"
        if device := device_registry.async_get_device_by_identifier(
            (DOMAIN, legacy_id), entry.entry_id
        ):
            device_registry.async_update_device(
                device.id, new_identifiers={(DOMAIN, entry.entry_id)}
            )
        hass.config_entries.async_update_entry(entry, minor_version=ENTRY_MINOR_VERSION)

    return True


def _remove_deselected_devices(
    hass: HomeAssistant,
    entry: MilesightGatewayConfigEntry,
    coordinator: MilesightGatewayCoordinator,
) -> None:
    """
    Drop the devices a device selection leaves out, with their entities.

    Only devices the gateway lists and the user did not pick: a device that is
    merely offline keeps its history and comes back when it returns.
    """
    selected = {device.dev_eui for device in coordinator.data.devices}
    left_out = {
        device.dev_eui
        for device in coordinator.data.matched
        if device.dev_eui not in selected
    }
    registry = dr.async_get(hass)
    for eui in left_out:
        if device := registry.async_get_device_by_identifier(
            (DOMAIN, eui), entry.entry_id
        ):
            registry.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )


async def async_setup_entry(
    hass: HomeAssistant, entry: MilesightGatewayConfigEntry
) -> bool:
    """Set up Milesight Gateway from a config entry."""
    only_gateway = gateway_only(entry.data)
    lorawan_source = uses_lorawan(entry.data, entry.options)
    # A gateway-only entry has no device entities, so it needs no broker either.
    if not only_gateway and not lorawan_source:
        # MQTT is only an after-dependency, so LoRaWAN entries need no broker;
        # an MQTT entry waits for it here instead.
        from homeassistant.components import mqtt  # noqa: PLC0415

        if not await mqtt.async_wait_for_mqtt_client(hass):
            msg = "MQTT is not set up"
            raise ConfigEntryNotReady(msg)
    coordinator = MilesightGatewayCoordinator(hass, entry)

    await coordinator.async_config_entry_first_refresh()

    if not only_gateway and not coordinator.api.connected:
        raise ConfigEntryNotReady

    # Register the gateway as a device so sensor devices can link to it
    # via via_device_id. Identified by the entry id rather than the address, so
    # changing the gateway's URL does not create a second device.
    gateway_url = entry.data[CONF_GATEWAY_URL]
    device_registry = dr.async_get(hass)
    gateway_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        # Lets DHCP discovery recognise the gateway at a new address.
        connections={(dr.CONNECTION_NETWORK_MAC, mac)}
        if (mac := entry.data.get(CONF_MAC))
        else set(),
        name=gateway_name(entry.data) or "Milesight Gateway",
        manufacturer="Milesight",
        model="LoRaWAN Gateway",
        configuration_url=gateway_url,
    )

    _remove_deselected_devices(hass, entry, coordinator)

    # Router device trackers load during startup too; their DHCP names are
    # there once Home Assistant has started.
    @callback
    def _after_start(_hass: HomeAssistant) -> None:
        learn_hostname(hass, entry, gateway_device.id)

    entry.async_on_unload(async_at_started(hass, _after_start))

    # Not a first refresh: the status calls must not stop the devices loading.
    diagnostics = MilesightDiagnosticsCoordinator(hass, entry, coordinator.api)
    await diagnostics.async_refresh()

    entry.runtime_data = RuntimeData(coordinator, gateway_device.id, diagnostics)

    if lorawan_source:
        lorawan = LoRaWANRuntime(hass, entry)
        try:
            await lorawan.async_start()
        except BaseException:
            await lorawan.async_stop()
            raise
        entry.runtime_data.lorawan = lorawan
        remove_mqtt_only_entities(hass, entry, coordinator)
        await async_setup_network_view(hass)
    elif uses_mqtt_discovery(entry.data, entry.options):
        found = MqttDiscoveryRuntime(hass, entry)
        try:
            await found.async_start()
        except BaseException:
            await found.async_stop()
            raise
        entry.runtime_data.mqtt = found

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if entry.runtime_data.devices is None:
        remove_lorawan_only_entities(hass, entry, coordinator)

    return True


async def async_remove_config_entry_device(
    _hass: HomeAssistant,
    _config_entry: MilesightGatewayConfigEntry,
    _device_entry: DeviceEntry,
) -> bool:
    """Allow a device to be removed from the UI."""
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: MilesightGatewayConfigEntry
) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # No runtime data when the setup never got that far.
    runtime = getattr(entry, "runtime_data", None)
    if unloaded and runtime is not None and runtime.devices is not None:
        await runtime.devices.async_stop()
    return unloaded
