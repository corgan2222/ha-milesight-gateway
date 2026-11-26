"""
DataUpdateCoordinator for the Milesight Gateway integration.

Fetches the device list from the gateway API on a slow polling interval.
Actual sensor state updates are delivered via MQTT (push), not here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import (
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    APIAuthError,
    APIConnectionError,
    FirmwareRelease,
    GatewayDetails,
    GatewayInfo,
    MilesightDevice,
    MilesightGatewayAPI,
    NetworkStatus,
    Packet,
    SystemStatus,
    async_get_latest_firmware,
)
from .const import (
    CONF_DEVICES,
    CONF_FIRMWARE_CHECK,
    CONF_GATEWAY_MQTT_BASE_TOPIC,
    CONF_GATEWAY_URL,
    CONF_IV,
    CONF_SECRET_KEY,
    CONF_STATUS_INTERVAL,
    CONF_WEB_DATA,
    DEFAULT_SCAN_INTERVAL,
    FIRMWARE_RETRY_INTERVAL,
    FIRMWARE_SCAN_INTERVAL,
    gateway_only,
)
from .hostname import learn_hostname

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aiohttp import ClientSession
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def status_interval(options: Mapping[str, Any]) -> int:
    """Return the seconds between status reads; older entries share the poll one."""
    return options.get(
        CONF_STATUS_INTERVAL, options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )


def web_data_enabled(options: Mapping[str, Any]) -> bool:
    """Return whether the web interface may be logged in to at all."""
    return options.get(CONF_WEB_DATA, True)


def firmware_check_enabled(options: Mapping[str, Any]) -> bool:
    """Return whether Milesight's download page may be read."""
    return options.get(CONF_FIRMWARE_CHECK, True)


def selected_devices(options: Mapping[str, Any]) -> set[str]:
    """Return the EUIs the user picked; empty means every device."""
    return {eui.upper() for eui in options.get(CONF_DEVICES) or ()}


def no_network() -> NetworkStatus:
    """Return the network status of an entry that does not read the network server."""
    return NetworkStatus(
        gateway_eui=None,
        forwarder_connected=None,
        pending_packets=None,
        gateways=[],
        links=[],
        application_count=None,
        codec_count=None,
        profile_count=None,
        codec_library_version=None,
        packets=[],
    )


@dataclass
class MilesightGatewayData:
    """All data fetched in a single coordinator refresh."""

    # The devices that get entities: the selection, or all when none is set.
    devices: list[MilesightDevice]
    online_count: int
    offline_count: int
    # Every active device with a known model, to choose from in the options.
    matched: list[MilesightDevice] = field(default_factory=list)


class MilesightGatewayCoordinator(DataUpdateCoordinator[MilesightGatewayData]):
    """Periodically refreshes the device list from the Milesight gateway."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        poll_interval = config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )

        super().__init__(
            hass,
            _LOGGER,
            name=f"Milesight Gateway ({config_entry.data[CONF_GATEWAY_URL]})",
            update_interval=timedelta(seconds=poll_interval),
            config_entry=config_entry,
        )

        self.api = MilesightGatewayAPI(
            gateway_url=config_entry.data[CONF_GATEWAY_URL],
            port=config_entry.data[CONF_PORT],
            username=config_entry.data[CONF_USERNAME],
            password=config_entry.data[CONF_PASSWORD],
            secret_key=config_entry.data[CONF_SECRET_KEY],
            iv=config_entry.data[CONF_IV],
            mqtt_base_topic=config_entry.data[CONF_GATEWAY_MQTT_BASE_TOPIC],
        )
        self.gateway_only = gateway_only(config_entry.data)

    async def _async_update_data(self) -> MilesightGatewayData:
        """Authenticate if needed, then fetch the device list and device counts."""
        session: ClientSession = async_get_clientsession(self.hass)
        if self.gateway_only:
            return await self._async_check_gateway(session)
        try:
            if not self.api.connected:
                await self.api.async_connect(session)
            devices, online_count, offline_count = await self.api.async_get_devices(
                session
            )
        except APIAuthError as err:
            msg = f"Authentication failed: {err}"
            raise ConfigEntryAuthFailed(msg) from err
        except APIConnectionError as err:
            msg = f"Cannot reach gateway: {err}"
            raise UpdateFailed(msg) from err
        except Exception as err:
            msg = f"Unexpected error fetching devices: {err}"
            raise UpdateFailed(msg) from err
        else:
            selected = selected_devices(self.config_entry.options)
            return MilesightGatewayData(
                devices=[d for d in devices if d.dev_eui.upper() in selected]
                if selected
                else devices,
                online_count=online_count,
                offline_count=offline_count,
                matched=devices,
            )

    async def _async_check_gateway(
        self, session: ClientSession
    ) -> MilesightGatewayData:
        """
        Check that a gateway-only entry's gateway answers; it has no device list.

        The web interface's login check needs no login, so no failed network
        server login piles up towards the gateway's account lock.
        """
        try:
            await self.api.async_get_gateway_info(session)
        except (APIAuthError, APIConnectionError) as err:
            msg = f"Cannot reach gateway: {err}"
            raise UpdateFailed(msg) from err
        return MilesightGatewayData(devices=[], online_count=0, offline_count=0)


@dataclass
class FirmwareData:
    """The gateway's firmware and the newest one Milesight offers."""

    installed: str
    latest: FirmwareRelease | None


