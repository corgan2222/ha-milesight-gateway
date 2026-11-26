"""
The LoRaWAN data source: sensor values from the gateway's packet log.

The connection polls the network server's packet log for raw uplinks, a
device collection decodes them with Milesight's own codecs, and each device
gets a coordinator that its entities follow. Nothing here needs MQTT or Home
Assistant's lorawan integration; where that integration exists, the gateway is
also registered as a LoRaWAN connection, so its overview lists the devices and
other brands' integrations can serve foreign devices on the gateway.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.setup import async_setup_component
from lorawan_connection import ConnectionUnavailable
from milesight_gateway_api import MilesightGatewayClient, decoder
from milesight_gateway_api.lorawan import AuthenticationError, MilesightConnection
from milesight_gateway_api.lorawan_devices import (
    MilesightDevice,
    MilesightDeviceCollection,
)

from .const import CONF_DEVICES, CONF_GATEWAY_URL, CONF_IV, CONF_SECRET_KEY
from .lorawan_coordinator import MilesightLoRaWANCoordinator

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# Codecs come packed; an uplink waits until a worker thread has unpacked its
# model's codec instead of reading the archive in the event loop.
MilesightDevice.wait_for_codec = True

# Packets read per poll. A fleet hub logged 1.4 packets/s (2026-11-24), so 100
# cover about 70 s against the 10 s poll interval.
PACKET_WINDOW = 100
GATEWAY_CACHE_SECONDS = 300


def _client(data: dict[str, Any]) -> MilesightGatewayClient:
    return MilesightGatewayClient(
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        data[CONF_SECRET_KEY].encode("utf-8"),
        data[CONF_IV].encode("utf-8"),
        data[CONF_GATEWAY_URL],
        data[CONF_PORT],
    )


class LoRaWANRuntime:
    """One gateway entry's connection, device models and coordinators."""

    sends_downlinks = True
    # How values come and settings go, for the device page.
    path = "rest"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Prepare; async_start() connects."""
        self.hass = hass
        self.entry = entry
        self.connection = MilesightConnection(
            _client(dict(entry.data)),
            async_get_clientsession(hass),
            network_id=entry.entry_id,
            packet_window=PACKET_WINDOW,
            devices=entry.options.get(CONF_DEVICES),
        )
        self.collection: MilesightDeviceCollection | None = None
        self.coordinators: dict[str, MilesightLoRaWANCoordinator] = {}
        self._listeners: list[Callable[[MilesightLoRaWANCoordinator], None]] = []
        self._cleanup: list[Callable[[], None]] = []
        self._gateways: list[dict[str, Any]] = []
        # None until the first read: the monotonic clock starts near zero
        # after a boot, so "0.0" would look like a fresh read for 5 minutes.
        self._gateways_read: float | None = None

    async def async_start(self) -> None:
        """Connect, create the device models and unpack their codecs."""
        await self.hass.async_add_executor_job(decoder.preload)
        try:
            await self.connection.async_connect()
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ConnectionUnavailable as err:
            raise ConfigEntryNotReady(str(err)) from err
        collection = self.collection = MilesightDeviceCollection(self.connection)
        self._cleanup.append(collection.subscribe_device_added(self._added))
        self._cleanup.append(collection.subscribe_device_removed(self._removed))
        await collection.async_setup()
        devices = list(collection.devices.values())
        # One pass over the codec archive for every model present now.
        await self.hass.async_add_executor_job(
            decoder.load, {device.model_id for device in devices}
        )
        for device in devices:
            device.codec_loaded()
        self._cleanup.append(self.connection.on_disconnect(self._disconnected))
        await self._async_register_with_lorawan()

    async def _async_register_with_lorawan(self) -> None:
        """Register with Home Assistant's lorawan integration, where it exists."""
        try:
            from homeassistant.components import lorawan  # noqa: PLC0415
        except ImportError:
            return  # Home Assistant before 2026.11
        try:
            if not await async_setup_component(self.hass, "lorawan", {}):
                return
            unregister = await lorawan.async_register_connection(
                self.hass,
                self.entry,
                connection=self.connection,
                configuration_url=self.entry.data[CONF_GATEWAY_URL],
            )
        except Exception:
            _LOGGER.warning(
                "Could not register with the LoRaWAN integration", exc_info=True
            )
            return
        self._cleanup.append(unregister)

    @callback
    def _disconnected(self) -> None:
        if not self.hass.is_stopping:
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

    @callback
    def _added(self, device: MilesightDevice) -> None:
        coordinator = MilesightLoRaWANCoordinator(
            self.hass,
            self.entry,
            device,
            self.connection,
            self.entry.runtime_data.gateway_device_id,
        )
        self.coordinators[device.descriptor.dev_eui] = coordinator
        if not decoder.is_loaded(device.model_id):
            self.entry.async_create_background_task(
                self.hass, self._async_load_codec(device), "Unpack Milesight codec"
            )
        for listener in tuple(self._listeners):
            listener(coordinator)

    async def _async_load_codec(self, device: MilesightDevice) -> None:
        await self.hass.async_add_executor_job(decoder.load, [device.model_id])
        device.codec_loaded()

    @callback
    def _removed(self, device: MilesightDevice) -> None:
        if coordinator := self.coordinators.pop(device.descriptor.dev_eui, None):
            coordinator.async_shutdown_listener()

    @callback
    def subscribe_coordinator_added(
        self, listener: Callable[[MilesightLoRaWANCoordinator], None]
    ) -> Callable[[], None]:
        """Call listener for every device now and for every one added later."""
        self._listeners.append(listener)
        for coordinator in tuple(self.coordinators.values()):
            listener(coordinator)
        return lambda: self._listeners.remove(listener)

    async def async_gateways(self) -> list[dict[str, Any]]:
        """Return the network server's gateways, read at most every 5 minutes."""
        read = self._gateways_read
        if read is None or time.monotonic() - read > GATEWAY_CACHE_SECONDS:
            self._gateways = await self.connection.async_get_gateways()
            self._gateways_read = time.monotonic()
        return self._gateways

    async def async_stop(self) -> None:
        """Withdraw the registration, retire the models, stop polling."""
        while self._cleanup:
            self._cleanup.pop()()
        if self.collection is not None:
            self.collection.close()
        for coordinator in self.coordinators.values():
            coordinator.async_shutdown_listener()
        self.coordinators.clear()
        await self.connection.close()
