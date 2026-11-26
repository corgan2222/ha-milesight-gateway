"""The Milesight Gateway integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PORT, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from milesight_gateway_api import decoder

from .api import APIAuthError, APIConnectionError, MqttTopics
from .battery import BatteryLog
from .const import (
    CONF_GATEWAY_MQTT_BASE_TOPIC,
    CONF_GATEWAY_URL,
    CONF_MAC,
    CONF_MQTT_ACK_TOPIC,
    CONF_MQTT_DOWNLINK_TOPIC,
    CONF_MQTT_ERROR_TOPIC,
    CONF_MQTT_JOIN_TOPIC,
    CONF_MQTT_REQUEST_TOPIC,
    CONF_MQTT_RESPONSE_TOPIC,
    CONF_MQTT_TOPIC,
    DOMAIN,
    gateway_name,
    gateway_only,
    uses_lorawan,
    uses_mqtt_discovery,
)
from .coordinator import (
    MilesightDiagnosticsCoordinator,
    MilesightGatewayCoordinator,
    MilesightGatewayData,
)
from .devices_panel import async_setup_devices_page
from .hostname import learn_hostname
from .journal import Journal
from .lorawan import LoRaWANRuntime
from .lorawan_cleanup import remove_stale_entities
from .lorawan_entity import RETIRE_EMPTY_OPTIONAL
from .mqtt_discovery import DEV_EUI, MqttDiscoveryRuntime, from_gateway
from .network import async_setup_network_view
from .settings import SettingsLog

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

# Seconds to wait for the gateway's MQTT topics at setup, and between the
# tries after it gave none in that time.
TOPICS_TIMEOUT = 10
TOPICS_RETRY = 30
TOPICS_RETRY_MAX = 600
ENTRY_MINOR_VERSION = 3

type MilesightGatewayConfigEntry = ConfigEntry[RuntimeData]


@dataclass
class RuntimeData:
    """Runtime data stored on the config entry."""

    coordinator: MilesightGatewayCoordinator
    gateway_device_id: str
    diagnostics: MilesightDiagnosticsCoordinator
    # Set when the data source is LoRaWAN.
    lorawan: LoRaWANRuntime | None = None
    # Set when the values come by MQTT; a gateway-only entry needs a topic.
    mqtt: MqttDiscoveryRuntime | None = None
    # The devices' battery levels over time, for the forecast.
    battery: BatteryLog | None = None
    # The settings each device was given or reported.
    settings: SettingsLog | None = None
    # What happened to the downlinks, for the device page.
    journal: Journal | None = None
    # While the gateway installs firmware it reboots; a lost connection is
    # then expected and the entry is reloaded only once it is done.
    installing: bool = False
    reload_after_install: bool = False

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

    Minor version 3 marks the entry: its region and line readings, made
    enabled by older versions, are disabled as they load without a value.
    The stored states that tell are not loaded yet when this runs.
    """
    if entry.version != ENTRY_VERSION:
        return True
    if entry.minor_version < 2:  # noqa: PLR2004 - the version of this step
        device_registry = dr.async_get(hass)
        legacy_id = f"{entry.data[CONF_GATEWAY_URL]}:{entry.data[CONF_PORT]}"
        if device := device_registry.async_get_device_by_identifier(
            (DOMAIN, legacy_id), entry.entry_id
        ):
            device_registry.async_update_device(
                device.id, new_identifiers={(DOMAIN, entry.entry_id)}
            )
    if entry.minor_version < 3:  # noqa: PLR2004
        hass.data.setdefault(RETIRE_EMPTY_OPTIONAL, set()).add(entry.entry_id)
    if entry.minor_version < ENTRY_MINOR_VERSION:
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


async def _async_load_stores(
    hass: HomeAssistant, entry: MilesightGatewayConfigEntry
) -> None:
    """Load what the entry keeps over restarts: batteries, settings, journal."""
    runtime = entry.runtime_data
    runtime.battery = BatteryLog(hass, entry.entry_id)
    runtime.settings = SettingsLog(hass, entry.entry_id)
    runtime.journal = Journal(hass, entry.entry_id)
    for store in (runtime.battery, runtime.settings, runtime.journal):
        await store.async_load()


async def _async_first_refresh(
    entry: MilesightGatewayConfigEntry, coordinator: MilesightGatewayCoordinator
) -> None:
    """Read the gateway once; only an MQTT-fed gateway-only entry goes on without."""
    if uses_mqtt_discovery(entry.data, entry.options):
        # Its devices come by MQTT, so it starts without waiting for the
        # gateway, which may be away (two timeouts would hold the start for
        # minutes); the gateway's own entities are unavailable until it answers.
        coordinator.data = MilesightGatewayData(
            devices=[], online_count=0, offline_count=0
        )
        coordinator.last_update_success = False
        entry.async_create_background_task(
            coordinator.hass, coordinator.async_refresh(), "Reach the gateway"
        )
    else:
        await coordinator.async_config_entry_first_refresh()


