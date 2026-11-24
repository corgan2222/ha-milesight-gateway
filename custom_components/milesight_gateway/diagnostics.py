"""
Diagnostics for a Milesight gateway entry.

Holds what helps with a support request: the options, which models were
matched with which entity keys, and the gateway status. Credentials,
addresses, serial numbers, EUIs and device names are left out or redacted,
since users post this file in public issues.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import device_registry as dr

from .const import CONF_GATEWAY_URL, CONF_IV, CONF_SECRET_KEY

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import MilesightGatewayConfigEntry

TO_REDACT = {
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_SECRET_KEY,
    CONF_IV,
    CONF_GATEWAY_URL,
    "configuration_url",
    "serial_number",
    "gateway_eui",
    "eui",
    "host",
    # Application and gateway names often carry a person's or a site's name.
    "name",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MilesightGatewayConfigEntry
) -> dict[str, Any]:
    """Return the entry's state, with nothing personal in it."""
    runtime = entry.runtime_data
    devices = runtime.coordinator.data
    status = runtime.diagnostics.data
    gateway = dr.async_get(hass).async_get(runtime.gateway_device_id)

    return async_redact_data(
        {
            "entry": {"data": dict(entry.data), "options": dict(entry.options)},
            "gateway_device": {
                "model": gateway.model,
                "model_id": gateway.model_id,
                "sw_version": gateway.sw_version,
                "hw_version": gateway.hw_version,
                "serial_number": gateway.serial_number,
            }
            if gateway
            else None,
            "web_login_refused": runtime.coordinator.api.web_login_refused,
            "device_list": {
                "last_update_success": runtime.coordinator.last_update_success,
                "online": devices.online_count,
                "offline": devices.offline_count,
                "matched": [
                    {
                        "model_id": device.model_id,
                        "model": device.model,
                        "entities": sorted(
                            f"{entity.platform}.{entity.key}"
                            for entity in device.entities
                        ),
                    }
                    for device in devices.devices
                ],
            }
            if devices
            else None,
            "status": {
                "last_update_success": runtime.diagnostics.last_update_success,
                "network": {
                    **{
                        key: value
                        for key, value in asdict(status.network).items()
                        if key != "packets"
                    },
                    "packets_in_window": len(status.network.packets),
                },
                "system": asdict(status.system) if status.system else None,
                "uplinks_per_minute": status.uplinks_per_minute,
                "downlinks_per_minute": status.downlinks_per_minute,
            }
            if status
            else None,
        },
        TO_REDACT,
    )