class MilesightFirmwareCoordinator(DataUpdateCoordinator[FirmwareData]):
    """Checks a few times a day whether newer gateway firmware is out."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        api: MilesightGatewayAPI,
        gateway_device_id: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"Milesight Gateway firmware ({config_entry.data[CONF_GATEWAY_URL]})",
            update_interval=FIRMWARE_SCAN_INTERVAL,
            config_entry=config_entry,
        )
        self.api = api
        self._gateway_device_id = gateway_device_id
        self._web_data = web_data_enabled(config_entry.options)
        self._check_firmware = firmware_check_enabled(config_entry.options)

    async def _async_update_data(self) -> FirmwareData:
        """Read the installed version, then look up the newest of its family."""
        session: ClientSession = async_get_clientsession(self.hass)
        try:
            info = await self.api.async_get_gateway_info(session)
            details = await self._async_get_details(session)
            # Before the download page, so the device is right even if that fails.
            self._update_gateway_device(info, details)
            latest = (
                await async_get_latest_firmware(session, info.firmware.split(".")[0])
                if self._check_firmware
                else None
            )
        except (APIAuthError, APIConnectionError) as err:
            # A busy start (a fleet hub loading hundreds of devices) can time
            # this out; without a retry the entity would wait 12 hours.
            self.update_interval = FIRMWARE_RETRY_INTERVAL
            msg = f"Cannot read the firmware versions: {err}"
            raise UpdateFailed(msg) from err
        self.update_interval = FIRMWARE_SCAN_INTERVAL
        return FirmwareData(installed=info.firmware, latest=latest)

    async def _async_get_details(self, session: ClientSession) -> GatewayDetails | None:
        """Read the details behind the web login, if allowed and not refused."""
        if not self._web_data or self.api.web_login_refused:
            return None
        try:
            return await self.api.async_get_gateway_details(session)
        except (APIAuthError, APIConnectionError) as err:
            # A refusal is logged once by the API, which also stops further logins.
            _LOGGER.debug("Cannot read the gateway details: %s", err)
        return None

    def _update_gateway_device(
        self, info: GatewayInfo, details: GatewayDetails | None
    ) -> None:
        """Show what the gateway reports on its device, also after upgrades."""
        registry = dr.async_get(self.hass)
        if (device := registry.async_get(self._gateway_device_id)) is None:
            return
        registry.async_update_device(
            device.id,
            model=info.model or device.model,
            sw_version=info.firmware,
            model_id=(details and details.model_id) or device.model_id,
            serial_number=(details and details.serial_number) or device.serial_number,
            hw_version=(details and details.hw_version) or device.hw_version,
        )
        if self.config_entry is not None:
            learn_hostname(
                self.hass, self.config_entry, device.id, details and details.hostname
            )


@dataclass
class DiagnosticsData:
    """The gateway's health, traffic and resources."""

    network: NetworkStatus
    system: SystemStatus | None
    uplinks_per_minute: float | None
    downlinks_per_minute: float | None
    # The newest uplink of each device, for its spreading factor and frequency.
    last_uplinks: dict[str, Packet]


class MilesightDiagnosticsCoordinator(DataUpdateCoordinator[DiagnosticsData]):
    """
    Reads the network server's status and the web interface's system status.

    Kept apart from the device list, so a failing status call never makes the
    device entities unavailable.
    """

    def __init__(
        self, hass: HomeAssistant, config_entry: ConfigEntry, api: MilesightGatewayAPI
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"Milesight Gateway status ({config_entry.data[CONF_GATEWAY_URL]})",
            update_interval=timedelta(seconds=status_interval(config_entry.options)),
            config_entry=config_entry,
        )
        self.api = api
        self.web_data = web_data_enabled(config_entry.options)
        self.gateway_only = gateway_only(config_entry.data)
        self._seen_packets: set[tuple[str, ...]] = set()
        self._last_refresh: datetime | None = None
        self._last_uplinks: dict[str, Packet] = {}

    async def _async_update_data(self) -> DiagnosticsData:
        """Read the status calls and count the packets since the last refresh."""
        session: ClientSession = async_get_clientsession(self.hass)
        if self.gateway_only:
            # The network server's status calls need its login; only the web
            # interface's system status is read.
            network = no_network()
        else:
            try:
                network = await self.api.async_get_network_status(session)
            except (APIAuthError, APIConnectionError) as err:
                msg = f"Cannot read the gateway status: {err}"
                raise UpdateFailed(msg) from err

        system = None
        if self.web_data and not self.api.web_login_refused:
            try:
                system = await self.api.async_get_system_status(session)
            except APIConnectionError as err:
                if self.gateway_only:
                    # Its only status call: unavailable, not quietly empty.
                    msg = f"Cannot read the gateway status: {err}"
                    raise UpdateFailed(msg) from err
                _LOGGER.debug("Cannot read the gateway system status: %s", err)
            except APIAuthError as err:
                _LOGGER.debug("Cannot read the gateway system status: %s", err)

        now = dt_util.utcnow()
        new = [p for p in network.packets if p.key not in self._seen_packets]
        uplinks = downlinks = None
        if self._last_refresh is not None:
            # ponytail: more than PACKET_WINDOW packets between two refreshes are
            # undercounted; raise the window if a network ever gets that busy.
            minutes = (now - self._last_refresh).total_seconds() / 60
            if minutes > 0:
                uplinks = round(sum(p.uplink for p in new) / minutes, 2)
                downlinks = round(sum(not p.uplink for p in new) / minutes, 2)
        self._seen_packets = {p.key for p in network.packets}
        self._last_refresh = now
        for packet in sorted(network.packets, key=lambda p: p.time):
            if packet.uplink:
                self._last_uplinks[packet.dev_eui] = packet

        return DiagnosticsData(
            network=network,
            system=system,
            uplinks_per_minute=uplinks,
            downlinks_per_minute=downlinks,
            last_uplinks=dict(self._last_uplinks),
        )