async def _async_mqtt_runtime(
    hass: HomeAssistant,
    entry: MilesightGatewayConfigEntry,
    coordinator: MilesightGatewayCoordinator,
) -> MqttDiscoveryRuntime:
    """
    Return the MQTT source of an entry.

    A gateway-only entry has its topics in its options. An entry with the
    network server takes them from the application whose uplink topic it
    listens to, and sends by MQTT when that application has a downlink topic.
    """
    options = entry.options
    if gateway_only(entry.data):
        topics = MqttTopics(
            uplink=options[CONF_MQTT_TOPIC],
            **{
                name: options.get(key) or None
                for name, key in (
                    ("downlink", CONF_MQTT_DOWNLINK_TOPIC),
                    ("ack", CONF_MQTT_ACK_TOPIC),
                    ("error", CONF_MQTT_ERROR_TOPIC),
                    ("join", CONF_MQTT_JOIN_TOPIC),
                    ("request", CONF_MQTT_REQUEST_TOPIC),
                    ("response", CONF_MQTT_RESPONSE_TOPIC),
                )
            },
        )
        return MqttDiscoveryRuntime(hass, entry, topics)
    uplink = f"{entry.data[CONF_GATEWAY_MQTT_BASE_TOPIC]}/{DEV_EUI}"
    store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.topics.{entry.entry_id}")
    try:
        async with asyncio.timeout(TOPICS_TIMEOUT):
            topics = await _async_read_topics(hass, coordinator, uplink)
    except (APIAuthError, APIConnectionError, TimeoutError) as err:
        # Usual at Home Assistant's start; the last answer serves until the
        # gateway gives a new one.
        stored = await store.async_load()
        topics = (
            MqttTopics(**stored) if stored and stored.get("uplink") == uplink else None
        )
        _LOGGER.info(
            "The gateway's MQTT topics are not readable yet (%s); using %s",
            str(err) or type(err).__name__,
            "the stored ones" if topics else "the network server's queue",
        )
        runtime = MqttDiscoveryRuntime(
            hass, entry, topics or MqttTopics(uplink=uplink), coordinator
        )
        entry.async_create_background_task(
            hass,
            _async_learn_topics(hass, coordinator, runtime, store),
            "Read the gateway's MQTT topics",
        )
        return runtime
    if topics is None or not topics.downlink:
        # The MQTT source sends, and hears ACKs and errors, by MQTT too; only
        # without a downlink topic on the gateway does the queue take over.
        _LOGGER.warning(
            "No application on the gateway publishes to %s with a downlink "
            "topic; settings go through the network server",
            uplink,
        )
    if topics is not None:
        await store.async_save(asdict(topics))
    return MqttDiscoveryRuntime(
        hass, entry, topics or MqttTopics(uplink=uplink), coordinator
    )


async def _async_read_topics(
    hass: HomeAssistant, coordinator: MilesightGatewayCoordinator, uplink: str
) -> MqttTopics | None:
    """Return the topics of the application that publishes to the uplink topic."""
    found = await coordinator.api.async_get_mqtt_topics(async_get_clientsession(hass))
    return next((t for t in map(from_gateway, found) if t.uplink == uplink), None)


async def _async_learn_topics(
    hass: HomeAssistant,
    coordinator: MilesightGatewayCoordinator,
    runtime: MqttDiscoveryRuntime,
    store: Store[dict[str, Any]],
) -> None:
    """Ask for the topics again until the gateway answers, then switch to them."""
    delay = TOPICS_RETRY
    while True:
        await asyncio.sleep(delay)
        try:
            topics = await _async_read_topics(hass, coordinator, runtime.topics.uplink)
        except (APIAuthError, APIConnectionError) as err:
            _LOGGER.debug("The gateway's MQTT topics are still not readable: %s", err)
            delay = min(delay * 2, TOPICS_RETRY_MAX)
            continue
        if topics is not None:
            await store.async_save(asdict(topics))
            if topics != runtime.topics:
                await runtime.async_apply_topics(topics)
        return


async def async_setup_entry(
    hass: HomeAssistant, entry: MilesightGatewayConfigEntry
) -> bool:
    """Set up Milesight Gateway from a config entry."""
    only_gateway = gateway_only(entry.data)
    lorawan_source = uses_lorawan(entry.data, entry.options)
    if not only_gateway and not lorawan_source:
        # MQTT is only an after-dependency, so LoRaWAN entries need no broker;
        # an MQTT entry waits for it before it asks the gateway anything.
        from homeassistant.components import mqtt  # noqa: PLC0415

        if not await mqtt.async_wait_for_mqtt_client(hass):
            msg = "MQTT is not set up"
            raise ConfigEntryNotReady(msg)
    if not only_gateway:
        # The device list names each device's catalog model; reading the
        # catalog touches a file, which the event loop must not.
        await hass.async_add_executor_job(decoder.preload)
    coordinator = MilesightGatewayCoordinator(hass, entry)

    await _async_first_refresh(entry, coordinator)

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
    # Not awaited: during Home Assistant's start the status calls can take a
    # minute (measured 2026-11-26: 3 s alone, 90 s while everything starts),
    # and nothing of the setup needs them; what they name comes when they do.
    diagnostics = MilesightDiagnosticsCoordinator(hass, entry, coordinator.api)
    entry.async_create_background_task(
        hass, diagnostics.async_refresh(), "Read the gateway status"
    )

    entry.runtime_data = RuntimeData(coordinator, gateway_device.id, diagnostics)
    await _async_load_stores(hass, entry)

    if lorawan_source:
        lorawan = LoRaWANRuntime(hass, entry)
        try:
            await lorawan.async_start()
        except BaseException:
            await lorawan.async_stop()
            raise
        entry.runtime_data.lorawan = lorawan
        await async_setup_network_view(hass)
    elif not only_gateway or uses_mqtt_discovery(entry.data, entry.options):
        found = await _async_mqtt_runtime(hass, entry, coordinator)
        try:
            await found.async_start()
        except BaseException:
            await found.async_stop()
            raise
        entry.runtime_data.mqtt = found
    if (devices := entry.runtime_data.devices) is not None:
        remove_stale_entities(hass, entry, devices)
        await async_setup_devices_page(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

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
    if unloaded and runtime is not None:
        for store in (runtime.battery, runtime.settings, runtime.journal):
            if store is not None:
                await store.async_flush()
    return unloaded
