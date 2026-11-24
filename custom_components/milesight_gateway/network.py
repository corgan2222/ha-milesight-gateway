"""
The LoRaWAN network map: a sidebar panel, a dashboard card and their data.

Home Assistant's own LoRaWAN graph shows only the gateways that heard a
device's last uplink, and no signal values. The map shows every gateway of
each LoRaWAN entry, a fleet hub's silent ones included, and labels and colours
each reception by RSSI and SNR. Its data comes from the command below, which
any logged-in user may read, so the card also works on non-admin dashboards.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError
from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component
from lorawan_connection import ConnectionUnavailable
from milesight_gateway_api.exceptions import MilesightGatewayError

from .const import CONF_GATEWAY_URL, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

try:  # Home Assistant 2026.11 moved websocket schemas to probatio.
    import probatio as schema
except ImportError:  # pragma: no cover - older Home Assistant
    import voluptuous as schema

URL_BASE = f"/{DOMAIN}_static"
SCRIPT = "milesight-lorawan-map.js"
PANEL_ELEMENT = "milesight-lorawan-network-panel"
_SET_UP = f"{DOMAIN}_network_view"


async def async_setup_network_view(hass: HomeAssistant) -> None:
    """Serve the script, add the panel and the card, register the command. Once."""
    if hass.data.get(_SET_UP):
        return
    hass.data[_SET_UP] = True
    websocket_api.async_register_command(hass, websocket_network)
    if "frontend" not in hass.config.components:
        return  # no frontend (tests, headless): the data command is enough
    folder = Path(__file__).parent / "frontend"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(URL_BASE, str(folder), cache_headers=False)]
    )
    # A new script gets a new URL; browsers kept the old one after an update.
    digest = await hass.async_add_executor_job(
        lambda: hashlib.sha256((folder / SCRIPT).read_bytes()).hexdigest()[:12]
    )
    url = f"{URL_BASE}/{SCRIPT}?v={digest}"
    # The card: loaded on every dashboard, no resource entry needed.
    frontend.add_extra_js_url(hass, url)
    await async_setup_component(hass, "panel_custom", {})
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path="milesight-lorawan",
        webcomponent_name=PANEL_ELEMENT,
        sidebar_title="LoRaWAN network",
        sidebar_icon="mdi:access-point-network",
        module_url=url,
    )


@websocket_api.websocket_command({schema.Required("type"): f"{DOMAIN}/network"})
@websocket_api.async_response
async def websocket_network(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Gateways and devices with their newest receptions, per LoRaWAN entry."""
    result = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime = getattr(entry, "runtime_data", None)
        if entry.state is not ConfigEntryState.LOADED or runtime is None:
            continue
        if (lorawan := runtime.lorawan) is None:
            continue
        item: dict[str, Any] = {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "configuration_url": entry.data[CONF_GATEWAY_URL],
            "gateways": [],
            "devices": [],
            "error": None,
        }
        try:
            item["gateways"] = await lorawan.async_gateways()
        except (
            ClientError,
            TimeoutError,
            MilesightGatewayError,
            ConnectionUnavailable,
        ) as err:
            item["error"] = str(err) or type(err).__name__
        link = lorawan.connection
        for eui, descriptor in link.devices.items():
            uplink = link.last_uplinks.get(eui)
            item["devices"].append(
                {
                    "dev_eui": eui,
                    "name": descriptor.name,
                    "model": descriptor.model,
                    "last_uplink": uplink.received_at.isoformat() if uplink else None,
                    "receptions": [
                        {"gateway_id": rx.gateway_id, "rssi": rx.rssi, "snr": rx.snr}
                        for rx in (uplink.receptions if uplink else ())
                    ],
                }
            )
        result.append(item)
    connection.send_result(msg["id"], {"connections": result})
