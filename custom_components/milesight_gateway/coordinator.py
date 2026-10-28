"""
DataUpdateCoordinator for the Milesight Gateway integration.

Fetches the device list from the gateway API on a slow polling interval.
Actual sensor state updates are delivered via MQTT (push), not here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import (
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import APIAuthError, APIConnectionError, MilesightDevice, MilesightGatewayAPI
from .const import (
    CONF_GATEWAY_MQTT_BASE_TOPIC,
    CONF_GATEWAY_URL,
    CONF_IV,
    CONF_SECRET_KEY,
    DEFAULT_SCAN_INTERVAL,
)

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


@dataclass
class MilesightGatewayData:
    """All data fetched in a single coordinator refresh."""

    devices: list[MilesightDevice]
    online_count: int
    offline_count: int


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

    async def _async_update_data(self) -> MilesightGatewayData:
        """Authenticate if needed, then fetch the device list and device counts."""
        session: ClientSession = async_get_clientsession(self.hass)
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
            return MilesightGatewayData(
                devices=devices,
                online_count=online_count,
                offline_count=offline_count,
            )
